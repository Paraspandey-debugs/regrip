<p align="center">
  <img src="assets/regrip-02-horizontal-lockup-transparent.png" alt="Regrip" width="420" />
</p>

# Regrip

**Know what breaks before your agents do. Test the behavior, not just the schema.**

Regrip is a behavioral regression testing and consumer-compatibility platform for Model Context Protocol (MCP) servers and AI agent tool integrations.

## Capabilities
- **Capture Proxies (`stdio` & `Streamable HTTP`) + Python SDK:** Transparently capture MCP `tools/call` interactions into canonical `InteractionEnvelope` records (`sqlite` WAL + `.jsonl.zst` blobs) with fail-open recording and W3C `traceparent` attribution.
- **Pre-Persistence PII & Secret Redaction:** Two-stage redaction (`drop`, `mask`, `hmac_token` + JWT/Bearer/AWS/email/SSN/card detectors) enforced in both Python and the Rust `regrip-proxy` crate prior to disk/DB writes.
- **Behavioral Baseline & Auto-Noise Calibration:** Clusters inputs by `(tool_name, arg_keys, value_shape)` and automatically masks volatile UUIDs/timestamps, with portable `.regrip-baseline.tar.zst` export/import.
- **9-Category Behavioral Diff Engine:** Deterministic detection of Schema, Output (including Jaro-Winkler key renames), Type, Value/Unit (10x/100x/1000x scaling & PSI), Default, Ordering (Kendall's tau & Top-K), Error, Semantic (AST-safe JSONPath assertions), and Side-Effect regressions.
- **Zero-Trust Safe Replay & Hybrid Sandbox:** Ignores untrusted `ToolAnnotations`, blocks production targets, enforces `READ_SAFE`/`DRY_RUN_ONLY`/`WRITE_BLOCKED` tiers, and audits unexpected filesystem/state mutations.
- **Consumer-Aware Impact & CI Gates:** 3-source field lineage (`regrip.yaml`, SDK `expect_fields`/`track_consumption`, session correlation), per-agent verdicts (`UNAFFECTED`, `POTENTIALLY_BROKEN`, `CONFIRMED_REGRESSION`), JSON/Markdown/JUnit XML exporters, `regrip history`, OTel GenAI spans, and local `regrip serve` dashboard.

## Quickstart

```bash
# 1. Initialize regrip.yaml and .regrip/ workspace
regrip init --project my-agents

# 2. Record MCP stdio or Streamable HTTP interactions
regrip record --server finance-mcp --agent billing-bot --stdio -- python -m finance_mcp
regrip record --server catalog-mcp --listen http://127.0.0.1:9100 --upstream http://127.0.0.1:8000/mcp

# 3. Build an immutable behavioral baseline
regrip baseline build --server finance-mcp --tag v1.0.0 --output .regrip/baselines/v1.json

# 4. Run full CI compatibility gate (replay + diff + consumer impact + reports)
regrip ci --baseline .regrip/baselines/v1.json --target "python -m finance_mcp_v2" --out-dir .regrip/reports

# 5. Inspect tool history or launch the local web dashboard
regrip history --tool get_balance
regrip serve --port 8787
```

## Python SDK

```python
from regrip import RegripRecorder, expect_fields

recorder = RegripRecorder(agent_id="support-agent", store_uri="sqlite:///.regrip/regrip.db")
async with recorder.wrap_mcp_session(mcp_session, server_name="orders-mcp") as session:
    with expect_fields(tool="search_products", jsonpaths=["$.results[*].id"], order_sensitive=True):
        result = await session.call_tool("search_products", {"query": "laptop"})

tracked = recorder.track_consumption(tool="get_user", payload=result.structuredContent)
```

## Development & Verification

```bash
uv sync --dev
uv run ruff check . && uv run ruff format --check .
uv run pytest -v --tb=short
cargo test --workspace
cargo clippy --all-targets -- -D warnings && cargo fmt --check
```
