"""Feed synthetic agent JSON-RPC traffic into `regrip record --stdio` for local demos."""

from __future__ import annotations

import json
import sys


def main() -> None:
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    call_id = 3
    for _ in range(3):
        frames.append(
            {
                "jsonrpc": "2.0",
                "id": call_id,
                "method": "tools/call",
                "params": {"name": "get_balance", "arguments": {"id": "acct-1"}},
            }
        )
        call_id += 1
        frames.append(
            {
                "jsonrpc": "2.0",
                "id": call_id,
                "method": "tools/call",
                "params": {"name": "search_products", "arguments": {"query": "laptop"}},
            }
        )
        call_id += 1

    for frame in frames:
        sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
