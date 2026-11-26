"""Phase 8 REST/OpenAPI ProtocolDriver adapter (SRS Section 2.3 & 6.2)."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from regrip.core.payload_normalizer import canonical_hash
from regrip.types.baseline import CapabilityManifest, ToolDescriptor
from regrip.types.envelope import InteractionEnvelope, ResponsePayload, ResponseStatus
from regrip.types.errors import ProtocolError

_PATH_PARAM_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")


def _strip_scheme(target_uri: str) -> str:
    if target_uri.lower().startswith("openapi+"):
        return target_uri[len("openapi+") :]
    return target_uri


def _operation_input_schema(op_spec: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for param in op_spec.get("parameters", []):
        if isinstance(param, dict) and isinstance(param.get("name"), str):
            name = param["name"]
            props[name] = param.get("schema", {"type": "string"})
            if param.get("required"):
                required.append(name)
    req_body = op_spec.get("requestBody", {})
    if isinstance(req_body, dict):
        app_json = req_body.get("content", {}).get("application/json", {})
        body_schema = app_json.get("schema", {})
        if isinstance(body_schema, dict):
            for k, v in body_schema.get("properties", {}).items():
                props[k] = v
            required.extend(body_schema.get("required", []))
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = sorted(set(required))
    return schema


def _operation_output_schema(op_spec: dict[str, Any]) -> dict[str, Any] | None:
    responses = op_spec.get("responses", {})
    ok_resp = responses.get("200") or responses.get("201")
    if not isinstance(ok_resp, dict):
        return None
    schema = ok_resp.get("content", {}).get("application/json", {}).get("schema")
    return schema if isinstance(schema, dict) else None


class HTTPDriver:
    """ProtocolDriver implementation for REST/OpenAPI 3 endpoints."""

    driver_name: str = "http"

    def __init__(
        self,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._transport = transport
        self._routes: dict[str, tuple[str, str]] = {}

    async def discover_capabilities(self, target_uri: str) -> CapabilityManifest:
        base_url = _strip_scheme(target_uri).rstrip("/")
        spec_url = base_url if base_url.endswith(".json") else f"{base_url}/openapi.json"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(spec_url)
                resp.raise_for_status()
                spec: Any = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProtocolError(
                f"Failed to fetch OpenAPI spec: {exc}", context={"url": spec_url}
            ) from exc

        if not isinstance(spec, dict):
            raise ProtocolError("OpenAPI spec must be a JSON object", context={"url": spec_url})

        tools: list[ToolDescriptor] = []
        for path, methods in sorted(spec.get("paths", {}).items()):
            if not isinstance(methods, dict):
                continue
            for method, op_spec in sorted(methods.items()):
                if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                    continue
                if not isinstance(op_spec, dict):
                    continue
                op_name = str(op_spec.get("operationId") or f"{method.upper()} {path}")
                self._routes[op_name] = (method.upper(), path)
                in_schema = _operation_input_schema(op_spec)
                out_schema = _operation_output_schema(op_spec)
                tools.append(
                    ToolDescriptor(
                        name=op_name,
                        description=op_spec.get("summary") or op_spec.get("description"),
                        input_schema=in_schema,
                        output_schema=out_schema,
                        input_schema_hash=canonical_hash(in_schema),
                        output_schema_hash=canonical_hash(out_schema) if out_schema else None,
                    )
                )
        info = spec.get("info", {}) if isinstance(spec.get("info"), dict) else {}
        return CapabilityManifest(
            server_name=str(info.get("title") or base_url),
            server_version=str(info["version"]) if "version" in info else None,
            spec_version=str(spec.get("openapi") or "3.0.0"),
            tools=tools,
        )

    def _resolve_method_and_path(self, envelope: InteractionEnvelope) -> tuple[str, str]:
        tool_name = envelope.operation.tool_name
        if tool_name in self._routes:
            return self._routes[tool_name]
        raw_method = envelope.operation.method.strip()
        if " " in raw_method:
            verb, path = raw_method.split(" ", 1)
            return verb.upper(), path
        return "POST", f"/{tool_name.lstrip('/')}"

    async def invoke(self, envelope: InteractionEnvelope, target_uri: str) -> ResponsePayload:
        base_url = _strip_scheme(target_uri).rstrip("/")
        if base_url.endswith("/openapi.json"):
            base_url = base_url[: -len("/openapi.json")]
        verb, path_tmpl = self._resolve_method_and_path(envelope)
        args = dict(envelope.request.arguments)

        def _replace_param(match: re.Match[str]) -> str:
            key = match.group(1)
            return str(args.pop(key, ""))

        resolved_path = _PATH_PARAM_RE.sub(_replace_param, path_tmpl)
        url = f"{base_url}{resolved_path}"
        kwargs: dict[str, Any] = {"params": args} if verb in {"GET", "DELETE"} else {"json": args}

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.request(verb, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ProtocolError(f"HTTP invocation failed: {exc}", context={"url": url}) from exc

        parsed: Any = None
        if resp.content:
            try:
                parsed = resp.json()
            except ValueError:
                try:
                    parsed = json.loads(resp.text)
                except ValueError:
                    parsed = None

        if resp.status_code >= 400:
            return ResponsePayload(
                status=ResponseStatus.TOOL_ERROR,
                is_error=True,
                error_code=resp.status_code,
                error_message=resp.text[:300] or f"HTTP {resp.status_code}",
                structured_content=parsed if isinstance(parsed, (dict, list)) else None,
            )
        return ResponsePayload(
            status=ResponseStatus.OK,
            is_error=False,
            structured_content=parsed if isinstance(parsed, (dict, list)) else None,
            content_blocks=[{"type": "text", "text": resp.text}] if parsed is None else [],
        )
