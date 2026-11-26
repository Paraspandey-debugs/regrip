"""MCP Streamable HTTP & SSE reverse capture proxy (FR-CAP-002, NFR-REL-001)."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlparse

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from regrip.adapters.mcp_jsonrpc import MCP_SPEC_VERSION, parse_jsonrpc_tool_response
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.core.trace_context import extract_trace_and_actor
from regrip.types.envelope import (
    ActorInfo,
    InteractionEnvelope,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    RequestPayload,
    TargetInfo,
    TelemetryInfo,
    TransportKind,
)


def _extract_sse_json_dicts(chunk: bytes, buffer: str) -> tuple[list[dict[str, Any]], str]:
    text = buffer + chunk.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    remainder = lines.pop() if lines and not lines[-1].endswith(("\n", "\r")) else ""
    frames: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        payload = stripped[5:].strip()
        if (
            payload
            and payload != "[DONE]"
            and (parsed := _try_parse_json_dict(payload.encode("utf-8")))
        ):
            frames.append(parsed)
    return frames, remainder


def _try_parse_json_dict(raw: bytes) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed: Any = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _build_envelope_from_http(
    req_frame: dict[str, Any],
    resp_frame: dict[str, Any],
    *,
    server_name: str,
    agent_id: str,
    session_id: str | None,
    traceparent: str | None,
    tracestate: str | None,
    duration_ms: float,
) -> InteractionEnvelope | None:
    if req_frame.get("method") != "tools/call":
        return None
    params = req_frame.get("params")
    if not isinstance(params, dict):
        return None
    tool_name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(tool_name, str) or not isinstance(arguments, dict):
        return None

    return InteractionEnvelope(
        protocol=ProtocolInfo(
            driver=ProtocolDriverKind.MCP,
            spec_version=MCP_SPEC_VERSION,
            transport=TransportKind.STREAMABLE_HTTP,
        ),
        actor=ActorInfo(agent_id=agent_id, session_id=session_id),
        target=TargetInfo(server_name=server_name),
        operation=OperationInfo(method="tools/call", tool_name=tool_name),
        request=RequestPayload(arguments=arguments),
        response=parse_jsonrpc_tool_response(resp_frame),
        telemetry=TelemetryInfo(
            duration_ms=round(duration_ms, 3),
            traceparent=traceparent,
            tracestate=tracestate,
        ),
    )


def _persist_fail_open(
    store: SQLiteStore,
    envelope: InteractionEnvelope | None,
    on_error: Callable[[Exception], None] | None,
) -> None:
    if envelope is None:
        return
    try:
        store.save_interaction(envelope)
    except Exception as exc:  # fail-open capture (NFR-REL-001)
        if on_error:
            on_error(exc)


def _resolve_target_url(request: Request, upstream_url: str) -> str:
    parsed = urlparse(upstream_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    if request.url.path == "/" and parsed.path:
        return upstream_url
    target = f"{base_url}{request.url.path}"
    return f"{target}?{request.url.query}" if request.url.query else target


async def _forward_sse(
    upstream_resp: httpx.Response,
    req_frame: dict[str, Any] | None,
    *,
    store: SQLiteStore,
    server_name: str,
    agent_id: str,
    session_id: str | None,
    traceparent: str | None,
    tracestate: str | None,
    started: float,
    on_error: Callable[[Exception], None] | None,
    close_client: Callable[[], Any],
) -> AsyncIterator[bytes]:
    sse_buf = ""
    try:
        async for chunk in upstream_resp.aiter_bytes():
            if req_frame is not None:
                frames, sse_buf = _extract_sse_json_dicts(chunk, sse_buf)
                for frame in frames:
                    env = _build_envelope_from_http(
                        req_frame,
                        frame,
                        server_name=server_name,
                        agent_id=agent_id,
                        session_id=session_id,
                        traceparent=traceparent,
                        tracestate=tracestate,
                        duration_ms=(time.monotonic() - started) * 1000.0,
                    )
                    _persist_fail_open(store, env, on_error)
            yield chunk
    finally:
        await upstream_resp.aclose()
        await close_client()


def create_http_proxy_app(
    upstream_url: str,
    *,
    store: SQLiteStore,
    server_name: str,
    default_agent: str = "default-agent",
    default_session: str | None = None,
    on_error: Callable[[Exception], None] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> Starlette:
    """Create a Starlette ASGI app proxying Streamable HTTP / SSE MCP traffic."""

    async def _proxy_request(request: Request) -> Response:
        body = await request.body()
        req_frame = _try_parse_json_dict(body)
        meta = (
            req_frame.get("params", {}).get("_meta")
            if req_frame and isinstance(req_frame.get("params"), dict)
            else None
        )
        agent_id, session_id, traceparent, tracestate = extract_trace_and_actor(
            meta if isinstance(meta, dict) else None,
            dict(request.headers),
            default_agent=default_agent,
            default_session=default_session,
        )

        client = http_client or httpx.AsyncClient(timeout=None)
        owns_client = http_client is None
        started = time.monotonic()
        req_headers = {
            k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length"}
        }
        upstream_req = client.build_request(
            method=request.method,
            url=_resolve_target_url(request, upstream_url),
            headers=req_headers,
            content=body,
        )
        upstream_resp = await client.send(upstream_req, stream=True)
        resp_headers = {
            k: v
            for k, v in upstream_resp.headers.items()
            if k.lower() not in {"content-length", "transfer-encoding", "content-encoding"}
        }
        resp_session = upstream_resp.headers.get("mcp-session-id") or session_id

        async def _close_if_owned() -> None:
            if owns_client:
                await client.aclose()

        if "text/event-stream" in upstream_resp.headers.get("content-type", ""):
            return StreamingResponse(
                _forward_sse(
                    upstream_resp,
                    req_frame,
                    store=store,
                    server_name=server_name,
                    agent_id=agent_id,
                    session_id=resp_session,
                    traceparent=traceparent,
                    tracestate=tracestate,
                    started=started,
                    on_error=on_error,
                    close_client=_close_if_owned,
                ),
                status_code=upstream_resp.status_code,
                headers=resp_headers,
                media_type="text/event-stream",
            )

        raw_body = await upstream_resp.aread()
        await upstream_resp.aclose()
        await _close_if_owned()
        if req_frame is not None and (resp_json := _try_parse_json_dict(raw_body)):
            env = _build_envelope_from_http(
                req_frame,
                resp_json,
                server_name=server_name,
                agent_id=agent_id,
                session_id=resp_session,
                traceparent=traceparent,
                tracestate=tracestate,
                duration_ms=(time.monotonic() - started) * 1000.0,
            )
            _persist_fail_open(store, env, on_error)
        return Response(
            content=raw_body, status_code=upstream_resp.status_code, headers=resp_headers
        )

    return Starlette(
        routes=[
            Route("/{path:path}", _proxy_request, methods=["GET", "POST", "DELETE"]),
            Route("/", _proxy_request, methods=["GET", "POST", "DELETE"]),
        ]
    )


def serve_http_proxy(
    listen_url: str,
    upstream_url: str,
    *,
    store: SQLiteStore,
    server_name: str,
    default_agent: str = "default-agent",
    default_session: str | None = None,
) -> None:
    """Start blocking uvicorn server for Streamable HTTP MCP reverse proxy."""
    parsed = urlparse(listen_url)
    app = create_http_proxy_app(
        upstream_url,
        store=store,
        server_name=server_name,
        default_agent=default_agent,
        default_session=default_session,
    )
    uvicorn.run(
        app, host=parsed.hostname or "127.0.0.1", port=parsed.port or 9100, log_level="warning"
    )
