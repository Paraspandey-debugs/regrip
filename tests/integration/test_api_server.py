"""Integration tests for REST API endpoints and local Web Dashboard."""

from __future__ import annotations

from pathlib import Path

import httpx

from regrip.adapters.api_server import create_api_app
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.types import (
    ActorInfo,
    ConsumerAgentConfig,
    ConsumerToolConfig,
    InteractionEnvelope,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    RegripConfig,
    ReplayCaseResult,
    ReplayCaseStatus,
    ReplayRun,
    ReplaySummary,
    RequestPayload,
    ResponsePayload,
    ResponseStatus,
    StorageConfig,
    TargetInfo,
    TelemetryInfo,
    TransportKind,
)


async def test_api_server_ingest_baseline_report_and_dashboard(tmp_path: Path) -> None:
    storage = StorageConfig(
        dsn=f"sqlite:///{tmp_path / 'regrip.db'}",
        blob_dir=str(tmp_path / "blobs"),
    )
    cfg = RegripConfig(
        storage=storage,
        consumers=[
            ConsumerAgentConfig(
                agent_id="pricing-bot",
                tools={"get_balance": ConsumerToolConfig(consumed_jsonpaths=["$.balance"])},
            )
        ],
    )
    store = SQLiteStore(storage, cfg.capture)
    app = create_api_app(cfg, store)

    env = InteractionEnvelope(
        protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
        actor=ActorInfo(agent_id="pricing-bot"),
        target=TargetInfo(server_name="finance-mcp"),
        operation=OperationInfo(tool_name="get_balance"),
        request=RequestPayload(arguments={"id": "a1"}),
        response=ResponsePayload(status=ResponseStatus.OK, structured_content={"balance": 100}),
        telemetry=TelemetryInfo(duration_ms=4.0),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://regrip.local") as client:
        r_health = await client.get("/api/v1/health")
        assert r_health.status_code == 200
        assert r_health.json()["status"] == "ok"

        r_dash = await client.get("/")
        assert r_dash.status_code == 200
        assert "regrip" in r_dash.text
        assert "behavioral diff" in r_dash.text

        r_icon = await client.get("/favicon.ico")
        assert r_icon.status_code == 200
        assert r_icon.headers["content-type"] == "image/png"

        r_asset = await client.get("/assets/regrip-04-favicon-32px.png")
        assert r_asset.status_code == 200
        assert len(r_asset.content) > 0

        r_ingest = await client.post(
            "/api/v1/projects/demo/interactions",
            json={"envelopes": [env.model_dump(mode="json")]},
        )
        assert r_ingest.status_code == 202
        assert r_ingest.json()["ingested"] == 1

        r_base = await client.post(
            "/api/v1/projects/demo/baselines",
            json={"server_name": "finance-mcp", "tag": "v1"},
        )
        assert r_base.status_code == 201
        baseline_data = r_base.json()

        run = ReplayRun(
            baseline_id=baseline_data["baseline_id"],
            baseline_tag="v1",
            target_uri="stdio://v2",
            summary=ReplaySummary(total=1, failed=1),
            results=[
                ReplayCaseResult(
                    case_id=baseline_data["cases"][0]["case_id"],
                    tool_name="get_balance",
                    cluster_key=baseline_data["cases"][0]["cluster_key"],
                    status=ReplayCaseStatus.FAIL,
                    reason="100x",
                    baseline_status=ResponseStatus.OK,
                    candidate_status=ResponseStatus.OK,
                    candidate_response=ResponsePayload(
                        status=ResponseStatus.OK, structured_content={"balance": 10000}
                    ),
                )
            ],
        )
        store.save_replay_run(run)

        r_report = await client.get(f"/api/v1/projects/demo/runs/{run.run_id}/report")
        assert r_report.status_code == 200
        report = r_report.json()
        assert report["verdict"] == "FAIL"
        assert report["agent_impacts"][0]["impact_status"] == "CONFIRMED_REGRESSION"

        r_hist = await client.get("/api/v1/projects/demo/tools/get_balance/history")
        assert r_hist.status_code == 200
        assert r_hist.json()["total_recorded_calls"] == 1
