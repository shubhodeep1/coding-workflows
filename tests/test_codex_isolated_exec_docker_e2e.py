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

TOKEN = "ghp_isolatione2etoken00000000000000000000"
KEY = "sk-or-isolation-e2e-key"
PROBE = (
	"echo ENV_START; env | sort; echo ENV_END; echo GITCFG_START; cat .git/config; echo GITCFG_END; "
	"(curl -sS -m 5 https://example.com >/dev/null 2>&1 && echo NETWORK=open) || echo NETWORK=blocked; "
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
