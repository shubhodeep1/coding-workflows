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
	# getattr keeps the fail-closed tests runnable against a verifier that
	# predates the base-deletion defense (red-on-base checks).
	base_deletion_cache = getattr(mod, "_BASE_DELETION_SUPERSEDE_CACHE", None)
	if base_deletion_cache is not None:
		base_deletion_cache.clear()
		mod._BASE_DELETION_FLAG_WARNED = False


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


# --------------------------------------------------------------------------
# Base-deletion supersede defense (issue #9930, unblocking project #9421):
# the default branch deleted a file after a sub-issue's fingerprint was
# captured, and the resolver correctly takes that deletion.
# --------------------------------------------------------------------------

_BASE_DELETION_ENV = "INTEGRATION_FINGERPRINT_BASE_DELETION_SUPERSEDE_ENABLED"
_DELETED_PATH = ".github/workflows/sample-lint.yml"
_DELETED_FILE_LINE = "runs-on: ubuntu-26.04"
_DELETED_MUST_CONTAIN_REGEX = re.escape(_DELETED_FILE_LINE)
_BEFORE_CAPTURE = "2026-06-01T12:00:00Z"
_D4_RESOLVE = "2026-06-04T00:00:00Z"


@contextlib.contextmanager
def _base_deletion_env(flag: str | None = None, base_branch: str | None = None) -> Iterator[None]:
	saved = {name: os.environ.get(name) for name in (_BASE_DELETION_ENV, "BASE_BRANCH")}
	try:
		for name, value in ((_BASE_DELETION_ENV, flag), ("BASE_BRANCH", base_branch)):
			if value is None:
				os.environ.pop(name, None)
			else:
				os.environ[name] = value
		yield
	finally:
		for name, value in saved.items():
			if value is None:
				os.environ.pop(name, None)
			else:
				os.environ[name] = value


def _run_git_nocheck(cwd: Path, *args: str, date: str | None = None) -> int:
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
	return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=False, env=env).returncode


def _write_path(repo: Path, rel: str, content: str) -> None:
	target = repo / rel
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_text(content, encoding="utf-8")
	_run_git(repo, "add", rel)


def _seed_base_deletion_merge(
	repo: Path,
	*,
	delete_subject: str = "Fold the PR-body lint into pr-checks.yml (#9698)",
	delete_date: str = _D3_LATER,
	base_deletes: bool = True,
) -> str | None:
	"""main adds the file (D1); `integration` adds the sub-issue's line (D1);
	main then deletes the file (or, with base_deletes=False, edits another
	file); `integration` merges main without committing and takes the
	deletion (`git rm`). Leaves MERGE_HEAD in place, like the resolver.
	Returns main's deleting commit SHA (None when main kept the file)."""
	_write_path(repo, _DELETED_PATH, "name: sample-lint\njobs: {}\n")
	_run_git(repo, "commit", "--quiet", "-m", "Add sample lint workflow (#9000)", date=_D1_IMPL)
	_run_git(repo, "checkout", "--quiet", "-b", "integration")
	_write_path(repo, _DELETED_PATH, f"name: sample-lint\njobs: {{}}\n{_DELETED_FILE_LINE}\n")
	_run_git(repo, "commit", "--quiet", "-m", "AI implementation for issue #9422 (#9430)", date=_D1_IMPL)
	_run_git(repo, "checkout", "--quiet", "main")
	deleting_sha: str | None = None
	if base_deletes:
		_run_git(repo, "rm", "--quiet", _DELETED_PATH)
		_run_git(repo, "commit", "--quiet", "-m", delete_subject, date=delete_date)
		deleting_sha = _run_git(repo, "rev-parse", "HEAD").strip()
	else:
		_write_path(repo, "README.md", "unrelated\n")
		_run_git(repo, "commit", "--quiet", "-m", "Unrelated main change (#9500)", date=delete_date)
	_run_git(repo, "checkout", "--quiet", "integration")
	_run_git_nocheck(repo, "merge", "--no-commit", "--no-ff", "main", date=_D4_RESOLVE)
	return deleting_sha


def _take_deletion(repo: Path) -> None:
	_run_git(repo, "rm", "--quiet", "--force", "--ignore-unmatch", _DELETED_PATH)
	(repo / _DELETED_PATH).unlink(missing_ok=True)


def _base_deletion_fingerprints(repo: Path, *, must_contain: bool = True, must_not_contain: bool = False) -> Path:
	entry: dict = {
		"issue": 9422,
		"pr": 9430,
		"captured_at": _CAPTURED_AT,
		"must_contain": [{"file": _DELETED_PATH, "regex": _DELETED_MUST_CONTAIN_REGEX}] if must_contain else [],
		"must_not_contain": [{"file": _DELETED_PATH, "regex": re.escape("ubuntu-latest")}] if must_not_contain else [],
		"must_not_exist": [],
	}
	# Written after the merge, as an untracked file inside the sandbox repo
	# (removed with it), like the other tests in this file.
	fp_path = repo / "fingerprints.json"
	fp_path.write_text(json.dumps({"9422": entry}), encoding="utf-8")
	return fp_path


def test_base_deletion_supersedes_missing_must_contain_in_merge_working_tree():
	mod = _verifier_module()
	with _base_deletion_env(), _sandbox_repo() as repo:
		_clear_caches(mod)
		deleting_sha = _seed_base_deletion_merge(repo)
		_take_deletion(repo)
		fp_path = _base_deletion_fingerprints(repo)
		rc, out, err = _run_verifier(mod, [str(fp_path)], repo)
	assert rc == 0, f"base deletion after capture must supersede the must_contain; rc={rc}, stdout=\n{out}\nstderr=\n{err}"
	assert "FINGERPRINT_BASE_DELETION_SUPERSEDED_V1" in err, err
	assert "issue=#9422" in err and "pr=#9430" in err and f"file={_DELETED_PATH}" in err
	assert deleting_sha is not None and f"commit={deleting_sha[:12]}" in err
	assert "base_ref=MERGE_HEAD" in err
	assert "Integration fingerprint verification FAILED" not in out


def test_base_deletion_must_not_contain_on_deleted_path_is_satisfied():
	mod = _verifier_module()
	with _base_deletion_env(), _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_base_deletion_merge(repo)
		_take_deletion(repo)
		fp_path = _base_deletion_fingerprints(repo, must_contain=False, must_not_contain=True)
		rc, out, err = _run_verifier(mod, [str(fp_path)], repo)
	assert rc == 0, f"must_not_contain on a deleted path must be satisfied; rc={rc}, stdout=\n{out}\nstderr=\n{err}"


def test_base_deletion_supersede_in_compare_against_baseline_mode():
	mod = _verifier_module()
	with _base_deletion_env(), _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_base_deletion_merge(repo)
		assert (repo / _DELETED_PATH).exists(), "modify/delete conflict must leave the integration copy"
		fp_path = _base_deletion_fingerprints(repo)
		baseline_path = fp_path.parent / "baseline.json"
		rc, out, err = _run_verifier(mod, ["--baseline-fingerprints-state", str(baseline_path), str(fp_path)], repo)
		assert rc == 0, f"baseline capture must succeed; stdout=\n{out}\nstderr=\n{err}"
		_take_deletion(repo)
		_clear_caches(mod)
		rc, out, err = _run_verifier(mod, ["--compare-against-baseline", str(baseline_path), str(fp_path)], repo)
	assert rc == 0, f"compare mode must accept the base deletion; rc={rc}, stdout=\n{out}\nstderr=\n{err}"
	assert "FINGERPRINT_BASE_DELETION_SUPERSEDED_V1" in err, err


def test_base_deletion_supersede_in_ref_mode_after_resolver_merge():
	mod = _verifier_module()
	with _base_deletion_env(), _sandbox_repo() as repo:
		_clear_caches(mod)
		deleting_sha = _seed_base_deletion_merge(repo)
		_take_deletion(repo)
		_run_git(repo, "commit", "--quiet", "-m", "[ai-merge-resolve] resolve merge conflicts", date=_D4_RESOLVE)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		fp_path = _base_deletion_fingerprints(repo)
		rc, out, err = _run_verifier(mod, ["--ref", head, str(fp_path)], repo)
	assert rc == 0, f"ref mode must accept the base deletion; rc={rc}, stdout=\n{out}\nstderr=\n{err}"
	assert deleting_sha is not None and f"commit={deleting_sha[:12]}" in err, err


def test_base_deletion_resolver_deleted_path_still_fails():
	mod = _verifier_module()
	# (a) main kept the file; the resolver deleted it during the merge.
	with _base_deletion_env(), _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_base_deletion_merge(repo, base_deletes=False)
		_take_deletion(repo)
		fp_path = _base_deletion_fingerprints(repo)
		rc, out, err = _run_verifier(mod, [str(fp_path)], repo)
		assert rc == 1, f"resolver-only deletion must fail; rc={rc}, stdout=\n{out}"
		assert "file does not exist" in out
		assert "FINGERPRINT_BASE_DELETION_SUPERSEDED_V1" not in err
		_run_git(repo, "commit", "--quiet", "-m", "[ai-merge-resolve] resolve merge conflicts", date=_D4_RESOLVE)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		_clear_caches(mod)
		rc, out, err = _run_verifier(mod, ["--ref", head, str(fp_path)], repo)
		assert rc == 1, f"resolver merge dropping the file must fail in ref mode; rc={rc}, stdout=\n{out}"
		assert "FINGERPRINT_BASE_DELETION_SUPERSEDED_V1" not in err
	# (b) the deleting commit is itself an [ai-merge-resolve] commit.
	with _base_deletion_env(), _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_base_deletion_merge(repo, delete_subject="[ai-merge-resolve] resolve merge conflicts")
		_take_deletion(repo)
		fp_path = _base_deletion_fingerprints(repo)
		rc, out, err = _run_verifier(mod, [str(fp_path)], repo)
	assert rc == 1, f"resolver-attributed deletion must fail; rc={rc}, stdout=\n{out}"
	assert "FINGERPRINT_BASE_DELETION_SUPERSEDED_V1" not in err


def test_base_deletion_ref_mode_integration_resolver_commit_still_fails():
	# main keeps the file; a non-merge [ai-merge-resolve] commit on the
	# integration branch deletes it. Ref mode walks the integration history,
	# so the resolver-subject exclusion is what must keep the violation.
	mod = _verifier_module()
	with _base_deletion_env(), _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_base_deletion_merge(repo, base_deletes=False)
		_run_git_nocheck(repo, "merge", "--abort")
		_take_deletion(repo)
		_run_git(repo, "commit", "--quiet", "-m", "[ai-merge-resolve] resolve merge conflicts", date=_D4_RESOLVE)
		head = _run_git(repo, "rev-parse", "HEAD").strip()
		fp_path = _base_deletion_fingerprints(repo)
		rc, out, err = _run_verifier(mod, ["--ref", head, str(fp_path)], repo)
	assert rc == 1, f"integration-side resolver deletion must fail in ref mode; rc={rc}, stdout=\n{out}"
	assert "FINGERPRINT_BASE_DELETION_SUPERSEDED_V1" not in err


def test_base_deletion_before_capture_fails_closed():
	mod = _verifier_module()
	with _base_deletion_env(), _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_base_deletion_merge(repo, delete_date=_BEFORE_CAPTURE)
		_take_deletion(repo)
		fp_path = _base_deletion_fingerprints(repo)
		rc, out, err = _run_verifier(mod, [str(fp_path)], repo)
	assert rc == 1, f"a deletion before capture must not supersede; rc={rc}, stdout=\n{out}"
	assert "FINGERPRINT_BASE_DELETION_SUPERSEDED_V1" not in err


def test_base_deletion_flag_off_keeps_strict_violation():
	mod = _verifier_module()
	for flag in ("false", "FALSE", "maybe"):
		with _base_deletion_env(flag=flag), _sandbox_repo() as repo:
			_clear_caches(mod)
			_seed_base_deletion_merge(repo)
			_take_deletion(repo)
			fp_path = _base_deletion_fingerprints(repo)
			rc, out, err = _run_verifier(mod, [str(fp_path)], repo)
		assert rc == 1, f"flag={flag!r} must keep the strict violation; rc={rc}, stdout=\n{out}"
		assert "FINGERPRINT_BASE_DELETION_SUPERSEDED_V1" not in err
		assert "file does not exist" in out


def test_base_deletion_invalid_flag_logs_warning_once():
	mod = _verifier_module()
	with _base_deletion_env(flag="false"):
		_clear_caches(mod)
		assert mod._base_deletion_supersede_enabled() is False
	with _base_deletion_env(flag="maybe"):
		_clear_caches(mod)
		err_buf = io.StringIO()
		with contextlib.redirect_stderr(err_buf):
			assert mod._base_deletion_supersede_enabled() is False
			assert mod._base_deletion_supersede_enabled() is False
	err = err_buf.getvalue()
	assert err.count(f"invalid {_BASE_DELETION_ENV}='maybe'") == 1, err


def test_base_deletion_flag_on_values_keep_defense():
	mod = _verifier_module()
	for flag in ("true", "TRUE", ""):
		with _base_deletion_env(flag=flag), _sandbox_repo() as repo:
			_clear_caches(mod)
			_seed_base_deletion_merge(repo)
			_take_deletion(repo)
			fp_path = _base_deletion_fingerprints(repo)
			rc, out, err = _run_verifier(mod, [str(fp_path)], repo)
		assert rc == 0, f"flag={flag!r} must keep the defense on; rc={rc}, stdout=\n{out}\nstderr=\n{err}"


def test_base_deletion_uses_origin_base_branch_without_merge_head():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_seed_base_deletion_merge(repo)
		_take_deletion(repo)
		_run_git(repo, "commit", "--quiet", "-m", "Merge main into integration", date=_D4_RESOLVE)
		_run_git(repo, "update-ref", "refs/remotes/origin/main", "main")
		fp_path = _base_deletion_fingerprints(repo)
		with _base_deletion_env(base_branch="main"):
			_clear_caches(mod)
			rc, out, err = _run_verifier(mod, [str(fp_path)], repo)
		assert rc == 0, f"origin/$BASE_BRANCH must supply the base history; rc={rc}, stdout=\n{out}\nstderr=\n{err}"
		assert "base_ref=refs/remotes/origin/main" in err, err
		with _base_deletion_env(base_branch="--output=/tmp/x"):
			_clear_caches(mod)
			rc, out, err = _run_verifier(mod, [str(fp_path)], repo)
		assert rc == 1, f"unsafe BASE_BRANCH must fail closed; rc={rc}, stdout=\n{out}"


@contextlib.contextmanager
def _raise_on_deletion_log(mod) -> Iterator[None]:
	"""Make the defense's `git log --diff-filter=D` raise (history check error)."""
	real_run = mod.subprocess.run

	def _raising_run(cmd, *args, **kwargs):
		if isinstance(cmd, list) and "--diff-filter=D" in cmd:
			raise subprocess.TimeoutExpired(cmd, 1)
		return real_run(cmd, *args, **kwargs)

	mod.subprocess.run = _raising_run
	try:
		yield
	finally:
		mod.subprocess.run = real_run


def test_base_deletion_helper_returns_none_on_history_error():
	mod = _verifier_module()
	with _base_deletion_env(), _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_base_deletion_merge(repo)
		_take_deletion(repo)
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			assert mod._base_deletion_superseding_commit(None, _CAPTURED_AT, _DELETED_PATH) is not None
			_clear_caches(mod)
			with _raise_on_deletion_log(mod):
				helper_result = mod._base_deletion_superseding_commit(None, _CAPTURED_AT, _DELETED_PATH)
		finally:
			os.chdir(prev_cwd)
	assert helper_result is None, f"history error must fail closed; got {helper_result!r}"


def test_base_deletion_history_error_fails_closed():
	mod = _verifier_module()
	# (a) no MERGE_HEAD and BASE_BRANCH unset: no base history to consult.
	with _base_deletion_env(), _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_base_deletion_merge(repo)
		_take_deletion(repo)
		_run_git(repo, "commit", "--quiet", "-m", "Merge main into integration", date=_D4_RESOLVE)
		fp_path = _base_deletion_fingerprints(repo)
		rc, out, err = _run_verifier(mod, [str(fp_path)], repo)
	assert rc == 1, f"no base history must fail closed; rc={rc}, stdout=\n{out}"
	assert "FINGERPRINT_BASE_DELETION_SUPERSEDED_V1" not in err
	# (b) the deletion-history `git log` raises.
	with _base_deletion_env(), _sandbox_repo() as repo:
		_clear_caches(mod)
		_seed_base_deletion_merge(repo)
		_take_deletion(repo)
		fp_path = _base_deletion_fingerprints(repo)
		with _raise_on_deletion_log(mod):
			rc, out, err = _run_verifier(mod, [str(fp_path)], repo)
	assert rc == 1, f"history error must keep the violation; rc={rc}, stdout=\n{out}"
	assert "FINGERPRINT_BASE_DELETION_SUPERSEDED_V1" not in err


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
