#!/usr/bin/env python3
"""Tests for the bounded memory-branch read/clone retry (issue #10052).

Run 45408016593 lost an `/approved` claim to one bare `returned error: 403`
on the `ai-memory` clone: GitHub throttled the token for a few seconds. The
write-path clone in `persist_memory_operation` and the clone in
`read_memory_root_from_branch` now retry a bare 403, a 429 and network errors
up to 3 attempts in total, while credential/permission failures still fail at
once. The claim stays exactly-once: only the clone is repeated.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "ai_memory_lib.py"

if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("ai_memory_lib", MODULE_PATH)
assert spec is not None and spec.loader is not None
ai_memory_lib = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ai_memory_lib
spec.loader.exec_module(ai_memory_lib)

# The claim CLI binds its ai_memory_lib names at import time, so load it now,
# while sys.modules["ai_memory_lib"] is this file's module instance.
CLI_MODULE_PATH = REPO_ROOT / "scripts" / "ai_memory.py"
cli_spec = importlib.util.spec_from_file_location("ai_memory_read_retry_cli", CLI_MODULE_PATH)
assert cli_spec is not None and cli_spec.loader is not None
ai_memory_cli = importlib.util.module_from_spec(cli_spec)
cli_spec.loader.exec_module(ai_memory_cli)


@pytest.fixture(autouse=True)
def _reset_write_clone_exhausted_marker():
	ai_memory_lib._memory_write_clone_exhausted_at = None
	yield
	ai_memory_lib._memory_write_clone_exhausted_at = None


FAKE_TOKEN = "ghs_FAKEtoken10052abcdef"
TOKEN_URL = f"https://x-access-token:{FAKE_TOKEN}@github.com/o/r"

BARE_403_STDERR = "fatal: unable to access 'https://github.com/o/r/': The requested URL returned error: 403"
PERMISSION_DENIED_STDERR = (
	"remote: Permission to o/r.git denied to some-bot.\n"
	"fatal: unable to access 'https://github.com/o/r/': The requested URL returned error: 403"
)
USERNAME_STDERR = "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
HTTP_401_STDERR = "fatal: unable to access 'https://github.com/o/r/': The requested URL returned error: 401"
HTTP_429_STDERR = "fatal: unable to access 'https://github.com/o/r/': The requested URL returned error: 429"
NETWORK_STDERR = "fatal: unable to access 'https://github.com/o/r/': Could not resolve host: github.com"
MISSING_BRANCH_STDERR = "warning: Could not find remote branch ai-memory to clone.\nfatal: Remote branch ai-memory not found in upstream origin"


class _FakeProc:
	"""Minimal stand-in for subprocess.CompletedProcess used by _run_git."""

	def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
		self.returncode = returncode
		self.stdout = stdout
		self.stderr = stderr


@contextlib.contextmanager
def _patched_module_attrs(module, **replacements):
	originals = {name: getattr(module, name) for name in replacements}
	try:
		for name, value in replacements.items():
			setattr(module, name, value)
		yield
	finally:
		for name, value in originals.items():
			setattr(module, name, value)


def _write_clone_error(stderr: str) -> str:
	"""The message `_run_git(check=True)` raises for a failed full clone."""
	return (
		f"git clone --no-tags --quiet {TOKEN_URL} /tmp/ai-memory-branch-x failed with exit 128\n"
		f"stdout:\n\n"
		f"stderr:\n{stderr}"
	)


def _telemetry_lines(stderr: str) -> list[dict]:
	prefix = "AI_MEMORY_TELEMETRY: "
	return [json.loads(line[len(prefix):]) for line in stderr.splitlines() if line.startswith(prefix)]


def _run_persist(clone_failures: list[str], *, operation=None):
	"""Drive persist_memory_operation; the clone raises each failure, then succeeds.

	Returns (result, exc, counters).
	"""
	counters = {"clone": 0, "push": 0, "operation": 0, "sleep": 0}
	failures = list(clone_failures)

	def fake_clone(_repo_root, _memory_branch):
		counters["clone"] += 1
		if failures:
			raise ai_memory_lib.MemoryGitError(_write_clone_error(failures.pop(0)))
		return Path(tempfile.mkdtemp(prefix="ai-memory-read-retry-test-"))

	def fake_run_git(cwd, args, check: bool = True, **_kwargs):  # noqa: ANN001 - test stub
		op = args[0] if args else ""
		if op == "diff":
			return _FakeProc(returncode=1)
		if op == "rev-parse":
			return _FakeProc(returncode=0, stdout="0123456789abcdef\n")
		if op == "push":
			counters["push"] += 1
			return _FakeProc(returncode=0)
		return _FakeProc(returncode=0)

	def counted_operation(clone_dir):
		counters["operation"] += 1
		if operation is not None:
			return operation(clone_dir)
		return {"value": 1}

	def fake_sleep(_seconds):
		counters["sleep"] += 1

	with _patched_module_attrs(
		ai_memory_lib,
		_clone_for_memory_branch=fake_clone,
		_run_git=fake_run_git,
	), _patched_module_attrs(ai_memory_lib.time, sleep=fake_sleep):
		try:
			result = ai_memory_lib.persist_memory_operation(
				REPO_ROOT,
				memory_branch="ai-memory",
				memory_root_relative="ai-memory",
				push_retries=16,
				commit_message="ai-memory: read retry test",
				operation=counted_operation,
			)
			return result, None, counters
		except ai_memory_lib.MemoryGitError as exc:
			return None, exc, counters


def test_claim_succeeds_after_one_bare_403(monkeypatch, capsys) -> None:
	monkeypatch.setenv("GH_TOKEN", FAKE_TOKEN)

	def claim(clone_dir):
		memory_root = ai_memory_lib.resolve_memory_root_dir(clone_dir, "ai-memory")
		entry, claimed = ai_memory_lib.claim_processed_command(
			memory_root,
			issue_number=10048,
			comment_id=123456,
			command="/approved",
			workflow="implement",
			actor="someone",
			run_id="45408016593",
			run_attempt=1,
		)
		entries = [
			path
			for path in memory_root.rglob("*.json")
			if entry["entry_id"] in path.read_text(encoding="utf-8")
		]
		return {"claimed": claimed, "entries": len(entries)}

	result, exc, counters = _run_persist([BARE_403_STDERR], operation=claim)
	assert exc is None, exc
	assert counters["clone"] == 2
	assert counters["operation"] == 1
	assert counters["push"] == 1
	assert counters["sleep"] == 1
	assert result["operation_result"] == {"claimed": True, "entries": 1}
	stderr = capsys.readouterr().err
	lines = _telemetry_lines(stderr)
	assert len(lines) == 1
	assert lines[0]["op"] == "memory-read-retry"
	assert lines[0]["retry_reason"] == "auth"
	assert lines[0]["target"] == "write-clone"
	assert lines[0]["attempt"] == 1
	assert lines[0]["max_attempts"] == 3
	assert "https://" not in stderr
	assert FAKE_TOKEN not in stderr


def test_persistent_403_fails_after_three_attempts_without_url(monkeypatch) -> None:
	monkeypatch.setenv("GH_TOKEN", FAKE_TOKEN)
	result, exc, counters = _run_persist([BARE_403_STDERR] * 5)
	assert result is None
	assert exc is not None
	assert counters["clone"] == 3
	assert counters["sleep"] == 2
	assert counters["operation"] == 0
	assert counters["push"] == 0
	message = str(exc)
	assert message.startswith("git clone")
	assert "returned error: 403" in message
	assert "https://" not in message
	assert "github.com/o/r" not in message
	assert FAKE_TOKEN not in message


@pytest.mark.parametrize("stderr", [PERMISSION_DENIED_STDERR, USERNAME_STDERR, HTTP_401_STDERR])
def test_permanent_failures_are_not_retried(stderr) -> None:
	result, exc, counters = _run_persist([stderr] * 3)
	assert result is None
	assert exc is not None
	assert counters["clone"] == 1
	assert counters["sleep"] == 0
	assert counters["operation"] == 0


@pytest.mark.parametrize("stderr", [HTTP_429_STDERR, NETWORK_STDERR])
def test_transient_failures_are_retried(stderr) -> None:
	result, exc, counters = _run_persist([stderr])
	assert exc is None, exc
	assert counters["clone"] == 2
	assert counters["sleep"] == 1
	assert counters["operation"] == 1


def test_missing_branch_is_not_retried() -> None:
	result, exc, counters = _run_persist([MISSING_BRANCH_STDERR] * 3)
	assert result is None
	assert exc is not None
	assert counters["clone"] == 1
	assert counters["sleep"] == 0


def test_success_path_writes_nothing_to_stderr(capsys) -> None:
	result, exc, counters = _run_persist([])
	assert exc is None, exc
	assert counters["clone"] == 1
	assert counters["sleep"] == 0
	assert capsys.readouterr().err == ""


def test_transient_predicate_classes() -> None:
	predicate = ai_memory_lib._is_transient_memory_read_failure
	assert predicate(BARE_403_STDERR)
	assert predicate(HTTP_429_STDERR)
	assert predicate(NETWORK_STDERR)
	assert predicate("remote: You have exceeded a secondary rate limit.\n" + BARE_403_STDERR)
	assert not predicate(PERMISSION_DENIED_STDERR)
	assert not predicate("remote: Permission to o/rate-limiter.git denied to bot.\n" + BARE_403_STDERR)
	assert not predicate(USERNAME_STDERR)
	assert not predicate(HTTP_401_STDERR)
	assert not predicate(MISSING_BRANCH_STDERR)
	assert not predicate("")
	assert not predicate("   ")


def _run_read(clone_script: list[tuple[int, str]]):
	"""Drive read_memory_root_from_branch with a scripted clone; return (path, exc, clones, sleeps)."""
	clone_results = list(clone_script)
	counters = {"clone": 0, "sleep": 0}

	def fake_run_git(cwd, args, check: bool = True, **_kwargs):  # noqa: ANN001 - test stub
		if args and args[0] == "clone":
			counters["clone"] += 1
			rc, stderr = clone_results.pop(0) if clone_results else (0, "")
			if rc == 0:
				Path(args[-1]).mkdir(parents=True, exist_ok=True)
			return _FakeProc(returncode=rc, stderr=stderr)
		return _FakeProc(returncode=0)

	def fake_sleep(_seconds):
		counters["sleep"] += 1

	with _patched_module_attrs(
		ai_memory_lib,
		_run_git=fake_run_git,
		_resolve_origin_url=lambda _repo_root: TOKEN_URL,
	), _patched_module_attrs(ai_memory_lib.time, sleep=fake_sleep):
		try:
			path = ai_memory_lib.read_memory_root_from_branch(
				REPO_ROOT,
				memory_branch="ai-memory",
				memory_root_relative="ai-memory",
			)
			return path, None, counters
		except ai_memory_lib.MemoryGitError as exc:
			return None, exc, counters


def test_read_retries_bare_403_then_succeeds(capsys) -> None:
	path, exc, counters = _run_read([(128, BARE_403_STDERR), (0, "")])
	try:
		assert exc is None, exc
		assert path is not None and Path(path).is_dir()
		assert counters["clone"] == 2
		assert counters["sleep"] == 1
		lines = _telemetry_lines(capsys.readouterr().err)
		assert [line["target"] for line in lines] == ["read-clone"]
	finally:
		if path is not None:
			shutil.rmtree(path, ignore_errors=True)


def test_read_persistent_403_keeps_message_prefix_without_url(monkeypatch) -> None:
	monkeypatch.setenv("GH_TOKEN", FAKE_TOKEN)
	path, exc, counters = _run_read([(128, BARE_403_STDERR)] * 5)
	assert path is None
	assert exc is not None
	assert counters["clone"] == 3
	assert counters["sleep"] == 2
	message = str(exc)
	assert message.startswith("Unable to read memory branch ai-memory:")
	assert "returned error: 403" in message
	assert "https://" not in message
	assert FAKE_TOKEN not in message


def test_read_permission_denied_is_not_retried() -> None:
	path, exc, counters = _run_read([(128, PERMISSION_DENIED_STDERR)] * 3)
	assert path is None
	assert exc is not None
	assert counters["clone"] == 1
	assert counters["sleep"] == 0


def test_full_clone_failure_removes_temp_dir() -> None:
	created: list[Path] = []
	real_mkdtemp = tempfile.mkdtemp

	def tracking_mkdtemp(*args, **kwargs):
		path = real_mkdtemp(*args, **kwargs)
		created.append(Path(path))
		return path

	def fake_run_git(cwd, args, check: bool = True, **_kwargs):  # noqa: ANN001 - test stub
		if args and args[0] in {"clone", "ls-remote"}:
			if check:
				raise ai_memory_lib.MemoryGitError(_write_clone_error(BARE_403_STDERR))
			return _FakeProc(returncode=128, stderr=BARE_403_STDERR)
		return _FakeProc(returncode=0)

	with _patched_module_attrs(
		ai_memory_lib,
		_run_git=fake_run_git,
		_resolve_origin_url=lambda _repo_root: TOKEN_URL,
	), _patched_module_attrs(ai_memory_lib.tempfile, mkdtemp=tracking_mkdtemp):
		with pytest.raises(ai_memory_lib.MemoryGitError):
			ai_memory_lib._clone_for_memory_branch(REPO_ROOT, "ai-memory")
	assert created
	assert not any(path.exists() for path in created)


def test_backoff_accepts_read_retry_bounds() -> None:
	for _ in range(200):
		assert 0.0 <= ai_memory_lib._push_retry_backoff_seconds(1, base_seconds=5.0, cap_seconds=20.0) <= 5.0
		assert 0.0 <= ai_memory_lib._push_retry_backoff_seconds(2, base_seconds=5.0, cap_seconds=20.0) <= 10.0
		assert 0.0 <= ai_memory_lib._push_retry_backoff_seconds(9, base_seconds=5.0, cap_seconds=20.0) <= 20.0
		assert 0.0 <= ai_memory_lib._push_retry_backoff_seconds(1) <= ai_memory_lib._PUSH_RETRY_BACKOFF_BASE_SECONDS
		assert 0.0 <= ai_memory_lib._push_retry_backoff_seconds(99) <= ai_memory_lib._PUSH_RETRY_BACKOFF_CAP_SECONDS


def _run_clone_with_script(handler):
	"""Drive _clone_for_memory_branch with a scripted _run_git; return (path, exc, created, calls)."""
	created: list[Path] = []
	calls: list[list[str]] = []
	real_mkdtemp = tempfile.mkdtemp

	def tracking_mkdtemp(*args, **kwargs):
		path = real_mkdtemp(*args, **kwargs)
		created.append(Path(path))
		return path

	def fake_run_git(cwd, args, check: bool = True, **_kwargs):  # noqa: ANN001 - test stub
		calls.append(list(args))
		rc, stdout, stderr = handler(args)
		if rc == 0 and args and args[0] == "clone":
			Path(args[-1]).mkdir(parents=True, exist_ok=True)
		if rc != 0 and check:
			raise ai_memory_lib.MemoryGitError(f"git {' '.join(args[:2])} failed with exit {rc}\nstderr:\n{stderr}")
		return _FakeProc(returncode=rc, stdout=stdout, stderr=stderr)

	with _patched_module_attrs(
		ai_memory_lib,
		_run_git=fake_run_git,
		_resolve_origin_url=lambda _repo_root: TOKEN_URL,
	), _patched_module_attrs(ai_memory_lib.tempfile, mkdtemp=tracking_mkdtemp):
		try:
			return ai_memory_lib._clone_for_memory_branch(REPO_ROOT, "ai-memory"), None, created, calls
		except ai_memory_lib.MemoryGitError as exc:
			return None, exc, created, calls


def test_full_clone_failed_heads_probe_is_not_branch_absent() -> None:
	# The first probe (against the origin URL) fails, so the full clone runs;
	# its `ls-remote --heads origin` probe then fails with a bare 403. That
	# must raise (so the read retry applies), not check out the default
	# branch as a fresh memory branch.
	def handler(args):
		if args[0] == "ls-remote":
			return 128, "", BARE_403_STDERR
		return 0, "", ""

	path, exc, created, calls = _run_clone_with_script(handler)
	assert path is None
	assert exc is not None
	assert "returned error: 403" in str(exc)
	assert ai_memory_lib._is_transient_memory_read_failure(str(exc))
	assert not any(call[0] == "checkout" for call in calls)
	assert created
	assert not any(p.exists() for p in created)


def test_fast_clone_checkout_failure_removes_temp_dir() -> None:
	def handler(args):
		if args[0] == "ls-remote":
			return 0, "0123456789abcdef\trefs/heads/ai-memory\n", ""
		if args[0] == "checkout":
			return 128, "", "fatal: checkout failed"
		return 0, "", ""

	path, exc, created, calls = _run_clone_with_script(handler)
	assert path is None
	assert exc is not None
	assert [call[0] for call in calls].count("clone") == 1
	assert created
	assert not any(p.exists() for p in created)


def test_read_after_exhausted_write_clone_is_skipped(monkeypatch) -> None:
	# The claim's recovery duplicate-check runs read_memory_root_from_branch
	# right after the write clone used its 3 attempts: it must not add 3 more.
	monkeypatch.setenv("GH_TOKEN", FAKE_TOKEN)
	_result, exc, counters = _run_persist([BARE_403_STDERR] * 5)
	assert exc is not None
	assert counters["clone"] == 3
	path, read_exc, read_counters = _run_read([(128, BARE_403_STDERR)] * 5)
	assert path is None
	assert read_exc is not None
	assert read_counters["clone"] == 0
	assert read_counters["sleep"] == 0
	assert str(read_exc).startswith("Unable to read memory branch")
	# The marker is consumed: the next read clones (and retries) again.
	path, read_exc, read_counters = _run_read([(128, BARE_403_STDERR), (0, "")])
	try:
		assert read_exc is None, read_exc
		assert read_counters["clone"] == 2
	finally:
		if path is not None:
			shutil.rmtree(path, ignore_errors=True)


def test_read_after_permanent_write_failure_still_clones() -> None:
	_result, exc, counters = _run_persist([PERMISSION_DENIED_STDERR])
	assert exc is not None
	assert counters["clone"] == 1
	assert ai_memory_lib._memory_write_clone_exhausted_at is None
	_path, read_exc, read_counters = _run_read([(128, PERMISSION_DENIED_STDERR)])
	assert read_exc is not None
	assert read_counters["clone"] == 1


def test_read_skip_marker_expires_after_window() -> None:
	ai_memory_lib._memory_write_clone_exhausted_at = (
		ai_memory_lib.time.monotonic() - ai_memory_lib._MEMORY_READ_RETRY_SKIP_WINDOW_SECONDS - 1.0
	)
	path, exc, counters = _run_read([(0, "")])
	try:
		assert exc is None, exc
		assert counters["clone"] == 1
	finally:
		if path is not None:
			shutil.rmtree(path, ignore_errors=True)


def test_claim_cli_persistent_403_makes_three_clone_attempts_in_total(monkeypatch, capsys) -> None:
	import argparse

	monkeypatch.setenv("GH_TOKEN", FAKE_TOKEN)
	counters = {"write_clone": 0, "read_clone": 0, "sleep": 0, "push": 0}

	def fake_clone(_repo_root, _memory_branch):
		counters["write_clone"] += 1
		raise ai_memory_lib.MemoryGitError(_write_clone_error(BARE_403_STDERR))

	def fake_run_git(cwd, args, check: bool = True, **_kwargs):
		if args and args[0] == "clone":
			counters["read_clone"] += 1
			return _FakeProc(returncode=128, stderr=BARE_403_STDERR)
		if args and args[0] == "push":
			counters["push"] += 1
		return _FakeProc(returncode=0)

	def fake_sleep(_seconds):
		counters["sleep"] += 1

	args = argparse.Namespace(
		repo_root=str(REPO_ROOT),
		memory_branch="ai-memory",
		memory_root="ai-memory",
		push_retries=16,
		enabled=True,
		retrieval_profiles="ai-memory/config/retrieval_profiles.v1.json",
		issue_number=10048,
		comment_id=123456,
		command="/approved",
		workflow="implement",
		actor="someone",
		run_id="45408016593",
		run_attempt=1,
		metadata_json=None,
	)
	with _patched_module_attrs(
		ai_memory_lib,
		_clone_for_memory_branch=fake_clone,
		_run_git=fake_run_git,
		_resolve_origin_url=lambda _repo_root: TOKEN_URL,
	), _patched_module_attrs(ai_memory_lib.time, sleep=fake_sleep), pytest.raises(ai_memory_lib.MemoryGitError) as excinfo:
		ai_memory_cli.cmd_processed_command_claim(args)
	assert counters["write_clone"] + counters["read_clone"] == 3
	assert counters["read_clone"] == 0
	assert counters["sleep"] == 2
	assert counters["push"] == 0
	message = str(excinfo.value)
	assert message.startswith("git clone")
	assert "returned error: 403" in message
	assert "https://" not in message
	assert FAKE_TOKEN not in message
	stderr = capsys.readouterr().err
	assert "recovery duplicate-check failed" in stderr
	assert "https://" not in stderr
	assert FAKE_TOKEN not in stderr


def test_write_clone_persistent_403_issues_three_clone_commands_in_total(monkeypatch) -> None:
	# The real _clone_for_memory_branch runs under the retry wrapper: the probe
	# passes and every shallow clone gets a bare 403. A transient shallow-clone
	# failure must not fall back to a full clone inside the same attempt, so
	# the step stops after 3 `git clone` commands, not 6.
	monkeypatch.setenv("GH_TOKEN", FAKE_TOKEN)
	monkeypatch.delenv("AI_MEMORY_SINGLE_BRANCH_CLONE_ENABLED", raising=False)
	monkeypatch.delenv("AI_MEMORY_CLONE_DIAGNOSTICS", raising=False)
	clone_calls: list[list[str]] = []
	counters = {"sleep": 0, "push": 0}

	def fake_run_git(cwd, args, check: bool = True, **_kwargs):
		if args and args[0] == "ls-remote":
			return _FakeProc(returncode=0, stdout="0123456789abcdef\trefs/heads/ai-memory\n")
		if args and args[0] == "clone":
			clone_calls.append(list(args))
			if check:
				raise ai_memory_lib.MemoryGitError(_write_clone_error(BARE_403_STDERR))
			return _FakeProc(returncode=128, stderr=BARE_403_STDERR)
		if args and args[0] == "push":
			counters["push"] += 1
		return _FakeProc(returncode=0)

	def fake_sleep(_seconds):
		counters["sleep"] += 1

	created: list[Path] = []
	real_mkdtemp = tempfile.mkdtemp

	def tracking_mkdtemp(*args, **kwargs):
		path = real_mkdtemp(*args, **kwargs)
		created.append(Path(path))
		return path

	with _patched_module_attrs(
		ai_memory_lib,
		_run_git=fake_run_git,
		_resolve_origin_url=lambda _repo_root: TOKEN_URL,
	), _patched_module_attrs(ai_memory_lib.time, sleep=fake_sleep), _patched_module_attrs(
		ai_memory_lib.tempfile, mkdtemp=tracking_mkdtemp
	), pytest.raises(ai_memory_lib.MemoryGitError) as excinfo:
		ai_memory_lib.persist_memory_operation(
			REPO_ROOT,
			memory_branch="ai-memory",
			memory_root_relative="ai-memory",
			push_retries=16,
			commit_message="ai-memory: read retry test",
			operation=lambda _clone_dir: {"value": 1},
		)
	assert len(clone_calls) == 3
	assert all("--depth" in call for call in clone_calls)
	assert counters["sleep"] == 2
	assert counters["push"] == 0
	message = str(excinfo.value)
	assert message.startswith("git clone")
	assert "returned error: 403" in message
	assert "https://" not in message
	assert FAKE_TOKEN not in message
	assert created
	assert not any(p.exists() for p in created)


def test_fast_clone_non_transient_failure_still_falls_back_to_full_clone(monkeypatch) -> None:
	monkeypatch.delenv("AI_MEMORY_SINGLE_BRANCH_CLONE_ENABLED", raising=False)

	def handler(args):
		if args[0] == "ls-remote":
			return 0, "0123456789abcdef\trefs/heads/ai-memory\n", ""
		if args[0] == "clone" and "--depth" in args:
			return 128, "", "fatal: dumb http transport does not support shallow capabilities"
		return 0, "", ""

	path, exc, _created, calls = _run_clone_with_script(handler)
	try:
		assert exc is None, exc
		clones = [call for call in calls if call[0] == "clone"]
		assert len(clones) == 2
		assert "--depth" in clones[0]
		assert "--depth" not in clones[1]
	finally:
		if path is not None:
			shutil.rmtree(path, ignore_errors=True)
