//! Async tokio stdio proxy forwarding JSON-RPC 2.0 frames with non-blocking capture.

use std::collections::HashMap;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Instant;

use chrono::Utc;
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;
use tokio::sync::mpsc;
use uuid::Uuid;

use crate::envelope::{
    parse_jsonrpc_response, ActorInfo, InteractionEnvelope, OperationInfo, ProtocolInfo,
    RequestPayload, TargetInfo, TelemetryInfo,
};
use crate::hmac_redactor::{redact_envelope, RedactionRule};
use crate::zstd_sink::write_zstd_envelope;

/// Configuration for the Rust stdio capture proxy.
#[derive(Debug, Clone)]
pub struct ProxyConfig {
    pub server_name: String,
    pub agent_id: String,
    pub session_id: Option<String>,
    pub blob_dir: PathBuf,
    pub hmac_secret: String,
    pub rules: Vec<RedactionRule>,
}

struct PendingCall {
    started: Instant,
    tool_name: String,
    arguments: Value,
    traceparent: Option<String>,
    tracestate: Option<String>,
}

/// Spawn a child stdio MCP server and proxy stdin/stdout/stderr transparently.
pub async fn run_stdio_proxy(command: &[String], config: ProxyConfig) -> std::io::Result<i32> {
    let mut child = Command::new(&command[0])
        .args(&command[1..])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()?;

    let mut child_stdin = child.stdin.take().expect("piped stdin");
    let child_stdout = child.stdout.take().expect("piped stdout");

    let (tx, mut rx) = mpsc::channel::<InteractionEnvelope>(1024);
    let sink_cfg = config.clone();
    let sink_task = tokio::spawn(async move {
        while let Some(mut env) = rx.recv().await {
            redact_envelope(&mut env, &sink_cfg.rules, &sink_cfg.hmac_secret);
            let _ = write_zstd_envelope(&sink_cfg.blob_dir, &env);
        }
    });

    let pending: std::sync::Arc<tokio::sync::Mutex<HashMap<String, PendingCall>>> =
        std::sync::Arc::new(tokio::sync::Mutex::new(HashMap::new()));
    let pending_in = pending.clone();

    let in_task = tokio::spawn(async move {
        let mut reader = BufReader::new(tokio::io::stdin()).lines();
        while let Ok(Some(line)) = reader.next_line().await {
            if let Ok(frame) = serde_json::from_str::<Value>(&line) {
                track_request(&frame, &pending_in).await;
            }
            if child_stdin.write_all(line.as_bytes()).await.is_err() {
                break;
            }
            if child_stdin.write_all(b"\n").await.is_err() {
                break;
            }
            let _ = child_stdin.flush().await;
        }
        drop(child_stdin);
    });

    let mut out_reader = BufReader::new(child_stdout).lines();
    let mut stdout = tokio::io::stdout();
    while let Ok(Some(line)) = out_reader.next_line().await {
        if let Ok(frame) = serde_json::from_str::<Value>(&line) {
            if let Some(env) = build_envelope_on_response(&frame, &pending, &config).await {
                let _ = tx.try_send(env);
            }
        }
        stdout.write_all(line.as_bytes()).await?;
        stdout.write_all(b"\n").await?;
        stdout.flush().await?;
    }

    let _ = in_task.await;
    drop(tx);
    let _ = sink_task.await;
    let status = child.wait().await?;
    Ok(status.code().unwrap_or(1))
}

async fn track_request(frame: &Value, pending: &tokio::sync::Mutex<HashMap<String, PendingCall>>) {
    if frame.get("method").and_then(|v| v.as_str()) != Some("tools/call") {
        return;
    }
    let Some(id_val) = frame.get("id") else {
        return;
    };
    let Some(params) = frame.get("params").and_then(|v| v.as_object()) else {
        return;
    };
    let Some(tool_name) = params.get("name").and_then(|v| v.as_str()) else {
        return;
    };
    let arguments = params
        .get("arguments")
        .cloned()
        .unwrap_or(Value::Object(Default::default()));
    let meta = params.get("_meta").and_then(|v| v.as_object());
    let traceparent = meta
        .and_then(|m| m.get("traceparent"))
        .and_then(|v| v.as_str())
        .map(str::to_string);
    let tracestate = meta
        .and_then(|m| m.get("tracestate"))
        .and_then(|v| v.as_str())
        .map(str::to_string);

    pending.lock().await.insert(
        id_val.to_string(),
        PendingCall {
            started: Instant::now(),
            tool_name: tool_name.to_string(),
            arguments,
            traceparent,
            tracestate,
        },
    );
}

async fn build_envelope_on_response(
    frame: &Value,
    pending: &tokio::sync::Mutex<HashMap<String, PendingCall>>,
    config: &ProxyConfig,
) -> Option<InteractionEnvelope> {
    let id_val = frame.get("id")?;
    let call = pending.lock().await.remove(&id_val.to_string())?;
    let duration_ms = call.started.elapsed().as_secs_f64() * 1000.0;
    Some(InteractionEnvelope {
        envelope_version: "1.0".to_string(),
        interaction_id: Uuid::new_v4(),
        timestamp_utc: Utc::now(),
        protocol: ProtocolInfo {
            driver: "mcp".to_string(),
            spec_version: "2025-11-25".to_string(),
            transport: "stdio".to_string(),
        },
        actor: ActorInfo {
            agent_id: config.agent_id.clone(),
            session_id: config.session_id.clone(),
            environment: "local".to_string(),
        },
        target: TargetInfo {
            server_name: config.server_name.clone(),
        },
        operation: OperationInfo {
            method: "tools/call".to_string(),
            tool_name: call.tool_name,
        },
        request: RequestPayload {
            arguments: call.arguments,
            omitted_optional_keys: Vec::new(),
            redacted_paths: Vec::new(),
        },
        response: parse_jsonrpc_response(frame),
        telemetry: TelemetryInfo {
            duration_ms,
            traceparent: call.traceparent,
            tracestate: call.tracestate,
            redaction_applied: false,
        },
    })
}
