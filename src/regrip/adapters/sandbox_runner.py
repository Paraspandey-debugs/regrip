"""Hybrid sandbox runner and filesystem write audit (FR-SAF-002, FR-SAF-003)."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from regrip.types.errors import SafetyViolationError


def detect_container_runtime() -> str | None:
    """Return available, responsive container runtime ('docker' or 'podman') or None."""
    for candidate in ("docker", "podman"):
        if not shutil.which(candidate):
            continue
        try:
            res = subprocess.run(
                [candidate, "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
            if res.returncode == 0:
                return candidate
        except OSError, subprocess.SubprocessError:
            continue
    return None


def resolve_sandbox_mode(requested: str) -> tuple[str, str | None]:
    """Resolve requested sandbox mode ('auto'|'docker'|'subprocess') to (mode, runtime)."""
    normalized = requested.lower()
    runtime = detect_container_runtime()
    if normalized == "docker":
        if runtime is None:
            raise SafetyViolationError(
                "Docker/Podman sandbox requested but no container runtime is available",
                context={"sandbox": requested},
            )
        return "docker", runtime
    if normalized == "subprocess":
        return "subprocess", None
    if normalized == "auto":
        if runtime is not None:
            return "docker", runtime
        return "subprocess", None
    raise SafetyViolationError(
        "Unsupported sandbox mode (expected auto, docker, or subprocess)",
        context={"sandbox": requested},
    )


def build_ephemeral_container_cmd(
    runtime: str,
    image: str,
    inner_cmd: list[str],
    *,
    network: str = "none",
) -> list[str]:
    """Construct an isolated ephemeral container invocation command."""
    return [
        runtime,
        "run",
        "--rm",
        "-i",
        f"--network={network}",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid",
        image,
        *inner_cmd,
    ]


def snapshot_directory(root: Path) -> dict[str, str]:
    """Return relative-path -> sha256 digest map for all files in root."""
    if not root.exists():
        return {}
    snap: dict[str, str] = {}
    for item in sorted(root.rglob("*")):
        if item.is_file():
            rel = str(item.relative_to(root))
            snap[rel] = hashlib.sha256(item.read_bytes()).hexdigest()
    return snap


def diff_directory_snapshots(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Return sorted list of files created, modified, or deleted during execution."""
    changed: list[str] = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) != after.get(path):
            changed.append(path)
    return changed
