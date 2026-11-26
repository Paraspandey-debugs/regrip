#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO_DIR="${REGRIP_DEMO_DIR:-$ROOT_DIR/.regrip-demo}"
PORT="${REGRIP_PORT:-8787}"

rm -rf "$DEMO_DIR"
mkdir -p "$DEMO_DIR"
cp "$ROOT_DIR/examples/regrip.yaml" "$DEMO_DIR/regrip.yaml"
cd "$DEMO_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
export PYTHONPATH="$ROOT_DIR/src"

echo "1/4 Recording v1 MCP interactions (with PII redaction & auto-noise UUIDs)..."
"$PYTHON_BIN" "$ROOT_DIR/examples/feed_agent_calls.py" \
  | "$PYTHON_BIN" -m regrip.cli.app record \
      --config "$DEMO_DIR/regrip.yaml" \
      --server commerce-mcp \
      --agent support-agent \
      --stdio -- "$PYTHON_BIN" "$ROOT_DIR/examples/mcp_v1.py" >/dev/null

echo "2/4 Building v1.0.0 baseline with schema discovery..."
"$PYTHON_BIN" -m regrip.cli.app baseline build \
  --config "$DEMO_DIR/regrip.yaml" \
  --server commerce-mcp \
  --tag v1.0.0 \
  --target "$PYTHON_BIN $ROOT_DIR/examples/mcp_v1.py"

echo "3/4 Running CI compatibility gate against v2 candidate server..."
set +e
"$PYTHON_BIN" -m regrip.cli.app ci \
  --config "$DEMO_DIR/regrip.yaml" \
  --server commerce-mcp \
  --baseline v1.0.0 \
  --target "$PYTHON_BIN $ROOT_DIR/examples/mcp_v2.py" \
  --format markdown,json,junit \
  --out-dir "$DEMO_DIR/reports"
RC=$?
set -e
echo "CI gate exited with code $RC (expected 1 due to behavioral regressions)."

if [ "${REGRIP_NO_SERVE:-0}" = "1" ]; then
  exit 0
fi

echo "4/4 Launching Regrip Dashboard at http://127.0.0.1:$PORT ..."
exec "$PYTHON_BIN" -m regrip.cli.app serve --config "$DEMO_DIR/regrip.yaml" --port "$PORT"
