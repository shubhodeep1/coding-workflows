#!/usr/bin/env python3
"""Graceful shutdown probe with bounded process polling and bounded log tails."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Probe graceful shutdown semantics")
	parser.add_argument("--pid", type=int, required=True)
	parser.add_argument("--compose-file", default="validation/docker-compose.test.yml")
	parser.add_argument("--app-service", default="app")
	parser.add_argument("--timeout-seconds", type=int, default=20)
	parser.add_argument("--poll-seconds", type=float, default=1.0)
	parser.add_argument("--log-tail-lines", type=int, default=40)
	return parser


def send_sigterm_to_container(pid: int, compose_file: str, app_service: str) -> tuple[bool, str]:
	proc = subprocess.run(
		[
			"docker",
			"compose",
			"-f",
			compose_file,
			"exec",
			"-T",
			app_service,
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


def process_is_running(pid: int, compose_file: str, app_service: str) -> bool:
	proc = subprocess.run(
		[
			"docker",
			"compose",
			"-f",
			compose_file,
			"exec",
			"-T",
			app_service,
			"/bin/sh",
			"-c",
			f"kill -0 {pid}",
		],
		text=True,
		capture_output=True,
		check=False,
	)
	return proc.returncode == 0


def bounded_compose_logs_tail(lines: int, compose_file: str, app_service: str) -> str:
	if lines <= 0:
		return ""
	proc = subprocess.run(
		["docker", "compose", "-f", compose_file, "logs", "--no-color", "--tail", str(lines), app_service],
		text=True,
		capture_output=True,
		check=False,
	)
	if proc.returncode != 0:
		return ""
	return (proc.stdout or "").strip()


def main() -> int:
	args = build_parser().parse_args()

	ok, message = send_sigterm_to_container(args.pid, args.compose_file, args.app_service)
	if not ok:
		print(
			f"# graceful_shutdown failed to send SIGTERM to container pid={args.pid} error={message}",
			file=sys.stderr,
		)
		return 1

	deadline = time.monotonic() + max(1, args.timeout_seconds)
	while time.monotonic() < deadline:
		if not process_is_running(args.pid, args.compose_file, args.app_service):
			print("# graceful_shutdown process transitioned to not-running after SIGTERM")
			return 0
		time.sleep(max(0.1, args.poll_seconds))

	tail = bounded_compose_logs_tail(args.log_tail_lines, args.compose_file, args.app_service)
	payload = {
		"reason": "timeout_waiting_for_shutdown",
		"pid": args.pid,
		"timeout_seconds": args.timeout_seconds,
		"log_tail": tail,
	}
	print(f"# graceful_shutdown timeout {json.dumps(payload, sort_keys=True)}", file=sys.stderr)
	return 1


if __name__ == "__main__":
	raise SystemExit(main())
