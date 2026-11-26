"""Portable .regrip-baseline.tar.zst export and import (FR-BAS-004)."""

from __future__ import annotations

import io
import json
import tarfile
from compression import zstd
from pathlib import Path

from regrip.types.baseline import BehavioralBaseline
from regrip.types.errors import BaselineError


def export_baseline_bundle(baseline: BehavioralBaseline, output_path: Path) -> Path:
    """Export an immutable BehavioralBaseline into a .regrip-baseline.tar.zst bundle."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_bytes = baseline.model_dump_json(indent=2).encode("utf-8")
    manifest_bytes = json.dumps(
        {
            "bundle_version": "1.0",
            "server_name": baseline.server_name,
            "tag": baseline.tag,
            "content_hash": baseline.content_hash,
            "case_count": len(baseline.cases),
        },
        indent=2,
    ).encode("utf-8")

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tf:
        for name, data in (("manifest.json", manifest_bytes), ("baseline.json", baseline_bytes)):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

    compressed = zstd.compress(tar_buf.getvalue())
    output_path.write_bytes(compressed)
    return output_path


def import_baseline_bundle(bundle_path: Path) -> BehavioralBaseline:
    """Load and validate a BehavioralBaseline from a .regrip-baseline.tar.zst bundle."""
    if not bundle_path.exists():
        raise BaselineError("Baseline bundle not found", context={"path": str(bundle_path)})
    try:
        raw_tar = zstd.decompress(bundle_path.read_bytes())
        with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as tf:
            member = tf.extractfile("baseline.json")
            if member is None:
                raise BaselineError(
                    "Invalid baseline bundle: missing baseline.json",
                    context={"path": str(bundle_path)},
                )
            baseline = BehavioralBaseline.model_validate_json(member.read().decode("utf-8"))
            manifest_member = tf.extractfile("manifest.json")
            if manifest_member is not None:
                manifest = json.loads(manifest_member.read().decode("utf-8"))
                expected_hash = manifest.get("content_hash")
                if expected_hash and expected_hash != baseline.content_hash:
                    raise BaselineError(
                        "Baseline bundle content_hash mismatch",
                        context={"path": str(bundle_path)},
                    )
            return baseline
    except BaselineError:
        raise
    except Exception as exc:
        raise BaselineError(
            f"Failed to import baseline bundle: {exc}", context={"path": str(bundle_path)}
        ) from exc
