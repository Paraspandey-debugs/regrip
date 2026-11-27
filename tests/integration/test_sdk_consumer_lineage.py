"""Exercise the runnable SDK example against the real bundled MCP servers and impact CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from regrip.adapters.config_store import load_config
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.types.impact import AgentImpactStatus, ContractDiscoverySource, ImpactReport


@pytest.fixture(scope="module")
def example_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    example = Path(__file__).resolve().parents[2] / "examples/sdk_consumer_lineage.py"
    working_dir = tmp_path_factory.mktemp("sdk-lineage") / "workspace with spaces"
    working_dir.mkdir()
    result = subprocess.run(
        [sys.executable, str(example)],
        cwd=working_dir,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (workspace,) = (working_dir / ".regrip/sdk-consumer-lineage").glob("run-*")
    return workspace, result.stdout


def test_example_persists_sdk_lineage(example_run: tuple[Path, str]) -> None:
    workspace, output = example_run
    assert "explicit_sdk: get_balance" in output
    assert "dynamic_access: search_products" in output
    config = load_config(workspace / "regrip.yaml")
    store = SQLiteStore(config.storage, config.capture)
    assert config.consumers == []  # The verdict must come from SDK contracts, not YAML.
    contracts = {contract.tool_name: contract for contract in store.list_consumer_contracts()}
    assert contracts["get_balance"].discovery_source == ContractDiscoverySource.EXPLICIT_SDK
    dynamic = contracts["search_products"]
    assert dynamic.discovery_source == ContractDiscoverySource.DYNAMIC_ACCESS
    assert "$.results[*].description" in dynamic.consumed_jsonpaths
    assert len(store.list_interactions(server_name="commerce-mcp")) == 6
    baseline = json.loads((workspace / "baseline.json").read_text(encoding="utf-8"))
    assert "$.request_id" in baseline["noise_masks"]


def test_example_confirms_dynamic_field_regression(example_run: tuple[Path, str]) -> None:
    workspace, output = example_run
    assert "[CONFIRMED_REGRESSION] support-agent -> search_products" in output
    assert "[CONFIRMED_REGRESSION] support-agent -> get_balance" in output
    config_path = workspace / "regrip.yaml"
    config = load_config(config_path)
    store = SQLiteStore(config.storage, config.capture)
    (run,) = store.list_replay_runs()
    assert run.summary.total == 2
    assert run.summary.failed == 2
    assert run.summary.errored == 0
    assert run.summary.skipped_unsafe == 0
    impact_args = [
        "impact",
        "--config",
        str(config_path),
        "--baseline",
        str(workspace / "baseline.json"),
        "--run-id",
        str(run.run_id),
        "--format",
        "json",
    ]
    report = subprocess.run(
        [sys.executable, "-c", "from regrip.cli.app import main; main()", *impact_args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    impacts = ImpactReport.model_validate_json(report.stdout).agent_impacts
    (product_impact,) = [entry for entry in impacts if entry.tool_name == "search_products"]
    assert product_impact.impact_status == AgentImpactStatus.CONFIRMED_REGRESSION
    assert "$.results[*].description" in product_impact.matched_jsonpaths
