"""Tests for FR-CICD-003 baseline resolver (JSON, tar.zst, tag:, HTTP, s3://, oci://)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from regrip.adapters.baseline_bundle import export_baseline_bundle
from regrip.adapters.baseline_resolver import resolve_baseline
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.types import BaselineCase, BaselineError, BehavioralBaseline, StorageConfig


def _sample_baseline(tag: str = "main") -> BehavioralBaseline:
    return BehavioralBaseline(
        server_name="svc",
        tag=tag,
        content_hash="abc123",
        cases=[BaselineCase(tool_name="ping", cluster_key="ping:1")],
    )


def test_resolve_local_json_bundle_and_tag_prefix(tmp_path: Path) -> None:
    b = _sample_baseline("v1.0.0")
    json_file = tmp_path / "b.json"
    json_file.write_text(b.model_dump_json(), encoding="utf-8")
    assert resolve_baseline(str(json_file)).baseline_id == b.baseline_id

    bundle = tmp_path / "b.regrip-baseline.tar.zst"
    export_baseline_bundle(b, bundle)
    assert resolve_baseline(str(bundle)).baseline_id == b.baseline_id

    store = SQLiteStore(
        StorageConfig(dsn=f"sqlite:///{tmp_path / 'regrip.db'}", blob_dir=str(tmp_path / "blobs"))
    )
    store.save_baseline(b)
    assert resolve_baseline("tag:v1.0.0", store=store).baseline_id == b.baseline_id


def test_resolve_http_s3_and_oci_baselines(tmp_path: Path) -> None:
    b = _sample_baseline("prod")
    raw_json = b.model_dump_json()

    resp = MagicMock()
    resp.text = raw_json
    resp.raise_for_status.return_value = None
    with patch("regrip.adapters.baseline_resolver.httpx.get", return_value=resp) as mock_get:
        loaded = resolve_baseline(
            "https://regrip.local/api/v1/projects/p/baselines/prod", api_token="tok"
        )
        assert loaded.baseline_id == b.baseline_id
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"

    with patch("regrip.adapters.baseline_resolver.shutil.which", return_value=None):
        with pytest.raises(BaselineError, match="aws"):
            resolve_baseline("s3://bucket/b.json")
        with pytest.raises(BaselineError, match="oras"):
            resolve_baseline("oci://ghcr.io/org/b:v1")

    def _fake_s3(cmd: list[str], **_kwargs: object) -> MagicMock:
        Path(cmd[-1]).write_text(raw_json, encoding="utf-8")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("regrip.adapters.baseline_resolver.shutil.which", return_value="/usr/bin/aws"),
        patch("regrip.adapters.baseline_resolver.subprocess.run", side_effect=_fake_s3),
    ):
        assert resolve_baseline("s3://bucket/b.json").baseline_id == b.baseline_id

    def _fake_oci(cmd: list[str], **_kwargs: object) -> MagicMock:
        out_dir = Path(cmd[cmd.index("-o") + 1])
        export_baseline_bundle(b, out_dir / "b.regrip-baseline.tar.zst")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("regrip.adapters.baseline_resolver.shutil.which", return_value="/usr/bin/oras"),
        patch("regrip.adapters.baseline_resolver.subprocess.run", side_effect=_fake_oci),
    ):
        assert resolve_baseline("oci://ghcr.io/org/b:v1").baseline_id == b.baseline_id
