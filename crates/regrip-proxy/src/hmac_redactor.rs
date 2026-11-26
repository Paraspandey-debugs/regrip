//! Zeroizing pre-persistence two-stage PII and secret redactor (FR-CAP-006, NFR-SEC-001).

use std::collections::BTreeSet;
use std::sync::LazyLock;

use hmac::{Hmac, Mac};
use regex::Regex;
use serde_json::Value;
use sha2::Sha256;
use zeroize::Zeroize;

use crate::envelope::InteractionEnvelope;

type HmacSha256 = Hmac<Sha256>;

/// Redaction action applied to explicit JSONPath targets.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RedactionAction {
    Drop,
    Mask,
    HmacToken,
}

/// Explicit JSONPath redaction rule.
#[derive(Debug, Clone)]
pub struct RedactionRule {
    pub path: String,
    pub action: RedactionAction,
}

struct BuiltinPattern {
    label: &'static str,
    regex: Regex,
}

static BUILTIN_PATTERNS: LazyLock<Vec<BuiltinPattern>> = LazyLock::new(|| {
    vec![
        BuiltinPattern {
            label: "JWT",
            regex: Regex::new(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
                .expect("valid jwt regex"),
        },
        BuiltinPattern {
            label: "BEARER",
            regex: Regex::new(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b")
                .expect("valid bearer regex"),
        },
        BuiltinPattern {
            label: "AWS_KEY",
            regex: Regex::new(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b").expect("valid aws regex"),
        },
        BuiltinPattern {
            label: "EMAIL",
            regex: Regex::new(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
                .expect("valid email regex"),
        },
        BuiltinPattern {
            label: "SSN",
            regex: Regex::new(r"\b\d{3}-\d{2}-\d{4}\b").expect("valid ssn regex"),
        },
    ]
});

/// Compute deterministic referential HMAC token (`tok_<12hex>`) using a zeroized key.
pub fn hmac_token(value: &str, secret: &str) -> String {
    let mut key_bytes = secret.as_bytes().to_vec();
    let mut mac = HmacSha256::new_from_slice(&key_bytes).expect("HMAC supports any key length");
    mac.update(value.as_bytes());
    let digest = hex::encode(mac.finalize().into_bytes());
    key_bytes.zeroize();
    format!("tok_{}", &digest[..12])
}

/// Redact an InteractionEnvelope in place prior to disk/DB persistence.
pub fn redact_envelope(
    envelope: &mut InteractionEnvelope,
    rules: &[RedactionRule],
    hmac_secret: &str,
) {
    let mut redacted_paths: BTreeSet<String> =
        envelope.request.redacted_paths.iter().cloned().collect();

    for rule in rules {
        let tokens = parse_jsonpath(&rule.path);
        if tokens.is_empty() {
            continue;
        }
        if tokens.first().map(String::as_str) == Some("arguments") {
            apply_rule(
                &mut envelope.request.arguments,
                &tokens[1..],
                rule.action,
                hmac_secret,
                "$.arguments",
                &mut redacted_paths,
            );
        } else if tokens.first().map(String::as_str) == Some("structured_content") {
            if let Some(sc) = envelope.response.structured_content.as_mut() {
                apply_rule(
                    sc,
                    &tokens[1..],
                    rule.action,
                    hmac_secret,
                    "$.structured_content",
                    &mut redacted_paths,
                );
            }
        }
    }

    scan_builtin(
        &mut envelope.request.arguments,
        "$.arguments",
        &mut redacted_paths,
    );
    if let Some(sc) = envelope.response.structured_content.as_mut() {
        scan_builtin(sc, "$.structured_content", &mut redacted_paths);
    }
    if let Some(parsed) = envelope.response.parsed_text_json.as_mut() {
        scan_builtin(parsed, "$.parsed_text_json", &mut redacted_paths);
    }
    for block in &mut envelope.response.content_blocks {
        scan_builtin(block, "$.content_blocks[*]", &mut redacted_paths);
    }

    envelope.request.redacted_paths = redacted_paths.into_iter().collect();
    envelope.telemetry.redaction_applied = true;
}

fn parse_jsonpath(path: &str) -> Vec<String> {
    path.strip_prefix("$.")
        .unwrap_or(path)
        .split('.')
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect()
}

fn apply_rule(
    node: &mut Value,
    tokens: &[String],
    action: RedactionAction,
    secret: &str,
    current_path: &str,
    redacted_paths: &mut BTreeSet<String>,
) {
    if tokens.is_empty() {
        redacted_paths.insert(current_path.to_string());
        if let Value::String(mut raw) = node.clone() {
            let replacement = match action {
                RedactionAction::Drop => Value::Null,
                RedactionAction::Mask => Value::String("[REDACTED]".to_string()),
                RedactionAction::HmacToken => Value::String(hmac_token(&raw, secret)),
            };
            raw.zeroize();
            *node = replacement;
        } else if action == RedactionAction::Mask {
            *node = Value::String("[REDACTED]".to_string());
        }
        return;
    }

    let head = &tokens[0];
    let tail = &tokens[1..];
    if let Value::Object(map) = node {
        let next_path = format!("{current_path}.{head}");
        if tail.is_empty() && action == RedactionAction::Drop {
            if let Some(Value::String(mut dropped)) = map.remove(head) {
                dropped.zeroize();
            }
            redacted_paths.insert(next_path);
            return;
        }
        if let Some(child) = map.get_mut(head) {
            apply_rule(child, tail, action, secret, &next_path, redacted_paths);
        }
    }
}

fn scan_builtin(value: &mut Value, path: &str, redacted_paths: &mut BTreeSet<String>) {
    match value {
        Value::String(s) => {
            let mut updated = s.clone();
            let mut matched = false;
            for pat in BUILTIN_PATTERNS.iter() {
                if pat.regex.is_match(&updated) {
                    let tag = format!("[REDACTED:{}]", pat.label);
                    updated = pat.regex.replace_all(&updated, tag.as_str()).into_owned();
                    matched = true;
                }
            }
            if matched {
                s.zeroize();
                *s = updated;
                redacted_paths.insert(path.to_string());
            } else {
                updated.zeroize();
            }
        }
        Value::Object(map) => {
            for (k, v) in map.iter_mut() {
                scan_builtin(v, &format!("{path}.{k}"), redacted_paths);
            }
        }
        Value::Array(items) => {
            for item in items.iter_mut() {
                scan_builtin(item, &format!("{path}[*]"), redacted_paths);
            }
        }
        _ => {}
    }
}
