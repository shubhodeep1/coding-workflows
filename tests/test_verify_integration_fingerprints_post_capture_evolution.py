#!/usr/bin/env python3
"""Regression coverage for the must_contain post-capture-evolution false-positive defense.

The wave-dispatch fingerprint gate captures each merged sub-issue's added
lines as exact (``re.escape``) ``must_contain`` regexes at merge time. When
a line is legitimately modified *after* that healthy capture — by a later
sub-issue PR squash-merge, an ``[ai-autofix]`` commit, or an out-of-band
fix PR merged onto the integration branch — the stale exact regex no longer
matches and the verifier used to report a fake "intent silently reverted"
violation that wrongly blocked the next wave's dispatch.

This reproduces the project #3042 wave-7 incident, where PR #3076 inserted
``--skip-git-repo-check`` into issue #3044's codex-exec lines (a normal
squash-merge, not an ``[ai-merge-resolve]`` resolver commit). The gate's
sole purpose is catching the integration-sync conflict resolver reverting
intent, so a must_contain miss is only a genuine regression when the
pickaxe-identified commit that removed the captured line is an
``[ai-merge-resolve]`` commit after capture.

Asserts that:

  * the verifier's ``_is_must_contain_post_capture_evolution_false_positive``
    helper returns the superseding non-resolver commit SHA when the line was
    modified after capture by a non-resolver commit;
  * it fails CLOSED (returns None) when the pickaxe-identified line-removal
    commit is an ``[ai-merge-resolve]`` resolver commit, when ``captured_at``
    is missing/unparseable, in working-tree mode (no ref), and when the file
    was not modified after capture at all;
  * end-to-end ``main(['--ref', <sha>, fingerprints.json])`` exits 0 (the
    violation is skipped) and emits the
    ``FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1`` marker;
  * end-to-end still FAILS (exit 1) on a genuine resolver-introduced
    regression.
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
# the original sub-issue merge (D1) and the later modification (D3).
_D1_IMPL = "2026-06-01T00:00:00Z"
_CAPTURED_AT = "2026-06-02T00:00:00Z"
_D3_LATER = "2026-06-03T00:00:00Z"

_TARGET_PATH = "scripts/sample_codex_exec.sh"

# Original line a sub-issue added (captured under must_contain). Capture
# strips leading whitespace before re.escape, so the stored regex is the
# escaped stripped line; re.search still matches it as a substring.
_ORIGINAL_LINE = '\t\t-- codex --ask-for-approval never exec --model "${MODEL_EDITOR}" --sandbox danger-full-access'
# Later non-resolver modification (project #3042: PR #3076 inserted
# --skip-git-repo-check between `exec` and `--model`). The escaped original
# line no longer substring-matches because `exec --model` became
# `exec --skip-git-repo-check --model`.
_MODIFIED_LINE = '\t\t-- codex --ask-for-approval never exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access'

_MUST_CONTAIN_REGEX = re.escape(_ORIGINAL_LINE.strip())


def _file_with(codex_line: str) -> str:
	return "\n".join([
		"#!/usr/bin/env bash",
		"run_codex() {",
		'\ttimeout 600 \\',
		codex_line,
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
	captured_at) cache keys, so the defense's git-lookup cache must be
	cleared between tests to keep coverage isolated."""
	mod._POST_CAPTURE_EVOLUTION_CACHE.clear()


@contextlib.contextmanager
def _sandbox_repo() -> Iterator[Path]:
	td = Path(tempfile.mkdtemp(prefix="verifier-post-capture-evolution-test-"))
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


def _seed_impl_commit(repo: Path) -> None:
	"""Commit 1: the sub-issue implementation adds the original line."""
	_write(repo, _file_with(_ORIGINAL_LINE))
	_run_git(repo, "commit", "--quiet", "-m", "AI implementation for issue #3044 (#3046)", date=_D1_IMPL)


def _seed_false_positive_repo(repo: Path) -> str:
	"""Healthy capture (D1 < captured_at) followed by a later non-resolver
	modification (D3 > captured_at). Returns the superseding commit SHA."""
	_seed_impl_commit(repo)
	_write(repo, _file_with(_MODIFIED_LINE))
	_run_git(
		repo, "commit", "--quiet",
		"-m", "fix(review-autofix): pass --skip-git-repo-check to in-workspace codex exec calls (#3076)",
		date=_D3_LATER,
	)
	return _run_git(repo, "rev-parse", "HEAD").strip()


def _seed_resolver_regression_repo(repo: Path) -> str:
	"""Healthy capture followed by an [ai-merge-resolve] commit that drops
	the line — a genuine regression the defense must NOT mask. Returns the
	resolver commit SHA."""
	_seed_impl_commit(repo)
	_write(repo, _file_with('\t\t-- codex --ask-for-approval never exec --sandbox danger-full-access'))
	_run_git(
		repo, "commit", "--quiet",
		"-m", "[ai-merge-resolve] resolve merge conflicts",
		date=_D3_LATER,
	)
	return _run_git(repo, "rev-parse", "HEAD").strip()


def _fingerprint_state(captured_at: str | None) -> dict:
	entry: dict = {
		"issue": 3044,
		"pr": 3046,
		"must_contain": [{"file": _TARGET_PATH, "regex": _MUST_CONTAIN_REGEX}],
		"must_not_contain": [],
		"must_not_exist": [],
	}
	if captured_at is not None:
		entry["captured_at"] = captured_at
	return {"3044": entry}


# --------------------------------------------------------------------------
# Helper-level coverage
# --------------------------------------------------------------------------

def test_helper_returns_superseding_sha_for_non_resolver_post_capture_modification():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_caches(mod)
		superseding_sha = _seed_false_positive_repo(repo)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			result = mod._is_must_contain_post_capture_evolution_false_positive(
				ref=head, captured_at=_CAPTURED_AT, path=_TARGET_PATH, regex_src=_MUST_CONTAIN_REGEX,
			)
		finally:
			os.chdir(prev_cwd)
	assert result == superseding_sha, (
		"defense must return the non-resolver commit that modified the line "
		f"after capture; got {result!r}, expected {superseding_sha!r}"
	)


def test_helper_fails_closed_when_resolver_touched_file_after_capture():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_resolver_regression_repo(repo)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			result = mod._is_must_contain_post_capture_evolution_false_positive(
				ref=head, captured_at=_CAPTURED_AT, path=_TARGET_PATH, regex_src=_MUST_CONTAIN_REGEX,
			)
		finally:
			os.chdir(prev_cwd)
	assert result is None, (
		"defense MUST fail closed when an [ai-merge-resolve] commit touched "
		f"the file after capture (possible genuine regression); got {result!r}"
	)


def test_helper_fails_closed_in_working_tree_mode():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_false_positive_repo(repo)
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			result = mod._is_must_contain_post_capture_evolution_false_positive(
				ref=None, captured_at=_CAPTURED_AT, path=_TARGET_PATH, regex_src=_MUST_CONTAIN_REGEX,
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
				assert mod._is_must_contain_post_capture_evolution_false_positive(
					ref=head, captured_at=bad, path=_TARGET_PATH, regex_src=_MUST_CONTAIN_REGEX,
				) is None, f"captured_at={bad!r} must fail closed"
		finally:
			os.chdir(prev_cwd)


def test_helper_fails_closed_when_file_unchanged_after_capture():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_caches(mod)
		# Only the impl commit (D1) — no post-capture modification at all.
		_seed_impl_commit(repo)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			result = mod._is_must_contain_post_capture_evolution_false_positive(
				ref=head, captured_at=_CAPTURED_AT, path=_TARGET_PATH, regex_src=_MUST_CONTAIN_REGEX,
			)
		finally:
			os.chdir(prev_cwd)
	assert result is None, (
		"with no commit touching the file after capture, the miss is not "
		f"explained by evolution and must surface; got {result!r}"
	)


# --------------------------------------------------------------------------
# End-to-end verifier coverage (the wave-dispatch gate path: ref-mode strict)
# --------------------------------------------------------------------------

def test_verifier_skips_post_capture_evolution_false_positive_with_marker():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_false_positive_repo(repo)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		fp_path = repo / "fingerprints.json"
		fp_path.write_text(json.dumps(_fingerprint_state(_CAPTURED_AT)), encoding="utf-8")
		rc, out, err = _run_verifier(mod, ["--ref", head, str(fp_path)], repo)
	assert rc == 0, f"expected the gate to skip the false positive (exit 0); got rc={rc}, stdout=\n{out}"
	assert "FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1" in err, (
		"verifier must emit the FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1 "
		"marker when it skips a post-capture-evolution false positive so the stale "
		"fingerprint stays observable in CI logs; stderr was:\n" + err
	)
	assert "issue=#3044" in err and "pr=#3046" in err and f"file={_TARGET_PATH}" in err
	assert "classified as post-capture evolution false positive" in err
	assert "Integration fingerprint verification FAILED" not in out


def test_verifier_still_fails_on_genuine_resolver_regression():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_resolver_regression_repo(repo)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		fp_path = repo / "fingerprints.json"
		fp_path.write_text(json.dumps(_fingerprint_state(_CAPTURED_AT)), encoding="utf-8")
		rc, out, err = _run_verifier(mod, ["--ref", head, str(fp_path)], repo)
	assert rc == 1, (
		"verifier MUST still fail when an [ai-merge-resolve] commit dropped the "
		f"line after capture (genuine regression). Got rc={rc}, stdout=\n{out}"
	)
	assert "FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1" not in err
	assert "must_contain pattern missing" in out


def test_verifier_still_fails_when_captured_at_absent_even_if_evolved():
	"""Without captured_at the defense cannot prove a legitimate window, so
	it must fail closed and surface the violation (strict legacy behaviour)."""
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
	assert "must_contain pattern missing" in out


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
