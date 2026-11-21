#!/usr/bin/env python3
"""Deterministic HTTP smoke helper with explicit Host header support for Flask."""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Run HTTP smoke check")
	parser.add_argument("--url", required=True)
	parser.add_argument("--host-header", required=True)
	parser.add_argument("--timeout-seconds", type=int, default=30)
	return parser


def main() -> int:
	args = build_parser().parse_args()
	deadline = time.monotonic() + max(1, args.timeout_seconds)
	attempt = 0
	last_error = ""

	while time.monotonic() < deadline:
		attempt += 1
		req = urllib.request.Request(
			args.url,
			headers={"Host": args.host_header, "Accept": "application/json, text/plain, */*"},
		)
		try:
			with urllib.request.urlopen(req, timeout=3) as resp:
				status = int(resp.status)
				if 200 <= status < 400:
					print(f"# http_smoke success attempt={attempt} status={status}")
					return 0
				last_error = f"unexpected_status={status}"
		except urllib.error.HTTPError as exc:
			last_error = f"http_error={exc.code}"
		except urllib.error.URLError as exc:
			last_error = f"url_error={exc.reason}"
		except Exception as exc:  # pragma: no cover
			last_error = f"unexpected_error={type(exc).__name__}:{exc}"
		time.sleep(1)

	print(
		f"# http_smoke failure url={args.url} host_header={args.host_header} attempts={attempt} error={last_error}",
		file=sys.stderr,
	)
	return 1


if __name__ == "__main__":
	raise SystemExit(main())
