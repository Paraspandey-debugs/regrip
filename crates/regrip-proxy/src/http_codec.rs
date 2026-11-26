//! HTTP header/meta extraction, SSE JSON-RPC framing, and envelope building.

use std::time::Instant;

use chrono::Utc;
use http::{HeaderMap, HeaderName, HeaderValue, Uri};
use serde_json::Value;
use uuid::Uuid;

use crate::envelope::{
    parse_jsonrpc_response, ActorInfo, InteractionEnvelope, OperationInfo, ProtocolInfo,
    RequestPayload, TargetInfo, TelemetryInfo,
};
use crate::stdio_proxy::ProxyConfig;

pub struct RequestMeta {
    pub started: Instant,
    pub tool_name: String,
    pub arguments: Value,
    pub agent_id: String,
    pub session_id: Option<String>,
    pub traceparent: Option<String>,
    pub tracestate: Option<String>,
}

pub fn extract_sse_frames(chunk: &[u8], buf: &mut String) -> Vec<Value> {
    buf.push_str(&String::from_utf8_lossy(chunk));
    let mut out = Vec::new();
    while let Some(pos) = buf.find('\n') {
        let line: String = buf.drain(..=pos).collect();
        let trimmed = line.trim();
        if let Some(rest) = trimmed.strip_prefix("data:") {
            let payload = rest.trim();
            if !payload.is_empty() && payload != "[DONE]" {
                if let Ok(val) = serde_json::from_str::<Value>(payload) {
                    if val.is_object() {
                        out.push(val);
                    }
                }
            }
        }
    }
    out
}

pub fn extract_request_meta(
    raw: &[u8],
    headers: &HeaderMap,
    cfg: &ProxyConfig,
) -> Option<RequestMeta> {
    let frame: Value = serde_json::from_slice(raw).ok()?;
    if frame.get("method").and_then(|v| v.as_str()) != Some("tools/call") {
        return None;
    }
    let params = frame.get("params").and_then(|v| v.as_object())?;
    let tool_name = params.get("name").and_then(|v| v.as_str())?.to_string();
    let arguments = params
        .get("arguments")
        .cloned()
        .unwrap_or(Value::Object(Default::default()));
    let meta = params.get("_meta").and_then(|v| v.as_object());
    let agent_id = header_str(headers, "x-regrip-agent")
        .or_else(|| meta_str(meta, "agent_id"))
        .unwrap_or_else(|| cfg.agent_id.clone());
    let session_id = header_str(headers, "mcp-session-id")
        .or_else(|| meta_str(meta, "session_id"))
        .or_else(|| cfg.session_id.clone());
    Some(RequestMeta {
        started: Instant::now(),
        tool_name,
        arguments,
        agent_id,
        session_id,
        traceparent: header_str(headers, "traceparent").or_else(|| meta_str(meta, "traceparent")),
        tracestate: header_str(headers, "tracestate").or_else(|| meta_str(meta, "tracestate")),
    })
}

pub fn build_http_envelope(
    meta: &RequestMeta,
    frame: &Value,
    resp_session: Option<String>,
    cfg: &ProxyConfig,
) -> InteractionEnvelope {
    InteractionEnvelope {
        envelope_version: "1.0".to_string(),
        interaction_id: Uuid::new_v4(),
        timestamp_utc: Utc::now(),
        protocol: ProtocolInfo {
            driver: "mcp".to_string(),
            spec_version: "2025-11-25".to_string(),
            transport: "streamable_http".to_string(),
        },
        actor: ActorInfo {
            agent_id: meta.agent_id.clone(),
            session_id: resp_session.or_else(|| meta.session_id.clone()),
            environment: "local".to_string(),
        },
        target: TargetInfo {
            server_name: cfg.server_name.clone(),
        },
        operation: OperationInfo {
            method: "tools/call".to_string(),
            tool_name: meta.tool_name.clone(),
        },
        request: RequestPayload {
            arguments: meta.arguments.clone(),
            omitted_optional_keys: Vec::new(),
            redacted_paths: Vec::new(),
        },
        response: parse_jsonrpc_response(frame),
        telemetry: TelemetryInfo {
            duration_ms: meta.started.elapsed().as_secs_f64() * 1000.0,
            traceparent: meta.traceparent.clone(),
            tracestate: meta.tracestate.clone(),
            redaction_applied: false,
        },
    }
}

pub fn build_upstream_uri(upstream: &Uri, incoming: &Uri) -> String {
    let base_path = upstream.path();
    let in_path = incoming.path();
    let path = if in_path == "/" && !base_path.is_empty() && base_path != "/" {
        base_path
    } else {
        in_path
    };
    match incoming.query() {
        Some(q) => format!("{path}?{q}"),
        None => path.to_string(),
    }
}

pub fn filter_response_headers(headers: &HeaderMap) -> HeaderMap {
    let mut out = HeaderMap::new();
    for (k, v) in headers {
        let name = k.as_str();
        if !name.eq_ignore_ascii_case("content-length")
            && !name.eq_ignore_ascii_case("transfer-encoding")
            && !name.eq_ignore_ascii_case("content-encoding")
        {
            if let (Ok(hk), Ok(hv)) = (
                HeaderName::from_bytes(k.as_str().as_bytes()),
                HeaderValue::from_bytes(v.as_bytes()),
            ) {
                out.insert(hk, hv);
            }
        }
    }
    out
}

fn header_str(headers: &HeaderMap, key: &str) -> Option<String> {
    headers
        .get(key)
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

fn meta_str(meta: Option<&serde_json::Map<String, Value>>, key: &str) -> Option<String> {
    meta.and_then(|m| m.get(key))
        .and_then(|v| v.as_str())
        .map(str::to_string)
}
