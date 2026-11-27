//! Unit tests for zeroizing pre-persistence redaction and zstd envelope writing.

use chrono::Utc;
use serde_json::json;
use uuid::Uuid;

use regrip_proxy::envelope::{
    parse_jsonrpc_response, ActorInfo, InteractionEnvelope, OperationInfo, ProtocolInfo,
    RequestPayload, TargetInfo, TelemetryInfo,
};
use regrip_proxy::hmac_redactor::{hmac_token, redact_envelope, RedactionAction, RedactionRule};
use regrip_proxy::zstd_sink::write_zstd_envelope;

#[test]
fn redacts_rules_and_builtin_secrets_before_zstd_write() {
    let frame = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "isError": false,
            "structuredContent": {
                "customer": {
                    "email": "alice@example.com",
                    "ssn": "123-45-6789"
                }
            }
        }
    });
    let mut env = InteractionEnvelope {
        envelope_version: "1.0".to_string(),
        interaction_id: Uuid::new_v4(),
        timestamp_utc: Utc::now(),
        protocol: ProtocolInfo {
            driver: "mcp".to_string(),
            spec_version: "2025-11-25".to_string(),
            transport: "stdio".to_string(),
        },
        actor: ActorInfo {
            agent_id: "billing".to_string(),
            session_id: None,
            environment: "local".to_string(),
        },
        target: TargetInfo {
            server_name: "finance-mcp".to_string(),
        },
        operation: OperationInfo {
            method: "tools/call".to_string(),
            tool_name: "get_customer".to_string(),
        },
        request: RequestPayload {
            arguments: json!({
                "api_key": "sk-secret",
                "token": "Bearer supersecrettoken1234",
                "anthropic": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
                "openai": "sk-proj-abcdefghijklmnopqrstuvwxyz1234"
            }),
            omitted_optional_keys: Vec::new(),
            redacted_paths: Vec::new(),
        },
        response: parse_jsonrpc_response(&frame),
        telemetry: TelemetryInfo {
            duration_ms: 1.5,
            traceparent: None,
            tracestate: None,
            redaction_applied: false,
        },
    };

    let rules = vec![
        RedactionRule {
            path: "$.arguments.api_key".to_string(),
            action: RedactionAction::Drop,
        },
        RedactionRule {
            path: "$.structured_content.customer.email".to_string(),
            action: RedactionAction::HmacToken,
        },
    ];
    redact_envelope(&mut env, &rules, "test-salt");
    assert!(env.request.arguments.get("api_key").is_none());
    assert_eq!(env.request.arguments["token"], "[REDACTED:BEARER]");
    assert_eq!(
        env.request.arguments["anthropic"],
        "[REDACTED:ANTHROPIC_KEY]"
    );
    assert_eq!(env.request.arguments["openai"], "[REDACTED:OPENAI_KEY]");
    let email_tok = env.response.structured_content.as_ref().unwrap()["customer"]["email"]
        .as_str()
        .unwrap();
    assert!(email_tok.starts_with("tok_"));
    assert_eq!(email_tok, hmac_token("alice@example.com", "test-salt"));
    assert_eq!(
        env.response.structured_content.as_ref().unwrap()["customer"]["ssn"],
        "[REDACTED:SSN]"
    );
    assert!(env.telemetry.redaction_applied);

    let tmp_dir = std::env::temp_dir().join(format!("regrip-rs-test-{}", Uuid::new_v4()));
    let path = write_zstd_envelope(&tmp_dir, &env).expect("zstd write");
    let raw = zstd::stream::decode_all(std::fs::read(&path).unwrap().as_slice()).unwrap();
    let decoded = String::from_utf8(raw).unwrap();
    assert!(!decoded.contains("alice@example.com"));
    assert!(!decoded.contains("sk-secret"));
    assert!(!decoded.contains("sk-ant-api03-abcdefghijklmnopqrstuvwxyz"));
    assert!(!decoded.contains("sk-proj-abcdefghijklmnopqrstuvwxyz1234"));
    let _ = std::fs::remove_dir_all(&tmp_dir);
}
