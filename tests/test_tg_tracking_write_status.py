#!/usr/bin/env python3
"""Telegram tracking-comment writes report failures (BUG-004).

`tg_store_msg_id` and `tg_store_phase_msg_id` used to write tracking comments
with `curl ... >/dev/null 2>&1 || true`, so an HTTP 4xx/5xx looked like
success and the Telegram message ID was silently never recorded. The writes
now go through `_tg_tracking_write`, which logs `TG_TRACKING_WRITE_FAILED`,
retries the idempotent PATCH once, never retries the POST, and still returns 0.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GH_TOKEN_VALUE = "ghp_SENTINEL_TOKEN_DO_NOT_LOG"
BODY_SENTINEL = "RESPONSE_BODY_SENTINEL_DO_NOT_LOG"

FAKE_CURL = r'''#!__PYTHON__
import json
import os
import sys

state = os.environ["FAKE_CURL_STATE"]
args = sys.argv[1:]
method = "GET"
out_file = None
write_out = None
data = []
max_time = None
url = ""
i = 0
while i < len(args):
	arg = args[i]
	if arg in ("-X", "--request"):
		method = args[i + 1]; i += 2
	elif arg == "-o":
		out_file = args[i + 1]; i += 2
	elif arg == "-w":
		write_out = args[i + 1]; i += 2
	elif arg in ("-d", "--data", "--data-urlencode"):
		data.append(args[i + 1]); i += 2
	elif arg == "-H":
		i += 2
	elif arg == "--max-time":
		max_time = args[i + 1]; i += 2
	elif arg.startswith("-"):
		i += 1
	else:
		url = arg; i += 1

config_path = os.path.join(state, "config.json")
with open(config_path, encoding="utf-8") as fh:
	config = json.load(fh)


def log(line):
	with open(os.path.join(state, "calls.log"), "a", encoding="utf-8") as fh:
		fh.write(line + "\n")


def respond(code, body):
	if out_file:
		if out_file != "/dev/null":
			with open(out_file, "w", encoding="utf-8") as fh:
				fh.write(body)
	else:
		sys.stdout.write(body)
	if write_out is not None:
		sys.stdout.write(write_out.replace("%{http_code}", str(code)))
	sys.exit(0)


target = url.split("https://api.github.com/", 1)[-1]
if method in ("PATCH", "POST"):
	body = json.loads(data[-1])["body"] if data else ""
	log(f"{method} {target} max_time={max_time} body={body}")
	queue = config["responses"].get(method, [])
	mode = queue.pop(0) if queue else "200"
	with open(config_path, "w", encoding="utf-8") as fh:
		json.dump(config, fh)
	if mode == "transport":
		sys.stdout.write("000")
		sys.exit(28)
	respond(int(mode), json.dumps({"message": "__BODY__", "id": 9001}))

log(f"GET {target}")
if url.endswith("/user"):
	respond(200, json.dumps({"login": "pipeline-bot"}))
if "/comments?" in url:
	if config.get("page_fails"):
		respond(500, json.dumps({"message": "Server Error"}))
	respond(200, json.dumps(config.get("comments", [])))
respond(200, json.dumps({"comments": len(config.get("comments", []))}))
'''.replace("__BODY__", BODY_SENTINEL)

BOT = {"login": "pipeline-bot", "type": "User"}


def _run(tmp_path: Path, script: str, responses: dict, comments: list[dict] | None = None,
		page_fails: bool = False) -> tuple[subprocess.CompletedProcess, list[str]]:
	bindir = tmp_path / "bin"
	state = tmp_path / "state"
	bindir.mkdir()
	state.mkdir()
	fake = bindir / "curl"
	fake.write_text(FAKE_CURL.replace("__PYTHON__", sys.executable), encoding="utf-8")
	fake.chmod(0o755)
	(state / "config.json").write_text(json.dumps({
		"responses": responses, "comments": comments or [], "page_fails": page_fails,
	}), encoding="utf-8")
	env = {k: v for k, v in os.environ.items() if not k.startswith(("TG_", "ALERT_MSG_LEVEL", "_TG_HELPERS", "GH_"))}
	env.update({
		"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
		"FAKE_CURL_STATE": str(state),
		"GH_TOKEN": GH_TOKEN_VALUE,
		"GITHUB_REPOSITORY": "o/r",
		"GH_RETRY_MAX_ATTEMPTS": "1",
		"TMPDIR": str(tmp_path),
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	calls_log = state / "calls.log"
	harness = (
		"set -euo pipefail\nsource scripts/tg_helpers.sh\n"
		f"sleep() {{ echo \"SLEEP $*\" >> {shlex.quote(str(calls_log))}; }}\n"
		f"{script}\necho RETURNED_OK\n"
	)
	result = subprocess.run(["bash", "-c", harness], cwd=REPO_ROOT, env=env, capture_output=True, text=True,
		check=False, timeout=120)
	calls = calls_log.read_text(encoding="utf-8").splitlines() if calls_log.exists() else []
	for secret in (GH_TOKEN_VALUE, BODY_SENTINEL):
		assert secret not in result.stdout
		assert secret not in result.stderr
	assert result.returncode == 0, result.stderr
	assert result.stdout == "RETURNED_OK\n", result.stdout
	return result, calls


def _writes(calls: list[str]) -> list[str]:
	return [c for c in calls if c.startswith(("PATCH ", "POST "))]


def _failed(result: subprocess.CompletedProcess) -> list[str]:
	return [line for line in result.stderr.splitlines() if "TG_TRACKING_WRITE_FAILED" in line]


@pytest.mark.parametrize("script,code,phase,body", [
	("tg_store_msg_id 42 8", "500", "none", "<!-- tg_cleanup:8 -->"),
	("tg_store_phase_msg_id 42 plan 8", "422", "plan", "<!-- tg_phase:plan:8 -->"),
])
def test_failed_create_warns_and_is_not_retried(tmp_path: Path, script: str, code: str, phase: str, body: str) -> None:
	result, calls = _run(tmp_path, script, {"POST": [code, "201"]})
	assert _writes(calls) == [f"POST repos/o/r/issues/42/comments max_time=30 body={body}"]
	assert "SLEEP 2" not in calls
	assert _failed(result) == [f"::warning::TG_TRACKING_WRITE_FAILED issue=42 op=create phase={phase} msg_id=8 http={code}"]


def test_create_transport_failure_reports_000_without_retry(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, "tg_store_msg_id 42 8", {"POST": ["transport", "201"]})
	assert len(_writes(calls)) == 1
	assert "SLEEP 2" not in calls
	assert _failed(result) == ["::warning::TG_TRACKING_WRITE_FAILED issue=42 op=create phase=none msg_id=8 http=000"]


@pytest.mark.parametrize("script,marker,expected", [
	("tg_store_msg_id 42 8", "<!-- tg_cleanup:5 -->", "<!-- tg_cleanup:5,8 -->"),
	("tg_store_phase_msg_id 42 plan 8", "<!-- tg_phase:plan:5 -->", "<!-- tg_phase:plan:5,8 -->"),
])
def test_patch_failure_is_retried_once_then_succeeds(tmp_path: Path, script: str, marker: str, expected: str) -> None:
	comments = [{"id": 77, "body": marker, "user": dict(BOT)}]
	result, calls = _run(tmp_path, script, {"PATCH": ["500", "200"]}, comments)
	patch = f"PATCH repos/o/r/issues/comments/77 max_time=30 body={expected}"
	assert _writes(calls) == [patch, patch]
	assert calls.count("SLEEP 2") == 1
	assert calls.index("SLEEP 2") == calls.index(patch) + 1
	assert _failed(result) == []


@pytest.mark.parametrize("script,marker,phase", [
	("tg_store_msg_id 42 8", "<!-- tg_cleanup:5 -->", "none"),
	("tg_store_phase_msg_id 42 plan 8", "<!-- tg_phase:plan:5 -->", "plan"),
])
def test_patch_failing_twice_warns_and_does_not_post(tmp_path: Path, script: str, marker: str, phase: str) -> None:
	comments = [{"id": 77, "body": marker, "user": dict(BOT)}]
	result, calls = _run(tmp_path, script, {"PATCH": ["500", "transport", "200"]}, comments)
	writes = _writes(calls)
	assert len(writes) == 2
	assert all(w.startswith("PATCH repos/o/r/issues/comments/77 max_time=30 ") for w in writes)
	assert calls.count("SLEEP 2") == 1
	assert _failed(result) == [f"::warning::TG_TRACKING_WRITE_FAILED issue=42 op=append phase={phase} msg_id=8 http=000"]


def test_patch_success_is_unchanged(tmp_path: Path) -> None:
	comments = [{"id": 77, "body": "<!-- tg_cleanup:5 -->", "user": dict(BOT)}]
	result, calls = _run(tmp_path, "tg_store_msg_id 42 8", {"PATCH": ["200"]}, comments)
	assert _writes(calls) == ["PATCH repos/o/r/issues/comments/77 max_time=30 body=<!-- tg_cleanup:5,8 -->"]
	assert "SLEEP 2" not in calls
	assert "::warning::" not in result.stderr


@pytest.mark.parametrize("script,body", [
	("tg_store_msg_id 42 8", "<!-- tg_cleanup:8 -->"),
	("tg_store_phase_msg_id 42 plan 8", "<!-- tg_phase:plan:8 -->"),
])
def test_create_success_is_unchanged(tmp_path: Path, script: str, body: str) -> None:
	result, calls = _run(tmp_path, script, {"POST": ["201"]})
	assert _writes(calls) == [f"POST repos/o/r/issues/42/comments max_time=30 body={body}"]
	assert "SLEEP 2" not in calls
	assert "::warning::" not in result.stderr


@pytest.mark.parametrize("script,phase,fetch_warning", [
	("tg_store_msg_id 42 8", "none", "Failed to fetch existing tracking comments for issue #42"),
	("tg_store_phase_msg_id 42 plan 8", "plan", "Failed to fetch existing phase tracking comments for issue #42 phase=plan"),
])
def test_fetch_failure_fallback_create_reports_failure(tmp_path: Path, script: str, phase: str, fetch_warning: str) -> None:
	result, calls = _run(tmp_path, script, {"POST": ["500", "201"]}, page_fails=True)
	assert len(_writes(calls)) == 1
	assert _writes(calls)[0].startswith("POST repos/o/r/issues/42/comments max_time=30 ")
	assert fetch_warning in result.stderr
	assert _failed(result) == [f"::warning::TG_TRACKING_WRITE_FAILED issue=42 op=create phase={phase} msg_id=8 http=500"]
