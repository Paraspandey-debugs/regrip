"""Pure W3C trace context, agent attribution, and auth header stripping helpers."""

from __future__ import annotations

from typing import Any


def extract_trace_and_actor(
    meta: dict[str, Any] | None,
    headers: dict[str, str] | None,
    *,
    default_agent: str = "default-agent",
    default_session: str | None = None,
) -> tuple[str, str | None, str | None, str | None]:
    """Extract (agent_id, session_id, traceparent, tracestate) from headers and _meta."""
    norm_headers = {k.lower(): v for k, v in (headers or {}).items()}
    meta_dict = meta or {}

    agent_id = (
        norm_headers.get("x-regrip-agent")
        or (
            meta_dict.get("regrip.agent_id")
            if isinstance(meta_dict.get("regrip.agent_id"), str)
            else None
        )
        or (meta_dict.get("agent_id") if isinstance(meta_dict.get("agent_id"), str) else None)
        or default_agent
    )
    session_id = (
        norm_headers.get("x-regrip-session")
        or norm_headers.get("mcp-session-id")
        or (
            meta_dict.get("regrip.session_id")
            if isinstance(meta_dict.get("regrip.session_id"), str)
            else None
        )
        or (meta_dict.get("session_id") if isinstance(meta_dict.get("session_id"), str) else None)
        or default_session
    )
    traceparent = (
        meta_dict.get("traceparent") if isinstance(meta_dict.get("traceparent"), str) else None
    ) or norm_headers.get("traceparent")
    tracestate = (
        meta_dict.get("tracestate") if isinstance(meta_dict.get("tracestate"), str) else None
    ) or norm_headers.get("tracestate")
    return agent_id, session_id, traceparent, tracestate


def strip_sensitive_headers(headers: dict[str, str], strip_list: list[str]) -> dict[str, str]:
    """Remove sensitive authentication headers prior to persistence (FR-CAP-007)."""
    blocked = {h.lower() for h in strip_list}
    return {k: v for k, v in headers.items() if k.lower() not in blocked}
