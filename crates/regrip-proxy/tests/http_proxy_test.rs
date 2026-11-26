//! Integration tests for YAML redaction rule loading and hyper Streamable HTTP/SSE proxy.

use std::convert::Infallible;
use std::path::PathBuf;

use bytes::Bytes;
use http::{Request, Response};
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use serde_json::{json, Value};
use tokio::net::{TcpListener, TcpStream};
use uuid::Uuid;

use regrip_proxy::config_loader::load_proxy_settings;
use regrip_proxy::hmac_redactor::RedactionAction;
use regrip_proxy::http_proxy::run_http_proxy_on_listener;
use regrip_proxy::stdio_proxy::ProxyConfig;

async fn spawn_mock_upstream() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        loop {
            let Ok((stream, _)) = listener.accept().await else {
                break;
            };
            tokio::spawn(async move {
                let svc = service_fn(|req: Request<Incoming>| async move {
                    let bytes = req.into_body().collect().await.unwrap().to_bytes();
                    let rpc: Value = serde_json::from_slice(&bytes).unwrap();
                    let id = rpc.get("id").cloned().unwrap_or(json!(1));
                    let tool = rpc["params"]["name"].as_str().unwrap_or("");
                    if tool == "stream_search" {
                        let frame = json!({
                            "jsonrpc": "2.0",
                            "id": id,
                            "result": {
                                "isError": false,
                                "structuredContent": {"email": "bob@example.com", "ok": true}
                            }
                        });
                        let sse = format!("data: {frame}\n\n");
                        let resp = Response::builder()
                            .status(200)
                            .header("content-type", "text/event-stream")
                            .header("mcp-session-id", "rs-sess-1")
                            .body(Full::new(Bytes::from(sse)))
                            .unwrap();
                        return Ok::<_, Infallible>(resp);
                    }
                    let resp_body = json!({
                        "jsonrpc": "2.0",
                        "id": id,
                        "result": {
                            "isError": false,
                            "structuredContent": {"email": "alice@example.com", "balance": 500}
                        }
                    });
                    let resp = Response::builder()
                        .status(200)
                        .header("content-type", "application/json")
                        .header("mcp-session-id", "rs-sess-1")
                        .body(Full::new(Bytes::from(resp_body.to_string())))
                        .unwrap();
                    Ok::<_, Infallible>(resp)
                });
                let _ = hyper::server::conn::http1::Builder::new()
                    .serve_connection(TokioIo::new(stream), svc)
                    .await;
            });
        }
    });
    format!("http://{addr}/mcp")
}

async fn post_mcp(addr: &str, body: Value) -> (String, String) {
    let stream = TcpStream::connect(addr).await.unwrap();
    let (mut sender, conn) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
        .await
        .unwrap();
    tokio::spawn(async move {
        let _ = conn.await;
    });
    let req = Request::builder()
        .method("POST")
        .uri("/mcp")
        .header("host", addr)
        .header("content-type", "application/json")
        .header("x-regrip-agent", "rust-proxy-agent")
        .body(Full::new(Bytes::from(body.to_string())))
        .unwrap();
    let resp = sender.send_request(req).await.unwrap();
    let ct = resp
        .headers()
        .get("content-type")
        .unwrap()
        .to_str()
        .unwrap()
        .to_string();
    let raw = resp.into_body().collect().await.unwrap().to_bytes();
    (ct, String::from_utf8(raw.to_vec()).unwrap())
}

#[tokio::test]
async fn loads_yaml_rules_and_proxies_json_and_sse_with_redaction() {
    let tmp_dir = std::env::temp_dir().join(format!("regrip-rs-http-{}", Uuid::new_v4()));
    std::fs::create_dir_all(&tmp_dir).unwrap();
    let yaml_path = tmp_dir.join("regrip.yaml");
    let blob_dir = tmp_dir.join("blobs");
    std::fs::write(
        &yaml_path,
        format!(
            "version: \"1.0\"\nstorage:\n  blob_dir: \"{}\"\ncapture:\n  redaction:\n    enabled: true\n    hmac_secret_env: \"REGRIP_TEST_SECRET\"\n    rules:\n      - path: \"$.arguments.api_key\"\n        action: drop\n      - path: \"$.structured_content.email\"\n        action: hmac_token\n",
            blob_dir.display()
        ),
    )
    .unwrap();

    let settings = load_proxy_settings(&yaml_path).unwrap();
    assert_eq!(settings.rules.len(), 2);
    assert_eq!(settings.rules[0].action, RedactionAction::Drop);
    assert_eq!(settings.rules[1].action, RedactionAction::HmacToken);
    assert_eq!(settings.blob_dir, Some(PathBuf::from(&blob_dir)));

    let upstream_url = spawn_mock_upstream().await;
    let proxy_listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let proxy_addr = proxy_listener.local_addr().unwrap().to_string();
    let cfg = ProxyConfig {
        server_name: "rust-http-mcp".to_string(),
        agent_id: "fallback-agent".to_string(),
        session_id: None,
        blob_dir: blob_dir.clone(),
        hmac_secret: "unit-test-secret".to_string(),
        rules: settings.rules,
    };
    tokio::spawn(async move {
        let _ = run_http_proxy_on_listener(proxy_listener, &upstream_url, cfg).await;
    });

    let (ct1, body1) = post_mcp(
        &proxy_addr,
        json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_profile", "arguments": {"api_key": "sk-live-secret", "id": "u1"}}
        }),
    )
    .await;
    assert!(ct1.contains("application/json"));
    assert!(body1.contains("alice@example.com"));

    let (ct2, body2) = post_mcp(
        &proxy_addr,
        json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "stream_search", "arguments": {"q": "laptop"}}
        }),
    )
    .await;
    assert!(ct2.contains("text/event-stream"));
    assert!(body2.contains("bob@example.com"));

    // Allow background zstd sink task to flush both envelopes
    for _ in 0..20 {
        let count = std::fs::read_dir(&blob_dir).map(|d| d.count()).unwrap_or(0);
        if count >= 2 {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }

    let entries: Vec<_> = std::fs::read_dir(&blob_dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    assert_eq!(entries.len(), 2);
    for path in entries {
        let raw = zstd::stream::decode_all(std::fs::read(&path).unwrap().as_slice()).unwrap();
        let decoded = String::from_utf8(raw).unwrap();
        assert!(!decoded.contains("sk-live-secret"));
        assert!(!decoded.contains("alice@example.com"));
        assert!(!decoded.contains("bob@example.com"));
        assert!(decoded.contains("tok_"));
        assert!(decoded.contains("rust-proxy-agent"));
    }

    let _ = std::fs::remove_dir_all(&tmp_dir);
}
