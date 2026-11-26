"""Lightweight local HTTP egress mock proxy for sandbox replay audit (FR-SAF-002..003)."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from types import TracebackType

_MUTATING_METHODS = {"POST", "PUT", "DELETE", "PATCH", "CONNECT"}


@dataclass(frozen=True)
class EgressAttempt:
    """Single outbound HTTP attempt observed from a sandboxed child process."""

    method: str
    target: str
    blocked: bool


class EgressMockProxy:
    """Local HTTP proxy blocking mutating outbound requests and mocking safe GETs."""

    def __init__(self, *, block_mutations: bool = True) -> None:
        self._block_mutations = block_mutations
        self._server: asyncio.Server | None = None
        self._port: int = 0
        self._attempts: list[EgressAttempt] = []

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def env_vars(self) -> dict[str, str]:
        """Return proxy environment variables to inject into sandboxed subprocesses."""
        url = self.proxy_url
        return {
            "HTTP_PROXY": url,
            "HTTPS_PROXY": url,
            "http_proxy": url,
            "https_proxy": url,
        }

    def drain_blocked(self) -> list[EgressAttempt]:
        """Return and clear blocked egress attempts recorded since last drain."""
        blocked = [a for a in self._attempts if a.blocked]
        self._attempts.clear()
        return blocked

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            parts = line.decode("utf-8", errors="replace").strip().split(" ")
            method = parts[0].upper() if parts and parts[0] else "GET"
            target = parts[1] if len(parts) > 1 else "/"
            # Drain remaining request headers before replying
            while True:
                hdr = await asyncio.wait_for(reader.readline(), timeout=1.0)
                if not hdr or hdr in (b"\r\n", b"\n"):
                    break

            blocked = self._block_mutations and method in _MUTATING_METHODS
            self._attempts.append(EgressAttempt(method=method, target=target, blocked=blocked))
            if blocked:
                body = b'{"error":"Blocked by Regrip sandbox egress audit"}'
                resp = (
                    b"HTTP/1.1 403 Forbidden\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                    + body
                )
            else:
                body = b'{"mocked":true}'
                resp = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                    + body
                )
            writer.write(resp)
            await writer.drain()
        except (TimeoutError, OSError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def __aenter__(self) -> EgressMockProxy:
        self._server = await asyncio.start_server(self._handle_client, "127.0.0.1", 0)
        sock = self._server.sockets[0]
        self._port = int(sock.getsockname()[1])
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
