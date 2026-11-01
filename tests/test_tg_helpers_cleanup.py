"""Contract tests for the Telegram tracking-comment cleanup in scripts/tg_helpers.sh.

BUG-003: `tg_cleanup_msgs` / `tg_cleanup_phase_msgs` deleted tracking comments
while paginating, so page-2 comments shifted onto page 1 and were skipped. The
cleanup now reads every page first and deletes nothing from a partial snapshot.

BUG-006: `tg_delete_msg` swallowed every Telegram failure and the cleanup then
deleted the tracking comment, losing the message IDs. A transient failure now
keeps the tracking comment so the next cleanup retries it.

The harness puts a fake `curl` executable on PATH and runs with the repo root
as the working directory, so the real `curl_gh_api` from scripts/gh_helpers.sh
drives the comment reads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BOT_TOKEN = "bot-sentinel-token-7f3a9c"
GH_TOKEN_VALUE = "gh-sentinel-token-b81d2e"

FAKE_CURL = r'''#!__PYTHON__
import json
import os
import re
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
	elif arg == "-D":
		i += 2
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

with open(os.path.join(state, "config.json"), encoding="utf-8") as fh:
	config = json.load(fh)


def log(line):
	with open(os.path.join(state, "calls.log"), "a", encoding="utf-8") as fh:
		fh.write(line + "\n")


def respond(code, body):
	if out_file:
		with open(out_file, "w", encoding="utf-8") as fh:
			fh.write(body)
	else:
		sys.stdout.write(body)
	if write_out is not None:
		sys.stdout.write(write_out.replace("\\n", "\n").replace("%{http_code}", str(code)))
	sys.exit(0)


if "api.telegram.org" in url:
	msg_id = ""
	for item in data:
		if item.startswith("message_id="):
			msg_id = item.split("=", 1)[1]
	endpoint = url.rsplit("/", 1)[-1]
	log(f"TG {endpoint} {msg_id} max_time={max_time}")
	mode = config.get("tg_modes", {}).get(msg_id, "ok")
	if mode == "transport":
		sys.exit(28)
	if mode == "ok":
		respond(200, '{"ok":true,"result":true}')
	if mode == "not_found":
		respond(400, '{"ok":false,"error_code":400,"description":"Bad Request: message to delete not found"}')
	if mode == "cant_delete":
		respond(200, '{"ok":false,"error_code":400,"description":"Bad Request: message can\'t be deleted"}')
	if mode == "429":
		respond(429, '{"ok":false,"error_code":429,"description":"Too Many Requests: retry after 5"}')
	if mode == "500":
		respond(500, '{"ok":false,"error_code":500,"description":"Internal Server Error"}')
	if mode == "garbage":
		respond(200, "<html>bad gateway</html>")
	sys.exit(0)

match = re.search(r"/issues/comments/(\d+)$", url)
if match and method == "DELETE":
	log(f"DELETE {match.group(1)} max_time={max_time}")
	sys.exit(0)

match = re.search(r"/issues/(\d+)/comments\?per_page=100&page=(\d+)$", url)
if match and method == "GET":
	page = match.group(2)
	log(f"GET page={page}")
	mode = config.get("page_modes", {}).get(page)
	if mode == "exit7":
		sys.exit(7)
	if mode == "500":
		respond(500, '{"message":"Server Error"}')
	if mode == "garbage":
		respond(200, "not json at all")
	if mode == "object":
		respond(200, '{"message":"Not Found"}')
	respond(200, json.dumps(config.get("pages", {}).get(page, [])))

log(f"UNEXPECTED {method}")
sys.exit(7)
'''


def _comment(comment_id: int, body: str) -> dict:
	return {"id": comment_id, "body": body}


def _filler(start: int, count: int) -> list[dict]:
	return [_comment(n, f"ordinary comment {n}") for n in range(start, start + count)]


def _run(tmp_path: Path, script: str, pages: dict[int, list[dict]], page_modes: dict[int, str] | None = None,
		tg_modes: dict[str, str] | None = None, **env_overrides: str) -> tuple[subprocess.CompletedProcess, list[str]]:
	state = tmp_path / "state"
	bindir = tmp_path / "bin"
	state.mkdir()
	bindir.mkdir()
	fake = bindir / "curl"
	fake.write_text(FAKE_CURL.replace("__PYTHON__", sys.executable), encoding="utf-8")
	fake.chmod(0o755)
	(state / "config.json").write_text(json.dumps({
		"pages": {str(k): v for k, v in pages.items()},
		"page_modes": {str(k): v for k, v in (page_modes or {}).items()},
		"tg_modes": tg_modes or {},
	}), encoding="utf-8")
	env = {k: v for k, v in os.environ.items() if not k.startswith(("TG_", "ALERT_MSG_LEVEL", "_TG_HELPERS", "GH_"))}
	env.update({
		"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
		"FAKE_CURL_STATE": str(state),
		"GH_TOKEN": GH_TOKEN_VALUE,
		"GITHUB_REPOSITORY": "o/r",
		"TG_BOT_SECRET": BOT_TOKEN,
		"TG_ADMIN_CHAT_ID": "-1001",
		"GH_RETRY_MAX_ATTEMPTS": "1",
		"TMPDIR": str(tmp_path),
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	env.update(env_overrides)
	harness = f"set -euo pipefail\nsource scripts/tg_helpers.sh\n{script}\necho TG_CLEANUP_RETURNED_OK\n"
	result = subprocess.run(["bash", "-c", harness], cwd=REPO_ROOT, env=env, capture_output=True, text=True,
		check=False, timeout=120)
	log = state / "calls.log"
	calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
	# The secrets never reach stdout or stderr, whatever the outcome.
	for secret in (BOT_TOKEN, GH_TOKEN_VALUE):
		assert secret not in result.stdout
		assert secret not in result.stderr
	assert result.returncode == 0, result.stderr
	assert "TG_CLEANUP_RETURNED_OK" in result.stdout, result.stderr
	return result, calls


def _deleted(calls: list[str]) -> list[int]:
	return sorted(int(c.split()[1]) for c in calls if c.startswith("DELETE "))


def _tg_ids(calls: list[str]) -> list[str]:
	return [c.split()[2] for c in calls if c.startswith("TG deleteMessage ")]


def _warnings(result: subprocess.CompletedProcess) -> list[str]:
	return [line for line in result.stderr.splitlines() if "Telegram cleanup kept" in line]


def test_markers_on_every_page_are_deleted(tmp_path: Path) -> None:
	page1 = _filler(1, 100)
	page1[4] = _comment(5, "<!-- tg_cleanup:11 -->")
	page1[49] = _comment(50, "<!-- tg_phase:plan:12 -->")
	page2 = _filler(101, 50)
	page2[19] = _comment(120, "<!-- tg_cleanup:13,14 -->")
	page2[48] = _comment(149, "<!-- tg_phase:clarify:15 -->")
	result, calls = _run(tmp_path, "tg_cleanup_msgs 42", {1: page1, 2: page2})
	assert _deleted(calls) == [5, 50, 120, 149]
	assert sorted(_tg_ids(calls)) == ["11", "12", "13", "14", "15"]
	# Every page is read before anything is deleted.
	first_mutation = next(i for i, c in enumerate(calls) if not c.startswith("GET "))
	assert [c for c in calls[:first_mutation]] == ["GET page=1", "GET page=2"]
	assert all(not c.startswith("GET ") for c in calls[first_mutation:])
	assert not _warnings(result)


@pytest.mark.parametrize("mode", ["500", "exit7"])
def test_page_read_failure_deletes_nothing(tmp_path: Path, mode: str) -> None:
	page1 = _filler(1, 100)
	page1[0] = _comment(1, "<!-- tg_cleanup:11 -->")
	result, calls = _run(tmp_path, "tg_cleanup_msgs 42", {1: page1}, page_modes={2: mode})
	assert _deleted(calls) == []
	assert _tg_ids(calls) == []
	assert "::warning::Failed to fetch issue comments for cleanup (issue #42, page 2); aborting cleanup" in result.stderr


def test_phase_page_read_failure_keeps_existing_warning(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, "tg_cleanup_phase_msgs 42 clarify", {}, page_modes={1: "500"})
	assert _deleted(calls) == [] and _tg_ids(calls) == []
	assert ("::warning::Failed to fetch issue comments for phase cleanup (issue #42, phase clarify, page 1); "
		"aborting cleanup") in result.stderr


@pytest.mark.parametrize("mode", ["garbage", "object"])
def test_unparsable_page_deletes_nothing(tmp_path: Path, mode: str) -> None:
	page1 = _filler(1, 100)
	page1[0] = _comment(1, "<!-- tg_cleanup:11 -->")
	result, calls = _run(tmp_path, "tg_cleanup_msgs 42", {1: page1}, page_modes={2: mode})
	assert _deleted(calls) == [] and _tg_ids(calls) == []
	assert "::warning::Unparsable issue comments page for cleanup (issue #42, page 2); aborting cleanup" in result.stderr


@pytest.mark.parametrize("mode", ["500", "429", "transport", "garbage"])
def test_transient_telegram_failure_keeps_only_that_comment(tmp_path: Path, mode: str) -> None:
	pages = {1: [
		_comment(1, "<!-- tg_cleanup:21 -->"),
		_comment(2, "<!-- tg_cleanup:22,23 -->"),
		_comment(3, "<!-- tg_phase:plan:24 -->"),
	]}
	result, calls = _run(tmp_path, "tg_cleanup_msgs 42", pages, tg_modes={"22": mode})
	assert _deleted(calls) == [1, 3]
	assert _tg_ids(calls) == ["21", "22", "23", "24"]
	assert all("max_time=10" in c for c in calls if c.startswith("TG "))
	assert all("max_time=10" in c for c in calls if c.startswith("DELETE "))
	warnings = _warnings(result)
	assert warnings == ["::warning::Telegram cleanup kept 1 tracking comment(s) on issue #42: 1 message ID(s) hit a "
		"transient Telegram failure; the next cleanup retries them"]


@pytest.mark.parametrize("mode", ["not_found", "cant_delete"])
def test_gone_or_permanent_message_still_deletes_the_comment(tmp_path: Path, mode: str) -> None:
	pages = {1: [_comment(7, "<!-- tg_cleanup:31,32 -->")]}
	result, calls = _run(tmp_path, "tg_cleanup_msgs 42", pages, tg_modes={"31": mode})
	assert _deleted(calls) == [7]
	assert _tg_ids(calls) == ["31", "32"]
	assert not _warnings(result)


def test_phase_cleanup_only_touches_its_phase(tmp_path: Path) -> None:
	pages = {1: [
		_comment(1, "<!-- tg_phase:clarify:41 -->"),
		_comment(2, "<!-- tg_phase:plan:42 -->"),
		_comment(3, "<!-- tg_cleanup:43 -->"),
		_comment(4, "<!-- tg_phase:clarify:44,45 -->"),
	]}
	result, calls = _run(tmp_path, "tg_cleanup_phase_msgs 42 clarify", pages, tg_modes={"45": "500"})
	assert _deleted(calls) == [1]
	assert _tg_ids(calls) == ["41", "44", "45"]
	assert _warnings(result) == ["::warning::Telegram cleanup kept 1 tracking comment(s) on issue #42 (phase clarify): "
		"1 message ID(s) hit a transient Telegram failure; the next cleanup retries them"]


def test_general_cleanup_handles_both_marker_formats(tmp_path: Path) -> None:
	pages = {1: [
		_comment(1, "<!-- tg_phase:clarify:41 -->"),
		_comment(2, "<!-- tg_phase:plan_review:42 -->"),
		_comment(3, "<!-- tg_cleanup:43 -->"),
		_comment(4, "plain text"),
		{"id": 5, "body": None},
	]}
	_, calls = _run(tmp_path, "tg_cleanup_msgs 42", pages)
	assert _deleted(calls) == [1, 2, 3]
	assert _tg_ids(calls) == ["41", "42", "43"]


def test_breaker_stops_telegram_calls_after_three_transient_failures(tmp_path: Path) -> None:
	pages = {1: [_comment(n, f"<!-- tg_cleanup:{50 + n} -->") for n in range(1, 6)]}
	tg_modes = {str(50 + n): "500" for n in range(1, 6)}
	result, calls = _run(tmp_path, "tg_cleanup_msgs 42", pages, tg_modes=tg_modes)
	assert _tg_ids(calls) == ["51", "52", "53"]
	assert _deleted(calls) == []
	# Skipped IDs are reported apart from the three that really failed.
	assert _warnings(result) == ["::warning::Telegram cleanup kept 5 tracking comment(s) on issue #42: 3 message ID(s) "
		"hit a transient Telegram failure (2 more not tried after 3 failures in a row); the next cleanup retries them"]


def test_missing_telegram_secret_still_deletes_comments(tmp_path: Path) -> None:
	pages = {1: [_comment(1, "<!-- tg_cleanup:61 -->")]}
	_, calls = _run(tmp_path, "tg_cleanup_msgs 42", pages, TG_BOT_SECRET="")
	assert _deleted(calls) == [1]
	assert _tg_ids(calls) == []


@pytest.mark.parametrize("mode", ["500", "transport", "ok"])
def test_tg_delete_msg_always_returns_zero(tmp_path: Path, mode: str) -> None:
	result, calls = _run(tmp_path, "tg_delete_msg 71; echo RC=$?", {}, tg_modes={"71": mode})
	assert "RC=0" in result.stdout
	assert calls == ["TG deleteMessage 71 max_time=10"]
	assert result.stdout.strip().splitlines()[0] == "RC=0"


@pytest.mark.parametrize("mode,expected", [
	("ok", 0), ("not_found", 0), ("cant_delete", 0), ("500", 1), ("429", 1), ("transport", 1), ("garbage", 1),
])
def test_checked_delete_classification(tmp_path: Path, mode: str, expected: int) -> None:
	result, _ = _run(tmp_path, "rc=0; _tg_delete_msg_checked 81 || rc=$?; echo RC=$rc", {}, tg_modes={"81": mode})
	assert f"RC={expected}" in result.stdout
	# The helper prints nothing of its own.
	assert result.stdout.strip().splitlines()[0] == f"RC={expected}"
