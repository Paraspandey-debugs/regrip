"""Discovery and hot-path delegation for the Rust `regrip-proxy` binary (SRS §6.1)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse


def _binary_name() -> str:
    return "regrip-proxy.exe" if os.name == "nt" else "regrip-proxy"


def find_rust_proxy_binary(package_root: Path | None = None) -> Path | None:
    """Locate bundled, PATH-installed, or repo-built `regrip-proxy` executable."""
    exe = _binary_name()
    pkg = package_root or Path(__file__).resolve().parents[1]
    bundled = pkg / "bin" / exe
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return bundled

    on_path = shutil.which("regrip-proxy")
    if on_path:
        return Path(on_path)

    repo_root = pkg.parents[1]
    for profile in ("release", "debug"):
        candidate = repo_root / "target" / profile / exe
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _listen_host_port(listen_url: str) -> str:
    if "://" not in listen_url:
        return listen_url
    parsed = urlparse(listen_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9100
    return f"{host}:{port}"


def build_rust_proxy_cmd(
    binary: Path,
    *,
    server_name: str,
    agent_id: str,
    session_id: str | None = None,
    blob_dir: str | None = None,
    config_path: Path | None = None,
    listen: str | None = None,
    upstream: str | None = None,
    stdio_command: list[str] | None = None,
) -> list[str]:
    """Construct CLI arguments for invoking `regrip-proxy` in stdio or HTTP mode."""
    cmd = [str(binary), "--server", server_name, "--agent", agent_id]
    if session_id:
        cmd.extend(["--session", session_id])
    if blob_dir:
        cmd.extend(["--blob-dir", blob_dir])
    if config_path:
        cmd.extend(["--config", str(config_path)])
    if listen and upstream:
        cmd.extend(["--listen", _listen_host_port(listen), "--upstream", upstream])
    elif stdio_command:
        cmd.append("--")
        cmd.extend(stdio_command)
    return cmd


def run_rust_proxy(cmd: list[str]) -> int:
    """Execute `regrip-proxy` inheriting stdio streams and return its exit code."""
    proc = subprocess.run(cmd, check=False)
    return int(proc.returncode)
