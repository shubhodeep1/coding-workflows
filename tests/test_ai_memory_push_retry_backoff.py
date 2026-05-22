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
	samples = {ai_memory_lib._push_retry_backoff_seconds(5) for _ in range(64)}
	assert len(samples) > 1, "backoff delay is not randomized"


def _scripted_run_git(push_codes: list[int]):
	"""Build a fake _run_git that yields scripted push return codes.

	Every git op other than `push` succeeds; `diff --cached` reports staged
	changes so the commit/push path is exercised.
	"""

	push_results = iter(push_codes)

	def fake_run_git(cwd, args, check: bool = True):  # noqa: ANN001 - test stub
		op = args[0] if args else ""
		if op == "ls-remote":
			return _FakeProc(returncode=0, stdout="")
		if op == "diff":
			return _FakeProc(returncode=1)  # staged changes present
		if op == "rev-parse":
			return _FakeProc(returncode=0, stdout="0123456789abcdef\n")
		if op == "push":
			rc = next(push_results)
			return _FakeProc(
				returncode=rc,
				stderr="" if rc == 0 else "! [remote rejected] cannot lock ref",
			)
		return _FakeProc(returncode=0)

	return fake_run_git


def _run_persist(push_codes: list[int], push_retries: int):
	"""Drive persist_memory_operation with scripted pushes; return (result, backoff_attempts).

	`_run_git` and `_push_retry_backoff_seconds` are stubbed so the retry loop
	runs without real git or real sleeps.  Either returns the result dict, or
	the raised MemoryGitError, alongside the recorded backoff attempt numbers.
	"""

	work = Path(tempfile.mkdtemp(prefix="ai-memory-retry-test-"))
	repo_root = work / "repo"
	repo_root.mkdir(parents=True, exist_ok=True)
	backoff_attempts: list[int] = []

	def fake_backoff(attempt: int) -> float:
		backoff_attempts.append(attempt)
		return 0.0

	original_run_git = ai_memory_lib._run_git
	original_backoff = ai_memory_lib._push_retry_backoff_seconds
	ai_memory_lib._run_git = _scripted_run_git(push_codes)
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
		return result, backoff_attempts, None
	except ai_memory_lib.MemoryGitError as exc:
		return None, backoff_attempts, exc
	finally:
		ai_memory_lib._run_git = original_run_git
		ai_memory_lib._push_retry_backoff_seconds = original_backoff
		shutil.rmtree(work, ignore_errors=True)


def test_persist_memory_operation_backs_off_then_succeeds_on_retry() -> None:
	# Two rejected pushes (concurrent ref-lock race) then success: the backoff
	# must fire once per failed attempt and the push must still land.
	result, backoff_attempts, exc = _run_persist([1, 1, 0], push_retries=5)
	assert exc is None, exc
	assert result is not None
	assert result["did_push"] is True, result
	assert result["push_attempts"] == 3, result
	# One backoff per failed attempt (1 and 2); none after the success.
	assert backoff_attempts == [1, 2], backoff_attempts


def test_persist_memory_operation_backs_off_every_attempt_before_raising() -> None:
	# All pushes rejected: backoff fires after every non-final attempt and the
	# loop still raises — claiming is mutual exclusion and is not fail-open.
	result, backoff_attempts, exc = _run_persist([1, 1, 1, 1], push_retries=4)
	assert result is None
	assert isinstance(exc, ai_memory_lib.MemoryGitError), exc
	# 4 attempts -> backoff after attempts 1, 2, 3 (not after the final one).
	assert backoff_attempts == [1, 2, 3], backoff_attempts


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
