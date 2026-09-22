#!/usr/bin/env python3
"""Loopback-only OpenRouter proxy for isolated model processes."""

from __future__ import annotations

import argparse
import ctypes
import http.client
import http.server
import json
import os
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit


ALLOWED_METHOD_PATHS = {
	"GET": {"/models"},
	"POST": {"/chat/completions", "/responses", "/embeddings"},
}
MAX_REQUEST_BYTES = 16 * 1024 * 1024


def _normalized_provider_path(raw_path: str) -> str | None:
	parsed = urlsplit(raw_path)
	path = parsed.path
	if path.startswith("/api/v1/"):
		path = path[len("/api/v1") :]
	allowed_paths = ALLOWED_METHOD_PATHS["GET"] | ALLOWED_METHOD_PATHS["POST"]
	if path not in allowed_paths:
		return None
	return path + (("?" + parsed.query) if parsed.query else "")


class ProviderProxy(http.server.BaseHTTPRequestHandler):
	server_version = "model-provider-proxy/1"
	protocol_version = "HTTP/1.1"

	def log_message(self, format_string: str, *args: object) -> None:
		print(f"model_provider_proxy: {format_string % args}", file=sys.stderr)

	def do_GET(self) -> None:  # noqa: N802
		if self.path == "/healthz":
			payload = b'{"status":"ok"}\n'
			self.send_response(200)
			self.send_header("Content-Type", "application/json")
			self.send_header("Content-Length", str(len(payload)))
			self.end_headers()
			self.wfile.write(payload)
			return
		self._proxy_request()

	def do_POST(self) -> None:  # noqa: N802
		self._proxy_request()

	def _proxy_request(self) -> None:
		self.close_connection = True
		provider_path = _normalized_provider_path(self.path)
		allowed_paths = ALLOWED_METHOD_PATHS.get(self.command, set())
		if provider_path is None or provider_path.split("?", 1)[0] not in allowed_paths:
			self.send_error(403, "provider route is not allowlisted")
			return
		try:
			content_length = int(self.headers.get("Content-Length", "0"))
		except ValueError:
			self.send_error(400, "invalid content length")
			return
		if content_length < 0 or content_length > MAX_REQUEST_BYTES:
			self.send_error(413, "request body too large")
			return
		request_body = self.rfile.read(content_length) if content_length else None
		forward_headers = {
			"Authorization": f"Bearer {self.server.provider_credential}",  # type: ignore[attr-defined]
			"Accept": self.headers.get("Accept", "application/json"),
			"Content-Type": self.headers.get("Content-Type", "application/json"),
			"User-Agent": "coding-workflows-isolated-agent/1",
		}
		connection = http.client.HTTPSConnection(
			"openrouter.ai", 443, timeout=300, context=ssl.create_default_context()
		)
		try:
			connection.request(self.command, f"/api/v1{provider_path}", request_body, forward_headers)
			response = connection.getresponse()
			self.send_response(response.status)
			for header_name, header_value in response.getheaders():
				if header_name.lower() in {
					"connection", "keep-alive", "proxy-authenticate",
					"proxy-authorization", "transfer-encoding", "upgrade",
				}:
					continue
				self.send_header(header_name, header_value)
			self.end_headers()
			while chunk := response.read(64 * 1024):
				self.wfile.write(chunk)
				self.wfile.flush()
		except (OSError, http.client.HTTPException) as exc:
			self.send_error(502, f"provider request failed: {type(exc).__name__}")
		finally:
			connection.close()


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--credential-file", required=True)
	parser.add_argument("--ready-file", required=True)
	parser.add_argument("--bind", default="127.0.0.2")
	args = parser.parse_args()
	credential = Path(args.credential_file).read_text(encoding="utf-8").strip()
	if not credential or "\n" in credential or "\r" in credential:
		raise SystemExit("provider credential file is empty or malformed")
	# PR_SET_DUMPABLE=0 prevents same-UID untrusted children from reading the
	# credential out of this process after its environment has been scrubbed.
	if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:
		raise SystemExit("could not make provider proxy non-dumpable")
	os.environ.pop("OPENROUTER_API_KEY", None)
	server = http.server.ThreadingHTTPServer((args.bind, 0), ProviderProxy)
	server.provider_credential = credential  # type: ignore[attr-defined]
	ready_path = Path(args.ready_file)
	ready_path.write_text(
		json.dumps({"host": args.bind, "port": server.server_port}) + "\n",
		encoding="utf-8",
	)
	os.chmod(ready_path, 0o600)
	try:
		server.serve_forever(poll_interval=0.2)
	finally:
		server.server_close()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
