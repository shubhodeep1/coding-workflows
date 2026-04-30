#!/usr/bin/env python3
"""Pre-flight MCP handshake probe.

Spawns a candidate MCP server, performs a single JSON-RPC ``initialize``
exchange over stdio, and exits 0 only if the server returns a well-formed
``result`` for the matching request id within the timeout.

Why this exists:
    Codex (used by implement.yml / validate.yml / review_autofix.yml) keeps
    each configured ``[mcp_servers.<name>]`` entry in its tool list even when
    the server's MCP ``initialize`` handshake fails. Some OpenRouter back-ends
    (notably Azure) reject the resulting payload with HTTP 400 because the
    failed entry has ``function: undefined``. Other back-ends silently
    accept the same payload, so the failure is intermittent and only surfaces
    when routing happens to land on a strict provider.

    By probing each optional MCP server before writing its block to
    ``~/.codex/config.toml``, we make sure that only servers that pass the
    handshake are advertised to Codex, eliminating the malformed tool entry at
    its source.

Usage:
    python3 scripts/mcp_handshake_probe.py \\
        --name context7 \\
        --timeout 15 \\
        --command npx -- -y @upstash/context7-mcp@2.1.8

Anything after the first ``--`` is treated as the server's argv. The probe
sends a single ``initialize`` request, reads stdout one line at a time, and
returns 0 on the first valid JSON-RPC response that matches the request id
and contains a ``result`` field. It returns non-zero (with a diagnostic on
stderr) on any of:

    * spawn failure (executable missing, permission denied, ...)
    * timeout waiting for a response
    * server exits before responding
    * response is not valid JSON
    * response is missing ``result`` or carries an ``error``
    * response id does not match the request id
    * response exceeds the buffered-bytes cap before a newline arrives
"""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import subprocess
import sys
import time
from typing import List, Optional


PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "coding-workflows-mcp-probe", "version": "1.0.0"}

# Cap the size of a single response we will buffer waiting for a newline.
# A well-behaved MCP `initialize` response is well under a kilobyte; anything
# larger than this without a newline indicates a misbehaving or malicious
# server, and unbounded buffering would let it grow process memory until the
# probe timeout fires. 1 MiB leaves comfortable headroom for legitimate
# servers while bounding worst-case allocation. Overridable via env var so
# tests can exercise the cap deterministically without flooding megabytes.
def _resolve_max_response_bytes() -> int:
	raw = os.environ.get("MCP_PROBE_MAX_RESPONSE_BYTES")
	if not raw:
		return 1024 * 1024
	try:
		value = int(raw)
	except ValueError:
		return 1024 * 1024
	# Reject non-positive values; a zero/negative cap would always trip.
	return value if value > 0 else 1024 * 1024


_MAX_RESPONSE_BYTES = _resolve_max_response_bytes()


class _ResponseTooLargeError(Exception):
	"""Raised by ``_read_line_with_timeout`` when the server has written more
	than ``_MAX_RESPONSE_BYTES`` bytes without emitting a newline."""


def _build_initialize_request(request_id: int) -> bytes:
	payload = {
		"jsonrpc": "2.0",
		"id": request_id,
		"method": "initialize",
		"params": {
			"protocolVersion": PROTOCOL_VERSION,
			"capabilities": {},
			"clientInfo": CLIENT_INFO,
		},
	}
	# MCP framing on stdio is line-delimited JSON.
	return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def _read_line_with_timeout(stream, deadline: float) -> Optional[bytes]:
	"""Read a single newline-terminated line from *stream* before *deadline*.

	Returns the raw bytes (without the trailing newline) on success, or
	None if EOF or timeout is hit first.
	"""
	buffer = bytearray()
	fd = stream.fileno()
	while True:
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			return None
		ready, _, _ = select.select([fd], [], [], remaining)
		if not ready:
			return None
		chunk = os.read(fd, 4096)
		if not chunk:
			# EOF — the server closed its stdout (often the symptom we are
			# trying to detect: handshake-time crash). Drop any partial
			# buffer: MCP framing requires newline-terminated JSON, so a
			# fragment without a newline is not a valid response.
			return None
		# Bound buffer growth so a misbehaving server cannot exhaust process
		# memory by streaming bytes without ever sending a newline. Check
		# *before* extending so the buffer never exceeds the documented cap
		# even by a single read chunk.
		if len(buffer) + len(chunk) > _MAX_RESPONSE_BYTES:
			raise _ResponseTooLargeError(len(buffer) + len(chunk))
		buffer.extend(chunk)
		newline = buffer.find(b"\n")
		if newline != -1:
			return bytes(buffer[:newline])


def _terminate(proc: subprocess.Popen) -> None:
	if proc.poll() is not None:
		return
	try:
		proc.terminate()
		try:
			proc.wait(timeout=2)
			return
		except subprocess.TimeoutExpired:
			pass
		proc.kill()
		try:
			proc.wait(timeout=2)
		except subprocess.TimeoutExpired:
			pass
	except (ProcessLookupError, OSError):
		pass


def probe(name: str, command: str, args: List[str], timeout: float) -> int:
	"""Run a single ``initialize`` exchange against *command* + *args*.

	Returns 0 on success and a non-zero code (1..7) on failure. The exit code
	maps to the failure reason so callers can distinguish them in logs:
	1 spawn failed (file not found / not executable),
	2 timeout waiting for the initialize response,
	3 server closed stdout before sending a complete response,
	4 response is not valid JSON,
	5 response is JSON-RPC but is missing ``result`` or carries ``error``,
	6 response id does not match the request id,
	7 response exceeded the buffered-bytes cap before a newline arrived.
	"""
	request_id = 1
	request = _build_initialize_request(request_id)
	# Preserve the parent's signal handlers; force unbuffered stdio so the
	# server's first response is visible without waiting for an LF flush.
	env = os.environ.copy()
	env.setdefault("PYTHONUNBUFFERED", "1")
	try:
		# Inherit stderr (don't capture). If we used `stderr=subprocess.PIPE`
		# without an active drainer, a chatty server or wrapper (notably
		# `npx`, which prints download progress + deprecation warnings)
		# could fill the ~64 KiB pipe buffer and block its own write before
		# our blocking stdout read returns — manifesting as a false probe
		# timeout. Inheriting the parent's stderr routes the child's
		# diagnostic output through CI logs naturally and trades a tail
		# snapshot in the EOF branch for deadlock-immunity.
		proc = subprocess.Popen(
			[command, *args],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=None,
			env=env,
			start_new_session=True,
		)
	except (FileNotFoundError, PermissionError, OSError) as exc:
		print(
			f"[mcp-probe:{name}] spawn failed: {exc.__class__.__name__}: {exc}",
			file=sys.stderr,
		)
		return 1

	deadline = time.monotonic() + timeout
	try:
		try:
			proc.stdin.write(request)
			proc.stdin.flush()
		except (BrokenPipeError, OSError) as exc:
			print(
				f"[mcp-probe:{name}] failed to send initialize: "
				f"{exc.__class__.__name__}: {exc}",
				file=sys.stderr,
			)
			return 3

		try:
			line = _read_line_with_timeout(proc.stdout, deadline)
		except _ResponseTooLargeError as exc:
			print(
				f"[mcp-probe:{name}] response exceeded {_MAX_RESPONSE_BYTES} "
				f"bytes without a newline (got {exc.args[0]} bytes); "
				"giving up to bound memory growth",
				file=sys.stderr,
			)
			return 7
		if line is None:
			# Distinguish timeout vs early EOF for clearer diagnostics.
			if proc.poll() is None:
				print(
					f"[mcp-probe:{name}] timed out after {timeout:.1f}s "
					"waiting for initialize response",
					file=sys.stderr,
				)
				return 2
			# stderr is inherited, so the server's own diagnostic lines have
			# already been forwarded to the calling process's stderr by now.
			print(
				f"[mcp-probe:{name}] server exited before sending a response "
				f"(returncode={proc.returncode}); see inherited stderr for the "
				"server's own diagnostic output",
				file=sys.stderr,
			)
			return 3

		try:
			message = json.loads(line.decode("utf-8", errors="replace"))
		except json.JSONDecodeError as exc:
			snippet = line[:200].decode("utf-8", errors="replace")
			print(
				f"[mcp-probe:{name}] response is not valid JSON ({exc}); "
				f"first 200 bytes: {snippet!r}",
				file=sys.stderr,
			)
			return 4

		if not isinstance(message, dict) or "result" not in message or "error" in message:
			print(
				f"[mcp-probe:{name}] initialize did not return a result: "
				f"{json.dumps(message)[:300]}",
				file=sys.stderr,
			)
			return 5

		if message.get("id") != request_id:
			print(
				f"[mcp-probe:{name}] response id={message.get('id')!r} "
				f"does not match request id={request_id}",
				file=sys.stderr,
			)
			return 6

		print(f"[mcp-probe:{name}] handshake OK", file=sys.stderr)
		return 0
	finally:
		# Best-effort: deliver SIGTERM/SIGKILL to the entire process group
		# so npx-spawned grandchildren do not linger after the probe exits.
		try:
			os.killpg(proc.pid, signal.SIGTERM)
		except (ProcessLookupError, PermissionError, OSError):
			pass
		_terminate(proc)


def main(argv: Optional[List[str]] = None) -> int:
	parser = argparse.ArgumentParser(
		description="MCP initialize handshake probe.",
		# Keep argv after `--` intact so callers can pass server flags.
		allow_abbrev=False,
	)
	parser.add_argument("--name", required=True, help="Display name (for log lines).")
	parser.add_argument("--command", required=True, help="MCP server executable.")
	parser.add_argument(
		"--timeout",
		type=float,
		default=15.0,
		help=(
			"Seconds to wait for the initialize response (default: 15, "
			"matching MCP_HANDSHAKE_PROBE_TIMEOUT in setup_serena.sh)."
		),
	)
	parser.add_argument(
		"server_args",
		nargs=argparse.REMAINDER,
		help="Args passed to the server. Prefix with `--` to separate from probe flags.",
	)
	ns = parser.parse_args(argv)

	args = list(ns.server_args)
	# argparse leaves the literal `--` in REMAINDER; strip a single leading one.
	if args and args[0] == "--":
		args = args[1:]

	if ns.timeout <= 0:
		print("[mcp-probe] --timeout must be positive", file=sys.stderr)
		return 64

	return probe(name=ns.name, command=ns.command, args=args, timeout=ns.timeout)


if __name__ == "__main__":
	sys.exit(main())
