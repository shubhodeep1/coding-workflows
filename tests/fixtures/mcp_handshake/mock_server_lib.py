#!/usr/bin/env python3
"""Helpers for stdio MCP handshake fixtures."""
from __future__ import annotations

import json
import sys
from typing import Any


def read_message() -> dict[str, Any]:
	headers: dict[str, str] = {}
	while True:
		line = sys.stdin.buffer.readline()
		if line == b"":
			raise EOFError("stdin closed before request headers")
		if line in (b"\n", b"\r\n"):
			break
		decoded = line.decode("ascii")
		name, value = decoded.split(":", 1)
		headers[name.strip().lower()] = value.strip()
	length = int(headers["content-length"])
	payload = sys.stdin.buffer.read(length)
	if len(payload) != length:
		raise EOFError("stdin closed before request body completed")
	return json.loads(payload.decode("utf-8"))


def write_message(payload: dict[str, Any]) -> None:
	body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
	sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
	sys.stdout.buffer.write(body)
	sys.stdout.buffer.flush()


def write_raw_json(raw_payload: bytes) -> None:
	sys.stdout.buffer.write(f"Content-Length: {len(raw_payload)}\r\n\r\n".encode("ascii"))
	sys.stdout.buffer.write(raw_payload)
	sys.stdout.buffer.flush()
