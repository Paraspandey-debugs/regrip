"""CI gate and Consumer Impact CLI commands (`regrip impact`, `regrip ci`)."""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from regrip.adapters.api_server import serve_api_server
from regrip.adapters.config_store import load_config
from regrip.adapters.replay_runner import ReplayRunner
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.core.consumer_impact import (
    compute_consumer_impact,
    infer_session_correlation_contracts,
    merge_consumer_contracts,
)
from regrip.core.detectors import diff_baseline_against_run, diff_baselines
from regrip.core.history_analyzer import build_tool_history
from regrip.core.report_exporters import (
    render_json_report,
    render_junit_xml,
    render_markdown_pr_comment,
)
from regrip.types.baseline import BehavioralBaseline
from regrip.types.diff import FindingSeverity
from regrip.types.errors import RegripError
from regrip.types.impact import ImpactReport


def _load_baseline_file_or_tag(
    baseline_ref: str, server: str | None, store: SQLiteStore
) -> BehavioralBaseline:
    path = Path(baseline_ref)
    if path.exists():
        return BehavioralBaseline.model_validate_json(path.read_text(encoding="utf-8"))
    if server:
        return store.get_baseline_by_tag(server, baseline_ref)
    raise RegripError(
        "Baseline file not found and --server not provided for tag lookup",
        context={"baseline": baseline_ref},
    )


def _format_impact_text(report: ImpactReport) -> str:
    lines = [
        f"Consumer Impact Report (baseline={report.baseline_tag}, target={report.target_label})"
    ]
    for entry in report.agent_impacts:
        paths = f" paths={','.join(entry.matched_jsonpaths)}" if entry.matched_jsonpaths else ""
        lines.append(
            f"  [{entry.impact_status.value}] {entry.agent_id} -> {entry.tool_name}{paths}"
        )
    for br in report.blast_radii:
        lines.append(
            f"  BlastRadius {br.tool_name}: {br.affected_agents}/{br.total_agents} "
            f"(score={br.risk_score})"
        )
    return "\n".join(lines)


def impact_cmd(
    baseline: Annotated[str, typer.Option("--baseline", help="Base baseline JSON path or tag.")],
    candidate: Annotated[
        str | None, typer.Option("--candidate", help="Candidate baseline JSON path or tag.")
    ] = None,
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="ReplayRun UUID to analyze.")
    ] = None,
    server: Annotated[
        str | None, typer.Option("--server", help="Server name for tag lookup.")
    ] = None,
    output_format: Annotated[str, typer.Option("--format", help="text or json.")] = "text",
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to regrip.yaml.")
    ] = None,
) -> None:
    """Compute per-agent Consumer Impact Report from a diff."""
    try:
        cfg = load_config(config_path)
        store = SQLiteStore(cfg.storage, cfg.capture)
        base = _load_baseline_file_or_tag(baseline, server, store)
        if run_id is not None:
            run = store.get_replay_run(UUID(run_id))
            diff_report = diff_baseline_against_run(base, run, cfg.comparison)
        elif candidate is not None:
            cand_base = _load_baseline_file_or_tag(candidate, server, store)
            diff_report = diff_baselines(base, cand_base, cfg.comparison)
        else:
            typer.echo("Error: Provide either --candidate or --run-id.", err=True)
            raise typer.Exit(code=3)

        interactions = store.list_interactions(server_name=base.server_name, limit=5000)
        session_contracts = infer_session_correlation_contracts(interactions)
        contracts = merge_consumer_contracts(cfg.consumers, [], session_contracts)
        volumes = dict(Counter(e.operation.tool_name for e in interactions))
        impact = compute_consumer_impact(diff_report, contracts, call_volumes=volumes)

        if output_format == "json":
            typer.echo(impact.model_dump_json(indent=2))
        else:
            typer.echo(_format_impact_text(impact))
    except RegripError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc


def ci_cmd(
    baseline: Annotated[str, typer.Option("--baseline", help="Baseline JSON path or tag.")],
    target: Annotated[str, typer.Option("--target", help="Candidate stdio cmd or HTTP URL.")],
    server: Annotated[
        str | None, typer.Option("--server", help="Server name for tag lookup.")
    ] = None,
    formats: Annotated[
        str, typer.Option("--format", help="Comma-separated: markdown,json,junit.")
    ] = "markdown,json",
    out_dir: Annotated[
        Path, typer.Option("--out-dir", help="Directory for generated CI report artifacts.")
    ] = Path(".regrip/reports"),
    strict: Annotated[
        bool, typer.Option("--strict", help="Exit with code 2 on WARN findings.")
    ] = False,
    allow_mutations: Annotated[
        bool, typer.Option("--allow-mutations", help="Allow WRITE_BLOCKED tools.")
    ] = False,
    sandbox: Annotated[
        str | None, typer.Option("--sandbox", help="Sandbox mode: auto, docker, subprocess.")
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to regrip.yaml.")
    ] = None,
) -> None:
    """Run full replay + diff + consumer impact + policy gate for CI/CD (FR-CICD-001)."""
    try:
        cfg = load_config(config_path)
        store = SQLiteStore(cfg.storage, cfg.capture)
        base = _load_baseline_file_or_tag(baseline, server, store)
        runner = ReplayRunner(cfg, store=store)
        run = asyncio.run(
            runner.run(base, target, allow_mutations=allow_mutations, sandbox=sandbox)
        )
        diff_report = diff_baseline_against_run(base, run, cfg.comparison)

        interactions = store.list_interactions(server_name=base.server_name, limit=5000)
        session_contracts = infer_session_correlation_contracts(interactions)
        contracts = merge_consumer_contracts(cfg.consumers, [], session_contracts)
        volumes = dict(Counter(e.operation.tool_name for e in interactions))
        impact = compute_consumer_impact(diff_report, contracts, call_volumes=volumes)

        out_dir.mkdir(parents=True, exist_ok=True)
        selected = {f.strip().lower() for f in formats.split(",") if f.strip()}
        if "json" in selected:
            (out_dir / "regrip-report.json").write_text(
                render_json_report(base, run, diff_report, impact), encoding="utf-8"
            )
        if "markdown" in selected:
            md = render_markdown_pr_comment(base, run, diff_report, impact)
            (out_dir / "regrip-report.md").write_text(md, encoding="utf-8")
            typer.echo(md)
        if "junit" in selected:
            (out_dir / "regrip-junit.xml").write_text(
                render_junit_xml(diff_report), encoding="utf-8"
            )

        if diff_report.verdict == FindingSeverity.FAIL or run.summary.errored > 0:
            raise typer.Exit(code=1)
        if diff_report.verdict == FindingSeverity.WARN and strict:
            raise typer.Exit(code=2)
    except RegripError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc


def history_cmd(
    tool: Annotated[str, typer.Option("--tool", help="MCP tool name to inspect.")],
    server: Annotated[
        str | None, typer.Option("--server", help="Optional server name filter.")
    ] = None,
    output_format: Annotated[str, typer.Option("--format", help="text or json.")] = "text",
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to regrip.yaml.")
    ] = None,
) -> None:
    """Query historical behavioral shifts, error rates, and consumers per tool (FR-OBS-001)."""
    try:
        cfg = load_config(config_path)
        store = SQLiteStore(cfg.storage, cfg.capture)
        interactions = store.list_interactions(server_name=server, tool_name=tool, limit=5000)
        runs = store.list_replay_runs(limit=500)
        report = build_tool_history(tool, interactions, runs)
        if output_format == "json":
            typer.echo(report.model_dump_json(indent=2))
            return
        typer.echo(
            f"History for {report.tool_name}: calls={report.total_recorded_calls} "
            f"error_rate={report.error_rate * 100:.1f}% "
            f"agents={','.join(report.dependent_agents) or 'none'}"
        )
        for ev in report.timeline:
            typer.echo(
                f"  - {ev.started_at} baseline={ev.baseline_tag} sha={ev.target_git_sha or '-'} "
                f"failed={ev.failed_cases}/{ev.total_cases}"
            )
    except RegripError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc


def serve_cmd(
    host: Annotated[str, typer.Option("--host", help="Bind host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Bind port.")] = 8787,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to regrip.yaml.")
    ] = None,
) -> None:
    """Start local REST API + Web Dashboard (default :8787)."""
    try:
        cfg = load_config(config_path)
        store = SQLiteStore(cfg.storage, cfg.capture)
        typer.echo(f"Serving Regrip Dashboard at http://{host}:{port}")
        serve_api_server(cfg, store, host=host, port=port)
    except RegripError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
