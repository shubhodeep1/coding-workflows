#!/usr/bin/env python3
"""Mock MCP server that hangs forever after receiving ``initialize``.

Used by tests/test_mcp_handshake_probe.py to exercise the probe's timeout
path. Reads one line from stdin, then blocks on stdin without ever
responding, so the probe must give up via its --timeout flag.
"""

from __future__ import annotations

import sys


def main() -> int:
	sys.stdin.readline()
	# Block forever — the probe will SIGTERM us when its deadline expires.
	while True:
		try:
			data = sys.stdin.read()
		except Exception:
			break
		if not data:
			# stdin closed; loop on an empty read would spin, so exit.
			break
	return 0


if __name__ == "__main__":
	sys.exit(main())
