"""Phase 8 gRPC / Connect-JSON ProtocolDriver adapter (SRS Section 2.3 & 6.2)."""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

import httpx

from regrip.types.baseline import CapabilityManifest, ToolDescriptor
from regrip.types.envelope import InteractionEnvelope, ResponsePayload, ResponseStatus
from regrip.types.errors import ProtocolError


def _strip_connect_scheme(target_uri: str) -> str:
    lowered = target_uri.lower()
    if lowered.startswith("grpc+"):
        return target_uri[len("grpc+") :]
    return target_uri


def _is_connect_http(target_uri: str) -> bool:
    return target_uri.lower().startswith(("grpc+http://", "grpc+https://"))


def _grpc_addr_and_plaintext(target_uri: str) -> tuple[str, bool]:
    lowered = target_uri.lower()
    if lowered.startswith("grpcs://"):
        return target_uri[len("grpcs://") :].rstrip("/"), False
    if lowered.startswith("grpc://"):
        return target_uri[len("grpc://") :].rstrip("/"), True
    return target_uri, True


class GRPCDriver:
    """ProtocolDriver supporting Connect/gRPC-JSON over HTTP and grpcurl reflection."""

    driver_name: str = "grpc"

    def __init__(
        self,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._transport = transport

    async def _run_grpcurl(self, args: list[str]) -> str:
        grpcurl = shutil.which("grpcurl")
        if not grpcurl:
            raise ProtocolError(
                "gRPC reflection/invocation for grpc:// targets requires `grpcurl` in PATH",
                context={"args": " ".join(args)},
            )
        proc = await asyncio.create_subprocess_exec(
            grpcurl,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        if proc.returncode != 0:
            raise ProtocolError(
                f"grpcurl failed: {stderr.decode('utf-8', errors='replace').strip()}"
            )
        return stdout.decode("utf-8", errors="replace")

    async def discover_capabilities(self, target_uri: str) -> CapabilityManifest:
        if _is_connect_http(target_uri):
            base = _strip_connect_scheme(target_uri).rstrip("/")
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout, transport=self._transport
                ) as client:
                    resp = await client.get(f"{base}/grpc.reflection.v1.ServerReflection/list")
                    resp.raise_for_status()
                    data: Any = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise ProtocolError(
                    f"Failed Connect/gRPC capability discovery: {exc}", context={"url": base}
                ) from exc
            methods = data.get("methods", []) if isinstance(data, dict) else []
            tools = [
                ToolDescriptor(name=str(m), input_schema={"type": "object", "properties": {}})
                for m in methods
            ]
            return CapabilityManifest(server_name=base, spec_version="grpc", tools=tools)

        addr, plaintext = _grpc_addr_and_plaintext(target_uri)
        flags = ["-plaintext", addr, "list"] if plaintext else [addr, "list"]
        raw = await self._run_grpcurl(flags)
        grpc_tools: list[ToolDescriptor] = []
        for svc in [line.strip() for line in raw.splitlines() if line.strip()]:
            if svc.startswith("grpc.reflection."):
                continue
            list_flags = ["-plaintext", addr, "list", svc] if plaintext else [addr, "list", svc]
            methods_raw = await self._run_grpcurl(list_flags)
            for method_name in [m.strip() for m in methods_raw.splitlines() if m.strip()]:
                grpc_tools.append(
                    ToolDescriptor(
                        name=method_name, input_schema={"type": "object", "properties": {}}
                    )
                )
        return CapabilityManifest(server_name=addr, spec_version="grpc", tools=grpc_tools)

    async def invoke(self, envelope: InteractionEnvelope, target_uri: str) -> ResponsePayload:
        method_path = envelope.operation.tool_name.lstrip("/")
        if _is_connect_http(target_uri):
            base = _strip_connect_scheme(target_uri).rstrip("/")
            url = f"{base}/{method_path}"
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout, transport=self._transport
                ) as client:
                    resp = await client.post(
                        url,
                        json=envelope.request.arguments,
                        headers={"Content-Type": "application/json"},
                    )
            except httpx.HTTPError as exc:
                raise ProtocolError(
                    f"Connect/gRPC invocation failed: {exc}", context={"url": url}
                ) from exc
            parsed: Any = None
            if resp.content:
                try:
                    parsed = resp.json()
                except ValueError:
                    parsed = None
            if resp.status_code >= 400:
                msg = (
                    str(parsed.get("message"))
                    if isinstance(parsed, dict) and "message" in parsed
                    else resp.text[:300]
                )
                return ResponsePayload(
                    status=ResponseStatus.TOOL_ERROR,
                    is_error=True,
                    error_code=parsed.get("code") if isinstance(parsed, dict) else resp.status_code,
                    error_message=msg,
                    structured_content=parsed if isinstance(parsed, (dict, list)) else None,
                )
            return ResponsePayload(
                status=ResponseStatus.OK,
                is_error=False,
                structured_content=parsed if isinstance(parsed, (dict, list)) else None,
            )

        addr, plaintext = _grpc_addr_and_plaintext(target_uri)
        payload = json.dumps(envelope.request.arguments)
        flags = ["-d", payload]
        if plaintext:
            flags.append("-plaintext")
        flags.extend([addr, method_path])
        out = await self._run_grpcurl(flags)
        parsed_out: Any = json.loads(out) if out.strip() else {}
        return ResponsePayload(
            status=ResponseStatus.OK,
            is_error=False,
            structured_content=parsed_out if isinstance(parsed_out, (dict, list)) else None,
        )
