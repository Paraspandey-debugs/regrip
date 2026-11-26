# Regrip Architecture

Regrip enforces a strict separation between pure protocol-agnostic domain logic, external I/O adapters, and CLI presentation:

- `src/regrip/types/` — Pydantic v2 schemas (`InteractionEnvelope`, `BehavioralBaseline`, `DiffReport`, `ImpactReport`, `RegripConfig`) and domain exceptions (`RegripError` hierarchy).
- `src/regrip/core/` — Pure, deterministic algorithms with zero network/disk I/O and zero MCP wire coupling:
  - `payload_normalizer.py`: JSON-in-TextContent extraction, JSONPath type inference, input shape clustering.
  - `redactor.py`: Two-stage pre-persistence PII/secret redactor.
  - `baseline_builder.py`: Representative case selection and auto-noise calibration.
  - `safety_policy.py`: Production target guardrails and zero-trust 3-tier `SafetyTier` policy.
  - `detectors/`: 9-category behavioral diff engine (`schema`, `structural`, `value`, `ordering_default`, `semantic`, `diff_engine`).
  - `consumer_impact.py`: 3-source field lineage and per-agent impact classification.
  - `history_analyzer.py` & `report_exporters.py`: Timeline aggregation and JSON/Markdown/JUnit formatting.
- `src/regrip/adapters/` — Protocol drivers (`MCPStdioDriver`, `MCPHttpDriver`, `HTTPDriver`, `A2ADriver`, `GraphQLDriver`, `GRPCDriver`), capture proxies (`MCPStdioProxy`, `MCPHttpProxy`), `SQLiteStore` (WAL + `.jsonl.zst`), `RemoteStore` (PostgreSQL + S3), `RegripRecorder` SDK, `SandboxRunner`, `EgressMockProxy`, `OTel` span exporter, `AdvisoryExplainer`, and `api_server`.
- `src/regrip/cli/` — Typer CLI argument parsing and formatting only.
- `crates/regrip-proxy/` — Rust (`tokio`/`hyper`/`hyper-rustls`) stdio and Streamable HTTP/HTTPS capture proxy with `zeroize`-backed HMAC/regex pre-persistence redaction, `regrip.yaml` rule loading, and `zstd` blob writing.
