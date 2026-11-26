# AGENTS.md

Instructions for AI coding agents working in this repository. Keep changes small, modular, tested, and idiomatic to the surrounding codebase. See `context.md` for the full Regrip SRS.

## Project Overview & Layout
- **Purpose:** Behavioral regression testing and consumer-compatibility platform for MCP servers and AI agent tool integrations.
- **Directory Contract (strictly enforce):**
  - `src/regrip/cli/` — Typer CLI argument parsing and output formatting only; no business logic.
  - `src/regrip/core/` — Pure, protocol-agnostic domain logic (baseline modeling, diff detectors, consumer impact graph, replay policy); no direct I/O, network calls, or MCP transport coupling.
  - `src/regrip/adapters/` — External integrations (`ProtocolDriver` implementations like `MCPDriver`, SQLite/blob/PostgreSQL stores, Docker/Podman sandbox, OTel exporters).
  - `src/regrip/types/` — Shared Pydantic v2 models, schemas (`InteractionEnvelope`), and custom error types.
  - `crates/regrip-proxy/` — Rust (`tokio`/`axum`) capture proxy and pre-persistence PII/secret redactor.
  - `tests/unit/` — Fast, isolated unit tests mirroring `src/` structure.
  - `tests/integration/` — End-to-end and I/O tests (stdio/HTTP proxy, SQLite, sandbox replay).
  - `docs/` — Architecture notes and user guides.

## Build, Lint & Test Commands
Run commands that apply to the files you touched (once project manifests exist):
- **Install Python dependencies:** `uv sync --dev`
- **Run Python unit tests:** `uv run pytest tests/unit -x --tb=short`
- **Run Python full test suite:** `uv run pytest`
- **Run Python linter & formatter:** `uv run ruff check . && uv run ruff format --check .`
- **Type check Python:** `uv run mypy src`
- **Run Rust tests (`regrip-proxy`):** `cargo test --workspace`
- **Run Rust lint & format:** `cargo clippy --all-targets -- -D warnings && cargo fmt --check`

## Anti-Monolith & Modularity Rules
- **File size ceiling:** Keep source files under **300 lines** (excluding tests and generated schemas). If a file grows past ~250 lines, split it by responsibility before adding new features.
- **Function size ceiling:** Keep functions/methods under **50 lines** with a single clear responsibility and max nesting depth of 3.
- **One concern per module:** Never mix CLI parsing, business logic, and network/disk I/O in the same file.
- **No god-files:** Never create catch-all files named `utils.py`, `helpers.py`, `common.rs`, or `misc.*`. Name modules after their domain capability (e.g., `noise_calibrator.py`, `hmac_redactor.rs`).
- **Explicit public surface:** Export only intentional public APIs; keep internal helpers private to their module.

## Code Quality & Regrip Invariants
- **Typed interfaces:** Annotate all Python function signatures, Pydantic models, and Rust public items. Avoid untyped `dict`/`Any` across module boundaries.
- **Fail explicitly:** Raise/return domain-specific errors with actionable context. Never swallow exceptions silently (`except Exception: pass`).
- **Protocol-agnostic core:** Keep `src/regrip/core/` independent of MCP wire details; isolate protocol-specific framing in `ProtocolDriver` adapters.
- **Zero-trust safety & redaction:** Treat MCP `ToolAnnotations` strictly as untrusted hints; enforce pre-persistence PII/secret redaction before disk/DB writes; keep any LLM explainer strictly advisory (never pass/fail).
- **Zero hardcoded secrets or local paths:** Read configuration from `regrip.yaml` or environment variables; provide `.env.example` if env vars are required.
- **Dependencies:** Do not add third-party dependencies when the standard library covers the requirement cleanly.

## Operational Boundaries
- **Always:**
  - Read existing modules in the target directory before creating new files to reuse established patterns.
  - Run the linter, type-checker, and test suite after every logical change and fix all failures before finishing.
  - Add or update unit tests alongside every behavior change or bug fix.
  - Update `README.md` or `docs/` if CLI flags, config keys, or public APIs change.
- **Ask First:**
  - Adding a new runtime dependency.
  - Changing public CLI flags, `regrip.yaml` schemas, or exported library signatures.
  - Restructuring top-level directories or deleting existing modules.
- **Never:**
  - Edit generated files, lockfiles manually, or vendored code.
  - Leave `TODO` stubs, placeholder implementations, or commented-out dead code.
  - Commit secrets, API keys, `.env` files, or binary artifacts.
