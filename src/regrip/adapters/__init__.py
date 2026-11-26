"""External adapters for config, storage, protocol drivers, and replay."""

from regrip.adapters.a2a_driver import A2ADriver
from regrip.adapters.baseline_bundle import export_baseline_bundle, import_baseline_bundle
from regrip.adapters.baseline_resolver import resolve_baseline
from regrip.adapters.config_store import load_config, write_default_config
from regrip.adapters.egress_proxy import EgressAttempt, EgressMockProxy
from regrip.adapters.http_driver import HTTPDriver
from regrip.adapters.mcp_http import MCPHttpDriver
from regrip.adapters.mcp_http_proxy import create_http_proxy_app, serve_http_proxy
from regrip.adapters.mcp_stdio import MCPStdioDriver
from regrip.adapters.mcp_stdio_proxy import MCPStdioProxy
from regrip.adapters.protocol_driver import ProtocolDriver
from regrip.adapters.proxy_binary import (
    build_rust_proxy_cmd,
    find_rust_proxy_binary,
    run_rust_proxy,
)
from regrip.adapters.remote_store import RemoteStore, open_store
from regrip.adapters.replay_runner import ReplayRunner, select_driver
from regrip.adapters.sandbox_runner import (
    build_ephemeral_container_cmd,
    detect_container_runtime,
    resolve_sandbox_mode,
)
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.adapters.store_protocol import StoreBackend

__all__ = [
    "A2ADriver",
    "EgressAttempt",
    "EgressMockProxy",
    "HTTPDriver",
    "MCPHttpDriver",
    "MCPStdioDriver",
    "MCPStdioProxy",
    "ProtocolDriver",
    "RemoteStore",
    "ReplayRunner",
    "SQLiteStore",
    "StoreBackend",
    "build_ephemeral_container_cmd",
    "build_rust_proxy_cmd",
    "create_http_proxy_app",
    "detect_container_runtime",
    "export_baseline_bundle",
    "find_rust_proxy_binary",
    "import_baseline_bundle",
    "load_config",
    "open_store",
    "resolve_baseline",
    "resolve_sandbox_mode",
    "run_rust_proxy",
    "select_driver",
    "serve_http_proxy",
    "write_default_config",
]
