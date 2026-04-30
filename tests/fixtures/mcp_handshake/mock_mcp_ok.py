#!/usr/bin/env python3
"""Mock MCP server that completes a clean ``initialize`` handshake.

Reads one JSON-RPC request line from stdin and replies with a valid
``initialize`` result echoing the client's request id. Used by
tests/test_mcp_handshake_probe.py as the positive case alongside
``mock_mcp_close_on_init.py``.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
	line = sys.stdin.readline()
	try:
		req = json.loads(line)
	except json.JSONDecodeError:
		return 1
	resp = {
		"jsonrpc": "2.0",
		"id": req.get("id"),
		"result": {
			"protocolVersion": "2024-11-05",
			"capabilities": {},
			"serverInfo": {"name": "mock-mcp-ok", "version": "1.0.0"},
		},
	}
	sys.stdout.write(json.dumps(resp) + "\n")
	sys.stdout.flush()
	# Linger briefly so the probe has time to read the response before EOF.
	# The probe terminates the process when it is done.
	try:
		sys.stdin.read()
	except Exception:
		pass
	return 0


if __name__ == "__main__":
	sys.exit(main())
