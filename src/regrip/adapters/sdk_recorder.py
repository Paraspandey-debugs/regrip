"""Python SDK instrumentation (`RegripRecorder`, `expect_fields`, dynamic tracking)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any

from regrip.adapters.sqlite_store import SQLiteStore
from regrip.core.payload_normalizer import normalize_response
from regrip.types.config import StorageConfig
from regrip.types.envelope import (
    ActorInfo,
    InteractionEnvelope,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    RequestPayload,
    ResponsePayload,
    ResponseStatus,
    TargetInfo,
    TelemetryInfo,
    TransportKind,
)
from regrip.types.impact import ConsumerContract, ContractDiscoverySource

_ACTIVE_RECORDER: ContextVar[RegripRecorder | None] = ContextVar(
    "regrip_active_recorder", default=None
)
_PENDING_CONTRACTS: ContextVar[list[ConsumerContract] | None] = ContextVar(
    "regrip_pending_contracts", default=None
)


class TrackedValueMixin:
    """Shared path-tracking proxy wrapper for dict and list responses."""

    def _wrap(self, value: Any, path: str, accessed: set[str]) -> Any:
        if isinstance(value, dict):
            return TrackedDict(value, path, accessed)
        if isinstance(value, list):
            return TrackedList(value, path, accessed)
        return value


class TrackedDict(dict[str, Any], TrackedValueMixin):
    """Dict proxy recording accessed JSONPaths into a consumer contract set."""

    def __init__(self, data: dict[str, Any], prefix: str, accessed: set[str]) -> None:
        super().__init__(data)
        self._prefix = prefix
        self._accessed = accessed

    def __getitem__(self, key: str) -> Any:
        val = super().__getitem__(key)
        child_path = f"{self._prefix}.{key}"
        self._accessed.add(child_path)
        return self._wrap(val, child_path, self._accessed)

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self:
            return default
        return self[key]


class TrackedList(list[Any], TrackedValueMixin):
    """List proxy recording wildcard `[*]` JSONPath access."""

    def __init__(self, data: list[Any], prefix: str, accessed: set[str]) -> None:
        super().__init__(data)
        self._prefix = prefix
        self._accessed = accessed

    def __getitem__(self, index: Any) -> Any:
        val = super().__getitem__(index)
        child_path = f"{self._prefix}[*]"
        self._accessed.add(child_path)
        return self._wrap(val, child_path, self._accessed)

    def __iter__(self) -> Iterator[Any]:
        child_path = f"{self._prefix}[*]"
        for item in super().__iter__():
            self._accessed.add(child_path)
            yield self._wrap(item, child_path, self._accessed)


@contextmanager
def expect_fields(
    *,
    tool: str,
    jsonpaths: Sequence[str],
    order_sensitive: bool = False,
) -> Iterator[None]:
    """Declare explicit field expectations inside a recorder session (FR-IMP-001)."""
    recorder = _ACTIVE_RECORDER.get()
    if recorder is not None:
        recorder.register_contract(
            tool_name=tool,
            jsonpaths=list(jsonpaths),
            order_sensitive=order_sensitive,
            source=ContractDiscoverySource.EXPLICIT_SDK,
        )
    else:
        pending = _PENDING_CONTRACTS.get()
        if pending is None:
            pending = []
            _PENDING_CONTRACTS.set(pending)
        pending.append(
            ConsumerContract(
                agent_id="default-agent",
                tool_name=tool,
                consumed_jsonpaths=list(jsonpaths),
                order_sensitive=order_sensitive,
                discovery_source=ContractDiscoverySource.EXPLICIT_SDK,
            )
        )
    yield


def _extract_mcp_result_payload(result: Any) -> ResponsePayload:
    is_error = bool(getattr(result, "isError", False))
    structured = getattr(result, "structuredContent", None)
    raw_content = getattr(result, "content", []) or []
    content_blocks: list[dict[str, Any]] = []
    for block in raw_content:
        if isinstance(block, dict):
            content_blocks.append(block)
        elif hasattr(block, "model_dump"):
            content_blocks.append(block.model_dump(mode="json"))
        elif hasattr(block, "text"):
            content_blocks.append({"type": "text", "text": str(block.text)})

    return normalize_response(
        ResponsePayload(
            status=ResponseStatus.TOOL_ERROR if is_error else ResponseStatus.OK,
            is_error=is_error,
            structured_content=structured if isinstance(structured, (dict, list)) else None,
            content_blocks=content_blocks,
        )
    )


class _WrappedMCPSession:
    """Transparent proxy around an MCP ClientSession."""

    def __init__(self, session: Any, recorder: RegripRecorder, server_name: str) -> None:
        self._session = session
        self._recorder = recorder
        self._server_name = server_name

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        started = time.monotonic()
        result = await self._session.call_tool(name, arguments or {}, **kwargs)
        duration_ms = round((time.monotonic() - started) * 1000.0, 3)
        self._recorder.record_tool_call(
            server_name=self._server_name,
            tool_name=name,
            arguments=arguments or {},
            result=result,
            duration_ms=duration_ms,
        )
        return result

    def __getattr__(self, item: str) -> Any:
        return getattr(self._session, item)


class RegripRecorder:
    """Non-invasive Python SDK recorder for MCP sessions and field lineage (FR-CAP-003)."""

    def __init__(
        self,
        *,
        agent_id: str,
        store_uri: str = "sqlite:///.regrip/regrip.db",
        blob_dir: str = ".regrip/blobs",
        session_id: str | None = None,
        fail_open: bool = True,
    ) -> None:
        self.agent_id = agent_id
        self.session_id = session_id
        self.fail_open = fail_open
        self._store: SQLiteStore | None = None
        if store_uri.startswith("sqlite:///") or "://" not in store_uri:
            try:
                self._store = SQLiteStore(StorageConfig(dsn=store_uri, blob_dir=blob_dir))
            except Exception:
                if not fail_open:
                    raise
        self._contracts: dict[tuple[str, ContractDiscoverySource], set[str]] = {}
        self._order_sensitive: dict[str, bool] = {}

    def register_contract(
        self,
        *,
        tool_name: str,
        jsonpaths: list[str],
        order_sensitive: bool = False,
        source: ContractDiscoverySource = ContractDiscoverySource.EXPLICIT_SDK,
    ) -> None:
        key = (tool_name, source)
        self._contracts.setdefault(key, set()).update(jsonpaths)
        if order_sensitive:
            self._order_sensitive[tool_name] = True

    def record_tool_call(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        duration_ms: float,
    ) -> None:
        if self._store is None:
            return
        envelope = InteractionEnvelope(
            protocol=ProtocolInfo(
                driver=ProtocolDriverKind.MCP,
                transport=TransportKind.SDK_INPROC,
            ),
            actor=ActorInfo(agent_id=self.agent_id, session_id=self.session_id),
            target=TargetInfo(server_name=server_name),
            operation=OperationInfo(method="tools/call", tool_name=tool_name),
            request=RequestPayload(arguments=arguments),
            response=_extract_mcp_result_payload(result),
            telemetry=TelemetryInfo(duration_ms=duration_ms),
        )
        try:
            self._store.save_interaction(envelope)
        except Exception:
            if not self.fail_open:
                raise

    @asynccontextmanager
    async def wrap_mcp_session(
        self, mcp_session: Any, *, server_name: str
    ) -> AsyncIterator[_WrappedMCPSession]:
        """Wrap an MCP ClientSession and record all call_tool invocations."""
        token = _ACTIVE_RECORDER.set(self)
        try:
            yield _WrappedMCPSession(mcp_session, self, server_name)
        finally:
            _ACTIVE_RECORDER.reset(token)

    def track_consumption(self, *, tool: str, payload: Any) -> Any:
        """Wrap a structured response dict/list to record accessed JSONPaths."""
        accessed = self._contracts.setdefault((tool, ContractDiscoverySource.DYNAMIC_ACCESS), set())
        if isinstance(payload, dict):
            return TrackedDict(payload, "$", accessed)
        if isinstance(payload, list):
            return TrackedList(payload, "$", accessed)
        return payload

    def get_consumer_contracts(self) -> list[ConsumerContract]:
        """Return all ConsumerContracts collected via expect_fields and track_consumption."""
        return [
            ConsumerContract(
                agent_id=self.agent_id,
                tool_name=tool_name,
                consumed_jsonpaths=sorted(paths),
                order_sensitive=self._order_sensitive.get(tool_name, False),
                discovery_source=source,
            )
            for (tool_name, source), paths in sorted(self._contracts.items())
        ]
