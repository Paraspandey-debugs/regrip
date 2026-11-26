"""Pure payload normalization, shape signatures, and JSONPath type inference."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import JsonValue

from regrip.types.envelope import JsonObject, ResponsePayload

_NUMERIC_STR_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value deterministically."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value: Any) -> str:
    """Return a SHA-256 hex digest of a JSON-serializable structure."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def extract_parsed_text_json(
    content_blocks: list[JsonObject],
) -> JsonObject | list[JsonValue] | None:
    """Parse JSON from the first valid JSON TextContent block if present."""
    for block in content_blocks:
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        stripped = text.strip()
        if not stripped or stripped[0] not in "{[":
            continue
        try:
            parsed: Any = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def infer_value_type(value: Any) -> str:
    """Infer fine-grained Regrip type label for a JSON value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "numeric_string" if _NUMERIC_STR_RE.match(value) else "str"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def flatten_json_paths(value: Any, prefix: str = "$") -> dict[str, Any]:
    """Flatten a nested dict/list payload into JSONPath -> leaf/container values."""
    paths: dict[str, Any] = {}
    if isinstance(value, dict):
        for key in sorted(value.keys()):
            child_path = f"{prefix}.{key}"
            child = value[key]
            paths[child_path] = child
            if isinstance(child, (dict, list)):
                paths.update(flatten_json_paths(child, child_path))
    elif isinstance(value, list):
        paths[prefix] = value
        for item in value:
            if isinstance(item, (dict, list)):
                paths.update(flatten_json_paths(item, f"{prefix}[*]"))
            else:
                paths.setdefault(f"{prefix}[*]", item)
    return paths


def infer_payload_types(payload: Any) -> dict[str, str]:
    """Infer a JSONPath -> type label map for a structured payload."""
    if payload is None:
        return {}
    flat = flatten_json_paths(payload)
    return {path: infer_value_type(val) for path, val in sorted(flat.items())}


def value_shape_signature(value: Any) -> Any:
    """Compute a structural shape signature for input clustering."""
    if isinstance(value, dict):
        return {k: value_shape_signature(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        if not value:
            return ["empty"]
        return ["list", value_shape_signature(value[0])]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "empty_str" if value == "" else "str"
    return "other"


def build_cluster_key(tool_name: str, arguments: JsonObject) -> str:
    """Compute clustering key from tool name, sorted arg keys, and value shapes."""
    sorted_keys = sorted(arguments.keys())
    shape = value_shape_signature(arguments)
    digest = canonical_hash({"keys": sorted_keys, "shape": shape})[:16]
    return f"{tool_name}:{digest}"


def normalize_response(response: ResponsePayload) -> ResponsePayload:
    """Populate parsed_text_json and inferred_types on a ResponsePayload."""
    parsed = response.parsed_text_json
    if response.structured_content is None and parsed is None and response.content_blocks:
        parsed = extract_parsed_text_json(response.content_blocks)
    effective = response.structured_content if response.structured_content is not None else parsed
    inferred = infer_payload_types(effective) if effective is not None else {}
    return response.model_copy(update={"parsed_text_json": parsed, "inferred_types": inferred})
