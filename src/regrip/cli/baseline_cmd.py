"""Typer subcommands for `regrip baseline` (argument parsing and formatting only)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from regrip.adapters.baseline_bundle import export_baseline_bundle, import_baseline_bundle
from regrip.adapters.config_store import load_config
from regrip.adapters.replay_runner import select_driver
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.cli.formatters import format_baseline_summary
from regrip.core.baseline_builder import build_baseline
from regrip.types.baseline import BehavioralBaseline, ToolDescriptor
from regrip.types.errors import RegripError

baseline_app = typer.Typer(help="Build and inspect behavioral baselines.")


@baseline_app.command("build")
def build_cmd(
    server: Annotated[str, typer.Option("--server", help="Target MCP server name.")],
    tag: Annotated[str, typer.Option("--tag", help="Immutable baseline version tag.")],
    git_sha: Annotated[
        str | None, typer.Option("--git-sha", help="Optional source git SHA.")
    ] = None,
    max_cases: Annotated[
        int, typer.Option("--max-cases", help="Max representative cases per tool.")
    ] = 50,
    auto_noise_calibrate: Annotated[
        bool,
        typer.Option(
            "--auto-noise-calibrate/--no-auto-noise-calibrate",
            help="Automatically mask volatile UUIDs and timestamps.",
        ),
    ] = True,
    target: Annotated[
        str | None,
        typer.Option("--target", help="Optional server command/URL to snapshot tool schemas."),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", help="Optional JSON export path.")
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to regrip.yaml.")
    ] = None,
) -> None:
    """Cluster recorded interactions into a versioned BehavioralBaseline."""
    try:
        cfg = load_config(config_path)
        store = SQLiteStore(cfg.storage, cfg.capture)
        interactions = store.list_interactions(server_name=server, limit=5000)
        tool_schemas: dict[str, ToolDescriptor] | None = None
        if target:
            manifest = asyncio.run(select_driver(target).discover_capabilities(target))
            tool_schemas = {t.name: t for t in manifest.tools}
        baseline = build_baseline(
            interactions,
            server_name=server,
            tag=tag,
            git_sha=git_sha,
            max_cases_per_tool=max_cases,
            auto_noise_calibrate=auto_noise_calibrate,
            tool_schemas=tool_schemas,
        )
        store.save_baseline(baseline)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(baseline.model_dump_json(indent=2), encoding="utf-8")
        typer.echo(format_baseline_summary(baseline))
    except RegripError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc


@baseline_app.command("inspect")
def inspect_cmd(
    server: Annotated[
        str | None, typer.Option("--server", help="Server name in local store.")
    ] = None,
    tag: Annotated[str | None, typer.Option("--tag", help="Baseline tag in local store.")] = None,
    file: Annotated[
        Path | None, typer.Option("--file", help="Path to exported baseline JSON.")
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to regrip.yaml.")
    ] = None,
) -> None:
    """Inspect inferred schemas, noise masks, and representative cases."""
    try:
        if file is not None:
            baseline = BehavioralBaseline.model_validate_json(file.read_text(encoding="utf-8"))
        elif server and tag:
            cfg = load_config(config_path)
            store = SQLiteStore(cfg.storage, cfg.capture)
            baseline = store.get_baseline_by_tag(server, tag)
        else:
            typer.echo("Error: Provide either --file or both --server and --tag.", err=True)
            raise typer.Exit(code=3)
        typer.echo(format_baseline_summary(baseline))
    except RegripError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc


@baseline_app.command("export")
def export_cmd(
    server: Annotated[str, typer.Option("--server", help="Server name in local store.")],
    tag: Annotated[str, typer.Option("--tag", help="Baseline tag to export.")],
    output: Annotated[
        Path, typer.Option("--output", help="Destination .regrip-baseline.tar.zst path.")
    ],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to regrip.yaml.")
    ] = None,
) -> None:
    """Export a baseline as a portable .regrip-baseline.tar.zst bundle."""
    try:
        cfg = load_config(config_path)
        store = SQLiteStore(cfg.storage, cfg.capture)
        baseline = store.get_baseline_by_tag(server, tag)
        dest = export_baseline_bundle(baseline, output)
        typer.echo(f"Exported baseline bundle to {dest}")
    except RegripError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc


@baseline_app.command("import")
def import_cmd(
    bundle: Annotated[Path, typer.Argument(help="Path to .regrip-baseline.tar.zst bundle.")],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to regrip.yaml.")
    ] = None,
) -> None:
    """Import a .regrip-baseline.tar.zst bundle into the local SQLite store."""
    try:
        cfg = load_config(config_path)
        store = SQLiteStore(cfg.storage, cfg.capture)
        baseline = import_baseline_bundle(bundle)
        store.save_baseline(baseline)
        typer.echo(format_baseline_summary(baseline))
    except RegripError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
