#!/usr/bin/env python3
"""Probe an MCP stdio server with a single JSON-RPC initialize request."""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
	sys.path.insert(0, str(_SCRIPT_DIR))
try:
	from emit_event import emit_event as _emit_event_helper
except Exception:
	_emit_event_helper = None


REQUEST_ID: int = 1
DEFAULT_TIMEOUT_SECONDS = 15.0
STDERR_TAIL_LIMIT_BYTES = 65536


def _mirror_event(prefix: str, **fields: object) -> None:
	if _emit_event_helper is None:
		return
	try:
		_emit_event_helper(prefix, **fields)
	except Exception:
		return


class ProbeError(RuntimeError):
	"""Raised when the initialize handshake fails validation."""

	def __init__(self, reason: str, message: str):
		super().__init__(message)
		self.reason = reason
		self.stderr_tail = ""


class _StderrTailBuffer:
	def __init__(self, limit_bytes: int = STDERR_TAIL_LIMIT_BYTES):
		self.limit_bytes = limit_bytes
		self._buffer = bytearray()
		self._lock = threading.Lock()

	def append(self, chunk: bytes) -> None:
		with self._lock:
			self._buffer.extend(chunk)
			overflow = len(self._buffer) - self.limit_bytes
			if overflow > 0:
				del self._buffer[:overflow]

	def text(self) -> str:
		with self._lock:
			return self._buffer.decode("utf-8", errors="replace").strip()


def _env_flag(name: str, default: bool) -> bool:
	value = os.environ.get(name)
	if value is None:
		return default
	value = value.strip().lower()
	if value in {"", "0", "false", "no", "off"}:
		return False
	if value in {"1", "true", "yes", "on"}:
		return True
	return default


def handshake_probe_enabled() -> bool:
	return _env_flag("MCP_HANDSHAKE_PROBE_ENABLED", True)


def handshake_probe_timeout() -> float:
	raw = os.environ.get("MCP_HANDSHAKE_PROBE_TIMEOUT", "")
	if not raw.strip():
		return DEFAULT_TIMEOUT_SECONDS
	try:
		value = float(raw)
	except ValueError:
		return DEFAULT_TIMEOUT_SECONDS
	if value <= 0:
		return DEFAULT_TIMEOUT_SECONDS
	return value


def _sanitize_log_value(value: Any) -> str:
	text = str(value)
	parts = text.split()
	if not parts:
		return "unknown"
	return "_".join(parts).replace("=", "_")


def _emit_probe_line(target: str, result: str, *, reason: str | None = None, detail: str | None = None, server_info: dict[str, Any] | None = None) -> None:
	safe_target = _sanitize_log_value(target)
	safe_result = _sanitize_log_value(result)
	fields = [
		"SERENA_PROBE",
		f"target={safe_target}",
		f"result={safe_result}",
	]
	event_fields: dict[str, object] = {
		"target": safe_target,
		"result": safe_result,
	}
	if reason:
		safe_reason = _sanitize_log_value(reason)
		fields.append(f"reason={safe_reason}")
		event_fields["reason"] = safe_reason
	if server_info:
		name = server_info.get("name")
		version = server_info.get("version")
		if name:
			safe_name = _sanitize_log_value(name)
			fields.append(f"server_name={safe_name}")
			event_fields["server_name"] = safe_name
		if version:
			safe_version = _sanitize_log_value(version)
			fields.append(f"server_version={safe_version}")
			event_fields["server_version"] = safe_version
	sys.stderr.write(" ".join(fields) + "\n")
	_mirror_event("SERENA_PROBE", **event_fields)
	if detail:
		sys.stderr.write(f"mcp_handshake_probe: {detail}\n")


def _encode_message(payload: dict[str, Any]) -> bytes:
	body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
	headers = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
	return headers + body


def _read_exact(stream: Any, size: int) -> bytes:
	chunks: list[bytes] = []
	remaining = size
	while remaining > 0:
		chunk = stream.read(remaining)
		if chunk == b"":
			raise ProbeError("eof", "mcp server closed before sending the full response body")
		chunks.append(chunk)
		remaining -= len(chunk)
	return b"".join(chunks)


def _read_message(stream: Any) -> bytes:
	headers: dict[str, str] = {}
	saw_headers = False
	while True:
		line = stream.readline()
		if line == b"":
			if not saw_headers:
				raise ProbeError("eof", "mcp server closed before sending a response")
			raise ProbeError("eof", "mcp server closed while sending response headers")
		if line in (b"\n", b"\r\n"):
			break
		saw_headers = True
		try:
			decoded = line.decode("ascii")
		except UnicodeDecodeError as exc:
			raise ProbeError("invalid-protocol", f"response header was not ASCII: {exc}") from exc
		if ":" not in decoded:
			raise ProbeError("invalid-protocol", f"malformed response header: {decoded.rstrip()!r}")
		name, value = decoded.split(":", 1)
		headers[name.strip().lower()] = value.strip()

	length_text = headers.get("content-length")
	if length_text is None:
		raise ProbeError("invalid-protocol", "response was missing a Content-Length header")
	try:
		length = int(length_text)
	except ValueError as exc:
		raise ProbeError("invalid-protocol", f"invalid Content-Length header: {length_text!r}") from exc
	if length < 0:
		raise ProbeError("invalid-protocol", f"negative Content-Length header: {length}")
	return _read_exact(stream, length)


def _reader_loop(stream: Any, out_queue: "queue.Queue[tuple[str, Any]]") -> None:
	try:
		while True:
			payload = _read_message(stream)
			try:
				message = json.loads(payload.decode("utf-8"))
			except (UnicodeDecodeError, json.JSONDecodeError) as exc:
				raise ProbeError("invalid-json", f"response body was not valid JSON: {exc}") from exc
			out_queue.put(("message", message))
	except Exception as exc:  # pragma: no cover - intentionally marshalled across thread boundary
		out_queue.put(("error", exc))


def _stderr_reader_loop(stream: Any, collector: _StderrTailBuffer) -> None:
	try:
		while True:
			chunk = stream.read(4096)
			if chunk == b"":
				return
			collector.append(chunk)
	except Exception:
		return


def _is_notification(message: Any) -> bool:
	return (
		isinstance(message, dict)
		and "id" not in message
		and "method" in message
		and "result" not in message
		and "error" not in message
	)


def validate_initialize_response(message: Any, expected_id: Any) -> dict[str, Any]:
	"""Validate an MCP initialize response and return its result object."""
	if not isinstance(message, dict):
		raise ProbeError("invalid-response", "initialize response was not a JSON object")
	if message.get("jsonrpc") != "2.0":
		raise ProbeError("invalid-response", f"initialize response did not declare jsonrpc=2.0: {message!r}")
	if message.get("id") != expected_id:
		raise ProbeError(
			"id-mismatch",
			f"initialize response id mismatch: expected {expected_id!r}, got {message.get('id')!r}",
		)
	if "error" in message:
		raise ProbeError("error-response", f"initialize response contained error: {message['error']!r}")
	result = message.get("result")
	if not isinstance(result, dict):
		raise ProbeError("invalid-response", "initialize response was missing a result object")
	server_info = result.get("serverInfo")
	if not isinstance(server_info, dict):
		raise ProbeError("invalid-response", "initialize response was missing result.serverInfo")
	return result


def _cleanup_process(proc: subprocess.Popen[bytes] | None) -> str:
	if proc is None:
		return ""
	try:
		if proc.stdin is not None and not proc.stdin.closed:
			proc.stdin.close()
	except OSError:
		pass
	if proc.poll() is None:
		proc.terminate()
		try:
			proc.wait(timeout=1.0)
		except subprocess.TimeoutExpired:
			proc.kill()
			proc.wait(timeout=1.0)
	stderr_tail = ""
	stderr_reader = getattr(proc, "_stderr_reader", None)
	if isinstance(stderr_reader, threading.Thread):
		stderr_reader.join(timeout=1.0)
	stderr_collector = getattr(proc, "_stderr_collector", None)
	if isinstance(stderr_collector, _StderrTailBuffer):
		stderr_tail = stderr_collector.text()
	elif proc.stderr is not None:
		stderr_tail = proc.stderr.read().decode("utf-8", errors="replace").strip()
	return stderr_tail


def probe_initialize(command: Sequence[str], *, server_name: str = "mcp", timeout_seconds: float | None = None) -> dict[str, Any] | None:
	"""Run an initialize handshake probe against *command*.

	Returns the validated ``result`` object on success. If the kill switch is
	disabled, returns ``None`` and treats the probe as skipped-success.
	"""
	if not handshake_probe_enabled():
		_emit_probe_line(server_name, "skipped", reason="disabled")
		return None

	timeout_seconds = timeout_seconds or handshake_probe_timeout()
	if timeout_seconds <= 0:
		timeout_seconds = DEFAULT_TIMEOUT_SECONDS

	proc: subprocess.Popen[bytes] | None = None
	try:
		proc = subprocess.Popen(
			list(command),
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
		)
	except FileNotFoundError as exc:
		raise ProbeError("spawn-failed", f"unable to spawn MCP server: {exc}") from exc
	except OSError as exc:
		raise ProbeError("spawn-failed", f"unable to spawn MCP server: {exc}") from exc

	stdin = proc.stdin
	stdout = proc.stdout
	stderr = proc.stderr
	if stdin is None or stdout is None or stderr is None:
		stderr_tail = _cleanup_process(proc)
		probe_error = ProbeError("spawn-failed", "mcp server did not provide stdio pipes")
		if stderr_tail:
			probe_error.stderr_tail = stderr_tail
		raise probe_error

	stderr_collector = _StderrTailBuffer()
	stderr_reader = threading.Thread(target=_stderr_reader_loop, args=(stderr, stderr_collector), daemon=True)
	proc._stderr_collector = stderr_collector  # type: ignore[attr-defined]
	proc._stderr_reader = stderr_reader  # type: ignore[attr-defined]
	stderr_reader.start()

	request = {
		"jsonrpc": "2.0",
		"id": REQUEST_ID,
		"method": "initialize",
		"params": {
			"protocolVersion": "2025-03-26",
			"capabilities": {},
			"clientInfo": {
				"name": "coding-workflows-serena-probe",
				"version": "1",
			},
		},
	}

	message_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()
	reader = threading.Thread(target=_reader_loop, args=(stdout, message_queue), daemon=True)
	reader.start()

	try:
		stdin.write(_encode_message(request))
		stdin.flush()
	except BrokenPipeError as exc:
		stderr_tail = _cleanup_process(proc)
		probe_error = ProbeError("eof", f"mcp server closed stdin before initialize completed: {exc}")
		if stderr_tail:
			probe_error.stderr_tail = stderr_tail
		raise probe_error from exc

	try:
		deadline = time.monotonic() + timeout_seconds
		while True:
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				raise ProbeError("timeout", f"initialize response timed out after {timeout_seconds:g}s")
			try:
				kind, payload = message_queue.get(timeout=remaining)
			except queue.Empty as exc:
				raise ProbeError("timeout", f"initialize response timed out after {timeout_seconds:g}s") from exc
			if kind == "error":
				assert isinstance(payload, BaseException)
				if isinstance(payload, ProbeError):
					raise payload
				raise ProbeError("probe-failed", str(payload)) from payload
			if _is_notification(payload):
				continue
			result = validate_initialize_response(payload, REQUEST_ID)
			_cleanup_process(proc)
			return result
	except ProbeError as exc:
		stderr_tail = _cleanup_process(proc)
		if stderr_tail:
			exc.stderr_tail = stderr_tail
		raise
	except BaseException:
		_cleanup_process(proc)
		raise


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--name", default="mcp", help="Name to use in SERENA_PROBE log lines.")
	parser.add_argument("--timeout", type=float, default=None, help="Timeout in seconds (defaults to MCP_HANDSHAKE_PROBE_TIMEOUT or 15).")
	parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to start the MCP server, passed after '--'.")
	args = parser.parse_args(argv)
	command = list(args.command)
	if command and command[0] == "--":
		command = command[1:]
	if not command:
		parser.error("a server command is required after '--'")
	args.command = command
	return args


def main(argv: Sequence[str] | None = None) -> int:
	args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
	try:
		result = probe_initialize(args.command, server_name=args.name, timeout_seconds=args.timeout)
	except ProbeError as exc:
		_emit_probe_line(args.name, "failed", reason=exc.reason, detail=str(exc))
		stderr_tail = getattr(exc, "stderr_tail", "")
		if stderr_tail:
			sys.stderr.write(f"mcp_handshake_probe: server-stderr: {stderr_tail}\n")
		return 1

	server_info: dict[str, Any] | None = None
	if result is not None:
		server_info = result.get("serverInfo") if isinstance(result, dict) else None
		_emit_probe_line(args.name, "ok", server_info=server_info)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
