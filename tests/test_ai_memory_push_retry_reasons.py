#!/usr/bin/env python3
"""Tests for the bounded `push_retry_reasons` memory-write telemetry (issue #8726).

`persist_memory_operation` already reports how many push attempts a memory
write needed (`push_attempts`).  `push_retry_reasons` adds why: one bounded
class per retried attempt, in order, capped at 10 entries, drawn only from
`non_fast_forward` / `auth` / `network` / `rate_limited` / `other`.  The raw
git output is classified and discarded, so it must never reach the result.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
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


ALLOWED_REASONS = {"non_fast_forward", "auth", "network", "rate_limited", "other"}

NON_FAST_FORWARD_STDERR = (
	"To https://github.com/o/r.git\n"
	" ! [rejected]        HEAD -> ai-memory (fetch first)\n"
	"error: failed to push some refs to 'https://github.com/o/r.git'\n"
)
NETWORK_STDERR = "fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com\n"
FORBIDDEN_STDERR = "remote: 403 Forbidden\nfatal: unable to push\n"
UNKNOWN_STDERR = "error: something entirely unexpected happened\n"
RATE_LIMIT_403_STDERR = (
	"remote: You have exceeded a secondary rate limit.\n"
	"fatal: unable to access 'https://github.com/o/r.git/': The requested URL returned error: 403\n"
)
PERMANENT_AUTH_STDERR = (
	"fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"
)


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


def _run_persist(push_script: list[tuple[int, str]], *, push_retries: int = 16, diff_returncode: int = 1):
	"""Drive persist_memory_operation with a scripted git; return (result, exc, push_count)."""

	push_results = iter(push_script)
	push_count = [0]

	def fake_clone(_repo_root, _memory_branch):
		return Path(tempfile.mkdtemp(prefix="ai-memory-retry-reasons-"))

	def fake_run_git(cwd, args, check: bool = True, **_kwargs):  # noqa: ANN001 - test stub
		op = args[0] if args else ""
		if op == "diff":
			return _FakeProc(returncode=diff_returncode)
		if op == "rev-parse":
			return _FakeProc(returncode=0, stdout="0123456789abcdef\n")
		if op == "push":
			push_count[0] += 1
			rc, stderr = next(push_results)
			return _FakeProc(returncode=rc, stderr="" if rc == 0 else stderr)
		return _FakeProc(returncode=0)

	with _patched_module_attrs(
		ai_memory_lib,
		_clone_for_memory_branch=fake_clone,
		_run_git=fake_run_git,
		_push_retry_backoff_seconds=lambda _attempt: 0.0,
	), _patched_module_attrs(ai_memory_lib.time, sleep=lambda _seconds: None):
		try:
			result = ai_memory_lib.persist_memory_operation(
				REPO_ROOT,
				memory_branch="ai-memory",
				memory_root_relative="ai-memory",
				push_retries=push_retries,
				commit_message="ai-memory: retry reasons test",
				operation=lambda _clone_dir: {"value": 1},
			)
			return result, None, push_count[0]
		except ai_memory_lib.MemoryGitError as exc:
			return None, exc, push_count[0]


def test_non_fast_forward_then_success() -> None:
	result, exc, pushes = _run_persist([(1, NON_FAST_FORWARD_STDERR), (0, "")])
	assert exc is None, exc
	assert pushes == 2
	assert result["push_attempts"] == 2
	assert result["push_retry_reasons"] == ["non_fast_forward"]


def test_mixed_reasons_keep_attempt_order() -> None:
	result, exc, _pushes = _run_persist(
		[
			(1, NON_FAST_FORWARD_STDERR),
			(1, NETWORK_STDERR),
			(1, FORBIDDEN_STDERR),
			(1, UNKNOWN_STDERR),
			(0, ""),
		]
	)
	assert exc is None, exc
	assert result["push_attempts"] == 5
	assert result["push_retry_reasons"] == ["non_fast_forward", "network", "auth", "other"]


def test_cannot_lock_ref_is_contention() -> None:
	result, exc, _pushes = _run_persist(
		[(1, "! [remote rejected] cannot lock ref"), (1, "! [remote rejected] cannot lock ref"), (0, "")]
	)
	assert exc is None, exc
	assert result["push_retry_reasons"] == ["non_fast_forward", "non_fast_forward"]


def test_rate_limit_403_is_rate_limited() -> None:
	result, exc, _pushes = _run_persist([(1, RATE_LIMIT_403_STDERR), (0, "")])
	assert exc is None, exc
	assert result["push_attempts"] == 2
	assert result["push_retry_reasons"] == ["rate_limited"]


def test_permanent_auth_failure_still_raises_without_retry() -> None:
	result, exc, pushes = _run_persist([(1, PERMANENT_AUTH_STDERR), (0, "")])
	assert result is None
	assert isinstance(exc, ai_memory_lib.MemoryGitError)
	assert pushes == 1
	assert "error_class=auth attempts=1" in str(exc)
	assert ai_memory_lib._classify_memory_push_retry_reason(PERMANENT_AUTH_STDERR) == "auth"


def test_exhausted_retries_message_unchanged() -> None:
	result, exc, pushes = _run_persist([(1, UNKNOWN_STDERR)] * 3, push_retries=3)
	assert result is None
	assert pushes == 3
	assert str(exc) == f"Failed to push memory branch after 3 attempts: {UNKNOWN_STDERR.strip()}"


def test_reasons_are_capped_at_ten() -> None:
	script = [(1, NON_FAST_FORWARD_STDERR)] * 12 + [(0, "")]
	result, exc, _pushes = _run_persist(script, push_retries=16)
	assert exc is None, exc
	assert result["push_attempts"] == 13
	assert len(result["push_retry_reasons"]) == 10
	assert ai_memory_lib._MEMORY_PUSH_RETRY_REASONS_MAX == 10


def test_first_push_success_has_empty_reasons() -> None:
	result, exc, _pushes = _run_persist([(0, "")])
	assert exc is None, exc
	assert result["push_attempts"] == 1
	assert result["push_retry_reasons"] == []


def test_no_change_path_has_empty_reasons() -> None:
	result, exc, pushes = _run_persist([], diff_returncode=0)
	assert exc is None, exc
	assert pushes == 0
	assert result["push_attempts"] == 0
	assert result["push_retry_reasons"] == []


def test_raw_output_never_leaks_into_result() -> None:
	sentinel_token = "ghs_SENTINELTOKEN0123456789"
	payload_marker = "PAYLOAD-MARKER-8726"
	leaky_stderr = (
		f"fatal: unable to access 'https://x-access-token:{sentinel_token}@github.com/o/rate-limiter/': "
		f"unexpected disconnect {payload_marker}\n"
	)
	result, exc, _pushes = _run_persist([(1, leaky_stderr), (1, NON_FAST_FORWARD_STDERR), (0, "")])
	assert exc is None, exc
	serialized = json.dumps(result)
	for forbidden in (sentinel_token, payload_marker, "x-access-token", "rate-limiter", "github.com", "fetch first"):
		assert forbidden not in serialized, forbidden
	assert all(reason in ALLOWED_REASONS for reason in result["push_retry_reasons"])
	# The URL-only `rate-limiter` decoy must not decide the class.
	assert result["push_retry_reasons"] == ["other", "non_fast_forward"]


def test_classifier_returns_only_allowed_values() -> None:
	classify = ai_memory_lib._classify_memory_push_retry_reason
	assert ai_memory_lib._MEMORY_PUSH_RETRY_REASON_VALUES == frozenset(ALLOWED_REASONS)
	cases = {
		"": "other",
		"error: HTTP 429 Too Many Requests": "rate_limited",
		"error: RPC failed; HTTP 429 curl 22 The requested URL returned error: 429": "rate_limited",
		"fatal: the remote end hung up unexpectedly": "network",
		"error: RPC failed; curl 56 Connection reset by peer": "network",
		"Updates were rejected because the tip of your current branch is behind": "non_fast_forward",
		"! [remote rejected] HEAD -> ai-memory (pre-receive hook declined)": "other",
		"remote: Permission to o/rate-limiter.git denied to bot.": "auth",
		"fatal: unable to access 'https://github.com/o/timed-out/': unexpected": "other",
	}
	for text, expected in cases.items():
		got = classify(text)
		assert got == expected, (text, got, expected)
		assert got in ALLOWED_REASONS


def main() -> int:
	for name, value in sorted(globals().items()):
		if name.startswith("test_") and callable(value):
			value()
	print("OK: ai_memory push_retry_reasons tests passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
