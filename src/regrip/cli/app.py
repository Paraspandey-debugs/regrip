"""Root Typer CLI entrypoint for Regrip (parsing and output formatting only)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from regrip.adapters.config_store import load_config, write_default_config
from regrip.adapters.mcp_stdio_proxy import MCPStdioProxy
from regrip.adapters.replay_runner import ReplayRunner
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.cli.baseline_cmd import baseline_app
from regrip.cli.formatters import format_replay_report
from regrip.types.baseline import BehavioralBaseline
from regrip.types.errors import RegripError

app = typer.Typer(
    name="regrip",
    help="Behavioral regression testing and compatibility engine for MCP servers.",
    no_args_is_help=True,
)
app.add_typer(baseline_app, name="baseline")


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
    stdio: Annotated[bool, typer.Option("--stdio", help="Wrap a stdio MCP server command.")] = True,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to regrip.yaml.")
    ] = None,
) -> None:
    """Wrap an MCP server command via stdio proxy and record InteractionEnvelopes."""
    if not stdio or not ctx.args:
        typer.echo("Error: Provide a stdio server command after `--`.", err=True)
        raise typer.Exit(code=3)
    try:
        cfg = load_config(config_path)
        store = SQLiteStore(cfg.storage, cfg.capture)
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
        run = asyncio.run(runner.run(loaded_baseline, target, allow_mutations=allow_mutations))
        typer.echo(format_replay_report(run))
        if run.summary.failed > 0 or run.summary.errored > 0:
            raise typer.Exit(code=1)
    except RegripError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc


def main() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":
    main()
