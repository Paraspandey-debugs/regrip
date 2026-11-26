"""Domain-specific exception hierarchy for Regrip."""

from __future__ import annotations


class RegripError(Exception):
    """Base exception for all Regrip domain and adapter failures."""

    exit_code: int = 3

    def __init__(self, message: str, *, context: dict[str, str] | None = None) -> None:
        self.context: dict[str, str] = context or {}
        detail = ", ".join(f"{k}={v}" for k, v in self.context.items())
        full_message = f"{message} ({detail})" if detail else message
        super().__init__(full_message)


class ConfigError(RegripError):
    """Raised when regrip.yaml or environment configuration is invalid."""


class StorageError(RegripError):
    """Raised when SQLite or blob storage operations fail."""


class ProtocolError(RegripError):
    """Raised on malformed or failed protocol driver communication."""


class SafetyViolationError(RegripError):
    """Raised when an operation violates replay safety guardrails."""


class BaselineError(RegripError):
    """Raised when baseline building, loading, or clustering fails."""
