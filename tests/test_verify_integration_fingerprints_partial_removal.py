#!/usr/bin/env python3
"""Regression coverage for the must_not_contain partial-removal false-positive defense.

When a sub-issue PR removes ONE occurrence of a line that recurs at
multiple positions in the same file, the pre-existing capture filters
(net-change, substring-overlap) still emit a ``must_not_contain``
fingerprint for that line. At verify time the unchanged occurrences
trip ``re.search`` and the verifier reports a fake "reappeared"
violation that blocks the next wave's dispatch.

This test sets up a small git repo that reproduces exactly that case
(based on the project #2836 wave 3 incident on
``orchestrator/project-2836`` — PR #2840 changed one of three
``previous_state["consecutive_failure_count"] = 4`` occurrences in
``tests/test_review_conflict_resolve_retry_state.py`` to ``= 7`` and
left the other two intact) and asserts that:

  * the verifier's ``_is_must_not_contain_partial_removal_false_positive``
    helper classifies the captured fingerprint as a false positive
    (returns the merge commit SHA, not None);
  * end-to-end ``main([fingerprints.json])`` exits 0 (the violation is
    skipped) and emits the ``FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1``
    marker plus a human-readable warning;
  * the defense is conservative — when the pattern is truly absent
    from the file at the PR's merge commit (a real regression
    introduced after the merge), the verifier still fails the gate.
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
import sys
import tempfile
from pathlib import Path
from typing import Iterator


REPO_ROOT = Path(__file__).resolve().parent.parent


def _verifier_module():
	spec = importlib.util.spec_from_file_location(
		"verify_integration_fingerprints",
		REPO_ROOT / "scripts" / "verify_integration_fingerprints.py",
	)
	assert spec is not None and spec.loader is not None
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


def _run_git(cwd: Path, *args: str, env_overrides: dict[str, str] | None = None) -> str:
	env = os.environ.copy()
	env.update({
		"GIT_AUTHOR_NAME": "Test Bot",
		"GIT_AUTHOR_EMAIL": "test@example.invalid",
		"GIT_COMMITTER_NAME": "Test Bot",
		"GIT_COMMITTER_EMAIL": "test@example.invalid",
	})
	if env_overrides:
		env.update(env_overrides)
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


def _clear_verifier_caches(mod) -> None:
	"""Defensive: each test must observe a fresh git-lookup state.

	The defense helpers cache merge-commit lookups and file content
	keyed on ``(ref, pr_num, path)`` to avoid duplicate git shells
	across the many patterns / issues a single verify run inspects.
	Tests construct fresh sandbox repos that reuse those keys, so the
	caches must be cleared between tests to keep coverage isolated.
	"""
	mod._PR_MERGE_COMMIT_CACHE.clear()
	mod._PARTIAL_REMOVAL_POST_MERGE_CACHE.clear()


@contextlib.contextmanager
def _sandbox_repo() -> Iterator[Path]:
	td = Path(tempfile.mkdtemp(prefix="verifier-partial-removal-test-"))
	try:
		_run_git(td, "init", "--quiet", "--initial-branch=main")
		# Disable commit signing inside the sandbox so the test stays
		# hermetic on developer machines that have global gpg / ssh
		# signing configured. The test only cares about commit subjects
		# and tree contents, not authorship trust.
		_run_git(td, "config", "commit.gpgsign", "false")
		_run_git(td, "config", "tag.gpgsign", "false")
		_run_git(td, "config", "gpg.format", "openpgp")
		yield td
	finally:
		shutil.rmtree(td, ignore_errors=True)


# Mirrors the project #2836 incident: the same line appears at three
# distinct positions in the file. The "PR" rewrites one occurrence and
# leaves the other two untouched — capture's per-line removal scan
# emits a must_not_contain entry for the line even though the line
# still exists post-merge.
_RECURRING_LINE = '\tprevious_state["consecutive_failure_count"] = 4'
_PRE_PR_FILE_CONTENT = "\n".join([
	"def test_a():",
	_RECURRING_LINE,
	"\tassert True",
	"",
	"def test_b():",
	_RECURRING_LINE,
	"\tassert True",
	"",
	"def test_c():",
	_RECURRING_LINE,
	"\tassert True",
	"",
])
_POST_PR_FILE_CONTENT = "\n".join([
	"def test_a():",
	_RECURRING_LINE,
	"\tassert True",
	"",
	"def test_b():",
	_RECURRING_LINE,
	"\tassert True",
	"",
	"def test_c():",
	'\tprevious_state["consecutive_failure_count"] = 7',
	"\tassert True",
	"",
])
_REGRESSED_FILE_CONTENT = "\n".join([
	"def test_a():",
	"\t# all occurrences truly removed by the PR",
	"\tassert True",
	"",
	"def test_b():",
	"\t# all occurrences truly removed by the PR",
	"\tassert True",
	"",
	"def test_c():",
	"\t# but the resolver later re-inserted one — this MUST fail",
	_RECURRING_LINE,
	"\tassert True",
	"",
])

_TARGET_PATH = "tests/sample_retry_state.py"


def _fingerprint_state_for_issue(issue: int, pr: int, regex_src: str) -> dict:
	return {
		str(issue): {
			"issue": issue,
			"pr": pr,
			"must_contain": [],
			"must_not_contain": [
				{"file": _TARGET_PATH, "regex": regex_src},
			],
			"must_not_exist": [],
		}
	}


def _seed_repo_with_partial_removal_merge(repo: Path, pr_num: int) -> str:
	"""Set up a tiny git history that reproduces the #2836 scenario.

	Returns the merge commit SHA for the simulated PR.
	"""
	target = repo / _TARGET_PATH
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_text(_PRE_PR_FILE_CONTENT, encoding="utf-8")
	_run_git(repo, "add", _TARGET_PATH)
	_run_git(repo, "commit", "--quiet", "-m", "seed file with three occurrences")
	target.write_text(_POST_PR_FILE_CONTENT, encoding="utf-8")
	_run_git(repo, "add", _TARGET_PATH)
	_run_git(repo, "commit", "--quiet", "-m", f"AI implementation for issue #999 (#{pr_num})")
	return _run_git(repo, "rev-parse", "HEAD").strip()


def _seed_repo_with_genuine_regression_merge(repo: Path, pr_num: int) -> str:
	"""Set up a history where the PR truly removes ALL occurrences and a later
	commit re-adds the line — a genuine regression the defense must NOT mask.
	"""
	target = repo / _TARGET_PATH
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_text(_PRE_PR_FILE_CONTENT, encoding="utf-8")
	_run_git(repo, "add", _TARGET_PATH)
	_run_git(repo, "commit", "--quiet", "-m", "seed file with three occurrences")
	target.write_text(
		"\n".join([
			"def test_a():",
			"\t# clean removal by the PR",
			"\tassert True",
			"",
			"def test_b():",
			"\t# clean removal by the PR",
			"\tassert True",
			"",
			"def test_c():",
			"\t# clean removal by the PR",
			"\tassert True",
			"",
		]),
		encoding="utf-8",
	)
	_run_git(repo, "add", _TARGET_PATH)
	merge_msg = f"AI implementation for issue #999 (#{pr_num})"
	_run_git(repo, "commit", "--quiet", "-m", merge_msg)
	merge_commit = _run_git(repo, "rev-parse", "HEAD").strip()
	target.write_text(_REGRESSED_FILE_CONTENT, encoding="utf-8")
	_run_git(repo, "add", _TARGET_PATH)
	_run_git(repo, "commit", "--quiet", "-m", "[ai-merge-resolve] regression sneaks the line back in")
	return merge_commit


def test_partial_removal_helper_returns_merge_commit_when_line_still_present_post_merge():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_verifier_caches(mod)
		merge_sha = _seed_repo_with_partial_removal_merge(repo, pr_num=2840)
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			pattern = re.compile(re.escape(_RECURRING_LINE))
			result = mod._is_must_not_contain_partial_removal_false_positive(
				ref=None, pr_num=2840, path=_TARGET_PATH, pattern=pattern,
			)
		finally:
			os.chdir(prev_cwd)
	assert result == merge_sha, (
		"defense should return the PR's merge commit when the pattern is "
		"still present in the post-merge file (partial removal); got "
		f"{result!r}, expected {merge_sha!r}"
	)


def test_partial_removal_helper_returns_none_when_pattern_truly_removed_at_merge_commit():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_verifier_caches(mod)
		_seed_repo_with_genuine_regression_merge(repo, pr_num=2841)
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			pattern = re.compile(re.escape(_RECURRING_LINE))
			result = mod._is_must_not_contain_partial_removal_false_positive(
				ref=None, pr_num=2841, path=_TARGET_PATH, pattern=pattern,
			)
		finally:
			os.chdir(prev_cwd)
	assert result is None, (
		"defense must NOT classify the violation as a false positive when "
		"the PR truly deleted every occurrence (the post-merge file is "
		"clean and the leftover match is a real regression). Got "
		f"{result!r}, expected None"
	)


def test_partial_removal_helper_fails_open_on_missing_pr_number():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_verifier_caches(mod)
		_seed_repo_with_partial_removal_merge(repo, pr_num=2840)
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			pattern = re.compile(re.escape(_RECURRING_LINE))
			# Non-numeric / sentinel pr_num values must short-circuit to None
			# so the original violation surfaces rather than being silently
			# masked.
			assert mod._is_must_not_contain_partial_removal_false_positive(
				ref=None, pr_num="?", path=_TARGET_PATH, pattern=pattern,
			) is None
			assert mod._is_must_not_contain_partial_removal_false_positive(
				ref=None, pr_num="", path=_TARGET_PATH, pattern=pattern,
			) is None
			assert mod._is_must_not_contain_partial_removal_false_positive(
				ref=None, pr_num=None, path=_TARGET_PATH, pattern=pattern,
			) is None
		finally:
			os.chdir(prev_cwd)


def test_partial_removal_helper_fails_open_when_merge_commit_not_found():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_verifier_caches(mod)
		# Seed the repo but use a PR number that doesn't appear in any
		# commit message — _find_pr_merge_commit_for_path returns None,
		# defense must fail open.
		_seed_repo_with_partial_removal_merge(repo, pr_num=2840)
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			pattern = re.compile(re.escape(_RECURRING_LINE))
			result = mod._is_must_not_contain_partial_removal_false_positive(
				ref=None, pr_num=9999, path=_TARGET_PATH, pattern=pattern,
			)
		finally:
			os.chdir(prev_cwd)
	assert result is None


def test_find_pr_merge_commit_ignores_later_same_path_followup_mentions():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_verifier_caches(mod)
		merge_sha = _seed_repo_with_partial_removal_merge(repo, pr_num=2840)
		target = repo / _TARGET_PATH
		target.write_text(_POST_PR_FILE_CONTENT + "# later follow-up\n", encoding="utf-8")
		_run_git(repo, "add", _TARGET_PATH)
		_run_git(repo, "commit", "--quiet", "-m", "follow-up for #2840")
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			result = mod._find_pr_merge_commit_for_path(None, 2840, _TARGET_PATH)
		finally:
			os.chdir(prev_cwd)
	assert result == merge_sha, (
		"merge-commit lookup must ignore later same-path commits that merely mention "
		f"the PR number; got {result!r}, expected original merge {merge_sha!r}"
	)


def test_verifier_main_skips_must_not_contain_false_positive_with_marker():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_verifier_caches(mod)
		_seed_repo_with_partial_removal_merge(repo, pr_num=2840)
		fingerprints = _fingerprint_state_for_issue(
			issue=2839, pr=2840, regex_src=re.escape(_RECURRING_LINE),
		)
		fp_path = repo / "fingerprints.json"
		fp_path.write_text(json.dumps(fingerprints), encoding="utf-8")
		rc, out, _err = _run_verifier(mod, [str(fp_path)], repo)
	# False positive — verify must succeed (exit 0) and emit the
	# machine-readable marker so the operator audit can find it.
	assert rc == 0, f"expected verifier to skip the false positive (exit 0); got rc={rc}, stdout=\n{out}"
	assert "FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1" in out, (
		"verifier must emit the FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1 "
		"marker when it skips a partial-removal false positive so the "
		"capture-side bug stays observable in CI logs; stdout was:\n" + out
	)
	assert "issue=#2839" in out and "pr=#2840" in out and f"file={_TARGET_PATH}" in out
	assert "classified as capture-side false positive" in out
	assert "Integration fingerprint verification FAILED" not in out


def test_verifier_main_still_fails_on_genuine_regression_after_clean_merge():
	mod = _verifier_module()
	with _sandbox_repo() as repo:
		_clear_verifier_caches(mod)
		_seed_repo_with_genuine_regression_merge(repo, pr_num=2841)
		fingerprints = _fingerprint_state_for_issue(
			issue=2841, pr=2841, regex_src=re.escape(_RECURRING_LINE),
		)
		fp_path = repo / "fingerprints.json"
		fp_path.write_text(json.dumps(fingerprints), encoding="utf-8")
		rc, out, _err = _run_verifier(mod, [str(fp_path)], repo)
	assert rc == 1, (
		"verifier must STILL fail on a genuine regression (the PR truly "
		"removed every occurrence; a later resolver commit re-inserted the "
		f"line). Got rc={rc}, stdout=\n{out}"
	)
	assert "FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1" not in out, (
		"defense must not mask the real regression with the partial-removal "
		"marker; stdout was:\n" + out
	)
	assert "must_not_contain pattern reappeared" in out


_CAPTURE_FUNCTION_SCRIPT = (REPO_ROOT / "scripts" / "orchestrate_poll_process.sh").read_text(encoding="utf-8")


def _extract_capture_heredoc() -> str:
	"""Extract the python heredoc embedded in capture_intent_fingerprints_for_merged_subissue.

	The shell function passes the heredoc to python3 with `python3 - "${diff_file}"`.
	For test coverage we reuse the exact same script body so the test
	pins capture's filter behaviour against the live source.
	"""
	body = _CAPTURE_FUNCTION_SCRIPT
	marker_start = body.index("capture_intent_fingerprints_for_merged_subissue()")
	heredoc_open = body.index("python3 - \"${diff_file}\" <<'PY'", marker_start)
	heredoc_body_start = body.index("\n", heredoc_open) + 1
	heredoc_close = body.index("\nPY\n", heredoc_body_start)
	return body[heredoc_body_start:heredoc_close]


def _run_capture_heredoc(
	*,
	diff_text: str,
	post_merge_ref: str,
	cwd: Path,
) -> dict:
	heredoc = _extract_capture_heredoc()
	diff_path = cwd / "pr.diff"
	diff_path.write_text(diff_text, encoding="utf-8")
	env = os.environ.copy()
	env.update({
		"FINGERPRINT_PER_FILE_CAP": "12",
		"FINGERPRINT_MIN_PATTERN_CHARS": "12",
		"FINGERPRINT_POST_MERGE_REF": post_merge_ref,
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	result = subprocess.run(
		[sys.executable, "-c", heredoc, str(diff_path)],
		cwd=str(cwd),
		capture_output=True,
		check=True,
		env=env,
	)
	stdout = result.stdout.decode("utf-8", errors="replace").strip()
	assert stdout, (
		"capture heredoc returned empty stdout; stderr was:\n"
		+ result.stderr.decode("utf-8", errors="replace")
	)
	return json.loads(stdout)


_PARTIAL_REMOVAL_DIFF = """\
diff --git a/tests/sample_retry_state.py b/tests/sample_retry_state.py
index 1111111..2222222 100644
--- a/tests/sample_retry_state.py
+++ b/tests/sample_retry_state.py
@@ -8,5 +8,5 @@ def test_b():
 def test_c():
-\tprevious_state["consecutive_failure_count"] = 4
+\tprevious_state["consecutive_failure_count"] = 7
 \tassert True
"""


def test_capture_post_merge_filter_drops_partial_removal_false_positive():
	"""Capture-side prevention: a removed line that still appears in the
	post-merge file content MUST NOT land in must_not_contain.
	"""
	with _sandbox_repo() as repo:
		_seed_repo_with_partial_removal_merge(repo, pr_num=2840)
		result = _run_capture_heredoc(
			diff_text=_PARTIAL_REMOVAL_DIFF,
			post_merge_ref="HEAD",
			cwd=repo,
		)
	must_not_contain = result.get("must_not_contain") or []
	# Capture's to_patterns() strips leading whitespace before applying
	# re.escape, so compare against the stripped form.
	regex_src = re.escape(_RECURRING_LINE.strip())
	matches = [
		entry for entry in must_not_contain
		if entry.get("file") == _TARGET_PATH and entry.get("regex") == regex_src
	]
	assert matches == [], (
		"capture must drop must_not_contain entries whose stripped text "
		"still appears in the post-merge file (multi-occurrence partial "
		f"removal). Got must_not_contain={must_not_contain!r}"
	)


def test_capture_post_merge_filter_keeps_must_not_contain_when_line_truly_removed():
	"""Capture-side defense must not over-trigger: when the post-merge
	file genuinely does not contain the removed line, the entry MUST
	survive into must_not_contain (so the verifier still catches a
	later resolver-introduced regression).
	"""
	with _sandbox_repo() as repo:
		_seed_repo_with_genuine_regression_merge(repo, pr_num=2841)
		merge_commit = _run_git(repo, "rev-parse", "HEAD~1").strip()
		result = _run_capture_heredoc(
			diff_text=_PARTIAL_REMOVAL_DIFF,
			post_merge_ref=merge_commit,
			cwd=repo,
		)
	must_not_contain = result.get("must_not_contain") or []
	# Capture's to_patterns() strips leading whitespace before applying
	# re.escape, so compare against the stripped form.
	regex_src = re.escape(_RECURRING_LINE.strip())
	matches = [
		entry for entry in must_not_contain
		if entry.get("file") == _TARGET_PATH and entry.get("regex") == regex_src
	]
	assert matches, (
		"capture must keep must_not_contain entries when the line is truly "
		"gone from the post-merge file — otherwise a later resolver "
		f"regression would silently pass verify. Got must_not_contain={must_not_contain!r}"
	)


def test_capture_post_merge_filter_fails_open_when_ref_is_unresolvable():
	"""If the post-merge ref can't be read (env var empty, branch
	unknown), the filter must leave the candidate must_not_contain
	entries in place so the existing capture invariants stay intact.
	The verifier-side defense will catch any leftover false positives.
	"""
	with _sandbox_repo() as repo:
		_seed_repo_with_partial_removal_merge(repo, pr_num=2840)
		result = _run_capture_heredoc(
			diff_text=_PARTIAL_REMOVAL_DIFF,
			post_merge_ref="",  # unresolvable — skip the filter
			cwd=repo,
		)
	must_not_contain = result.get("must_not_contain") or []
	# Capture's to_patterns() strips leading whitespace before applying
	# re.escape, so compare against the stripped form.
	regex_src = re.escape(_RECURRING_LINE.strip())
	matches = [
		entry for entry in must_not_contain
		if entry.get("file") == _TARGET_PATH and entry.get("regex") == regex_src
	]
	assert matches, (
		"capture must fail open when the post-merge ref is empty so the "
		"verifier-side defense can still inspect the entry; "
		f"got must_not_contain={must_not_contain!r}"
	)


def main() -> int:
	# Surface helpful diagnostics if the verifier module fails to import
	# (e.g. syntax errors in the script under test). The standalone test
	# runner pattern matches scripts/test_verify_integration_fingerprints_baseline.py.
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
