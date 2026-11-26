"""Phase 8 GraphQL ProtocolDriver adapter (SRS Section 2.3 & 6.2)."""

from __future__ import annotations

from typing import Any

import httpx

from regrip.core.payload_normalizer import canonical_hash
from regrip.types.baseline import CapabilityManifest, ToolDescriptor
from regrip.types.envelope import InteractionEnvelope, ResponsePayload, ResponseStatus
from regrip.types.errors import ProtocolError

_INTROSPECTION_QUERY = """
query RegripIntrospect {
  __schema {
    queryType {
      fields {
        name
        description
        args { name type { kind name ofType { kind name } } }
      }
    }
    mutationType {
      fields {
        name
        description
        args { name type { kind name ofType { kind name } } }
      }
    }
  }
}
""".strip()


def _strip_scheme(target_uri: str) -> str:
    if target_uri.lower().startswith("graphql+"):
        return target_uri[len("graphql+") :]
    return target_uri


def _gql_type_to_json_type(type_ref: dict[str, Any]) -> tuple[str, bool]:
    kind = type_ref.get("kind")
    required = kind == "NON_NULL"
    inner = (
        type_ref.get("ofType")
        if required and isinstance(type_ref.get("ofType"), dict)
        else type_ref
    )
    name = str((inner or {}).get("name") or "String")
    mapping = {"Int": "integer", "Float": "number", "Boolean": "boolean", "ID": "string"}
    return mapping.get(name, "string"), required


def _field_input_schema(args: list[Any]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for arg in args:
        if not isinstance(arg, dict) or not isinstance(arg.get("name"), str):
            continue
        raw_type = arg.get("type")
        t_ref = raw_type if isinstance(raw_type, dict) else {}
        json_type, is_req = _gql_type_to_json_type(t_ref)
        props[arg["name"]] = {"type": json_type}
        if is_req:
            required.append(arg["name"])
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = sorted(required)
    return schema


class GraphQLDriver:
    """ProtocolDriver implementation for GraphQL HTTP endpoints."""

    driver_name: str = "graphql"

    def __init__(
        self,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._transport = transport
        self._mutations: set[str] = set()

    async def discover_capabilities(self, target_uri: str) -> CapabilityManifest:
        url = _strip_scheme(target_uri)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(url, json={"query": _INTROSPECTION_QUERY})
                resp.raise_for_status()
                body: Any = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProtocolError(
                f"GraphQL introspection failed: {exc}", context={"url": url}
            ) from exc

        schema_data = (body.get("data") or {}).get("__schema") if isinstance(body, dict) else None
        if not isinstance(schema_data, dict):
            raise ProtocolError("Invalid GraphQL introspection response", context={"url": url})

        tools: list[ToolDescriptor] = []
        for root_key, is_mut in (("queryType", False), ("mutationType", True)):
            root = schema_data.get(root_key)
            if not isinstance(root, dict):
                continue
            for field in root.get("fields", []):
                if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                    continue
                fname = field["name"]
                if is_mut:
                    self._mutations.add(fname)
                in_schema = _field_input_schema(field.get("args") or [])
                tools.append(
                    ToolDescriptor(
                        name=fname,
                        description=field.get("description"),
                        input_schema=in_schema,
                        input_schema_hash=canonical_hash(in_schema),
                    )
                )
        return CapabilityManifest(server_name=url, spec_version="graphql", tools=tools)

    async def invoke(self, envelope: InteractionEnvelope, target_uri: str) -> ResponsePayload:
        url = _strip_scheme(target_uri)
        args = dict(envelope.request.arguments)
        raw_query = args.pop("query", None)
        if isinstance(raw_query, str):
            payload: dict[str, Any] = {"query": raw_query, "variables": args}
        else:
            op_kind = (
                "mutation"
                if envelope.operation.tool_name in self._mutations
                or envelope.operation.method.lower() == "mutation"
                else "query"
            )
            arg_str = ", ".join(f"{k}: ${k}" for k in sorted(args))
            call_expr = (
                f"{envelope.operation.tool_name}({arg_str})"
                if arg_str
                else envelope.operation.tool_name
            )
            payload = {"query": f"{op_kind} {{ {call_expr} }}", "variables": args}

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                body: Any = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProtocolError(f"GraphQL invocation failed: {exc}", context={"url": url}) from exc

        if not isinstance(body, dict):
            raise ProtocolError("GraphQL response must be a JSON object", context={"url": url})

        errors = body.get("errors")
        data = body.get("data")
        if isinstance(errors, list) and errors:
            msg = "; ".join(
                str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors
            )
            return ResponsePayload(
                status=ResponseStatus.TOOL_ERROR,
                is_error=True,
                error_message=msg,
                structured_content=data if isinstance(data, (dict, list)) else None,
            )
        return ResponsePayload(
            status=ResponseStatus.OK,
            is_error=False,
            structured_content=data if isinstance(data, (dict, list)) else None,
        )
