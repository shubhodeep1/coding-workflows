#!/usr/bin/env python3
"""Regression coverage for the must_contain resolver-union-merge defense.

Project #7355 wave 3 was blocked because issue #7358 (PR #7365) and a
back-merge of ``main`` (#7376) edited the same line of
``_is_gh_permanent_failure`` in ``scripts/gh_helpers.sh``. The
``[ai-merge-resolve]`` commit kept both edits on one line, so the exact
captured ``must_contain`` regex no longer matched, and the post-capture-
evolution defense refuses on purpose for resolver commits.

Asserts that the verifier accepts that miss only when the resolver merge
kept the captured line whole and added nothing beyond the other merge side's
line, and still FAILS when the resolver:

  * dropped or changed any part of the captured line;
  * inserted text that the other side's line does not have;
  * made a single-parent commit (not a merge);
  * committed before capture;

and fails closed in working-tree mode and without ``captured_at``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator


REPO_ROOT = Path(__file__).resolve().parent.parent

_D0_BASE = "2026-10-13T00:00:00Z"
_D1_IMPL = "2026-10-14T05:25:00Z"
_CAPTURED_AT = "2026-10-14T05:25:11Z"
_D2_MAIN = "2026-10-14T12:00:00Z"
_D3_RESOLVE = "2026-10-14T13:33:45Z"

_TARGET_PATH = "scripts/gh_helpers.sh"
_BASE_LINE = "\tprintf '%s' \"$1\" | grep -qiE \"HTTP 404|HTTP 422|Resource not accessible\""
# The sub-issue inserts alternatives in the middle (captured under must_contain).
_SUB_LINE = "\tprintf '%s' \"$1\" | grep -qiE \"HTTP 404|HTTP 400|HTTP 401|HTTP 422|Bad credentials|Resource not accessible\""
# The other side (main) appends an alternative at the end of the same line.
_MAIN_LINE = "\tprintf '%s' \"$1\" | grep -qiE \"HTTP 404|HTTP 422|Resource not accessible|Commenting is disabled\""
_UNION_LINE = "\tprintf '%s' \"$1\" | grep -qiE \"HTTP 404|HTTP 400|HTTP 401|HTTP 422|Bad credentials|Resource not accessible|Commenting is disabled\""

_MUST_CONTAIN_REGEX = re.escape(_SUB_LINE.strip())


def _file_with(line: str) -> str:
	return "\n".join(["#!/usr/bin/env bash", "_is_gh_permanent_failure()", "{", line, "}", ""])


def _verifier_module():
	spec = importlib.util.spec_from_file_location(
		"verify_integration_fingerprints",
		REPO_ROOT / "scripts" / "verify_integration_fingerprints.py",
	)
	assert spec is not None and spec.loader is not None
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


def _run_git(cwd: Path, *args: str, date: str | None = None, check: bool = True) -> str:
	env = os.environ.copy()
	env.update({
		"GIT_AUTHOR_NAME": "Test Bot",
		"GIT_AUTHOR_EMAIL": "test@example.invalid",
		"GIT_COMMITTER_NAME": "Test Bot",
		"GIT_COMMITTER_EMAIL": "test@example.invalid",
		"GIT_CONFIG_GLOBAL": "/dev/null",
	})
	if date is not None:
		env["GIT_AUTHOR_DATE"] = date
		env["GIT_COMMITTER_DATE"] = date
	result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=check, env=env)
	return result.stdout.decode("utf-8", errors="replace")


def _run_verifier(mod, argv: list[str], cwd: Path) -> tuple[int, str, str]:
	out_buf = io.StringIO()
	err_buf = io.StringIO()
	prev_cwd = os.getcwd()
	try:
		os.chdir(cwd)
		with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
			rc = mod.main(argv)
	finally:
		os.chdir(prev_cwd)
	return rc, out_buf.getvalue(), err_buf.getvalue()


def _clear_caches(mod) -> None:
	for name in ("_POST_CAPTURE_EVOLUTION_CACHE", "_RESOLVER_UNION_MERGE_CACHE"):
		cache = getattr(mod, name, None)
		if cache is not None:
			cache.clear()


@contextlib.contextmanager
def _sandbox_repo() -> Iterator[Path]:
	td = Path(tempfile.mkdtemp(prefix="verifier-resolver-union-test-"))
	try:
		_run_git(td, "init", "--quiet", "--initial-branch=main")
		_run_git(td, "config", "commit.gpgsign", "false")
		yield td
	finally:
		shutil.rmtree(td, ignore_errors=True)


def _write(repo: Path, content: str) -> None:
	target = repo / _TARGET_PATH
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_text(content, encoding="utf-8")
	_run_git(repo, "add", _TARGET_PATH)


def _seed_merge(repo: Path, resolved_line: str, *, resolve_date: str = _D3_RESOLVE, merge: bool = True) -> str:
	"""Base on main, the sub-issue edit on the integration branch, a same-line
	edit on main, then an [ai-merge-resolve] commit on the integration branch
	that writes ``resolved_line``. Returns the integration HEAD SHA."""
	_write(repo, _file_with(_BASE_LINE))
	_run_git(repo, "commit", "--quiet", "-m", "base", date=_D0_BASE)
	_run_git(repo, "checkout", "--quiet", "-b", "orchestrator/project-1")
	_write(repo, _file_with(_SUB_LINE))
	_run_git(repo, "commit", "--quiet", "-m", "AI implementation for issue #7358 (#7365)", date=_D1_IMPL)
	_run_git(repo, "checkout", "--quiet", "main")
	_write(repo, _file_with(_MAIN_LINE))
	_run_git(repo, "commit", "--quiet", "-m", "Keep tracking issues under the comment cap (#7376)", date=_D2_MAIN)
	_run_git(repo, "checkout", "--quiet", "orchestrator/project-1")
	if merge:
		_run_git(repo, "merge", "--no-ff", "--no-commit", "main", date=resolve_date, check=False)
	_write(repo, _file_with(resolved_line))
	_run_git(repo, "commit", "--quiet", "-m", "[ai-merge-resolve] resolve merge conflicts", date=resolve_date)
	return _run_git(repo, "rev-parse", "HEAD").strip()


def _fingerprint_state(captured_at: str | None = _CAPTURED_AT) -> dict:
	entry: dict = {
		"issue": 7358,
		"pr": 7365,
		"must_contain": [{"file": _TARGET_PATH, "regex": _MUST_CONTAIN_REGEX}],
		"must_not_contain": [],
		"must_not_exist": [],
	}
	if captured_at is not None:
		entry["captured_at"] = captured_at
	return {"7358": entry}


def _verify(repo: Path, head: str | None, state: dict) -> tuple[int, str]:
	mod = _verifier_module()
	_clear_caches(mod)
	fps = repo / "fps.json"
	fps.write_text(json.dumps(state), encoding="utf-8")
	argv = (["--ref", head] if head else []) + [str(fps)]
	rc, out, err = _run_verifier(mod, argv, repo)
	return rc, out + err


def test_union_merge_of_both_sides_passes_with_marker():
	with _sandbox_repo() as repo:
		head = _seed_merge(repo, _UNION_LINE)
		rc, out = _verify(repo, head, _fingerprint_state())
		assert rc == 0, out
		assert "FINGERPRINT_RESOLVER_UNION_MERGE_V1 issue=#7358 pr=#7365" in out, out
		assert f"resolver_commit={head[:12]}" in out, out


def test_helper_returns_the_resolver_merge_sha():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		head = _seed_merge(repo, _UNION_LINE)
		_clear_caches(mod)
		prev = os.getcwd()
		try:
			os.chdir(repo)
			result = mod._is_must_contain_resolver_union_merge_false_positive(
				head, _CAPTURED_AT, _TARGET_PATH, _MUST_CONTAIN_REGEX, _file_with(_UNION_LINE),
			)
		finally:
			os.chdir(prev)
		assert result == head, result


def test_resolver_that_kept_only_the_other_side_still_fails():
	with _sandbox_repo() as repo:
		head = _seed_merge(repo, _MAIN_LINE)
		rc, out = _verify(repo, head, _fingerprint_state())
		assert rc == 1, out
		assert "must_contain pattern missing" in out and "FINGERPRINT_RESOLVER_UNION_MERGE_V1" not in out, out


def test_resolver_that_took_other_side_verbatim_after_sub_issue_deletion_still_fails():
	# The sub-issue deleted a space; main edited the same line. Taking main's line
	# verbatim keeps the captured characters in order but undoes the deletion.
	base_line = "\tx='foo bar'"
	sub_line = "\tx='foobar'"
	main_line = "\tx='foo bar'  # main"
	regex = re.escape(sub_line.strip())
	with _sandbox_repo() as repo:
		_write(repo, _file_with(base_line))
		_run_git(repo, "commit", "--quiet", "-m", "base", date=_D0_BASE)
		_run_git(repo, "checkout", "--quiet", "-b", "orchestrator/project-1")
		_write(repo, _file_with(sub_line))
		_run_git(repo, "commit", "--quiet", "-m", "AI implementation for issue #7358 (#7365)", date=_D1_IMPL)
		_run_git(repo, "checkout", "--quiet", "main")
		_write(repo, _file_with(main_line))
		_run_git(repo, "commit", "--quiet", "-m", "main edit (#7376)", date=_D2_MAIN)
		_run_git(repo, "checkout", "--quiet", "orchestrator/project-1")
		_run_git(repo, "merge", "--no-ff", "--no-commit", "main", date=_D3_RESOLVE, check=False)
		_write(repo, _file_with(main_line))
		_run_git(repo, "commit", "--quiet", "-m", "[ai-merge-resolve] resolve merge conflicts", date=_D3_RESOLVE)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		state = _fingerprint_state()
		state["7358"]["must_contain"] = [{"file": _TARGET_PATH, "regex": regex}]
		rc, out = _verify(repo, head, state)
		assert rc == 1, out
		assert "FINGERPRINT_RESOLVER_UNION_MERGE_V1" not in out, out


def test_resolver_that_duplicated_other_side_text_still_fails():
	# Captured abc, other side aXb, resolver aXbXc: every inserted run "X" appears
	# in the other line, but the second X is the resolver's own text.
	base_line = "\tx='ab'"
	sub_line = "\tx='abc'"
	main_line = "\tx='aXb'"
	regex = re.escape(sub_line.strip())
	with _sandbox_repo() as repo:
		_write(repo, _file_with(base_line))
		_run_git(repo, "commit", "--quiet", "-m", "base", date=_D0_BASE)
		_run_git(repo, "checkout", "--quiet", "-b", "orchestrator/project-1")
		_write(repo, _file_with(sub_line))
		_run_git(repo, "commit", "--quiet", "-m", "AI implementation for issue #7358 (#7365)", date=_D1_IMPL)
		_run_git(repo, "checkout", "--quiet", "main")
		_write(repo, _file_with(main_line))
		_run_git(repo, "commit", "--quiet", "-m", "main edit (#7376)", date=_D2_MAIN)
		_run_git(repo, "checkout", "--quiet", "orchestrator/project-1")
		_run_git(repo, "merge", "--no-ff", "--no-commit", "main", date=_D3_RESOLVE, check=False)
		_write(repo, _file_with("\tx='aXbXc'"))
		_run_git(repo, "commit", "--quiet", "-m", "[ai-merge-resolve] resolve merge conflicts", date=_D3_RESOLVE)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		state = _fingerprint_state()
		state["7358"]["must_contain"] = [{"file": _TARGET_PATH, "regex": regex}]
		rc, out = _verify(repo, head, state)
		assert rc == 1, out
		assert "FINGERPRINT_RESOLVER_UNION_MERGE_V1" not in out, out


def test_resolver_text_taken_from_an_unrelated_other_side_line_still_fails():
	# Captured abc, resolver aXbc, and the other side's file holds an unrelated,
	# unchanged line aXc that is kept whole in aXbc. Main edited a different line,
	# so the inserted X is the resolver's own text, not the other side's edit.
	def body(x_line: str, extra: str) -> str:
		return "\n".join(["#!/usr/bin/env bash", x_line, "\tx='aXc'", extra, ""])
	regex = re.escape("x='abc'")
	with _sandbox_repo() as repo:
		_write(repo, body("\tx='ab'", "y=1"))
		_run_git(repo, "commit", "--quiet", "-m", "base", date=_D0_BASE)
		_run_git(repo, "checkout", "--quiet", "-b", "orchestrator/project-1")
		_write(repo, body("\tx='abc'", "y=1"))
		_run_git(repo, "commit", "--quiet", "-m", "AI implementation for issue #7358 (#7365)", date=_D1_IMPL)
		_run_git(repo, "checkout", "--quiet", "main")
		_write(repo, body("\tx='ab'", "y=2"))
		_run_git(repo, "commit", "--quiet", "-m", "main edit (#7376)", date=_D2_MAIN)
		_run_git(repo, "checkout", "--quiet", "orchestrator/project-1")
		_run_git(repo, "merge", "--no-ff", "--no-commit", "main", date=_D3_RESOLVE, check=False)
		_write(repo, body("\tx='aXbc'", "y=2"))
		_run_git(repo, "commit", "--quiet", "-m", "[ai-merge-resolve] resolve merge conflicts", date=_D3_RESOLVE)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		state = _fingerprint_state()
		state["7358"]["must_contain"] = [{"file": _TARGET_PATH, "regex": regex}]
		rc, out = _verify(repo, head, state)
		assert rc == 1, out
		assert "FINGERPRINT_RESOLVER_UNION_MERGE_V1" not in out, out


def test_resolver_that_dropped_part_of_the_sub_issue_line_still_fails():
	partial = _UNION_LINE.replace("|Bad credentials", "")
	with _sandbox_repo() as repo:
		head = _seed_merge(repo, partial)
		rc, out = _verify(repo, head, _fingerprint_state())
		assert rc == 1, out


def test_resolver_that_added_its_own_text_still_fails():
	invented = _UNION_LINE.replace("|Commenting is disabled", "|Commenting is disabled|Invented by resolver")
	with _sandbox_repo() as repo:
		head = _seed_merge(repo, invented)
		rc, out = _verify(repo, head, _fingerprint_state())
		assert rc == 1, out


def test_single_parent_resolver_commit_still_fails():
	with _sandbox_repo() as repo:
		head = _seed_merge(repo, _UNION_LINE, merge=False)
		rc, out = _verify(repo, head, _fingerprint_state())
		assert rc == 1, out


def test_resolver_commit_before_capture_still_fails():
	with _sandbox_repo() as repo:
		head = _seed_merge(repo, _UNION_LINE)
		rc, out = _verify(repo, head, _fingerprint_state(captured_at="2026-10-15T00:00:00Z"))
		assert rc == 1, out


def test_fails_closed_without_captured_at_and_in_working_tree_mode():
	with _sandbox_repo() as repo:
		head = _seed_merge(repo, _UNION_LINE)
		rc, out = _verify(repo, head, _fingerprint_state(captured_at=None))
		assert rc == 1, out
		rc, out = _verify(repo, None, _fingerprint_state())
		assert rc == 1, out


def main() -> int:
	failures: list[str] = []
	for name, value in sorted(globals().items()):
		if not name.startswith("test_") or not callable(value):
			continue
		try:
			value()
		except AssertionError as exc:
			failures.append(f"{name}: {exc}")
		except Exception as exc:  # noqa: BLE001 — surface unexpected errors verbatim
			failures.append(f"{name}: {type(exc).__name__}: {exc}")
	if failures:
		print("FAIL")
		for line in failures:
			print(f"  {line}")
		return 1
	print("PASS")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
