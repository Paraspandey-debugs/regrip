"""Unit tests for `diff_baselines` metadata/cluster diffs and `regrip baseline diff` (#8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from regrip.adapters.config_store import write_default_config
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.cli.app import app
from regrip.core.detectors import diff_baselines
from regrip.types.baseline import BaselineCase, BehavioralBaseline, ToolDescriptor
from regrip.types.config import ComparisonConfig, RegripConfig
from regrip.types.diff import DiffReport, FindingSeverity
from regrip.types.envelope import ResponseStatus

runner = CliRunner()


def test_diff_baselines_reports_identical_added_removed_and_mask_diffs() -> None:
    tool_a = ToolDescriptor(
        name="get_balance",
        description="Get balance",
        input_schema={"properties": {"id": {"type": "string"}}, "required": ["id"]},
    )
    tool_b = ToolDescriptor(name="search", description="Search catalog", input_schema={})
    base = BehavioralBaseline(
        server_name="svc",
        tag="v1",
        noise_masks=["$.request_id"],
        tool_schemas={"get_balance": tool_a},
        cases=[
            BaselineCase(
                tool_name="get_balance",
                cluster_key="c1",
                expected_status=ResponseStatus.OK,
                expected_payload={"balance": 100},
            )
        ],
    )
    assert diff_baselines(base, base, ComparisonConfig()).verdict == FindingSeverity.PASS

    cand = BehavioralBaseline(
        server_name="svc",
        tag="v2",
        noise_masks=["$.trace_id"],
        tool_schemas={"get_balance": tool_a, "search": tool_b},
        cases=[
            base.cases[0],
            BaselineCase(
                tool_name="search",
                cluster_key="c2",
                expected_status=ResponseStatus.OK,
                expected_payload={"results": []},
            ),
        ],
    )
    report = diff_baselines(base, cand, ComparisonConfig())
    assert report.verdict == FindingSeverity.WARN
    reasons = " ".join(f.deterministic_reason for f in report.findings)
    assert "Tool 'search' added" in reasons
    assert "Noise mask '$.trace_id' added" in reasons
    assert "Noise mask '$.request_id' removed" in reasons
    assert "New baseline cluster added" in reasons


def test_cli_baseline_diff_files_and_store_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_file = write_default_config(tmp_path, project_name="diff-cli")
    cfg = RegripConfig.model_validate_json(json.dumps({}))
    cfg.storage.dsn = f"sqlite:///{tmp_path / '.regrip/regrip.db'}"
    store = SQLiteStore(cfg.storage, cfg.capture)

    v1 = BehavioralBaseline(
        server_name="svc",
        tag="v1",
        tool_schemas={"ping": ToolDescriptor(name="ping", description="Ping")},
        cases=[
            BaselineCase(
                tool_name="ping",
                cluster_key="c1",
                expected_status=ResponseStatus.OK,
                expected_payload={"ok": True},
            )
        ],
    )
    v2 = BehavioralBaseline(
        server_name="svc",
        tag="v2",
        tool_schemas={},
        cases=[],
    )
    store.save_baseline(v1)
    store.save_baseline(v2)

    p1 = tmp_path / "v1.json"
    p2 = tmp_path / "v2.json"
    p1.write_text(v1.model_dump_json(), encoding="utf-8")
    p2.write_text(v2.model_dump_json(), encoding="utf-8")

    pass_res = runner.invoke(
        app,
        ["baseline", "diff", str(p1), str(p1), "--config", str(cfg_file)],
    )
    assert pass_res.exit_code == 0
    assert "Diff Verdict: PASS" in pass_res.output

    fail_res = runner.invoke(
        app,
        [
            "baseline",
            "diff",
            "v1",
            "v2",
            "--server",
            "svc",
            "--format",
            "json",
            "--config",
            str(cfg_file),
        ],
    )
    assert fail_res.exit_code == 1
    parsed = DiffReport.model_validate_json(fail_res.output)
    assert parsed.verdict == FindingSeverity.FAIL
    assert any(
        "missing from candidate tools/list" in f.deterministic_reason for f in parsed.findings
    )
