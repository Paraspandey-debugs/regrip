//! Canonical InteractionEnvelope v1.0 Rust types matching SRS Section 2.5.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

/// Protocol metadata for a captured interaction.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProtocolInfo {
    pub driver: String,
    pub spec_version: String,
    pub transport: String,
}

/// Calling agent attribution.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ActorInfo {
    pub agent_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    pub environment: String,
}

/// Target server identity.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TargetInfo {
    pub server_name: String,
}

/// Invoked operation metadata.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OperationInfo {
    pub method: String,
    pub tool_name: String,
}

/// Normalized request payload and redaction audit list.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RequestPayload {
    pub arguments: Value,
    pub omitted_optional_keys: Vec<String>,
    pub redacted_paths: Vec<String>,
}

/// Normalized MCP tool response payload.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResponsePayload {
    pub status: String,
    pub is_error: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_code: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_message: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub structured_content: Option<Value>,
    pub content_blocks: Vec<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parsed_text_json: Option<Value>,
}

/// Timing and trace telemetry.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TelemetryInfo {
    pub duration_ms: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub traceparent: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tracestate: Option<String>,
    pub redaction_applied: bool,
}

/// Canonical protocol-agnostic InteractionEnvelope v1.0.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InteractionEnvelope {
    pub envelope_version: String,
    pub interaction_id: Uuid,
    pub timestamp_utc: DateTime<Utc>,
    pub protocol: ProtocolInfo,
    pub actor: ActorInfo,
    pub target: TargetInfo,
    pub operation: OperationInfo,
    pub request: RequestPayload,
    pub response: ResponsePayload,
    pub telemetry: TelemetryInfo,
}

/// Parse a JSON-RPC MCP CallToolResult frame into a normalized ResponsePayload.
pub fn parse_jsonrpc_response(frame: &Value) -> ResponsePayload {
    if let Some(err) = frame.get("error").and_then(|v| v.as_object()) {
        return ResponsePayload {
            status: "protocol_error".to_string(),
            is_error: true,
            error_code: err.get("code").cloned(),
            error_message: err
                .get("message")
                .and_then(|v| v.as_str())
                .map(str::to_string),
            structured_content: None,
            content_blocks: Vec::new(),
            parsed_text_json: None,
        };
    }

    let result = frame.get("result").and_then(|v| v.as_object());
    let is_error = result
        .and_then(|r| r.get("isError"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let content_blocks = result
        .and_then(|r| r.get("content"))
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let structured_content = result.and_then(|r| r.get("structuredContent")).cloned();
    let parsed_text_json = if structured_content.is_none() {
        extract_parsed_text_json(&content_blocks)
    } else {
        None
    };

    ResponsePayload {
        status: if is_error { "tool_error" } else { "ok" }.to_string(),
        is_error,
        error_code: None,
        error_message: None,
        structured_content,
        content_blocks,
        parsed_text_json,
    }
}

fn extract_parsed_text_json(blocks: &[Value]) -> Option<Value> {
    for block in blocks {
        if block.get("type").and_then(|v| v.as_str()) != Some("text") {
            continue;
        }
        let Some(text) = block.get("text").and_then(|v| v.as_str()) else {
            continue;
        };
        let trimmed = text.trim();
        if !(trimmed.starts_with('{') || trimmed.starts_with('[')) {
            continue;
        }
        if let Ok(parsed) = serde_json::from_str::<Value>(trimmed) {
            if parsed.is_object() || parsed.is_array() {
                return Some(parsed);
            }
        }
    }
    None
}
