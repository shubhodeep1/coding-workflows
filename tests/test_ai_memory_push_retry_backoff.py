#!/usr/bin/env python3
"""Tests for memory-branch push retry backoff.

The `ai-memory` branch is a single shared ref every concurrent workflow run
pushes to.  Without a randomized delay between push retries the contenders
retry in lockstep and keep losing the same server-side ref-lock race, which
surfaced as `Failed to push memory branch after N attempts`.  These tests
lock in the jittered exponential backoff that decorrelates the pushers.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "ai_memory_lib.py"

if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("ai_memory_lib", MODULE_PATH)
assert spec is not None and spec.loader is not None
ai_memory_lib = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ai_memory_lib
spec.loader.exec_module(ai_memory_lib)


class _FakeProc:
	"""Minimal stand-in for subprocess.CompletedProcess used by _run_git."""

	def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
		self.returncode = returncode
		self.stdout = stdout
		self.stderr = stderr


def test_backoff_stays_within_jittered_exponential_bounds() -> None:
	base = ai_memory_lib._PUSH_RETRY_BACKOFF_BASE_SECONDS
	cap = ai_memory_lib._PUSH_RETRY_BACKOFF_CAP_SECONDS
	for attempt in range(1, 9):
		ceiling = min(cap, base * (2 ** (attempt - 1)))
		for _ in range(200):
			delay = ai_memory_lib._push_retry_backoff_seconds(attempt)
			assert 0.0 <= delay <= ceiling, (attempt, delay, ceiling)


def test_backoff_ceiling_never_exceeds_cap() -> None:
	base = ai_memory_lib._PUSH_RETRY_BACKOFF_BASE_SECONDS
	cap = ai_memory_lib._PUSH_RETRY_BACKOFF_CAP_SECONDS
	assert base > 0.0
	assert cap >= base
	# A very large attempt must clamp to the cap, not grow unbounded.
	for _ in range(200):
		assert ai_memory_lib._push_retry_backoff_seconds(99) <= cap


def test_backoff_clamps_nonpositive_attempt() -> None:
	# attempt < 1 must not raise or yield a negative ceiling.
	ceiling = ai_memory_lib._PUSH_RETRY_BACKOFF_BASE_SECONDS
	for attempt in (0, -1, -50):
		for _ in range(50):
			delay = ai_memory_lib._push_retry_backoff_seconds(attempt)
			assert 0.0 <= delay <= ceiling, (attempt, delay)


def test_backoff_is_randomized() -> None:
	# Jitter must actually vary — a constant delay would not decorrelate
	# concurrent pushers, which is the whole point of the backoff.
	calls: list[tuple[float, float]] = []
	returned_delays = iter([0.25, 0.75])
	original_uniform = ai_memory_lib.random.uniform

	def fake_uniform(low: float, high: float) -> float:
		calls.append((low, high))
		return next(returned_delays)

	ai_memory_lib.random.uniform = fake_uniform
	try:
		delays = [ai_memory_lib._push_retry_backoff_seconds(5) for _ in range(2)]
	finally:
		ai_memory_lib.random.uniform = original_uniform

	assert delays == [0.25, 0.75]
	assert calls == [(0.0, ai_memory_lib._PUSH_RETRY_BACKOFF_CAP_SECONDS)] * 2


def test_backoff_large_attempt_clamps_before_overflow() -> None:
	calls: list[tuple[float, float]] = []
	original_uniform = ai_memory_lib.random.uniform

	def fake_uniform(low: float, high: float) -> float:
		calls.append((low, high))
		return high

	ai_memory_lib.random.uniform = fake_uniform
	try:
		delay = ai_memory_lib._push_retry_backoff_seconds(10_000)
	finally:
		ai_memory_lib.random.uniform = original_uniform

	assert delay == ai_memory_lib._PUSH_RETRY_BACKOFF_CAP_SECONDS
	assert calls == [(0.0, ai_memory_lib._PUSH_RETRY_BACKOFF_CAP_SECONDS)]


def _scripted_run_git(
	push_codes: list[int],
	*,
	fetch_codes: list[int] | None = None,
	show_ref_codes: list[int] | None = None,
	rebase_codes: list[int] | None = None,
	rebase_abort_codes: list[int] | None = None,
	call_log: list[tuple[str, tuple[str, ...]]] | None = None,
):
	"""Build a fake _run_git with scripted failure points in the retry loop."""

	push_results = iter(push_codes)
	fetch_results = iter(fetch_codes or [])
	show_ref_results = iter(show_ref_codes or [])
	rebase_results = iter(rebase_codes or [])
	rebase_abort_results = iter(rebase_abort_codes or [])

	def fake_run_git(  # noqa: ANN001 - test stub
		cwd,
		args,
		check: bool = True,
		*,
		inherit_location_env: bool = False,
	):
		op = args[0] if args else ""
		_ = inherit_location_env
		if call_log is not None:
			call_log.append((op, tuple(args)))
		if op == "ls-remote":
			return _FakeProc(returncode=0, stdout="")
		if op == "diff":
			return _FakeProc(returncode=1)  # staged changes present
		if op == "rev-parse":
			return _FakeProc(returncode=0, stdout="0123456789abcdef\n")
		if op == "fetch":
			rc = next(fetch_results, 0)
			return _FakeProc(returncode=rc, stderr="" if rc == 0 else "fetch failed")
		if op == "show-ref":
			rc = next(show_ref_results, 0)
			return _FakeProc(returncode=rc, stderr="" if rc == 0 else "show-ref missing")
		if op == "rebase":
			if len(args) > 1 and args[1] == "--abort":
				rc = next(rebase_abort_results, 0)
				return _FakeProc(returncode=rc, stderr="" if rc == 0 else "rebase abort failed")
			rc = next(rebase_results, 0)
			return _FakeProc(returncode=rc, stderr="" if rc == 0 else "rebase conflict")
		if op == "push":
			rc = next(push_results)
			return _FakeProc(
				returncode=rc,
				stderr="" if rc == 0 else "! [remote rejected] cannot lock ref",
			)
		return _FakeProc(returncode=0)

	return fake_run_git


def _run_persist(
	push_codes: list[int],
	push_retries: int,
	*,
	fetch_codes: list[int] | None = None,
	show_ref_codes: list[int] | None = None,
	rebase_codes: list[int] | None = None,
	rebase_abort_codes: list[int] | None = None,
):
	"""Drive persist_memory_operation; return (result, backoff_attempts, call_log, exc).

	`_run_git` and `_push_retry_backoff_seconds` are stubbed so the retry loop
	runs without real git or real sleeps.  Either returns the result dict, or
	the raised MemoryGitError, alongside the recorded backoff attempt numbers.
	"""

	work_tempdir = tempfile.tempdir
	scratch_root: Path | None = None
	work: Path | None = None
	try:
		try:
			work = Path(tempfile.mkdtemp(prefix="ai-memory-retry-test-"))
		except FileNotFoundError:
			scratch_root = Path(tempfile.mkdtemp(prefix=".tmp-ai-memory-tests-", dir=REPO_ROOT))
			tempfile.tempdir = str(scratch_root)
			work = Path(tempfile.mkdtemp(prefix="ai-memory-retry-test-"))
		repo_root = work / "repo"
		repo_root.mkdir(parents=True, exist_ok=True)
		backoff_attempts: list[int] = []
		call_log: list[tuple[str, tuple[str, ...]]] = []

		def fake_backoff(attempt: int) -> float:
			backoff_attempts.append(attempt)
			return 0.0

		original_run_git = ai_memory_lib._run_git
		original_backoff = ai_memory_lib._push_retry_backoff_seconds
		ai_memory_lib._run_git = _scripted_run_git(
			push_codes,
			fetch_codes=fetch_codes,
			show_ref_codes=show_ref_codes,
			rebase_codes=rebase_codes,
			rebase_abort_codes=rebase_abort_codes,
			call_log=call_log,
		)
		ai_memory_lib._push_retry_backoff_seconds = fake_backoff

		def _op(_clone_dir: Path) -> dict:
			return {"claimed": True}

		try:
			result = ai_memory_lib.persist_memory_operation(
				repo_root,
				memory_branch="ai-memory",
				memory_root_relative="ai-memory",
				push_retries=push_retries,
				commit_message="ai-memory: test claim",
				operation=_op,
			)
			return result, backoff_attempts, call_log, None
		except ai_memory_lib.MemoryGitError as exc:
			return None, backoff_attempts, call_log, exc
		finally:
			ai_memory_lib._run_git = original_run_git
			ai_memory_lib._push_retry_backoff_seconds = original_backoff
	finally:
		tempfile.tempdir = work_tempdir
		if work is not None:
			shutil.rmtree(work, ignore_errors=True)
		if scratch_root is not None:
			shutil.rmtree(scratch_root, ignore_errors=True)


def test_persist_memory_operation_backs_off_then_succeeds_on_retry() -> None:
	# Two rejected pushes (concurrent ref-lock race) then success: the backoff
	# must fire once per failed attempt and the push must still land.
	result, backoff_attempts, _call_log, exc = _run_persist([1, 1, 0], push_retries=5)
	assert exc is None, exc
	assert result is not None
	assert result["did_push"] is True, result
	assert result["push_attempts"] == 3, result
	# One backoff per failed attempt (1 and 2); none after the success.
	assert backoff_attempts == [1, 2], backoff_attempts


def test_persist_memory_operation_backs_off_every_attempt_before_raising() -> None:
	# All pushes rejected: backoff fires after every non-final attempt and the
	# loop still raises — claiming is mutual exclusion and is not fail-open.
	result, backoff_attempts, _call_log, exc = _run_persist([1, 1, 1, 1], push_retries=4)
	assert result is None
	assert isinstance(exc, ai_memory_lib.MemoryGitError), exc
	assert "Failed to push memory branch after 4 attempts" in str(exc)
	# 4 attempts -> backoff after attempts 1, 2, 3 (not after the final one).
	assert backoff_attempts == [1, 2, 3], backoff_attempts


def test_persist_memory_operation_survives_seven_rejections_with_eight_retry_budget() -> None:
	# Regression coverage for shared ai-memory branch contention: under the
	# raised retry budget (8), a burst of 7 consecutive non-fast-forward
	# rejections must still land on the 8th attempt instead of aborting the
	# fail-closed claim.
	result, backoff_attempts, _call_log, exc = _run_persist(
		[1, 1, 1, 1, 1, 1, 1, 0], push_retries=8
	)
	assert exc is None, exc
	assert result is not None
	assert result["did_push"] is True, result
	assert result["push_attempts"] == 8, result
	# Backoff fires after each of the 7 failed attempts, not after the success.
	assert backoff_attempts == [1, 2, 3, 4, 5, 6, 7], backoff_attempts


def test_persist_memory_operation_survives_fifteen_rejections_with_sixteen_retry_budget() -> None:
	# Regression coverage for run 32849764877: an orchestrator dispatch burst
	# pushed a foreign commit to the shared ai-memory ref every 3-5s for ~2
	# minutes, and the previous 8-attempt budget (~80s of loop) exhausted
	# mid-burst, hard-failing the fail-closed /answer claim and the whole plan
	# phase.  Under the raised default budget (16), a burst of 15 consecutive
	# rejections must still land on the 16th attempt instead of aborting.
	result, backoff_attempts, _call_log, exc = _run_persist(
		[1] * 15 + [0], push_retries=16
	)
	assert exc is None, exc
	assert result is not None
	assert result["did_push"] is True, result
	assert result["push_attempts"] == 16, result
	# Backoff fires after each of the 15 failed attempts, not after the success.
	assert backoff_attempts == list(range(1, 16)), backoff_attempts


def test_persist_memory_operation_raises_on_fetch_failure_before_rebase() -> None:
	result, backoff_attempts, call_log, exc = _run_persist([1], push_retries=2, fetch_codes=[1])
	assert result is None
	assert isinstance(exc, ai_memory_lib.MemoryGitError), exc
	assert "Memory branch fetch failed while retrying push: fetch failed" in str(exc)
	assert backoff_attempts == [1], backoff_attempts
	assert not any(op == "show-ref" for op, _args in call_log), call_log
	assert not any(op == "rebase" for op, _args in call_log), call_log


def test_persist_memory_operation_skips_rebase_when_remote_ref_is_missing() -> None:
	result, backoff_attempts, call_log, exc = _run_persist([1, 0], push_retries=3, show_ref_codes=[1])
	assert exc is None, exc
	assert result is not None
	assert result["did_push"] is True, result
	assert backoff_attempts == [1], backoff_attempts
	assert not any(args == ("rebase", "refs/remotes/origin/ai-memory") for _op, args in call_log), call_log


def test_persist_memory_operation_aborts_rebase_before_raising() -> None:
	result, backoff_attempts, call_log, exc = _run_persist([1], push_retries=2, rebase_codes=[1])
	assert result is None
	assert isinstance(exc, ai_memory_lib.MemoryGitError), exc
	assert "Memory branch rebase failed while retrying push: rebase conflict" in str(exc)
	assert backoff_attempts == [1], backoff_attempts
	assert any(args == ("rebase", "--abort") for _op, args in call_log), call_log


def test_persist_memory_operation_surfaces_rebase_abort_failure() -> None:
	result, backoff_attempts, call_log, exc = _run_persist(
		[1],
		push_retries=2,
		rebase_codes=[1],
		rebase_abort_codes=[1],
	)
	assert result is None
	assert isinstance(exc, ai_memory_lib.MemoryGitError), exc
	assert "Memory branch rebase failed while retrying push: rebase conflict" in str(exc)
	assert "rebase --abort also failed: rebase abort failed" in str(exc)
	assert backoff_attempts == [1], backoff_attempts
	assert any(args == ("rebase", "--abort") for _op, args in call_log), call_log


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as exc:  # noqa: BLE001 - test harness reports all
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
