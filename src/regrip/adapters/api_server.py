"""REST API server and local Web Dashboard for `regrip serve` (SRS §4.3, FR-OBS-002)."""

from __future__ import annotations

from collections import Counter
from typing import Any
from uuid import UUID

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from regrip import __version__
from regrip.adapters.dashboard_html import DASHBOARD_HTML
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.core.baseline_builder import build_baseline
from regrip.core.consumer_impact import (
    compute_consumer_impact,
    infer_session_correlation_contracts,
    merge_consumer_contracts,
)
from regrip.core.detectors import diff_baseline_against_run
from regrip.core.history_analyzer import build_tool_history
from regrip.types.config import RegripConfig
from regrip.types.envelope import InteractionEnvelope
from regrip.types.errors import RegripError


def _find_baseline_for_run(store: SQLiteStore, baseline_id: UUID, tag: str) -> Any:
    for b in store.list_baselines(limit=500):
        if b.baseline_id == baseline_id or b.tag == tag:
            return b
    return None


def create_api_app(config: RegripConfig, store: SQLiteStore) -> Starlette:
    """Create Starlette REST API + Web Dashboard ASGI application."""

    async def dashboard(_request: Request) -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML)

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

    async def list_runs(_request: Request) -> JSONResponse:
        runs = [r.model_dump(mode="json") for r in store.list_replay_runs()]
        return JSONResponse(runs)

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
            config.consumers, [], infer_session_correlation_contracts(envs)
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
            Route("/api/v1/projects/{slug}/runs", list_runs, methods=["GET"]),
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
