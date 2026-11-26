"""Build and bundle the Rust `regrip-proxy` binary into `src/regrip/bin/` for wheel packaging."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


def bundle_binary(*, skip_build: bool = False, profile: str = "release") -> Path:
    """Compile `regrip-proxy` (unless skipped) and stage it into `src/regrip/bin/`."""
    repo_root = Path(__file__).resolve().parents[1]
    exe = "regrip-proxy.exe" if os.name == "nt" else "regrip-proxy"
    if not skip_build:
        cmd = ["cargo", "build", "-p", "regrip-proxy"]
        if profile == "release":
            cmd.append("--release")
        subprocess.run(cmd, cwd=repo_root, check=True)

    src_bin = repo_root / "target" / profile / exe
    if not src_bin.is_file():
        raise FileNotFoundError(f"Compiled binary not found at {src_bin}")

    dest_dir = repo_root / "src" / "regrip" / "bin"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_bin = dest_dir / exe
    shutil.copy2(src_bin, dest_bin)
    dest_bin.chmod(dest_bin.stat().st_mode | 0o755)
    return dest_bin


def main() -> None:
    parser = argparse.ArgumentParser(description="Bundle regrip-proxy binary into Python package.")
    parser.add_argument("--skip-build", action="store_true", help="Copy existing target binary.")
    parser.add_argument("--profile", choices=["release", "debug"], default="release")
    args = parser.parse_args()
    dest = bundle_binary(skip_build=args.skip_build, profile=args.profile)
    print(f"Bundled regrip-proxy at {dest}")


if __name__ == "__main__":
    main()
