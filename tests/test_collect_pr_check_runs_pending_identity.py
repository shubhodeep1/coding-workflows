#!/usr/bin/env python3
"""Contract tests for the CHECK_RUNS_WAIT_PENDING / CHECK_RUNS_WAIT_EXIT
diagnostics in scripts/collect_pr_check_runs_context.py.

The diagnostics name the in-flight check-runs while the review step waits, and
summarise the wait when it ends. They must not change wait semantics, the
(id, status)-only backoff equality, or the snapshot file.
"""

from __future__ import annotations

import contextlib
import email.message
import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_RUNS_HELPER = REPO_ROOT / "scripts" / "collect_pr_check_runs_context.py"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"

_MOCK_GH = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_GH_STATE_FILE"])
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
args = sys.argv[1:]
state.setdefault("calls", []).append(args)

if args[:1] == ["api"] and any("/check-runs" in arg for arg in args):
	responses = state.get("check_runs_responses", []) or []
	idx = int(state.get("check_runs_index", 0) or 0)
	if idx < len(responses):
		response = responses[idx]
		state["check_runs_index"] = idx + 1
	else:
		response = state.get("check_runs_default", {"json": [{"check_runs": []}]})
	state_path.write_text(json.dumps(state), encoding="utf-8")
	exit_code = int(response.get("exit_code", 0) or 0)
	stdout = json.dumps(response["json"]) if "json" in response else str(response.get("stdout", ""))
	stderr = str(response.get("stderr", ""))
	if stdout:
		sys.stdout.write(stdout)
	if stderr:
		sys.stderr.write(stderr + ("" if stderr.endswith("\n") else "\n"))
	sys.exit(exit_code)

state_path.write_text(json.dumps(state), encoding="utf-8")
sys.stderr.write(f"mock gh: unsupported args: {args!r}\n")
sys.exit(1)
'''


def _run_helper(
	*,
	check_runs_responses: list[dict[str, object]],
	self_run_id: str = "",
	wait_timeout_secs: str = "0",
	poll_interval_secs: str = "20",
) -> dict[str, object]:
	with tempfile.TemporaryDirectory(prefix="collect-pr-check-runs-pending-") as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		mock_gh = bin_dir / "gh"
		mock_gh.write_text(_MOCK_GH, encoding="utf-8")
		mock_gh.chmod(0o755)
		state_file = tmp / "gh_state.json"
		state_file.write_text(json.dumps({"check_runs_responses": check_runs_responses}), encoding="utf-8")
		payload_file = tmp / "pr_payload.json"
		payload_file.write_text(json.dumps({"head": {"sha": HEAD_SHA}}), encoding="utf-8")
		context_file = tmp / "pr_check_runs_context.txt"

		env = os.environ.copy()
		env.update({
			"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			"MOCK_GH_STATE_FILE": str(state_file),
			"PYTHONDONTWRITEBYTECODE": "1",
			"GH_TOKEN": "test-token",
			"GITHUB_REPOSITORY": "owner/repo",
			"PR_PAYLOAD_FILE": str(payload_file),
			"PR_CHECK_RUNS_CONTEXT_FILE": str(context_file),
			"CHECK_RUNS_AUTOFIX_ENABLED": "true",
			"CHECK_RUNS_WAIT_TIMEOUT_SECS": wait_timeout_secs,
			"CHECK_RUNS_POLL_INTERVAL_SECS": poll_interval_secs,
			"CHECK_RUNS_LOG_TAIL_BYTES": "0",
			"GH_RETRY_MAX_ATTEMPTS": "1",
			"SELF_RUN_ID": self_run_id,
			"CHECK_RUNS_EXCLUDE_SELF_FROM_CONTEXT": "false",
		})
		env.pop("PR_CHECK_RUNS_HEAD_SHA_OVERRIDE", None)
		result = subprocess.run(
			["python3", str(CHECK_RUNS_HELPER)],
			cwd=str(REPO_ROOT),
			env=env,
			capture_output=True,
			text=True,
			check=False,
		)
		return {
			"returncode": result.returncode,
			"stdout": result.stdout,
			"stderr": result.stderr,
			"context_text": context_file.read_text(encoding="utf-8"),
		}


def _lines_with_prefix(stdout: str, prefix: str) -> list[str]:
	return [line for line in stdout.splitlines() if line.startswith(prefix)]


def _checks_entries(pending_line: str) -> list[str]:
	match = re.search(r" checks=(\S+)", pending_line)
	assert match, pending_line
	return match.group(1).split(",")


def _load_module():
	spec = importlib.util.spec_from_file_location("collect_pr_check_runs_context_pending_test", CHECK_RUNS_HELPER)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def test_pending_identities_are_listed_in_id_order() -> None:
	result = _run_helper(check_runs_responses=[{"json": [{"check_runs": [
		{"id": 20, "name": "lint", "status": "queued", "app": {"slug": "github-actions"}},
		{"id": 10, "name": "unit-tests", "status": "in_progress", "app": {"slug": "github-actions"}, "started_at": "2020-01-01T00:00:00Z"},
		{"id": 5, "name": "done", "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}},
	]}]}])
	assert result["returncode"] == 0, result
	pending = _lines_with_prefix(result["stdout"], "CHECK_RUNS_WAIT_PENDING ")
	assert len(pending) == 1, result["stdout"]
	line = pending[0]
	assert line.startswith(f"CHECK_RUNS_WAIT_PENDING head_sha={HEAD_SHA} poll=1 in_flight=2 self_excluded=0 checks="), line
	entries = _checks_entries(line)
	assert len(entries) == 2
	assert re.fullmatch(r"unit-tests\|github-actions\|in_progress\|\d+", entries[0]), entries
	assert int(entries[0].rsplit("|", 1)[1]) > 0
	assert entries[1] == "lint|github-actions|queued|-"
	assert "truncated=" not in line
	assert "done|" not in line


def test_self_run_exclusion_count_is_reported() -> None:
	result = _run_helper(
		self_run_id="777",
		check_runs_responses=[{"json": [{"check_runs": [
			{"id": 1, "name": "self-review-job", "status": "in_progress", "app": {"slug": "github-actions"}, "details_url": "https://github.com/owner/repo/actions/runs/777/job/9"},
			{"id": 2, "name": "other-check", "status": "in_progress", "app": {"slug": "github-actions"}, "details_url": "https://github.com/owner/repo/actions/runs/888/job/3"},
		]}]}],
	)
	pending = _lines_with_prefix(result["stdout"], "CHECK_RUNS_WAIT_PENDING ")
	assert len(pending) == 1, result["stdout"]
	assert " in_flight=1 self_excluded=1 " in pending[0], pending[0]
	assert "self-review-job" not in pending[0]
	assert _checks_entries(pending[0]) == ["other-check|github-actions|in_progress|-"]
	# Existing line stays byte-identical and agrees with in_flight.
	assert "::warning::CHECK_RUNS_WAIT_TIMEOUT reached after 0s with 1 check-run(s) still queued/in_progress; proceeding with snapshot." in result["stdout"].splitlines()


def test_untrusted_names_are_sanitised_and_secrets_never_logged() -> None:
	long_name = "L" * 200
	result = _run_helper(check_runs_responses=[{"json": [{"check_runs": [
		{
			"id": 1,
			"name": "evil\n::error::pwned | x",
			"status": "in_progress",
			"app": {"slug": "a b::c"},
			"details_url": "https://example.com/actions/runs/1/job/2?token=SECRET123",
			"output": {"title": "TITLELEAK", "summary": "SUMMARYLEAK"},
		},
		{"id": 2, "name": long_name, "status": "queued\r\n::warning::x", "app": "not-a-dict"},
	]}]}])
	stdout = result["stdout"]
	for line in stdout.splitlines():
		if line.startswith("::"):
			assert line.startswith("::warning::CHECK_RUNS_WAIT_TIMEOUT "), line
	pending = _lines_with_prefix(stdout, "CHECK_RUNS_WAIT_PENDING ")
	assert len(pending) == 1, stdout
	line = pending[0]
	assert "::" not in line
	assert "\r" not in line and "\n" not in line
	entries = _checks_entries(line)
	assert entries[0] == "evil:_error:_pwned___x|a_b:_c|in_progress|-", entries
	name2, slug2, status2, elapsed2 = entries[1].split("|")
	assert name2 == "L" * 80 + "..."
	assert slug2 == "-"
	assert status2 == "queued:_warning:_x"
	assert elapsed2 == "-"
	for secret in ("SECRET123", "SUMMARYLEAK", "TITLELEAK", "example.com"):
		assert secret not in stdout


def test_legacy_annotation_syntax_is_neutralised() -> None:
	module = _load_module()
	assert module._sanitize_wait_field("abc##[error]pwned") == "abc#_[error]pwned"
	assert module._sanitize_wait_field("###[warning]x") == "##_[warning]x"
	result = _run_helper(check_runs_responses=[{"json": [{"check_runs": [
		{"id": 1, "name": "abc##[error]pwned", "status": "in_progress", "app": {"slug": "x##[warning]"}},
	]}]}])
	pending = _lines_with_prefix(result["stdout"], "CHECK_RUNS_WAIT_PENDING ")
	assert len(pending) == 1, result["stdout"]
	assert "##[" not in pending[0]
	assert _checks_entries(pending[0]) == ["abc#_[error]pwned|x#_[warning]|in_progress|-"]


def test_pending_list_is_truncated_above_twenty_entries() -> None:
	runs = [
		{"id": idx, "name": f"check-{idx}", "status": "queued", "app": {"slug": "ci"}}
		for idx in range(25, 0, -1)
	]
	result = _run_helper(check_runs_responses=[{"json": [{"check_runs": runs}]}])
	pending = _lines_with_prefix(result["stdout"], "CHECK_RUNS_WAIT_PENDING ")
	assert len(pending) == 1, result["stdout"]
	line = pending[0]
	assert " in_flight=25 " in line
	assert line.endswith(" truncated=5"), line
	entries = _checks_entries(line)
	assert entries == [f"check-{idx}|ci|queued|-" for idx in range(1, 21)]
	for idx in range(21, 26):
		assert f"check-{idx}|" not in line


def test_exit_line_on_ready() -> None:
	result = _run_helper(check_runs_responses=[{"json": [{"check_runs": []}]}])
	assert result["returncode"] == 0, result
	exits = _lines_with_prefix(result["stdout"], "CHECK_RUNS_WAIT_EXIT")
	assert len(exits) == 1, result["stdout"]
	assert re.fullmatch(
		rf"CHECK_RUNS_WAIT_EXIT head_sha={HEAD_SHA} status=ready polls=1 waited_s=\d+ last_in_flight=0",
		exits[0],
	), exits
	assert not _lines_with_prefix(result["stdout"], "CHECK_RUNS_WAIT_PENDING")
	assert "collection_status: ready\n" in result["context_text"]


def test_exit_line_on_timeout() -> None:
	result = _run_helper(check_runs_responses=[{"json": [{"check_runs": [
		{"id": 1, "name": "slow", "status": "in_progress", "app": {"slug": "github-actions"}},
	]}]}])
	stdout_lines = result["stdout"].splitlines()
	exits = _lines_with_prefix(result["stdout"], "CHECK_RUNS_WAIT_EXIT")
	assert len(exits) == 1, result["stdout"]
	assert re.fullmatch(
		rf"CHECK_RUNS_WAIT_EXIT head_sha={HEAD_SHA} status=timeout polls=1 waited_s=\d+ last_in_flight=1",
		exits[0],
	), exits
	assert "::warning::CHECK_RUNS_WAIT_TIMEOUT reached after 0s with 1 check-run(s) still queued/in_progress; proceeding with snapshot." in stdout_lines
	# The pending line names the stuck check on the timeout poll itself.
	pending_idx = next(i for i, line in enumerate(stdout_lines) if line.startswith("CHECK_RUNS_WAIT_PENDING "))
	timeout_idx = stdout_lines.index("::warning::CHECK_RUNS_WAIT_TIMEOUT reached after 0s with 1 check-run(s) still queued/in_progress; proceeding with snapshot.")
	assert pending_idx < timeout_idx
	assert "collection_status: timeout\n" in result["context_text"]


def test_exit_line_on_api_error() -> None:
	result = _run_helper(check_runs_responses=[{"exit_code": 1, "stderr": "mock gh: check-runs failure"}])
	exits = _lines_with_prefix(result["stdout"], "CHECK_RUNS_WAIT_EXIT")
	assert len(exits) == 1, result["stdout"]
	assert re.fullmatch(
		rf"CHECK_RUNS_WAIT_EXIT head_sha={HEAD_SHA} status=api_error polls=1 waited_s=\d+ last_in_flight=0",
		exits[0],
	), exits
	assert "collection_status: api_error\n" in result["context_text"]


def _run_in_process(module, responses: list[str], *, poll_interval: str = "20") -> dict[str, object]:
	with tempfile.TemporaryDirectory(prefix="collect-pr-check-runs-pending-inproc-") as td:
		tmp = Path(td)
		payload_file = tmp / "pr_payload.json"
		payload_file.write_text(json.dumps({"head": {"sha": HEAD_SHA}}), encoding="utf-8")
		context_file = tmp / "pr_check_runs_context.txt"
		clock = {"now": 1_700_000_000.0}
		sleeps: list[int] = []
		calls = {"n": 0}

		def _fake_api(*, repository: str, head_sha: str, script_dir: Path) -> subprocess.CompletedProcess[str]:
			idx = min(calls["n"], len(responses) - 1)
			calls["n"] += 1
			return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=responses[idx], stderr="")

		def _fake_sleep(secs: float) -> None:
			sleeps.append(int(secs))
			clock["now"] += secs

		original_env = os.environ.copy()
		original_api = module._run_check_runs_api
		original_time = module.time
		try:
			os.environ.update({
				"PR_PAYLOAD_FILE": str(payload_file),
				"PR_CHECK_RUNS_CONTEXT_FILE": str(context_file),
				"CHECK_RUNS_AUTOFIX_ENABLED": "true",
				"CHECK_RUNS_WAIT_TIMEOUT_SECS": "3600",
				"CHECK_RUNS_POLL_INTERVAL_SECS": poll_interval,
				"CHECK_RUNS_LOG_TAIL_BYTES": "0",
				"GITHUB_REPOSITORY": "owner/repo",
				"SELF_RUN_ID": "",
				"CHECK_RUNS_EXCLUDE_SELF_FROM_CONTEXT": "false",
			})
			os.environ.pop("PR_CHECK_RUNS_HEAD_SHA_OVERRIDE", None)
			module._run_check_runs_api = _fake_api
			module.time = SimpleNamespace(time=lambda: clock["now"], sleep=_fake_sleep)
			stdout = io.StringIO()
			stderr = io.StringIO()
			with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
				returncode = module.main()
		finally:
			os.environ.clear()
			os.environ.update(original_env)
			module._run_check_runs_api = original_api
			module.time = original_time
		return {
			"returncode": returncode,
			"stdout": stdout.getvalue(),
			"stderr": stderr.getvalue(),
			"sleeps": sleeps,
			"context_text": context_file.read_text(encoding="utf-8"),
		}


def _pending_snapshot(poll: int) -> str:
	# Same (id, status) every poll; name and started_at change every poll.
	return json.dumps([{"check_runs": [{
		"id": 42,
		"name": f"renamed-{poll}",
		"status": "in_progress",
		"app": {"slug": "github-actions"},
		"started_at": f"2023-11-14T22:1{poll}:00Z",
	}]}])


_COMPLETED_SNAPSHOT = json.dumps([{"check_runs": [{
	"id": 42, "name": "renamed-final", "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"},
}]}])


def test_backoff_still_escalates_when_only_elapsed_time_and_names_change() -> None:
	module = _load_module()
	result = _run_in_process(module, [_pending_snapshot(p) for p in range(1, 5)] + [_COMPLETED_SNAPSHOT])
	assert result["returncode"] == 0, result
	# poll_interval=20: unchanged snapshots escalate 20 -> 40 -> 80 (x4 cap).
	assert result["sleeps"] == [20, 40, 80, 80], result["sleeps"]
	waiting = [line for line in result["stdout"].splitlines() if line.startswith("Waiting for ")]
	assert len(waiting) == 4
	assert waiting[0].startswith(f"Waiting for 1 in-progress/queued check-run(s) on {HEAD_SHA} (sleep 20s, deadline in ")
	assert waiting[1].startswith(f"Waiting for 1 in-progress/queued check-run(s) on {HEAD_SHA} (sleep 40s, deadline in ")
	pending = _lines_with_prefix(result["stdout"], "CHECK_RUNS_WAIT_PENDING ")
	assert [re.search(r" poll=(\d+) ", line).group(1) for line in pending] == ["1", "2", "3", "4"]
	assert "renamed-1|github-actions|in_progress|" in pending[0]
	assert "renamed-4|github-actions|in_progress|" in pending[3]
	exits = _lines_with_prefix(result["stdout"], "CHECK_RUNS_WAIT_EXIT")
	assert len(exits) == 1
	assert re.fullmatch(
		rf"CHECK_RUNS_WAIT_EXIT head_sha={HEAD_SHA} status=ready polls=5 waited_s=220 last_in_flight=0",
		exits[0],
	), exits
	assert "collection_status: ready\n" in result["context_text"]


def test_diagnostics_fail_open_without_changing_status_or_snapshot() -> None:
	responses = [_pending_snapshot(1), _COMPLETED_SNAPSHOT]
	baseline = _run_in_process(_load_module(), responses)

	module = _load_module()

	def _boom(*args, **kwargs):
		raise RuntimeError("diagnostics boom")

	module._build_wait_identity = _boom
	module._format_wait_pending_line = _boom
	module._sanitize_wait_field = _boom
	patched = _run_in_process(module, responses)

	assert patched["returncode"] == 0, patched
	assert "wait diagnostics skipped (RuntimeError)" in patched["stderr"]
	assert not _lines_with_prefix(patched["stdout"], "CHECK_RUNS_WAIT_PENDING")
	assert not _lines_with_prefix(patched["stdout"], "CHECK_RUNS_WAIT_EXIT")
	assert patched["sleeps"] == baseline["sleeps"] == [20]
	assert patched["context_text"] == baseline["context_text"]
	assert "collection_status: ready\n" in patched["context_text"]


# --- Issue #8799: log_tail window selection ---------------------------------
# The fixture follows shubhodeep1/fun-token-multi-chain job 125140539489: the
# cause line and the step's ``##[error]`` line are followed by ~35 KB of
# Postgres service-container teardown output, so the last 16 KiB of the log hold
# no failure detail at all. The window must end at the step error instead.
# These tests live here because this file is already wired into ci.yml.

HELPER = REPO_ROOT / "scripts" / "collect_pr_check_runs_context.py"
REPO = "octo/repo"
DETAILS_URL = f"https://github.com/{REPO}/actions/runs/41403592424/job/125140539489"
CAUSE = "TransactionExecutionError: Transaction reverted: trying to deploy a contract whose code is too large"
STEP_ERROR = "##[error]Process completed with exit code 1."
FIRST_PG_LINE_KEY = "Key (id)=(0) already exists."


def _load():
	spec = importlib.util.spec_from_file_location("collect_pr_check_runs_context_8799", HELPER)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


mod = _load()


def _ts(n: int) -> str:
	return f"2026-11-01T10:{(n // 60) % 60:02d}:{n % 60:02d}.0000000Z "


def build_job_log(*, padding_lines: int = 0, with_step_error: bool = True) -> bytes:
	lines: list[str] = []
	for n in range(padding_lines):
		lines.append(_ts(n) + f"padding line {n:06d} " + "p" * 60)
	lines += [_ts(1) + "##[group]Run npm test", _ts(1) + "##[endgroup]"]
	lines += [_ts(2) + f" ✓ test/unit_{n}.test.ts (3 tests) 12ms" for n in range(40)]
	lines.append(_ts(3) + " Test Files  8 passed (8)")
	lines += [_ts(4) + "##[group]Run npm run smoke", _ts(4) + "##[endgroup]"]
	lines.append(_ts(5) + CAUSE)
	long_line = _ts(5) + "Error: could not deploy (code=CALL_EXCEPTION, data=0x"
	lines.append(long_line + "a" * (65000 - len(long_line)))
	if with_step_error:
		lines.append(_ts(6) + STEP_ERROR)
	lines += [
		_ts(7) + "Post job cleanup.",
		_ts(7) + "Stop and remove container: 4f1c",
		_ts(7) + "##[command]/usr/bin/docker logs --details 4f1c",
		_ts(7) + "PostgreSQL init process complete; ready for start up.",
		_ts(7) + "listening on IPv4 address \"0.0.0.0\", port 5432",
		_ts(7) + "database system is ready to accept connections",
	]
	n = 0
	while sum(len(line) + 1 for line in lines) < 35000 + 65000 + 8000 + padding_lines * 100:
		lines.append(
			_ts(8) + f" 2026-11-01 10:00:08.000 UTC [61] ERROR:  duplicate key value violates unique constraint "
			f"\"users_pkey\" DETAIL:  Key (id)=({n}) already exists."
		)
		n += 1
	return ("\n".join(lines) + "\n").encode("utf-8")


class _LogServer:
	"""Serves HEAD and Range GET like the signed job-log blob URL."""

	def __init__(self, data: bytes):
		self.data = data
		self.requests: list[tuple[str, str]] = []
		server_ref = self

		class Handler(BaseHTTPRequestHandler):
			def log_message(self, *args):
				return

			def do_HEAD(self):
				server_ref.requests.append(("HEAD", ""))
				self.send_response(200)
				self.send_header("Content-Length", str(len(server_ref.data)))
				self.end_headers()

			def do_GET(self):
				range_header = self.headers.get("Range") or ""
				server_ref.requests.append(("GET", range_header))
				start_s, end_s = range_header.removeprefix("bytes=").split("-")
				chunk = server_ref.data[int(start_s):int(end_s) + 1]
				self.send_response(206)
				self.send_header("Content-Length", str(len(chunk)))
				self.end_headers()
				self.wfile.write(chunk)

		self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
		self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/blob"
		self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

	def __enter__(self):
		self.thread.start()
		return self

	def __exit__(self, *exc):
		self.httpd.shutdown()
		self.httpd.server_close()


class _RedirectOpener:
	def __init__(self, location: str):
		self.location = location
		self.calls = 0

	def open(self, req, timeout=None):
		self.calls += 1
		headers = email.message.Message()
		headers["Location"] = self.location
		raise urllib.error.HTTPError(req.full_url, 302, "Found", headers, None)


@pytest.fixture
def local_http(monkeypatch):
	for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
		monkeypatch.delenv(name, raising=False)
	monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
	monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
	monkeypatch.setattr(urllib.request, "_opener", None)


def _fetch(monkeypatch, data: bytes, log_tail_bytes: int = mod.DEFAULT_LOG_TAIL_BYTES):
	with _LogServer(data) as server:
		opener = _RedirectOpener(server.url)
		monkeypatch.setattr(mod, "_LOG_REDIRECT_OPENER", opener)
		result = mod._fetch_log_tail(details_url=DETAILS_URL, log_tail_bytes=log_tail_bytes, repository=REPO, token="t")
	return result, opener.calls + len(server.requests), server.requests


def _old_tail(data: bytes, log_tail_bytes: int) -> str:
	"""The pre-#8799 selection: last log_tail_bytes bytes, last 200 lines."""
	out = data[-log_tail_bytes:]
	lines = out.decode("utf-8", errors="replace").splitlines()
	return "\n".join(lines[-mod.LOG_TAIL_LINES:])


def test_window_ends_at_the_step_error_not_the_teardown_dump(monkeypatch, local_http):
	data = build_job_log(padding_lines=2500)
	assert len(data) > mod.LOG_SCAN_BYTES
	result, request_count, requests = _fetch(monkeypatch, data)
	assert request_count == 3
	assert requests[-1] == ("GET", f"bytes={len(data) - mod.LOG_SCAN_BYTES - 1}-{len(data) - 1}")
	assert result.startswith("log_tail_anchor: step_error\n")
	body = result.split("\n", 1)[1]
	assert CAUSE in body and STEP_ERROR in body
	assert "duplicate key" not in body
	assert "[… 63000 chars truncated]" in body
	assert len(body.encode("utf-8")) <= mod.DEFAULT_LOG_TAIL_BYTES
	assert len(body.split("\n")) <= mod.LOG_TAIL_LINES
	# Up to five lines after the step error are kept.
	assert body.split("\n")[-1].endswith('listening on IPv4 address "0.0.0.0", port 5432')


def test_small_log_is_fetched_from_byte_zero(monkeypatch, local_http):
	data = build_job_log()
	assert len(data) < mod.LOG_SCAN_BYTES
	result, request_count, requests = _fetch(monkeypatch, data)
	assert request_count == 3
	assert requests[-1] == ("GET", f"bytes=0-{len(data) - 1}")
	assert result.startswith("log_tail_anchor: step_error\n") and CAUSE in result


def test_log_without_step_error_keeps_the_old_tail(monkeypatch, local_http):
	lines = [_ts(n) + f"ok line {n} " + "z" * 50 for n in range(3000)]
	data = ("\n".join(lines) + "\n").encode("utf-8")
	result, request_count, _ = _fetch(monkeypatch, data)
	assert request_count == 3
	assert result == "log_tail_anchor: end_of_log\n" + _old_tail(data, mod.DEFAULT_LOG_TAIL_BYTES)


def _boundary_log(*, aligned: bool) -> bytes:
	"""A log whose scan (last LOG_SCAN_BYTES bytes) starts with the only step error."""
	body = (_ts(6) + STEP_ERROR + "\n").encode("utf-8")
	filler = (_ts(8) + "teardown " + "t" * 80 + "\n").encode("utf-8")
	while len(body) + len(filler) <= mod.LOG_SCAN_BYTES:
		body += filler
	body += b"x" * (mod.LOG_SCAN_BYTES - len(body) - 1) + b"\n"
	assert len(body) == mod.LOG_SCAN_BYTES
	prefix = b"cause line before the scan\n" if aligned else b"cut mid-line "
	return prefix + body


def test_scan_starting_on_a_line_boundary_keeps_the_first_line_as_anchor(monkeypatch, local_http):
	data = _boundary_log(aligned=True)
	result, request_count, requests = _fetch(monkeypatch, data)
	assert request_count == 3
	assert requests[-1] == ("GET", f"bytes={len(data) - mod.LOG_SCAN_BYTES - 1}-{len(data) - 1}")
	assert result.startswith("log_tail_anchor: step_error\n") and STEP_ERROR in result


def test_scan_starting_mid_line_still_skips_the_fragment(monkeypatch, local_http):
	data = _boundary_log(aligned=False)
	result, request_count, _ = _fetch(monkeypatch, data)
	assert request_count == 3
	assert result.startswith("log_tail_anchor: end_of_log\n") and STEP_ERROR not in result


def test_zero_budget_returns_empty_without_requests(monkeypatch, local_http):
	result, request_count, _ = _fetch(monkeypatch, build_job_log(), log_tail_bytes=0)
	assert result == "" and request_count == 0


def test_cap_long_lines():
	limit = mod.LOG_LINE_CHAR_CAP
	assert mod._cap_long_lines(["a" * limit, "b"]) == ["a" * limit, "b"]
	capped = mod._cap_long_lines(["c" * (limit + 7)])
	assert capped == ["c" * limit + " [… 7 chars truncated]"]
	source = ["d" * (limit + 1)]
	mod._cap_long_lines(source)
	assert source == ["d" * (limit + 1)]


def test_select_window_respects_budgets():
	data = build_job_log(padding_lines=50)
	result = mod._select_log_tail_window(data, log_tail_bytes=4096, partial_start=False)
	body = result.split("\n", 1)[1]
	assert result.startswith("log_tail_anchor: step_error\n")
	assert len(body.encode("utf-8")) <= 4096 and STEP_ERROR in body
	assert len(body.split("\n")) <= mod.LOG_TAIL_LINES
	lines_only = "\n".join(f"line {n}" for n in range(500)) + "\n" + STEP_ERROR
	result = mod._select_log_tail_window(lines_only.encode(), log_tail_bytes=100000, partial_start=False)
	assert len(result.split("\n", 1)[1].split("\n")) == mod.LOG_TAIL_LINES


def test_partial_first_line_is_never_the_anchor():
	data = b"##[error]fragment of a cut line\nnext line\nlast line"
	assert mod._select_log_tail_window(data, log_tail_bytes=1000, partial_start=True).startswith("log_tail_anchor: end_of_log\n")
	assert mod._select_log_tail_window(data, log_tail_bytes=1000, partial_start=False).startswith("log_tail_anchor: step_error\n")


def test_tiny_budget_keeps_the_step_error_line():
	data = ("before\n" + _ts(6) + STEP_ERROR + "\nafter\n").encode("utf-8")
	result = mod._select_log_tail_window(data, log_tail_bytes=64, partial_start=False)
	body = result.split("\n", 1)[1]
	assert STEP_ERROR in body and len(body.encode("utf-8")) <= 64
	assert mod._select_log_tail_window(b"", log_tail_bytes=64, partial_start=False) == ""


def test_end_of_log_long_trailing_line_is_capped_before_the_byte_budget():
	early = [f"context line {n}" for n in range(300)]
	data = ("\n".join(early + ["x" * 14000]) + "\n").encode("utf-8")
	result = mod._select_log_tail_window(data, log_tail_bytes=mod.DEFAULT_LOG_TAIL_BYTES, partial_start=False)
	assert result.startswith("log_tail_anchor: end_of_log\n")
	body = result.split("\n", 1)[1]
	assert len(body.encode("utf-8")) <= mod.DEFAULT_LOG_TAIL_BYTES
	assert len(body.split("\n")) == mod.LOG_TAIL_LINES
	assert body.endswith("[… 12000 chars truncated]")
	assert "context line 299" in body and "context line 120" in body


def test_partial_first_line_is_not_kept_as_lead_context():
	data = b"fragment-of-cut-line\nwhole line\n" + STEP_ERROR.encode() + b"\n"
	body = mod._select_log_tail_window(data, log_tail_bytes=1000, partial_start=True).split("\n", 1)[1]
	assert "fragment-of-cut-line" not in body
	assert body.startswith("whole line\n") and STEP_ERROR in body


def test_credentials_are_redacted_before_the_line_cut():
	long_line = "x" * 1985 + " ghp_" + "Zq9" * 12 + " " + "y" * 3000
	data = ("step\n" + long_line + "\n" + STEP_ERROR + "\n").encode("utf-8")
	result = mod._select_log_tail_window(data, log_tail_bytes=8000, partial_start=False)
	assert "ghp_[redacted]" in result and "Zq9" not in result


def test_encoded_access_token_is_redacted():
	import base64 as b64
	encoded = b64.b64encode(b"x-access-token:" + b"Q7" * 20).decode()
	data = ("config " + encoded + "\n" + STEP_ERROR + "\n").encode("utf-8")
	result = mod._select_log_tail_window(data, log_tail_bytes=8000, partial_start=False)
	assert encoded not in result and "config [redacted]" in result
	# Plain hex runs (SHAs, initcode) stay readable.
	assert mod._redact_log_text("0x" + "ab" * 40) == "0x" + "ab" * 40


def test_redaction_never_swallows_the_next_lines_step_error():
	data = ("x-header extraheader:\n" + _ts(6) + STEP_ERROR + "\nauth Bearer\nafter\n").encode("utf-8")
	result = mod._select_log_tail_window(data, log_tail_bytes=8000, partial_start=False)
	assert result.startswith("log_tail_anchor: step_error\n") and STEP_ERROR in result
	assert mod._redact_log_text("extraheader: AUTHORIZATION: basic abcdefgh") == "extraheader: [redacted]"


# --- Issue #9318: prefer the runner's step-exit line as the anchor ----------
FIXTURE_ERROR = "##[error][lint_pr_body_auto_close] PR body:line 1 — auto-close keyword 'Fixes' references #2734."


def test_window_prefers_the_step_exit_line_over_earlier_fixture_errors():
	lines = [_ts(1) + "##[group]Run python3 tests/test_lint_pr_body_auto_close.py"]
	lines.append(_ts(1) + FIXTURE_ERROR)
	lines += [_ts(2) + f"PASS  test_lint_case_{n:03d}" for n in range(60)]
	lines += [_ts(3) + "##[group]Run python3 tests/test_failing_step.py", _ts(3) + "AssertionError: boom"]
	lines.append(_ts(4) + STEP_ERROR)
	lines += [_ts(5) + f"teardown line {n}" for n in range(20)]
	data = ("\n".join(lines) + "\n").encode("utf-8")
	result = mod._select_log_tail_window(data, log_tail_bytes=512, partial_start=False)
	assert result.startswith("log_tail_anchor: step_error\n")
	body_lines = result.split("\n", 1)[1].split("\n")
	body = "\n".join(body_lines)
	assert "AssertionError: boom" in body and STEP_ERROR in body
	assert "[lint_pr_body_auto_close]" not in body
	exit_idx = next(i for i, line in enumerate(body_lines) if STEP_ERROR in line)
	assert len(body_lines) - 1 - exit_idx <= mod.LOG_TAIL_STEP_ERROR_AFTER_LINES
	assert len(body.encode("utf-8")) <= 512


def test_window_falls_back_to_first_error_without_a_step_exit_line():
	lines = ["context"] + ["##[error]first failure"] + [f"after {n}" for n in range(10)] + ["##[error]second failure", "tail"]
	data = ("\n".join(lines) + "\n").encode("utf-8")
	result = mod._select_log_tail_window(data, log_tail_bytes=8000, partial_start=False)
	assert result.startswith("log_tail_anchor: step_error\n")
	body_lines = result.split("\n", 1)[1].split("\n")
	assert "##[error]first failure" in body_lines
	assert "##[error]second failure" not in body_lines
	first_idx = body_lines.index("##[error]first failure")
	assert len(body_lines) - 1 - first_idx <= mod.LOG_TAIL_STEP_ERROR_AFTER_LINES
	assert body_lines[-1] == "after 4"


def test_partial_first_line_step_exit_is_never_the_anchor():
	data = b"##[error]Process completed with exit code 1.\nnext\n##[error]other\nlast"
	partial = mod._select_log_tail_window(data, log_tail_bytes=1000, partial_start=True)
	assert partial.startswith("log_tail_anchor: step_error\n")
	partial_body = partial.split("\n", 1)[1]
	assert partial_body == "next\n##[error]other\nlast"
	whole = mod._select_log_tail_window(data, log_tail_bytes=1000, partial_start=False)
	assert whole.startswith("log_tail_anchor: step_error\n")
	assert whole.split("\n", 1)[1] == "##[error]Process completed with exit code 1.\nnext\n##[error]other\nlast"
	assert mod._select_log_tail_window(
		b"##[error]other\n##[error]Process completed with exit code 2.\nafter",
		log_tail_bytes=1000,
		partial_start=False,
	).split("\n", 1)[1] == "##[error]other\n##[error]Process completed with exit code 2.\nafter"


def test_first_of_several_step_exit_lines_is_the_anchor():
	second = "##[error]Process completed with exit code 2."
	lines = ["cause one", STEP_ERROR] + [f"between {n}" for n in range(10)] + ["cause two", second, "tail"]
	data = ("\n".join(lines) + "\n").encode("utf-8")
	result = mod._select_log_tail_window(data, log_tail_bytes=8000, partial_start=False)
	assert result.startswith("log_tail_anchor: step_error\n")
	body_lines = result.split("\n", 1)[1].split("\n")
	assert second not in body_lines and "cause one" in body_lines
	first_idx = body_lines.index(STEP_ERROR)
	assert len(body_lines) - 1 - first_idx <= mod.LOG_TAIL_STEP_ERROR_AFTER_LINES


if __name__ == "__main__":
	import pytest

	raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
