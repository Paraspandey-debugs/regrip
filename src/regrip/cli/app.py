"""Root Typer CLI entrypoint for Regrip (parsing and output formatting only)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from regrip.adapters.config_store import load_config, write_default_config
from regrip.adapters.mcp_http_proxy import serve_http_proxy
from regrip.adapters.mcp_stdio_proxy import MCPStdioProxy
from regrip.adapters.proxy_binary import (
    build_rust_proxy_cmd,
    find_rust_proxy_binary,
    run_rust_proxy,
)
from regrip.adapters.replay_runner import ReplayRunner
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.cli.baseline_cmd import baseline_app
from regrip.cli.ci_cmd import ci_cmd, export_cmd, history_cmd, impact_cmd, serve_cmd
from regrip.cli.formatters import format_diff_report, format_replay_report
from regrip.core.detectors import diff_baseline_against_run, diff_baselines
from regrip.types.baseline import BehavioralBaseline
from regrip.types.diff import FindingSeverity
from regrip.types.errors import RegripError

app = typer.Typer(
    name="regrip",
    help="Behavioral regression testing and compatibility engine for MCP servers.",
    no_args_is_help=True,
)
app.add_typer(baseline_app, name="baseline")
app.command("impact")(impact_cmd)
app.command("ci")(ci_cmd)
app.command("export")(export_cmd)
app.command("history")(history_cmd)
app.command("serve")(serve_cmd)


def _load_baseline_arg(
    baseline_ref: str,
    server: str | None,
    store: SQLiteStore,
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


@app.command("init")
def init_cmd(
    project: Annotated[str, typer.Option("--project", help="Project slug.")] = "default-project",
    directory: Annotated[Path | None, typer.Option("--dir", help="Target directory.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing regrip.yaml.")] = False,
) -> None:
    """Initialize regrip.yaml and .regrip/ workspace."""
    try:
        cfg_file = write_default_config(directory or Path("."), project_name=project, force=force)
        typer.echo(f"Initialized Regrip workspace at {cfg_file}")
    except RegripError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc


@app.command(
    "record",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def record_cmd(
    ctx: typer.Context,
    server: Annotated[str, typer.Option("--server", help="Logical MCP server name.")],
    agent: Annotated[str, typer.Option("--agent", help="Calling agent ID.")] = "default-agent",
    session: Annotated[str | None, typer.Option("--session", help="Optional session ID.")] = None,
    stdio: Annotated[
        bool, typer.Option("--stdio", help="Wrap a stdio MCP server command.")
    ] = False,
    listen: Annotated[
        str | None,
        typer.Option("--listen", help="HTTP proxy listen URL (e.g. http://127.0.0.1:9100)."),
    ] = None,
    upstream: Annotated[
        str | None, typer.Option("--upstream", help="Upstream Streamable HTTP MCP URL.")
    ] = None,
    engine: Annotated[
        str, typer.Option("--engine", help="Proxy engine: auto, rust, or python.")
    ] = "auto",
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to regrip.yaml.")
    ] = None,
) -> None:
    """Wrap an MCP server via stdio or Streamable HTTP proxy and record interactions."""
    try:
        cfg = load_config(config_path)
        store = SQLiteStore(cfg.storage, cfg.capture)
        is_http = bool(listen and upstream)
        if not is_http and not stdio and not ctx.args:
            typer.echo(
                "Error: Provide `--listen` + `--upstream`, or `--stdio -- <command>`.",
                err=True,
            )
            raise typer.Exit(code=3)

        norm_engine = engine.strip().lower()
        if norm_engine not in {"auto", "rust", "python"}:
            typer.echo(f"Error: Unsupported proxy engine '{engine}'.", err=True)
            raise typer.Exit(code=3)

        rust_bin = None if norm_engine == "python" else find_rust_proxy_binary()
        if norm_engine == "rust" and rust_bin is None:
            typer.echo("Error: Rust `regrip-proxy` binary not found.", err=True)
            raise typer.Exit(code=3)
        if rust_bin is not None:
            rust_cmd = build_rust_proxy_cmd(
                rust_bin,
                server_name=server,
                agent_id=agent,
                session_id=session,
                blob_dir=cfg.storage.blob_dir,
                config_path=config_path,
                listen=listen,
                upstream=upstream,
                stdio_command=None if is_http else ctx.args,
            )
            raise typer.Exit(code=run_rust_proxy(rust_cmd))

        if listen and upstream:
            serve_http_proxy(
                listen,
                upstream,
                store=store,
                server_name=server,
                default_agent=agent,
                default_session=session,
            )
            return
        proxy = MCPStdioProxy(
            ctx.args,
            store=store,
            server_name=server,
            agent_id=agent,
            session_id=session,
        )
        rc = asyncio.run(proxy.run())
        raise typer.Exit(code=rc)
    except RegripError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc


@app.command("replay")
def replay_cmd(
    baseline: Annotated[str, typer.Option("--baseline", help="Baseline JSON path or tag.")],
    target: Annotated[str, typer.Option("--target", help="Target stdio command or HTTP URL.")],
    server: Annotated[
        str | None, typer.Option("--server", help="Server name for tag lookup.")
    ] = None,
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
    """Safely replay a behavioral baseline against a candidate MCP server."""
    try:
        cfg = load_config(config_path)
        store = SQLiteStore(cfg.storage, cfg.capture)
        loaded_baseline = _load_baseline_arg(baseline, server, store)
        runner = ReplayRunner(cfg, store=store)
        run = asyncio.run(
            runner.run(loaded_baseline, target, allow_mutations=allow_mutations, sandbox=sandbox)
        )
        typer.echo(format_replay_report(run))
        if run.summary.failed > 0 or run.summary.errored > 0:
            raise typer.Exit(code=1)
    except RegripError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc


@app.command("diff")
def diff_cmd(
    baseline: Annotated[str, typer.Option("--baseline", help="Base baseline JSON path or tag.")],
    candidate: Annotated[
        str | None, typer.Option("--candidate", help="Candidate baseline JSON path or tag.")
    ] = None,
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="ReplayRun UUID to compare against baseline.")
    ] = None,
    server: Annotated[
        str | None, typer.Option("--server", help="Server name for tag lookup.")
    ] = None,
    output_format: Annotated[
        str, typer.Option("--format", help="Output format: text or json.")
    ] = "text",
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to regrip.yaml.")
    ] = None,
) -> None:
    """Compare a baseline against a candidate baseline or replay run."""
    try:
        cfg = load_config(config_path)
        store = SQLiteStore(cfg.storage, cfg.capture)
        base = _load_baseline_arg(baseline, server, store)
        if run_id is not None:
            run = store.get_replay_run(UUID(run_id))
            report = diff_baseline_against_run(base, run, cfg.comparison)
        elif candidate is not None:
            cand_base = _load_baseline_arg(candidate, server, store)
            report = diff_baselines(base, cand_base, cfg.comparison)
        else:
            typer.echo("Error: Provide either --candidate or --run-id.", err=True)
            raise typer.Exit(code=3)

        if output_format == "json":
            typer.echo(report.model_dump_json(indent=2))
        else:
            typer.echo(format_diff_report(report))

        if report.verdict == FindingSeverity.FAIL:
            raise typer.Exit(code=1)
        if report.verdict == FindingSeverity.WARN:
            raise typer.Exit(code=2)
    except RegripError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc


def main() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":
    main()
