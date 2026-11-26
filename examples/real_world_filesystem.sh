#!/usr/bin/env bash
# real_world_filesystem.sh — Compare two real published versions of
# @modelcontextprotocol/server-filesystem (2025.3.28 vs 2025.11.25) with regrip.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="${REGRIP_REAL_DEMO_DIR:-/tmp/regrip-fs-demo}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"
echo '{"hello":"world"}' > "$WORK_DIR/sample.json"
cd "$WORK_DIR"

cat > regrip.yaml << 'EOF'
version: "1.0"
project: filesystem-mcp-audit

storage:
  mode: local
  dsn: "sqlite:///.regrip/regrip.db"
  blob_dir: ".regrip/blobs"

safety:
  trust_mcp_annotations: false
  default_tier: WRITE_BLOCKED
  tool_policies:
    list_directory:
      tier: READ_SAFE
    read_file:
      tier: READ_SAFE

consumers:
  - agent_id: coding-assistant
    tools:
      list_directory:
        consumed_jsonpaths: ["$.content[0].text"]
      read_file:
        consumed_jsonpaths: ["$.content[0].text"]
EOF

cat > feed.jsonl << EOF
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"coding-assistant","version":"1.0.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_directory","arguments":{"path":"$WORK_DIR"}}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"$WORK_DIR/sample.json"}}}
EOF

export PYTHONPATH="$ROOT_DIR/src"

echo "1/3 Recording @modelcontextprotocol/server-filesystem@2025.3.28..."
"$PYTHON_BIN" -m regrip.cli.app record \
  --config regrip.yaml \
  --server fs-mcp \
  --agent coding-assistant \
  --stdio -- npx -y @modelcontextprotocol/server-filesystem@2025.3.28 "$WORK_DIR" < feed.jsonl >/dev/null

echo "2/3 Building baseline for 2025.3.28..."
"$PYTHON_BIN" -m regrip.cli.app baseline build \
  --config regrip.yaml \
  --server fs-mcp \
  --tag 2025.3.28 \
  --target "npx -y @modelcontextprotocol/server-filesystem@2025.3.28 $WORK_DIR"

echo "3/3 Running regrip CI against @modelcontextprotocol/server-filesystem@2025.11.25..."
# Pre-warm npx cache so --offline works cleanly inside the sandboxed replay cwd
npx -y @modelcontextprotocol/server-filesystem@2025.11.25 "$WORK_DIR" < feed.jsonl >/dev/null 2>&1 || true

set +e
"$PYTHON_BIN" -m regrip.cli.app ci \
  --config regrip.yaml \
  --server fs-mcp \
  --baseline 2025.3.28 \
  --target "npx --offline @modelcontextprotocol/server-filesystem@2025.11.25 $WORK_DIR" \
  --sandbox subprocess \
  --format markdown
RC=$?
set -e
echo "CI gate exited with code $RC."
