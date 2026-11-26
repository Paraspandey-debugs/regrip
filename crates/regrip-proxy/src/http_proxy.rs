//! High-speed hyper Streamable HTTP & SSE capture proxy (FR-CAP-002, NFR-REL-001).

use std::convert::Infallible;
use std::pin::Pin;
use std::sync::Arc;

use bytes::Bytes;
use http::{HeaderMap, Request, Response, StatusCode, Uri};
use http_body_util::{BodyExt, Full};
use hyper::body::{Body, Frame, Incoming};
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use serde_json::Value;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;

use crate::envelope::InteractionEnvelope;
use crate::hmac_redactor::redact_envelope;
use crate::http_codec::{
    build_http_envelope, build_upstream_uri, extract_request_meta, extract_sse_frames,
    filter_response_headers, RequestMeta,
};
use crate::stdio_proxy::ProxyConfig;
use crate::zstd_sink::write_zstd_envelope;

type BoxBody = http_body_util::combinators::BoxBody<Bytes, hyper::Error>;

/// Bind `listen_addr` and serve the Streamable HTTP / SSE capture proxy.
pub async fn run_http_proxy(
    listen_addr: &str,
    upstream_url: &str,
    config: ProxyConfig,
) -> Result<(), String> {
    let listener = TcpListener::bind(listen_addr)
        .await
        .map_err(|e| format!("Failed to bind {listen_addr}: {e}"))?;
    run_http_proxy_on_listener(listener, upstream_url, config).await
}

/// Serve the HTTP capture proxy on an already-bound TcpListener.
pub async fn run_http_proxy_on_listener(
    listener: TcpListener,
    upstream_url: &str,
    config: ProxyConfig,
) -> Result<(), String> {
    let upstream: Uri = upstream_url
        .parse()
        .map_err(|e| format!("Invalid upstream URL '{upstream_url}': {e}"))?;
    let (tx, mut rx) = mpsc::channel::<InteractionEnvelope>(1024);
    let sink_cfg = config.clone();
    tokio::spawn(async move {
        while let Some(mut env) = rx.recv().await {
            redact_envelope(&mut env, &sink_cfg.rules, &sink_cfg.hmac_secret);
            let _ = write_zstd_envelope(&sink_cfg.blob_dir, &env);
        }
    });

    let state = Arc::new((upstream, config, tx));
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
    state: Arc<(Uri, ProxyConfig, mpsc::Sender<InteractionEnvelope>)>,
) -> Result<Response<BoxBody>, Infallible> {
    let (upstream, cfg, tx) = (&state.0, &state.1, &state.2);
    let (parts, body) = req.into_parts();
    let Ok(collected) = body.collect().await else {
        return Ok(error_response(StatusCode::BAD_REQUEST, "Invalid body"));
    };
    let raw_req = collected.to_bytes();
    let req_meta = extract_request_meta(&raw_req, &parts.headers, cfg);
    let target_uri = build_upstream_uri(upstream, &parts.uri);

    let Ok(up_resp) =
        forward_to_upstream(upstream, parts.method, target_uri, parts.headers, raw_req).await
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
        let stream_body =
            stream_sse_body(up_resp.into_body(), req_meta, sess, cfg.clone(), tx.clone());
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
        let env = build_http_envelope(&meta, &frame, sess, cfg);
        let _ = tx.try_send(env);
    }
    let mut resp = Response::new(Full::new(up_bytes).map_err(|never| match never {}).boxed());
    *resp.status_mut() = status;
    *resp.headers_mut() = resp_headers;
    Ok(resp)
}

async fn forward_to_upstream(
    upstream: &Uri,
    method: http::Method,
    target_uri: String,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response<Incoming>, String> {
    let authority = upstream
        .authority()
        .ok_or_else(|| "Upstream URI missing host".to_string())?;
    let host_port = if authority.port_u16().is_some() {
        authority.as_str().to_string()
    } else {
        format!("{}:{}", authority.host(), 80)
    };
    let stream = TcpStream::connect(&host_port)
        .await
        .map_err(|e| e.to_string())?;
    let (mut sender, conn) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
        .await
        .map_err(|e| e.to_string())?;
    tokio::spawn(async move {
        let _ = conn.await;
    });

    let mut builder = Request::builder().method(method).uri(target_uri);
    for (k, v) in &headers {
        let name = k.as_str();
        if !name.eq_ignore_ascii_case("host") && !name.eq_ignore_ascii_case("content-length") {
            builder = builder.header(k, v);
        }
    }
    builder = builder.header("host", authority.as_str());
    let out_req = builder.body(Full::new(body)).map_err(|e| e.to_string())?;
    sender
        .send_request(out_req)
        .await
        .map_err(|e| e.to_string())
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
