"""Protocol-agnostic driver interface for capability discovery and invocation."""

from __future__ import annotations

from typing import Protocol

from regrip.types.baseline import CapabilityManifest
from regrip.types.envelope import InteractionEnvelope, ResponsePayload


class ProtocolDriver(Protocol):
    """Interface implemented by protocol-specific drivers (MCP, HTTP, A2A)."""

    driver_name: str

    async def discover_capabilities(self, target_uri: str) -> CapabilityManifest:
        """Connect to target and extract normalized tool capabilities."""
        ...

    async def invoke(
        self,
        envelope: InteractionEnvelope,
        target_uri: str,
    ) -> ResponsePayload:
        """Execute a single interaction against target and return normalized response."""
        ...
