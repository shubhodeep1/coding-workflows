#!/usr/bin/env python3
"""Mock MCP server that reproduces the handshake-failure path.

Reads one line from stdin (the client's ``initialize`` request) and exits
without responding. This mimics the observed Context7 MCP failure mode
("MCP client for context7 failed to start: ... connection closed:
initialize response") that triggers Codex to emit a malformed tool entry.

Used by tests/test_mcp_handshake_probe.py to confirm that the probe
detects the failure and returns a non-zero exit code instead of letting
the broken server slip into Codex's tool list.
"""

from __future__ import annotations

import sys


def main() -> int:
	# Drain at most one line of input so the client's write() does not block,
	# then close stdout to simulate a process that crashes during handshake.
	try:
		sys.stdin.readline()
	except Exception:
		pass
	try:
		sys.stdout.close()
	except Exception:
		pass
	return 0


if __name__ == "__main__":
	sys.exit(main())
