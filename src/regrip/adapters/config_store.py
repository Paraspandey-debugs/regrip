"""Configuration loading and workspace initialization adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from regrip.types.config import RegripConfig
from regrip.types.errors import ConfigError


def load_config(config_path: Path | None = None) -> RegripConfig:
    """Load regrip.yaml from path or return defaults if no path exists."""
    target = config_path or Path("regrip.yaml")
    if not target.exists():
        if config_path is not None:
            raise ConfigError("Configuration file not found", context={"path": str(target)})
        return RegripConfig()

    try:
        raw: Any = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"Invalid YAML in configuration: {exc}", context={"path": str(target)}
        ) from exc

    if raw is None:
        return RegripConfig()
    if not isinstance(raw, dict):
        raise ConfigError("regrip.yaml root must be a mapping", context={"path": str(target)})

    try:
        return RegripConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(
            f"Invalid configuration schema: {exc}", context={"path": str(target)}
        ) from exc


def write_default_config(target_dir: Path, *, project_name: str, force: bool = False) -> Path:
    """Create regrip.yaml and .regrip/ workspace directories."""
    config_file = target_dir / "regrip.yaml"
    if config_file.exists() and not force:
        raise ConfigError(
            "regrip.yaml already exists (use --force to overwrite)",
            context={"path": str(config_file)},
        )

    regrip_dir = target_dir / ".regrip"
    (regrip_dir / "blobs").mkdir(parents=True, exist_ok=True)
    (regrip_dir / "baselines").mkdir(parents=True, exist_ok=True)
    (regrip_dir / "reports").mkdir(parents=True, exist_ok=True)

    config = RegripConfig(project=project_name)
    serialized = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    config_file.write_text(serialized, encoding="utf-8")
    return config_file
