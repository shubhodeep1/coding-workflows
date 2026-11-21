#!/usr/bin/env python3
"""Opt-in end-to-end test: real Docker, real image, real Codex CLI.

Skipped unless CODEX_ISOLATION_DOCKER_E2E=1 and a Docker daemon answers,
because it builds the sandbox image (network) and takes about a minute.

The model is a local fake of the OpenRouter Responses endpoint. The test
copies the support files into a scratch directory and points that copy of
the broker at the fake (the shipped broker only ever talks to
openrouter.ai). The fake's first reply asks Codex to run a shell command
that prints the agent's environment, its .git config and a network probe;
the second reply ends the turn. The assertions read what the agent saw.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_isolation_fakes import copy_isolation_support, short_temp_dir  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

TOKEN = "ghp_isolatione2etoken00000000000000000000"
KEY = "sk-or-isolation-e2e-key"
PROBE = (
	"echo ENV_START; env | sort; echo ENV_END; echo GITCFG_START; cat .git/config; echo GITCFG_END; "
	"(python3 -c 'import urllib.request; urllib.request.urlopen(\"https://example.com\", timeout=5)' >/dev/null 2>&1 && echo NETWORK=open) || echo NETWORK=blocked; "
	"echo edited > agent_file.txt"
)


def docker_ready() -> bool:
	if os.environ.get("CODEX_ISOLATION_DOCKER_E2E") != "1" or not shutil.which("docker"):
		return False
	return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not docker_ready(), reason="set CODEX_ISOLATION_DOCKER_E2E=1 with a Docker daemon")


class FakeResponses(http.server.BaseHTTPRequestHandler):
	requests: list = []

	def log_message(self, *_args):
		pass

	def do_POST(self):
		body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
		FakeResponses.requests.append({"auth": self.headers.get("Authorization"), "model": body.get("model"), "input": body.get("input")})
		n = len(FakeResponses.requests)
		created = {"type": "response.created", "response": {"id": f"r{n}"}}
		done = {"type": "response.completed", "response": {"id": f"r{n}", "usage": {"input_tokens": 1, "input_tokens_details": None, "output_tokens": 1, "output_tokens_details": None, "total_tokens": 2}}}
		if n == 1:
			item = {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": json.dumps({"command": ["bash", "-lc", PROBE]})}
		else:
			item = {"type": "message", "role": "assistant", "id": "m1", "content": [{"type": "output_text", "text": "E2E_FINAL"}]}
		self.send_response(200)
		self.send_header("Content-Type", "text/event-stream")
		self.end_headers()
		for event in (created, {"type": "response.output_item.done", "item": item}, done):
			self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
			self.wfile.flush()


@pytest.fixture()
def e2e(tmp_path):
	server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeResponses)
	threading.Thread(target=server.serve_forever, daemon=True).start()
	FakeResponses.requests = []
	support = tmp_path / "support"
	copy_isolation_support(support)
	broker = support / "clarify_openrouter_broker.py"
	source = broker.read_text()
	patched = source.replace(
		'http.client.HTTPSConnection("openrouter.ai", timeout=600, context=ssl.create_default_context())',
		f'http.client.HTTPConnection("127.0.0.1", {server.server_address[1]}, timeout=600)',
	)
	assert patched != source
	broker.write_text(patched)
	runner_temp = short_temp_dir()
	repo = runner_temp / "repo"
	repo.mkdir()
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	for args in (["init", "-q"], ["remote", "add", "origin", f"https://x-access-token:{TOKEN}@github.com/o/r"]):
		subprocess.run(["git", *args], cwd=repo, env=env, check=True)
	(repo / "README.md").write_text("hello\n")
	subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
	subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], cwd=repo, env=env, check=True)
	yield support, repo, runner_temp
	server.shutdown()
	shutil.rmtree(runner_temp, ignore_errors=True)


def run(support, repo, runner_temp, mode):
	env = dict(os.environ)
	env.update({"GH_TOKEN": TOKEN, "GITHUB_TOKEN": TOKEN, "OPENROUTER_API_KEY": KEY, "RUNNER_TEMP": str(runner_temp), "PYTHONDONTWRITEBYTECODE": "1"})
	return subprocess.run(
		["bash", str(support / "codex_isolated_exec.sh"), "run", "--mode", mode, "--reasoning", "low", "--web-search", "disabled",
		"--", "--ask-for-approval", "never", "exec", "--skip-git-repo-check", "--model", "openai/gpt-5.4", "--sandbox", "danger-full-access"],
		cwd=repo, input="Do the task", capture_output=True, text=True, env=env, timeout=900,
	)


def tool_output():
	items = FakeResponses.requests[-1]["input"]
	outputs = [item.get("output", "") for item in items if isinstance(item, dict) and item.get("type") == "function_call_output"]
	assert outputs, "the shell probe never ran"
	return outputs[-1]


@pytest.mark.parametrize("mode", ["read-only", "workspace"])
def test_real_container_hides_credentials_and_network(e2e, mode):
	support, repo, runner_temp = e2e
	proc = run(support, repo, runner_temp, mode)
	assert proc.returncode == 0, proc.stderr[-4000:]
	assert proc.stdout.strip().endswith("E2E_FINAL")
	assert {r["auth"] for r in FakeResponses.requests} == {f"Bearer {KEY}"}, "only the host broker adds the key"
	output = tool_output()
	assert "ENV_START" in output and "NETWORK=blocked" in output and "NETWORK=open" not in output
	for secret in (TOKEN, KEY):
		assert secret not in output
	assert "CODEX_ISOLATED_PROXY_KEY=isolated-placeholder" in output
	if mode == "workspace":
		assert (repo / "agent_file.txt").read_text() == "edited\n"
	else:
		assert not (repo / "agent_file.txt").exists()


# --- the Claude engine: real image with the pinned Claude Code CLI --------------------

CLAUDE_KEY = "sk-ant-oat01-isolation-e2e-token"
CLAUDE_MODELS = "claude-opus-5-5,claude-haiku-4-5-20251001"


class FakeMessages(http.server.BaseHTTPRequestHandler):
	"""A fake Anthropic Messages API: one Bash tool call, then a final answer."""

	requests: list = []

	def log_message(self, *_args):
		pass

	def _sse(self, events):
		self.send_response(200)
		self.send_header("Content-Type", "text/event-stream")
		self.end_headers()
		for event in events:
			self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
			self.wfile.flush()

	def do_POST(self):
		body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
		FakeMessages.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "model": body.get("model"), "body": body})
		if "count_tokens" in self.path:
			payload = json.dumps({"input_tokens": 10}).encode()
			self.send_response(200)
			self.send_header("Content-Type", "application/json")
			self.send_header("Content-Length", str(len(payload)))
			self.end_headers()
			self.wfile.write(payload)
			return
		messages = body.get("messages", [])
		answered = any(
			isinstance(part, dict) and part.get("type") == "tool_result"
			for message in messages if isinstance(message.get("content"), list)
			for part in message["content"]
		)
		has_tools = any(tool.get("name") == "Bash" for tool in body.get("tools", []) or [])
		usage = {"input_tokens": 1, "output_tokens": 1}
		start = {"type": "message_start", "message": {"id": f"msg_{len(FakeMessages.requests)}", "type": "message", "role": "assistant", "model": body.get("model"), "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}}
		if has_tools and not answered:
			call = json.dumps({"command": PROBE, "description": "probe"})
			blocks = [
				{"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_e2e", "name": "Bash", "input": {}}},
				{"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": call}},
				{"type": "content_block_stop", "index": 0},
			]
			stop = "tool_use"
		else:
			blocks = [
				{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
				{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "E2E_FINAL"}},
				{"type": "content_block_stop", "index": 0},
			]
			stop = "end_turn"
		if not body.get("stream"):
			payload = json.dumps({"id": "msg_x", "type": "message", "role": "assistant", "model": body.get("model"), "content": [{"type": "text", "text": "E2E_FINAL"}], "stop_reason": "end_turn", "stop_sequence": None, "usage": usage}).encode()
			self.send_response(200)
			self.send_header("Content-Type", "application/json")
			self.send_header("Content-Length", str(len(payload)))
			self.end_headers()
			self.wfile.write(payload)
			return
		self._sse([start, *blocks, {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 1}}, {"type": "message_stop"}])


@pytest.fixture()
def claude_e2e(tmp_path):
	server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeMessages)
	threading.Thread(target=server.serve_forever, daemon=True).start()
	FakeMessages.requests = []
	support = tmp_path / "support"
	copy_isolation_support(support)
	relay = support / "claude_anthropic_relay.py"
	source = relay.read_text()
	patched = source.replace(
		"http.client.HTTPSConnection(UPSTREAM_HOST, timeout=600, context=ssl.create_default_context())",
		f'http.client.HTTPConnection("127.0.0.1", {server.server_address[1]}, timeout=600)',
	)
	assert patched != source
	relay.write_text(patched)
	runner_temp = short_temp_dir()
	repo = runner_temp / "repo"
	repo.mkdir()
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	for args in (["init", "-q"], ["remote", "add", "origin", f"https://x-access-token:{TOKEN}@github.com/o/r"]):
		subprocess.run(["git", *args], cwd=repo, env=env, check=True)
	(repo / "README.md").write_text("hello\n")
	(repo / "CLAUDE.md").write_text("SECRET-CLAUDE-MD-MARKER\n")
	subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
	subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], cwd=repo, env=env, check=True)
	token = runner_temp / "token"
	token.write_text(CLAUDE_KEY + "\n")
	token.chmod(0o600)
	files = runner_temp / "claude-files"
	files.mkdir()
	subprocess.run(
		[sys.executable, str(REPO_ROOT / "scripts" / "claude_engine.py"), "settings", "--checkout", str(repo), "--out", str(files / "settings.json"), "--profile", "write", "--guard-hook", "/support/guard.py"],
		check=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
	)
	shutil.copy(REPO_ROOT / ".claude" / "hooks" / "gh_api_write_guard.py", files / "guard.py")
	shutil.copy(REPO_ROOT / "unattended_system_instructions.md", files / "instructions.md")
	yield support, repo, runner_temp, token, files
	server.shutdown()
	shutil.rmtree(runner_temp, ignore_errors=True)


@pytest.mark.parametrize("mode", ["read-only", "workspace"])
def test_real_claude_container_hides_credentials_and_network(claude_e2e, mode):
	support, repo, runner_temp, token, files = claude_e2e
	cli_version = json.loads((REPO_ROOT / ".github" / "ai" / "claude_engine.json").read_text())["cli_version"]
	env = dict(os.environ)
	env.update({"GH_TOKEN": TOKEN, "GITHUB_TOKEN": TOKEN, "OPENROUTER_API_KEY": KEY, "RUNNER_TEMP": str(runner_temp), "PYTHONDONTWRITEBYTECODE": "1"})
	tools = "Read,Grep,Glob,Bash" if mode == "read-only" else "Read,Grep,Glob,Bash,Edit,Write"
	assert "WebFetch" not in tools and "WebSearch" not in tools
	# Bypass permissions so the sandbox probe runs with the production tool list.
	permission = "bypassPermissions"
	proc = subprocess.run(
		["bash", str(support / "codex_isolated_exec.sh"), "run", "--engine", "claude", "--mode", mode,
		"--claude-token-file", str(token), "--claude-models", CLAUDE_MODELS, "--claude-cli-version", cli_version,
		"--claude-settings", str(files / "settings.json"), "--claude-guard-hook", str(files / "guard.py"),
		"--claude-instructions", str(files / "instructions.md"), "--claude-home", str(runner_temp / "claude-home"), "--hide-claude-md",
		"--", "-p", "--model", "claude-opus-5-5", "--effort", "low", "--system-prompt-file", "/support/instructions.md",
		"--setting-sources", "", "--settings", "/support/settings.json", "--strict-mcp-config", "--disable-slash-commands",
		"--exclude-dynamic-system-prompt-sections", "--tools", tools, "--permission-mode", permission,
		"--output-format", "stream-json", "--verbose"],
		cwd=repo, input="Do the task", capture_output=True, text=True, env=env, timeout=1500,
	)
	assert proc.returncode == 0, proc.stderr[-4000:]
	result = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")][-1]
	assert result.get("type") == "result" and result.get("result") == "E2E_FINAL", proc.stdout[-2000:]
	assert {r["auth"] for r in FakeMessages.requests} == {f"Bearer {CLAUDE_KEY}"}, "only the host relay adds the token"
	assert {r["model"] for r in FakeMessages.requests} <= set(CLAUDE_MODELS.split(","))
	outputs = [
		json.dumps(part.get("content"))
		for r in FakeMessages.requests for message in r["body"].get("messages", []) if isinstance(message.get("content"), list)
		for part in message["content"] if isinstance(part, dict) and part.get("type") == "tool_result"
	]
	assert outputs, "the Bash probe never ran"
	output = outputs[-1]
	assert "ENV_START" in output and "NETWORK=blocked" in output and "NETWORK=open" not in output
	for secret in (TOKEN, KEY, CLAUDE_KEY):
		assert secret not in output
	for name in ("GH_TOKEN=", "GITHUB_TOKEN=", "GH_PAT=", "OPENROUTER_API_KEY="):
		assert name not in output
	assert "ANTHROPIC_BASE_URL=http://127.0.0.1:8765" in output
	assert "x-access-token" not in output, "the synthetic .git has no remote"
	# hide_claude_md: the marker never reached the model.
	assert "SECRET-CLAUDE-MD-MARKER" not in json.dumps(FakeMessages.requests)
	assert (repo / "CLAUDE.md").read_text() == "SECRET-CLAUDE-MD-MARKER\n"
	if mode == "workspace":
		assert (repo / "agent_file.txt").read_text() == "edited\n"
	else:
		assert not (repo / "agent_file.txt").exists()
