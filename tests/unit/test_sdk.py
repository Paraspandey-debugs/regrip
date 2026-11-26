"""Unit tests for the Regrip Python SDK (`RegripRecorder`, `expect_fields`, dynamic tracking)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from regrip import RegripRecorder, expect_fields
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.types import ContractDiscoverySource, StorageConfig


@dataclass
class _FakeToolResult:
    isError: bool = False  # noqa: N815
    structuredContent: dict[str, Any] | None = None  # noqa: N815
    content: list[Any] | None = None


class _FakeMCPSession:
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _FakeToolResult:
        return _FakeToolResult(
            structuredContent={
                "results": [{"id": "p1", "price": 99, "description": "laptop"}],
                "email": "alice@example.com",
            }
        )


async def test_sdk_wrap_session_expect_fields_and_track_consumption(tmp_path: Path) -> None:
    dsn = f"sqlite:///{tmp_path / 'sdk.db'}"
    blob_dir = str(tmp_path / "blobs")
    recorder = RegripRecorder(
        agent_id="support-agent-v2",
        store_uri=dsn,
        blob_dir=blob_dir,
        session_id="sess-sdk-1",
    )

    fake = _FakeMCPSession()
    async with recorder.wrap_mcp_session(fake, server_name="orders-mcp") as session:
        with expect_fields(
            tool="search_products",
            jsonpaths=["$.results[*].id", "$.results[*].description"],
            order_sensitive=True,
        ):
            result = await session.call_tool("search_products", {"query": "laptop"})

    tracked = recorder.track_consumption(tool="get_user", payload=result.structuredContent)
    _ = tracked["email"]
    _ = tracked["results"][0]["id"]

    contracts = recorder.get_consumer_contracts()
    by_source = {(c.tool_name, c.discovery_source): c for c in contracts}
    assert by_source[("search_products", ContractDiscoverySource.EXPLICIT_SDK)].order_sensitive
    assert by_source[("get_user", ContractDiscoverySource.DYNAMIC_ACCESS)].consumed_jsonpaths == [
        "$.email",
        "$.results",
        "$.results[*]",
        "$.results[*].id",
    ]

    store = SQLiteStore(StorageConfig(dsn=dsn, blob_dir=blob_dir))
    saved = store.list_interactions(server_name="orders-mcp")
    assert len(saved) == 1
    assert saved[0].actor.agent_id == "support-agent-v2"
    assert "[REDACTED:EMAIL]" in str(saved[0].response.structured_content)
