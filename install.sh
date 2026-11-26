#!/usr/bin/env bash
# install.sh — one-liner installer for regrip
# Usage: bash <(curl -fsSL https://raw.githubusercontent.com/Paraspandey-debugs/regrip/main/install.sh)
set -euo pipefail

REGRIP_VERSION="${REGRIP_VERSION:-latest}"

echo ""
echo "  ┌─────────────────────────────────────────┐"
echo "  │  regrip — MCP behavioral regression CI  │"
echo "  └─────────────────────────────────────────┘"
echo ""

_have() { command -v "$1" >/dev/null 2>&1; }

if _have uv; then
  echo "→  Installing via uv tool install …"
  if [ "$REGRIP_VERSION" = "latest" ]; then
    uv tool install regrip
  else
    uv tool install "regrip==${REGRIP_VERSION}"
  fi
  echo ""
  echo "✓  regrip installed.  Run: regrip --help"
  echo ""
  echo "   Try the interactive demo:"
  echo "     git clone https://github.com/Paraspandey-debugs/regrip"
  echo "     cd regrip && bash examples/run_demo.sh"
  exit 0
fi

if _have pip; then
  echo "→  uv not found — falling back to pip …"
  if [ "$REGRIP_VERSION" = "latest" ]; then
    pip install --quiet regrip
  else
    pip install --quiet "regrip==${REGRIP_VERSION}"
  fi
  echo ""
  echo "✓  regrip installed.  Run: regrip --help"
  echo ""
  echo "   Tip: install uv for faster dependency resolution:"
  echo "     curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 0
fi

echo "✗  Neither uv nor pip found."
echo ""
echo "   Install uv first (recommended):"
echo "     curl -LsSf https://astral.sh/uv/install.sh | sh"
echo ""
echo "   Or install pip:"
echo "     python -m ensurepip --upgrade"
exit 1
