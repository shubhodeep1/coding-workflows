"""scripts/cs_bridge_query.sh (issue #9440): one agent DB bridge request.

It refuses any URL other than the bridge, never echoes the OIDC token, and
leaves no token file behind. A loopback stub plays the Actions OIDC endpoint
and the bridge.
"""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import urllib.parse

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "cs_bridge_query.sh"
REQUEST_TOKEN = "request-token-0123456789abcdef"
OIDC_TOKEN = "oidc.bridge-token-0123456789abcdefSECRET"
needs_curl = pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")


class _Stub:
	def __init__(self) -> None:
		self.seen: list[dict[str, str]] = []
		self.bridge_status = 200
		self.bridge_body = b'{"rows":[{"user_id":7}]}'
		stub = self

		class Handler(http.server.BaseHTTPRequestHandler):
			def log_message(self, *args: object) -> None:
				pass

			def _send(self, status: int, body: bytes) -> None:
				self.send_response(status)
				self.send_header("Content-Type", "application/json")
				self.send_header("Content-Length", str(len(body)))
				self.end_headers()
				self.wfile.write(body)

			def do_GET(self) -> None:
				parsed = urllib.parse.urlsplit(self.path)
				if parsed.path != "/oidc" or self.headers.get("Authorization") != f"bearer {REQUEST_TOKEN}":
					self._send(401, b"{}")
					return
				audience = urllib.parse.parse_qs(parsed.query).get("audience", [""])[0]
				stub.seen.append({"kind": "oidc", "audience": audience})
				self._send(200, json.dumps({"value": OIDC_TOKEN}).encode())

			def do_POST(self) -> None:
				length = int(self.headers.get("Content-Length") or 0)
				body = self.rfile.read(length)
				stub.seen.append({
					"kind": "bridge",
					"path": self.path,
					"authorization": self.headers.get("Authorization", ""),
					"content_type": self.headers.get("Content-Type", ""),
					"body": body.decode(),
				})
				self._send(stub.bridge_status, stub.bridge_body)

		self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
		self.port = self.server.server_address[1]
		threading.Thread(target=self.server.serve_forever, daemon=True).start()

	def close(self) -> None:
		self.server.shutdown()
		self.server.server_close()


@pytest.fixture
def stub():
	server = _Stub()
	yield server
	server.close()


def _run(body: str, url: str | None, tmp_path: Path, extra: dict[str, str] | None = None, stub: _Stub | None = None) -> subprocess.CompletedProcess[str]:
	env = {key: value for key, value in os.environ.items() if not key.startswith(("ACTIONS_ID_TOKEN", "CS_BRIDGE", "GITHUB_ACTIONS"))}
	tmp = tmp_path / "tmp"
	tmp.mkdir(exist_ok=True)
	env["TMPDIR"] = str(tmp)
	env["CS_OIDC_RETRY_SLEEP_SECONDS"] = "0"
	if url is not None:
		env["CS_BRIDGE_URL"] = url
	if stub is not None:
		env["ACTIONS_ID_TOKEN_REQUEST_URL"] = f"http://127.0.0.1:{stub.port}/oidc?api-version=2.0"
		env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"] = REQUEST_TOKEN
	env.update(extra or {})
	return subprocess.run(["bash", str(SCRIPT)], input=body, env=env, capture_output=True, text=True, timeout=60, check=False)


@pytest.mark.parametrize(
	("url", "extra"),
	[
		("https://evil.example/claude-db/query", {}),
		("https://mongo-explorer-5iucg.ondigitalocean.app.evil.example/claude-db/query", {}),
		("https://user@mongo-explorer-5iucg.ondigitalocean.app/claude-db/query", {}),
		("https://mongo-explorer-5iucg.ondigitalocean.app:8443/claude-db/query", {}),
		("https://mongo-explorer-5iucg.ondigitalocean.app/claude-db/query?x=1", {}),
		("https://mongo-explorer-5iucg.ondigitalocean.app/claude-db/query#x", {}),
		("https://mongo-explorer-5iucg.ondigitalocean.app/other", {}),
		("http://mongo-explorer-5iucg.ondigitalocean.app/claude-db/query", {}),
		("http://127.0.0.1:8080/claude-db/query", {"GITHUB_ACTIONS": "true"}),
		("http://10.0.0.1:8080/claude-db/query", {}),
	],
)
def test_refuses_any_url_but_the_bridge(url: str, extra: dict[str, str], tmp_path: Path) -> None:
	result = _run('{"q":1}', url, tmp_path, extra)
	assert result.returncode == 2
	assert result.stdout == ""
	assert "CS_BRIDGE_QUERY outcome=refused reason=host_not_allowed" in result.stderr
	assert url not in result.stderr


def test_default_url_is_the_bridge() -> None:
	text = SCRIPT.read_text(encoding="utf-8")
	assert 'CS_BRIDGE_DEFAULT_URL="https://mongo-explorer-5iucg.ondigitalocean.app/claude-db/query"' in text
	assert "set -x" not in text


@pytest.mark.parametrize(("body", "reason"), [("[1,2]", "body_not_object"), ("not json", "body_not_object"), ('{"a":"' + "x" * 17000 + '"}', "body_too_large")])
def test_refuses_a_bad_body(body: str, reason: str, tmp_path: Path) -> None:
	result = _run(body, "http://127.0.0.1:9/claude-db/query", tmp_path)
	assert result.returncode == 2
	assert f"reason={reason}" in result.stderr


def test_without_oidc_variables_exits_3(tmp_path: Path) -> None:
	result = _run('{"q":1}', "http://127.0.0.1:9/claude-db/query", tmp_path)
	assert result.returncode == 3
	assert "reason=oidc_unavailable" in result.stderr


@needs_curl
def test_posts_the_body_with_the_bridge_token_and_never_echoes_it(stub: _Stub, tmp_path: Path) -> None:
	result = _run('{"collection":"users","filter":{"user_id":7}}', f"http://127.0.0.1:{stub.port}/claude-db/query", tmp_path, stub=stub)
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout) == {"rows": [{"user_id": 7}]}
	assert "CS_BRIDGE_QUERY outcome=ok status=200" in result.stderr
	assert OIDC_TOKEN not in result.stdout + result.stderr
	assert REQUEST_TOKEN not in result.stdout + result.stderr
	assert {"kind": "oidc", "audience": "agent-db-bridge"} in stub.seen
	bridge = [item for item in stub.seen if item["kind"] == "bridge"]
	assert len(bridge) == 1
	assert bridge[0]["path"] == "/claude-db/query"
	assert bridge[0]["authorization"] == f"Bearer {OIDC_TOKEN}"
	assert bridge[0]["content_type"] == "application/json"
	assert json.loads(bridge[0]["body"]) == {"collection": "users", "filter": {"user_id": 7}}
	assert list((tmp_path / "tmp").iterdir()) == []


@needs_curl
def test_http_error_returns_the_body_and_exit_4_without_the_token(stub: _Stub, tmp_path: Path) -> None:
	stub.bridge_status = 400
	stub.bridge_body = b'{"error":"unknown collection"}'
	result = _run('{"collection":"nope"}', f"http://127.0.0.1:{stub.port}/claude-db/query", tmp_path, stub=stub)
	assert result.returncode == 4
	assert json.loads(result.stdout) == {"error": "unknown collection"}
	assert "CS_BRIDGE_QUERY outcome=error reason=http status=400" in result.stderr
	assert OIDC_TOKEN not in result.stdout + result.stderr
	assert list((tmp_path / "tmp").iterdir()) == []


@needs_curl
def test_sourced_oidc_fetch_writes_a_private_header_file(stub: _Stub, tmp_path: Path) -> None:
	header = tmp_path / "auth"
	env = dict(os.environ)
	env.update({
		"ACTIONS_ID_TOKEN_REQUEST_URL": f"http://127.0.0.1:{stub.port}/oidc",
		"ACTIONS_ID_TOKEN_REQUEST_TOKEN": REQUEST_TOKEN,
		"TMPDIR": str(tmp_path),
	})
	result = subprocess.run(
		["bash", "-c", 'source "$0"; cs_oidc_fetch cs-hub "$1"', str(SCRIPT), str(header)],
		env=env, capture_output=True, text=True, timeout=60, check=False,
	)
	assert result.returncode == 0, result.stderr
	assert header.read_text(encoding="utf-8") == f"Authorization: Bearer {OIDC_TOKEN}\n"
	assert header.stat().st_mode & 0o777 == 0o600
	assert OIDC_TOKEN not in result.stdout + result.stderr
	assert {"kind": "oidc", "audience": "cs-hub"} in stub.seen
