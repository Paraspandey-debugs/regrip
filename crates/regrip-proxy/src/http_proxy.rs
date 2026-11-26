//! High-speed hyper Streamable HTTP & SSE capture proxy (FR-CAP-002, NFR-REL-001).

use std::convert::Infallible;
use std::path::Path;
use std::pin::Pin;
use std::sync::Arc;

use bytes::Bytes;
use http::{Request, Response, StatusCode, Uri};
use http_body_util::{BodyExt, Full};
use hyper::body::{Body, Frame, Incoming};
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use serde_json::Value;
use tokio::net::TcpListener;
use tokio::sync::mpsc;

use crate::envelope::InteractionEnvelope;
use crate::hmac_redactor::redact_envelope;
use crate::http_codec::{
    build_http_envelope, build_upstream_uri, extract_request_meta, extract_sse_frames,
    filter_response_headers, RequestMeta,
};
use crate::stdio_proxy::ProxyConfig;
use crate::tls_client::{build_upstream_client, send_upstream_request, UpstreamClient};
use crate::zstd_sink::write_zstd_envelope;

type BoxBody = http_body_util::combinators::BoxBody<Bytes, hyper::Error>;

struct ProxyState {
    upstream: Uri,
    config: ProxyConfig,
    client: UpstreamClient,
    tx: mpsc::Sender<InteractionEnvelope>,
}

/// Bind `listen_addr` and serve the Streamable HTTP / SSE capture proxy.
pub async fn run_http_proxy(
    listen_addr: &str,
    upstream_url: &str,
    config: ProxyConfig,
) -> Result<(), String> {
    run_http_proxy_with_tls(listen_addr, upstream_url, config, None).await
}

/// Bind `listen_addr` and serve the HTTP/HTTPS capture proxy with optional custom CA PEM.
pub async fn run_http_proxy_with_tls(
    listen_addr: &str,
    upstream_url: &str,
    config: ProxyConfig,
    ca_cert: Option<&Path>,
) -> Result<(), String> {
    let listener = TcpListener::bind(listen_addr)
        .await
        .map_err(|e| format!("Failed to bind {listen_addr}: {e}"))?;
    run_http_proxy_on_listener_with_tls(listener, upstream_url, config, ca_cert).await
}

/// Serve the HTTP capture proxy on an already-bound TcpListener.
pub async fn run_http_proxy_on_listener(
    listener: TcpListener,
    upstream_url: &str,
    config: ProxyConfig,
) -> Result<(), String> {
    run_http_proxy_on_listener_with_tls(listener, upstream_url, config, None).await
}

/// Serve the HTTP/HTTPS capture proxy on a bound TcpListener with optional custom CA PEM.
pub async fn run_http_proxy_on_listener_with_tls(
    listener: TcpListener,
    upstream_url: &str,
    config: ProxyConfig,
    ca_cert: Option<&Path>,
) -> Result<(), String> {
    let upstream: Uri = upstream_url
        .parse()
        .map_err(|e| format!("Invalid upstream URL '{upstream_url}': {e}"))?;
    let client = build_upstream_client(ca_cert)?;
    let (tx, mut rx) = mpsc::channel::<InteractionEnvelope>(1024);
    let sink_cfg = config.clone();
    tokio::spawn(async move {
        while let Some(mut env) = rx.recv().await {
            redact_envelope(&mut env, &sink_cfg.rules, &sink_cfg.hmac_secret);
            let _ = write_zstd_envelope(&sink_cfg.blob_dir, &env);
        }
    });

    let state = Arc::new(ProxyState {
        upstream,
        config,
        client,
        tx,
    });
    loop {
        let (stream, _) = listener
            .accept()
            .await
            .map_err(|e| format!("Accept failed: {e}"))?;
        let state = Arc::clone(&state);
        tokio::spawn(async move {
            let io = TokioIo::new(stream);
            let svc = service_fn(move |req| handle_request(req, Arc::clone(&state)));
            let _ = hyper::server::conn::http1::Builder::new()
                .serve_connection(io, svc)
                .await;
        });
    }
}

async fn handle_request(
    req: Request<Incoming>,
    state: Arc<ProxyState>,
) -> Result<Response<BoxBody>, Infallible> {
    let (parts, body) = req.into_parts();
    let Ok(collected) = body.collect().await else {
        return Ok(error_response(StatusCode::BAD_REQUEST, "Invalid body"));
    };
    let raw_req = collected.to_bytes();
    let req_meta = extract_request_meta(&raw_req, &parts.headers, &state.config);
    let target_uri = build_upstream_uri(&state.upstream, &parts.uri);

    let Ok(up_resp) = send_upstream_request(
        &state.client,
        &state.upstream,
        parts.method,
        &target_uri,
        &parts.headers,
        raw_req,
    )
    .await
    else {
        return Ok(error_response(
            StatusCode::BAD_GATEWAY,
            "Upstream connection failed",
        ));
    };

    let status = up_resp.status();
    let resp_headers = filter_response_headers(up_resp.headers());
    let is_sse = up_resp
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|ct| ct.contains("text/event-stream"));
    let sess = up_resp
        .headers()
        .get("mcp-session-id")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);

    if is_sse {
        let stream_body = stream_sse_body(
            up_resp.into_body(),
            req_meta,
            sess,
            state.config.clone(),
            state.tx.clone(),
        );
        let mut resp = Response::new(stream_body);
        *resp.status_mut() = status;
        *resp.headers_mut() = resp_headers;
        return Ok(resp);
    }

    let Ok(up_bytes) = up_resp.into_body().collect().await.map(|c| c.to_bytes()) else {
        return Ok(error_response(
            StatusCode::BAD_GATEWAY,
            "Upstream read failed",
        ));
    };
    if let (Some(meta), Ok(frame)) = (req_meta, serde_json::from_slice::<Value>(&up_bytes)) {
        let env = build_http_envelope(&meta, &frame, sess, &state.config);
        let _ = state.tx.try_send(env);
    }
    let mut resp = Response::new(Full::new(up_bytes).map_err(|never| match never {}).boxed());
    *resp.status_mut() = status;
    *resp.headers_mut() = resp_headers;
    Ok(resp)
}

fn stream_sse_body(
    mut body: Incoming,
    req_meta: Option<RequestMeta>,
    resp_session: Option<String>,
    cfg: ProxyConfig,
    tx: mpsc::Sender<InteractionEnvelope>,
) -> BoxBody {
    let (chunk_tx, chunk_rx) = mpsc::channel::<Result<Frame<Bytes>, hyper::Error>>(32);
    tokio::spawn(async move {
        let mut sse_buf = String::new();
        while let Some(next) = body.frame().await {
            match next {
                Ok(frame) => {
                    if let Some(data) = frame.data_ref() {
                        if let Some(meta) = req_meta.as_ref() {
                            for rpc in extract_sse_frames(data, &mut sse_buf) {
                                let env =
                                    build_http_envelope(meta, &rpc, resp_session.clone(), &cfg);
                                let _ = tx.try_send(env);
                            }
                        }
                    }
                    if chunk_tx.send(Ok(frame)).await.is_err() {
                        break;
                    }
                }
                Err(err) => {
                    let _ = chunk_tx.send(Err(err)).await;
                    break;
                }
            }
        }
    });
    BodyExt::boxed(ChannelBody(chunk_rx))
}

struct ChannelBody(mpsc::Receiver<Result<Frame<Bytes>, hyper::Error>>);

impl Body for ChannelBody {
    type Data = Bytes;
    type Error = hyper::Error;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Option<Result<Frame<Self::Data>, Self::Error>>> {
        self.0.poll_recv(cx)
    }
}

fn error_response(status: StatusCode, msg: &str) -> Response<BoxBody> {
    let mut resp = Response::new(
        Full::new(Bytes::from(msg.to_string()))
            .map_err(|never| match never {})
            .boxed(),
    );
    *resp.status_mut() = status;
    resp
}
