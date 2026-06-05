#!/usr/bin/env python3
"""Regression coverage for the must_not_contain post-capture-reintroduction false-positive defense.

The wave-dispatch fingerprint gate captures each merged sub-issue's *deleted*
lines as exact (``re.escape``) ``must_not_contain`` regexes at merge time.
When a line the sub-issue genuinely removed is later *re-introduced* on the
integration branch by a non-resolver commit — most commonly a back-merge of
the default branch whose conflict resolution kept the default branch's
still-present copy — the verifier used to report a fake "intent silently
reverted" violation that wrongly blocked the next wave's dispatch.

This reproduces the project #3042 wave-8 incident, where the ``Merge main into
orchestrator/project-3042`` back-merge (a judge-authored merge, NOT an
``[ai-merge-resolve]`` resolver commit) re-added
``local stall_secs=$(( STALL_THRESHOLD_MINUTES * 60 ))`` to
``build_active_issue_set()`` after issue #3051 had deleted it — while #3051's
actual refactor and every other ``must_contain`` fingerprint stayed intact.
The gate's sole purpose is catching the integration-sync conflict resolver
reverting intent, so a must_not_contain reappearance is only a genuine
regression when the pickaxe-identified commit that re-added the line is an
``[ai-merge-resolve]`` commit after capture — exactly mirroring the
must_contain post-capture-evolution defense in
``test_verify_integration_fingerprints_post_capture_evolution.py``.

Asserts that:

  * the verifier's
    ``_is_must_not_contain_post_capture_evolution_false_positive`` helper
    returns the re-introducing non-resolver commit SHA when the line was
    re-added after capture by a non-resolver commit;
  * it fails CLOSED (returns None) when the pickaxe-identified
    line-reintroduction commit is an ``[ai-merge-resolve]`` resolver commit,
    when ``captured_at`` is missing/unparseable, in working-tree mode (no
    ref), and when the line was not re-added after capture at all;
  * end-to-end ``main(['--ref', <sha>, fingerprints.json])`` exits 0 (the
    violation is skipped) and emits the
    ``FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1`` marker;
  * end-to-end still FAILS (exit 1) on a genuine resolver-introduced
    reintroduction and when ``captured_at`` is absent.
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

# Three well-separated commit dates so captured_at can sit strictly between
# the sub-issue deletion (D1) and the later reintroduction (D3).
_D1_DELETE = "2026-06-02T00:00:00Z"
_CAPTURED_AT = "2026-06-03T00:00:00Z"
_D3_LATER = "2026-06-04T00:00:00Z"

_TARGET_PATH = "scripts/sample_poll.sh"

# Line a sub-issue deleted (captured under must_not_contain). Capture strips
# leading whitespace before re.escape, so the stored regex is the escaped
# stripped line; re.search still matches it inside the indented source line.
_FORBIDDEN_LINE = "\tlocal stall_secs=$(( STALL_THRESHOLD_MINUTES * 60 ))"
_MUST_NOT_CONTAIN_REGEX = re.escape(_FORBIDDEN_LINE.strip())


def _file_with_line() -> str:
	return "\n".join([
		"#!/usr/bin/env bash",
		"build_active_issue_set() {",
		"\tlocal now_epoch",
		'\tnow_epoch="$(date +%s)"',
		_FORBIDDEN_LINE,
		'\techo "${now_epoch} ${stall_secs}"',
		"}",
		"",
	])


def _file_without_line() -> str:
	return "\n".join([
		"#!/usr/bin/env bash",
		"build_active_issue_set() {",
		"\tlocal now_epoch",
		'\tnow_epoch="$(date +%s)"',
		'\tlocal threshold_secs',
		'\tthreshold_secs="$(workflow_run_stall_threshold_seconds)"',
		'\techo "${now_epoch} ${threshold_secs}"',
		"}",
		"",
	])


def _verifier_module():
	spec = importlib.util.spec_from_file_location(
		"verify_integration_fingerprints",
		REPO_ROOT / "scripts" / "verify_integration_fingerprints.py",
	)
	assert spec is not None and spec.loader is not None
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


def _run_git(cwd: Path, *args: str, date: str | None = None) -> str:
	env = os.environ.copy()
	env.update({
		"GIT_AUTHOR_NAME": "Test Bot",
		"GIT_AUTHOR_EMAIL": "test@example.invalid",
		"GIT_COMMITTER_NAME": "Test Bot",
		"GIT_COMMITTER_EMAIL": "test@example.invalid",
	})
	if date is not None:
		env["GIT_AUTHOR_DATE"] = date
		env["GIT_COMMITTER_DATE"] = date
	result = subprocess.run(
		["git", *args],
		cwd=str(cwd),
		capture_output=True,
		check=True,
		env=env,
	)
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
	"""Each test builds a fresh sandbox repo that reuses (ref, path,
	captured_at) cache keys, so the shared defense cache must be cleared
	between tests to keep coverage isolated."""
	mod._POST_CAPTURE_EVOLUTION_CACHE.clear()
	mod._PR_MERGE_COMMIT_CACHE.clear()
	mod._PARTIAL_REMOVAL_POST_MERGE_CACHE.clear()


@contextlib.contextmanager
def _sandbox_repo() -> Iterator[Path]:
	td = Path(tempfile.mkdtemp(prefix="verifier-post-capture-reintroduction-test-"))
	try:
		_run_git(td, "init", "--quiet", "--initial-branch=main")
		_run_git(td, "config", "commit.gpgsign", "false")
		_run_git(td, "config", "tag.gpgsign", "false")
		_run_git(td, "config", "gpg.format", "openpgp")
		yield td
	finally:
		shutil.rmtree(td, ignore_errors=True)


def _write(repo: Path, content: str) -> None:
	target = repo / _TARGET_PATH
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_text(content, encoding="utf-8")
	_run_git(repo, "add", _TARGET_PATH)


def _seed_deletion_commits(repo: Path) -> None:
	"""Commit 1 adds the line (pre-existing); commit 2 is the sub-issue PR
	squash-merge that DELETES it (subject ends with the merge marker so the
	partial-removal defense can locate the PR merge commit and confirm the
	line was genuinely absent there)."""
	_write(repo, _file_with_line())
	_run_git(repo, "commit", "--quiet", "-m", "pre-existing stall_secs", date="2026-06-01T00:00:00Z")
	_write(repo, _file_without_line())
	_run_git(repo, "commit", "--quiet", "-m", "AI implementation for issue #3051 (#3056)", date=_D1_DELETE)


def _seed_false_positive_repo(repo: Path) -> str:
	"""Deletion (D1 < captured_at) followed by a non-resolver reintroduction
	(D3 > captured_at), e.g. a back-merge of the default branch. Returns the
	re-introducing commit SHA."""
	_seed_deletion_commits(repo)
	_write(repo, _file_with_line())
	_run_git(
		repo, "commit", "--quiet",
		"-m", "Merge main into orchestrator/project-3042",
		date=_D3_LATER,
	)
	return _run_git(repo, "rev-parse", "HEAD").strip()


def _seed_resolver_regression_repo(repo: Path) -> str:
	"""Deletion followed by an [ai-merge-resolve] commit that re-adds the
	line — a genuine regression the defense must NOT mask. Returns the
	resolver commit SHA."""
	_seed_deletion_commits(repo)
	_write(repo, _file_with_line())
	_run_git(
		repo, "commit", "--quiet",
		"-m", "[ai-merge-resolve] resolve merge conflicts",
		date=_D3_LATER,
	)
	return _run_git(repo, "rev-parse", "HEAD").strip()


def _fingerprint_state(captured_at: str | None) -> dict:
	entry: dict = {
		"issue": 3051,
		"pr": 3056,
		"must_contain": [],
		"must_not_contain": [{"file": _TARGET_PATH, "regex": _MUST_NOT_CONTAIN_REGEX}],
		"must_not_exist": [],
	}
	if captured_at is not None:
		entry["captured_at"] = captured_at
	return {"3051": entry}


# --------------------------------------------------------------------------
# Helper-level coverage
# --------------------------------------------------------------------------

def test_helper_returns_reintroducing_sha_for_non_resolver_post_capture_readd():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_caches(mod)
		reintroducing_sha = _seed_false_positive_repo(repo)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			result = mod._is_must_not_contain_post_capture_evolution_false_positive(
				ref=head, captured_at=_CAPTURED_AT, path=_TARGET_PATH, regex_src=_MUST_NOT_CONTAIN_REGEX,
			)
		finally:
			os.chdir(prev_cwd)
	assert result == reintroducing_sha, (
		"defense must return the non-resolver commit that re-added the line "
		f"after capture; got {result!r}, expected {reintroducing_sha!r}"
	)


def test_helper_fails_closed_when_resolver_reintroduced_after_capture():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_resolver_regression_repo(repo)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			result = mod._is_must_not_contain_post_capture_evolution_false_positive(
				ref=head, captured_at=_CAPTURED_AT, path=_TARGET_PATH, regex_src=_MUST_NOT_CONTAIN_REGEX,
			)
		finally:
			os.chdir(prev_cwd)
	assert result is None, (
		"defense MUST fail closed when an [ai-merge-resolve] commit re-added "
		f"the line after capture (genuine regression); got {result!r}"
	)


def test_helper_fails_closed_in_working_tree_mode():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_false_positive_repo(repo)
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			result = mod._is_must_not_contain_post_capture_evolution_false_positive(
				ref=None, captured_at=_CAPTURED_AT, path=_TARGET_PATH, regex_src=_MUST_NOT_CONTAIN_REGEX,
			)
		finally:
			os.chdir(prev_cwd)
	assert result is None, "defense is scoped to ref-mode; working-tree mode (ref=None) must return None"


def test_helper_fails_closed_on_missing_or_unparseable_captured_at():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_false_positive_repo(repo)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			for bad in (None, "", "   ", "not-a-timestamp", 12345):
				assert mod._is_must_not_contain_post_capture_evolution_false_positive(
					ref=head, captured_at=bad, path=_TARGET_PATH, regex_src=_MUST_NOT_CONTAIN_REGEX,
				) is None, f"captured_at={bad!r} must fail closed"
		finally:
			os.chdir(prev_cwd)


def test_helper_fails_closed_when_line_not_readded_after_capture():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_caches(mod)
		# Only the deletion commits — the line is absent at HEAD and was never
		# re-added, so there is no reappearance to explain away.
		_seed_deletion_commits(repo)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			result = mod._is_must_not_contain_post_capture_evolution_false_positive(
				ref=head, captured_at=_CAPTURED_AT, path=_TARGET_PATH, regex_src=_MUST_NOT_CONTAIN_REGEX,
			)
		finally:
			os.chdir(prev_cwd)
	assert result is None, (
		"with no commit re-adding the line after capture the defense must "
		f"return None; got {result!r}"
	)


# --------------------------------------------------------------------------
# End-to-end verifier coverage (the wave-dispatch gate path: ref-mode strict)
# --------------------------------------------------------------------------

def test_verifier_skips_post_capture_reintroduction_false_positive_with_marker():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_false_positive_repo(repo)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		fp_path = repo / "fingerprints.json"
		fp_path.write_text(json.dumps(_fingerprint_state(_CAPTURED_AT)), encoding="utf-8")
		rc, out, err = _run_verifier(mod, ["--ref", head, str(fp_path)], repo)
	assert rc == 0, f"expected the gate to skip the false positive (exit 0); got rc={rc}, stdout=\n{out}"
	assert "FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1" in err, (
		"verifier must emit the FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1 "
		"marker when it skips a post-capture-reintroduction false positive so the stale "
		"fingerprint stays observable in CI logs; stderr was:\n" + err
	)
	assert "issue=#3051" in err and "pr=#3056" in err and f"file={_TARGET_PATH}" in err
	assert "classified as post-capture reintroduction false positive" in err
	assert "Integration fingerprint verification FAILED" not in out
	assert "Integration fingerprint verification PASSED" in out


def test_verifier_still_fails_on_genuine_resolver_reintroduction():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_resolver_regression_repo(repo)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		fp_path = repo / "fingerprints.json"
		fp_path.write_text(json.dumps(_fingerprint_state(_CAPTURED_AT)), encoding="utf-8")
		rc, out, err = _run_verifier(mod, ["--ref", head, str(fp_path)], repo)
	assert rc == 1, (
		"verifier MUST still fail when an [ai-merge-resolve] commit re-added the "
		f"line after capture (genuine regression). Got rc={rc}, stdout=\n{out}"
	)
	assert "FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1" not in err
	assert "must_not_contain pattern reappeared" in out


def test_verifier_still_fails_when_captured_at_absent_even_if_reintroduced():
	"""Without captured_at the defense cannot prove a legitimate window, so it
	must fail closed and surface the violation (strict legacy behaviour)."""
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_false_positive_repo(repo)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		fp_path = repo / "fingerprints.json"
		fp_path.write_text(json.dumps(_fingerprint_state(captured_at=None)), encoding="utf-8")
		rc, out, _err = _run_verifier(mod, ["--ref", head, str(fp_path)], repo)
	assert rc == 1, (
		"with no captured_at the defense must fail closed; got rc="
		f"{rc}, stdout=\n{out}"
	)
	assert "must_not_contain pattern reappeared" in out


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
