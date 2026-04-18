#!/usr/bin/env python3
"""Graceful shutdown probe with bounded readiness polling and bounded log tails."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Probe graceful shutdown semantics")
	parser.add_argument("--pid", type=int, required=True)
	parser.add_argument("--url", required=True)
	parser.add_argument("--host-header", default="app.local.test")
	parser.add_argument("--timeout-seconds", type=int, default=30)
	parser.add_argument("--poll-seconds", type=float, default=1.0)
	parser.add_argument("--log-tail-lines", type=int, default=40)
	return parser


def http_ok(url: str, host_header: str) -> bool:
	req = urllib.request.Request(url, headers={"Host": host_header})
	try:
		with urllib.request.urlopen(req, timeout=3) as resp:
			return 200 <= int(resp.status) < 400
	except (urllib.error.HTTPError, urllib.error.URLError):
		return False


def send_sigterm_to_container(pid: int) -> tuple[bool, str]:
	proc = subprocess.run(
		[
			"docker",
			"compose",
			"-f",
			"validation/docker-compose.test.yml",
			"exec",
			"-T",
			"app",
			"/bin/sh",
			"-c",
			f"kill -TERM {pid}",
		],
		text=True,
		capture_output=True,
		check=False,
	)
	if proc.returncode != 0:
		message = (proc.stderr or proc.stdout or "").strip()
		return False, message
	return True, ""


def bounded_compose_logs_tail(lines: int) -> str:
	proc = subprocess.run(
		["docker", "compose", "-f", "validation/docker-compose.test.yml", "logs", "--no-color", "app"],
		text=True,
		capture_output=True,
		check=False,
	)
	if proc.returncode != 0:
		return ""
	log_lines = (proc.stdout or "").splitlines()
	if lines <= 0:
		return ""
	return "\n".join(log_lines[-lines:])


def main() -> int:
	args = build_parser().parse_args()

	if not http_ok(args.url, args.host_header):
		print("# graceful_shutdown precondition failed: service not healthy before SIGTERM", file=sys.stderr)
		return 1

	ok, message = send_sigterm_to_container(args.pid)
	if not ok:
		print(
			f"# graceful_shutdown failed to send SIGTERM to container pid={args.pid} error={message}",
			file=sys.stderr,
		)
		return 1

	deadline = time.monotonic() + max(1, args.timeout_seconds)
	while time.monotonic() < deadline:
		if not http_ok(args.url, args.host_header):
			print("# graceful_shutdown ready endpoint transitioned to unavailable after SIGTERM")
			return 0
		time.sleep(max(0.1, args.poll_seconds))

	tail = bounded_compose_logs_tail(args.log_tail_lines)
	payload = {
		"reason": "timeout_waiting_for_shutdown",
		"url": args.url,
		"timeout_seconds": args.timeout_seconds,
		"log_tail": tail,
	}
	print(f"# graceful_shutdown timeout {json.dumps(payload, sort_keys=True)}", file=sys.stderr)
	return 1


if __name__ == "__main__":
	raise SystemExit(main())
