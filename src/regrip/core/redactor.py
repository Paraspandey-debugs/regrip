"""Pre-persistence two-stage PII and secret redactor (FR-CAP-006, FR-CAP-007)."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Any

from regrip.types.config import CaptureConfig, RedactionAction, RedactionRule
from regrip.types.envelope import InteractionEnvelope

_SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "bearer": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE),
    "aws_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b"),
    "openai_key": re.compile(r"\bsk-proj-[A-Za-z0-9_-]{12,}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
}


def _hmac_token(value: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256)
    return f"tok_{digest.hexdigest()[:12]}"


def _apply_rule_to_tree(
    node: Any,
    parts: list[str],
    action: RedactionAction,
    secret: str,
    current_path: str,
    redacted_paths: list[str],
) -> Any:
    if not parts:
        redacted_paths.append(current_path)
        if action == RedactionAction.DROP:
            return None
        if action == RedactionAction.HMAC_TOKEN:
            return _hmac_token(str(node), secret)
        return "[REDACTED]"

    head, tail = parts[0], parts[1:]
    if isinstance(node, dict) and head in node:
        updated = dict(node)
        next_path = f"{current_path}.{head}"
        if not tail and action == RedactionAction.DROP:
            del updated[head]
            redacted_paths.append(next_path)
            return updated
        updated[head] = _apply_rule_to_tree(
            updated[head], tail, action, secret, next_path, redacted_paths
        )
        return updated

    if isinstance(node, list) and head == "[*]":
        return [
            _apply_rule_to_tree(item, tail, action, secret, f"{current_path}[*]", redacted_paths)
            for item in node
        ]
    return node


def _parse_jsonpath(path: str) -> list[str]:
    cleaned = path.removeprefix("$.")
    tokens: list[str] = []
    for part in cleaned.split("."):
        if part.endswith("[*]"):
            base = part[:-3]
            if base:
                tokens.append(base)
            tokens.append("[*]")
        elif part:
            tokens.append(part)
    return tokens


def _scan_builtin_secrets(
    value: Any,
    detectors: list[str],
    path: str,
    redacted_paths: list[str],
) -> Any:
    if isinstance(value, str):
        updated = value
        for name in detectors:
            pattern = _SECRET_PATTERNS.get(name)
            if pattern and pattern.search(updated):
                updated = pattern.sub(f"[REDACTED:{name.upper()}]", updated)
                if path not in redacted_paths:
                    redacted_paths.append(path)
        return updated
    if isinstance(value, dict):
        return {
            k: _scan_builtin_secrets(v, detectors, f"{path}.{k}", redacted_paths)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _scan_builtin_secrets(item, detectors, f"{path}[*]", redacted_paths) for item in value
        ]
    return value


def _apply_explicit_rules(
    root: dict[str, Any],
    rules: list[RedactionRule],
    secret: str,
    redacted_paths: list[str],
) -> dict[str, Any]:
    updated = root
    for rule in rules:
        tokens = _parse_jsonpath(rule.path)
        if not tokens:
            continue
        result = _apply_rule_to_tree(updated, tokens, rule.action, secret, "$", redacted_paths)
        if isinstance(result, dict):
            updated = result
    return updated


def redact_envelope(
    envelope: InteractionEnvelope,
    config: CaptureConfig,
) -> InteractionEnvelope:
    """Apply configured JSONPath rules and built-in regex detectors before persistence."""
    if not config.redaction.enabled:
        return envelope

    secret = os.environ.get(config.redaction.hmac_secret_env, "regrip-local-default-salt")
    redacted_paths = list(envelope.request.redacted_paths)

    req_root: dict[str, Any] = {"arguments": dict(envelope.request.arguments)}
    req_root = _apply_explicit_rules(req_root, config.redaction.rules, secret, redacted_paths)
    req_args = _scan_builtin_secrets(
        req_root["arguments"],
        config.redaction.builtin_detectors,
        "$.arguments",
        redacted_paths,
    )

    resp_root: dict[str, Any] = {
        "structured_content": envelope.response.structured_content,
        "parsed_text_json": envelope.response.parsed_text_json,
        "content_blocks": envelope.response.content_blocks,
        "error_message": envelope.response.error_message,
    }
    resp_root = _apply_explicit_rules(resp_root, config.redaction.rules, secret, redacted_paths)
    resp_root = _scan_builtin_secrets(
        resp_root, config.redaction.builtin_detectors, "$", redacted_paths
    )

    new_request = envelope.request.model_copy(
        update={"arguments": req_args, "redacted_paths": sorted(set(redacted_paths))}
    )
    new_response = envelope.response.model_copy(update=resp_root)
    new_telemetry = envelope.telemetry.model_copy(update={"redaction_applied": True})
    return envelope.model_copy(
        update={
            "request": new_request,
            "response": new_response,
            "telemetry": new_telemetry,
        }
    )
