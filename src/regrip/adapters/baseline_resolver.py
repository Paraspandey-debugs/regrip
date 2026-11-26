"""Unified baseline resolver for local files, bundles, tags, HTTP, S3, and OCI (FR-CICD-003)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from regrip.adapters.baseline_bundle import import_baseline_bundle
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.types.baseline import BehavioralBaseline
from regrip.types.errors import BaselineError


def _load_local_artifact(path: Path) -> BehavioralBaseline:
    if path.name.endswith(".tar.zst") or path.suffix == ".zst":
        return import_baseline_bundle(path)
    try:
        return BehavioralBaseline.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BaselineError(
            f"Invalid baseline JSON at {path}: {exc}", context={"path": str(path)}
        ) from exc


def _resolve_http_baseline(url: str, api_token: str | None) -> BehavioralBaseline:
    headers: dict[str, str] = {}
    token = api_token or os.environ.get("REGRIP_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.get(url, headers=headers, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise BaselineError(
            f"Failed to fetch remote baseline from {url}: {exc}", context={"url": url}
        ) from exc

    if url.endswith(".tar.zst") or url.endswith(".zst"):
        with tempfile.TemporaryDirectory(prefix="regrip-dl-") as tmp_dir:
            bundle = Path(tmp_dir) / "baseline.tar.zst"
            bundle.write_bytes(resp.content)
            return import_baseline_bundle(bundle)
    try:
        return BehavioralBaseline.model_validate_json(resp.text)
    except Exception as exc:
        raise BaselineError(
            f"Invalid baseline payload from {url}: {exc}", context={"url": url}
        ) from exc


def _resolve_s3_baseline(s3_uri: str) -> BehavioralBaseline:
    aws_bin = shutil.which("aws")
    if not aws_bin:
        raise BaselineError(
            "S3 baseline resolution requires the `aws` CLI in PATH",
            context={"baseline": s3_uri},
        )
    with tempfile.TemporaryDirectory(prefix="regrip-s3-") as tmp_dir:
        suffix = ".tar.zst" if s3_uri.endswith(".zst") else ".json"
        dest = Path(tmp_dir) / f"baseline{suffix}"
        res = subprocess.run(
            [aws_bin, "s3", "cp", s3_uri, str(dest)], capture_output=True, text=True
        )
        if res.returncode != 0:
            raise BaselineError(
                f"Failed to download S3 baseline: {res.stderr.strip() or res.stdout.strip()}",
                context={"baseline": s3_uri},
            )
        return _load_local_artifact(dest)


def _resolve_oci_baseline(oci_uri: str) -> BehavioralBaseline:
    oras_bin = shutil.which("oras")
    if not oras_bin:
        raise BaselineError(
            "OCI baseline resolution requires the `oras` CLI in PATH",
            context={"baseline": oci_uri},
        )
    ref = oci_uri.removeprefix("oci://")
    with tempfile.TemporaryDirectory(prefix="regrip-oci-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        res = subprocess.run([oras_bin, "pull", ref, "-o", tmp_dir], capture_output=True, text=True)
        if res.returncode != 0:
            raise BaselineError(
                f"Failed to pull OCI baseline: {res.stderr.strip() or res.stdout.strip()}",
                context={"baseline": oci_uri},
            )
        for pattern in ("*.tar.zst", "*.json"):
            matches = sorted(tmp_path.rglob(pattern))
            if matches:
                return _load_local_artifact(matches[0])
        raise BaselineError(
            "OCI artifact did not contain a .tar.zst or .json baseline",
            context={"baseline": oci_uri},
        )


def resolve_baseline(
    baseline_ref: str,
    *,
    server: str | None = None,
    store: SQLiteStore | None = None,
    api_token: str | None = None,
) -> BehavioralBaseline:
    """Resolve a BehavioralBaseline from path, .tar.zst, tag, HTTP URL, s3://, or oci://."""
    lowered = baseline_ref.lower()
    if lowered.startswith(("http://", "https://")):
        return _resolve_http_baseline(baseline_ref, api_token)
    if lowered.startswith("s3://"):
        return _resolve_s3_baseline(baseline_ref)
    if lowered.startswith("oci://"):
        return _resolve_oci_baseline(baseline_ref)

    path = Path(baseline_ref)
    if path.exists():
        return _load_local_artifact(path)

    tag = baseline_ref.removeprefix("tag:")
    if store is not None:
        if server:
            return store.get_baseline_by_tag(server, tag)
        matches = [b for b in store.list_baselines(limit=500) if b.tag == tag]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise BaselineError(
                "Multiple baselines match tag; pass --server to disambiguate",
                context={"tag": tag, "servers": ", ".join(m.server_name for m in matches)},
            )
    raise BaselineError(
        "Baseline file not found and tag lookup failed",
        context={"baseline": baseline_ref, "server": server or ""},
    )
