"""REST API server and local Web Dashboard for `regrip serve` (SRS §4.3, FR-OBS-002)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from regrip import __version__
from regrip.adapters.dashboard_html import DASHBOARD_HTML
from regrip.adapters.replay_runner import ReplayRunner
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.core.baseline_builder import build_baseline
from regrip.core.consumer_impact import (
    compute_consumer_impact,
    infer_session_correlation_contracts,
    merge_consumer_contracts,
)
from regrip.core.detectors import diff_baseline_against_run
from regrip.core.history_analyzer import build_tool_history
from regrip.types.baseline import ReplayRun
from regrip.types.config import RegripConfig
from regrip.types.envelope import InteractionEnvelope
from regrip.types.errors import RegripError
from regrip.types.impact import ConsumerContract, ContractDiscoverySource

_ASSETS_DIR = Path(__file__).resolve().parents[3] / "assets"


def _find_baseline_for_run(store: SQLiteStore, baseline_id: UUID, tag: str) -> Any:
    for b in store.list_baselines(limit=500):
        if b.baseline_id == baseline_id or b.tag == tag:
            return b
    return None


def create_api_app(config: RegripConfig, store: SQLiteStore) -> Starlette:
    """Create Starlette REST API + Web Dashboard ASGI application."""

    async def dashboard(_request: Request) -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML)

    async def serve_asset(request: Request) -> Response:
        filename = Path(request.path_params["filename"]).name
        asset_path = _ASSETS_DIR / filename
        if filename != request.path_params["filename"] or not asset_path.is_file():
            return JSONResponse({"error": "Asset not found"}, status_code=404)
        return Response(content=asset_path.read_bytes(), media_type="image/png")

    async def favicon(_request: Request) -> Response:
        icon_path = _ASSETS_DIR / "regrip-04-favicon-32px.png"
        if not icon_path.is_file():
            return Response(status_code=404)
        return Response(content=icon_path.read_bytes(), media_type="image/png")

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "version": __version__, "storage": config.storage.mode.value}
        )

    async def ingest_interactions(request: Request) -> JSONResponse:
        body: Any = await request.json()
        raw_list = body.get("envelopes", []) if isinstance(body, dict) else []
        count = 0
        for item in raw_list:
            env = InteractionEnvelope.model_validate(item)
            store.save_interaction(env)
            count += 1
        return JSONResponse({"ingested": count}, status_code=202)

    async def baselines_endpoint(request: Request) -> JSONResponse:
        if request.method == "GET":
            items = [b.model_dump(mode="json") for b in store.list_baselines()]
            return JSONResponse(items)
        body: Any = await request.json()
        server_name = str(body["server_name"])
        tag = str(body["tag"])
        git_sha = body.get("git_sha")
        envs = store.list_interactions(server_name=server_name, limit=5000)
        baseline = build_baseline(envs, server_name=server_name, tag=tag, git_sha=git_sha)
        store.save_baseline(baseline)
        return JSONResponse(baseline.model_dump(mode="json"), status_code=201)

    async def get_baseline(request: Request) -> JSONResponse:
        tag = request.path_params["tag"]
        server = request.query_params.get("server")
        if server:
            b = store.get_baseline_by_tag(server, tag)
            return JSONResponse(b.model_dump(mode="json"))
        for item in store.list_baselines(limit=500):
            if item.tag == tag:
                return JSONResponse(item.model_dump(mode="json"))
        return JSONResponse({"error": "Baseline not found"}, status_code=404)

    async def runs_endpoint(request: Request) -> JSONResponse:
        if request.method == "GET":
            runs = [r.model_dump(mode="json") for r in store.list_replay_runs()]
            return JSONResponse(runs)
        body: Any = await request.json()
        if isinstance(body, dict) and "run" in body:
            run_obj = ReplayRun.model_validate(body["run"])
            store.save_replay_run(run_obj)
            return JSONResponse(run_obj.model_dump(mode="json"), status_code=201)
        tag = str(body["baseline_tag"])
        server = body.get("server_name")
        baseline = (
            store.get_baseline_by_tag(str(server), tag)
            if server
            else _find_baseline_for_run(store, UUID(int=0), tag)
        )
        if baseline is None:
            return JSONResponse({"error": "Baseline not found"}, status_code=404)
        runner = ReplayRunner(config, store=store)
        run_obj = await runner.run(
            baseline,
            str(body["target_uri"]),
            allow_mutations=bool(body.get("allow_mutations", False)),
            target_git_sha=body.get("target_git_sha"),
            sandbox=body.get("sandbox"),
        )
        return JSONResponse(run_obj.model_dump(mode="json"), status_code=201)

    async def upsert_contract(request: Request) -> JSONResponse:
        agent_key = request.path_params["agent_key"]
        body: Any = await request.json()
        contract = ConsumerContract(
            agent_id=agent_key,
            tool_name=str(body["tool_name"]),
            consumed_jsonpaths=list(body.get("consumed_jsonpaths", [])),
            order_sensitive=bool(body.get("order_sensitive", False)),
            discovery_source=ContractDiscoverySource.EXPLICIT_SDK,
        )
        store.save_consumer_contract(contract)
        return JSONResponse(contract.model_dump(mode="json"))

    async def get_run_report(request: Request) -> JSONResponse:
        run_id = UUID(request.path_params["run_id"])
        try:
            run = store.get_replay_run(run_id)
        except RegripError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        baseline = _find_baseline_for_run(store, run.baseline_id, run.baseline_tag)
        if baseline is None:
            return JSONResponse({"error": "Baseline for run not found"}, status_code=404)
        diff_report = diff_baseline_against_run(baseline, run, config.comparison)
        envs = store.list_interactions(server_name=baseline.server_name, limit=5000)
        contracts = merge_consumer_contracts(
            config.consumers,
            store.list_consumer_contracts(),
            infer_session_correlation_contracts(envs),
        )
        volumes = dict(Counter(e.operation.tool_name for e in envs))
        impact = compute_consumer_impact(diff_report, contracts, call_volumes=volumes)
        return JSONResponse(
            {
                "verdict": diff_report.verdict.value,
                "findings": [f.model_dump(mode="json") for f in diff_report.findings],
                "agent_impacts": [a.model_dump(mode="json") for a in impact.agent_impacts],
                "blast_radii": [b.model_dump(mode="json") for b in impact.blast_radii],
                "summary": diff_report.summary_counts,
            }
        )

    async def tool_history(request: Request) -> JSONResponse:
        tool = request.path_params["tool"]
        envs = store.list_interactions(tool_name=tool, limit=5000)
        runs = store.list_replay_runs(limit=500)
        return JSONResponse(build_tool_history(tool, envs, runs).model_dump(mode="json"))

    return Starlette(
        routes=[
            Route("/", dashboard, methods=["GET"]),
            Route("/favicon.ico", favicon, methods=["GET"]),
            Route("/assets/{filename}", serve_asset, methods=["GET"]),
            Route("/api/v1/health", health, methods=["GET"]),
            Route(
                "/api/v1/projects/{slug}/interactions",
                ingest_interactions,
                methods=["POST"],
            ),
            Route(
                "/api/v1/projects/{slug}/baselines",
                baselines_endpoint,
                methods=["GET", "POST"],
            ),
            Route(
                "/api/v1/projects/{slug}/baselines/{tag}",
                get_baseline,
                methods=["GET"],
            ),
            Route("/api/v1/projects/{slug}/runs", runs_endpoint, methods=["GET", "POST"]),
            Route(
                "/api/v1/projects/{slug}/agents/{agent_key}/contracts",
                upsert_contract,
                methods=["PUT"],
            ),
            Route(
                "/api/v1/projects/{slug}/runs/{run_id}/report",
                get_run_report,
                methods=["GET"],
            ),
            Route(
                "/api/v1/projects/{slug}/tools/{tool}/history",
                tool_history,
                methods=["GET"],
            ),
        ]
    )


def serve_api_server(
    config: RegripConfig,
    store: SQLiteStore,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> None:
    """Start local REST API and Web Dashboard server."""
    app = create_api_app(config, store)
    uvicorn.run(app, host=host, port=port, log_level="warning")
