"""Run with `uv run python examples/sdk_consumer_lineage.py` from the repository root."""

from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path
from tempfile import mkdtemp
from types import SimpleNamespace

import yaml

from regrip import RegripRecorder, expect_fields
from regrip.adapters.mcp_stdio import MCPStdioDriver
from regrip.adapters.replay_runner import ReplayRunner
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.core.baseline_builder import build_baseline
from regrip.types.baseline import BehavioralBaseline, SafetyTier
from regrip.types.config import RegripConfig, SafetyConfig, StorageConfig, ToolPolicyConfig
from regrip.types.envelope import (
    ActorInfo,
    InteractionEnvelope,
    JsonObject,
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

SERVER_NAME = "commerce-mcp"


def server_command(version: str) -> str:
    """Quote absolute paths for the driver's shlex parser, including Windows paths."""
    server = Path(__file__).resolve().with_name(f"mcp_{version}.py")
    return shlex.join([Path(sys.executable).as_posix(), server.as_posix()])


class DemoMCPSession:
    """Adapt the built-in stdio driver to call_tool without an extra MCP dependency."""

    async def call_tool(self, name: str, arguments: JsonObject) -> SimpleNamespace:
        request = InteractionEnvelope(
            protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
            actor=ActorInfo(agent_id="support-agent"),
            target=TargetInfo(server_name=SERVER_NAME),
            operation=OperationInfo(tool_name=name),
            request=RequestPayload(arguments=arguments),
            response=ResponsePayload(status=ResponseStatus.OK),
            telemetry=TelemetryInfo(duration_ms=0),
        )
        response = await MCPStdioDriver().invoke(request, server_command("v1"))
        return SimpleNamespace(
            isError=response.is_error,
            structuredContent=response.structured_content,
            content=response.content_blocks,
        )


def example_config(workspace: Path) -> RegripConfig:
    """Keep each run's capture, contracts, and replay in a separate local workspace."""
    return RegripConfig(
        project="sdk-consumer-lineage",
        storage=StorageConfig(
            dsn=f"sqlite:///{workspace / 'regrip.db'}", blob_dir=str(workspace / "blobs")
        ),
        safety=SafetyConfig(
            tool_policies={
                name: ToolPolicyConfig(tier=SafetyTier.READ_SAFE)
                for name in ("get_balance", "search_products")
            }
        ),
    )


async def record_baseline(config: RegripConfig, store: SQLiteStore) -> BehavioralBaseline:
    """Capture real v1 responses and both explicit and dynamically observed dependencies."""
    recorder = RegripRecorder(
        agent_id="support-agent",
        store_uri=config.storage.dsn,
        blob_dir=config.storage.blob_dir,
        fail_open=False,
    )
    async with recorder.wrap_mcp_session(DemoMCPSession(), server_name=SERVER_NAME) as session:
        # Three samples let the baseline automatically mask the random request_id UUIDs.
        for _ in range(3):
            with expect_fields(tool="get_balance", jsonpaths=["$.balance", "$.currency"]):
                await session.call_tool("get_balance", {"id": "acct-1"})

            result = await session.call_tool("search_products", {"query": "laptop"})
            tracked = recorder.track_consumption(
                tool="search_products", payload=result.structuredContent
            )
            # No explicit description contract: this key access discovers the dependency.
            description = tracked["results"][0]["description"]
            print(f"Agent reads product description: {description}", flush=True)

    # The SDK collects contracts in memory; persist them so the CLI can load them.
    for contract in recorder.get_consumer_contracts():
        store.save_consumer_contract(contract)
        paths = ", ".join(contract.consumed_jsonpaths)
        print(f"{contract.discovery_source.value}: {contract.tool_name} -> {paths}", flush=True)

    baseline = build_baseline(
        store.list_interactions(server_name=SERVER_NAME), server_name=SERVER_NAME, tag="sdk-v1"
    )
    store.save_baseline(baseline)
    return baseline


async def main() -> None:
    output_root = Path(".regrip/sdk-consumer-lineage")
    output_root.mkdir(parents=True, exist_ok=True)
    workspace = Path(mkdtemp(prefix="run-", dir=output_root)).resolve()
    config = example_config(workspace)
    config_path = workspace / "regrip.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    store = SQLiteStore(config.storage, config.capture)

    print(f"Workspace: {workspace}", flush=True)
    baseline = await record_baseline(config, store)
    baseline_path = workspace / "baseline.json"
    baseline_path.write_text(baseline.model_dump_json(indent=2), encoding="utf-8")
    run = await ReplayRunner(config, store=store).run(
        baseline, server_command("v2"), sandbox="subprocess"
    )
    if run.summary.errored or run.summary.skipped_unsafe:
        raise RuntimeError(f"Replay did not complete: {run.summary.model_dump()}")

    # v2 removes description and scales balance 100x; both consumed fields should regress.
    impact_args = [
        "impact",
        "--config",
        config_path.as_posix(),
        "--baseline",
        baseline_path.as_posix(),
        "--run-id",
        str(run.run_id),
    ]
    print("\nRun this command again to inspect the saved replay:", flush=True)
    print(shlex.join(["uv", "run", "regrip", *impact_args]), flush=True)
    # Use the console entrypoint with this interpreter, including outside an activated venv.
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "from regrip.cli.app import main; main()", *impact_args
    )
    if await process.wait() != 0:
        raise RuntimeError("regrip impact failed; inspect the CLI error above.")


if __name__ == "__main__":
    asyncio.run(main())
