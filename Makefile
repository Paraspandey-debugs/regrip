.PHONY: install dev test lint fmt typecheck demo demo-serve \
        rust-build rust-test rust-lint clean help

# ── Python ────────────────────────────────────────────────────────────────────

install:  ## Sync runtime dependencies
	uv sync

dev:  ## Sync all dependencies including dev extras
	uv sync --dev

test:  ## Run the full test suite
	uv run pytest -v --tb=short

test-unit:  ## Run unit tests only (fast, no I/O)
	uv run pytest tests/unit -x --tb=short

lint:  ## Check code style with ruff
	uv run ruff check .

fmt:  ## Format code with ruff
	uv run ruff format .

fmt-check:  ## Verify formatting without modifying files
	uv run ruff format --check .

typecheck:  ## Static type-check with mypy
	uv run mypy src

check: lint fmt-check typecheck test  ## Run all quality checks (CI-equivalent)

# ── Demo ──────────────────────────────────────────────────────────────────────

demo:  ## Run the v1→v2 regression demo (no browser, prints CI gate report)
	REGRIP_NO_SERVE=1 bash examples/run_demo.sh

demo-serve:  ## Run the demo and open the local dashboard at http://localhost:8787
	bash examples/run_demo.sh

# ── Rust ──────────────────────────────────────────────────────────────────────

rust-build:  ## Build the regrip-proxy Rust binary
	cargo build -p regrip-proxy

rust-build-release:  ## Build release-optimised regrip-proxy binary
	cargo build -p regrip-proxy --release

rust-test:  ## Run Rust unit and integration tests
	cargo test --workspace

rust-lint:  ## Lint and format-check Rust code
	cargo clippy --all-targets -- -D warnings
	cargo fmt --check

# ── Housekeeping ──────────────────────────────────────────────────────────────

clean:  ## Remove build artefacts and local regrip workspaces
	rm -rf .regrip/ .regrip-demo/ dist/ build/ *.egg-info/ target/debug/regrip-proxy

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
