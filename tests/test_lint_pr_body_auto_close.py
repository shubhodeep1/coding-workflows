#!/usr/bin/env python3
"""Tests for scripts/lint_pr_body_auto_close.py — the enforcement check for
CLAUDE.md §19 / unattended_system_instructions.md §19 (the rule that
forbids auto-close keywords against ai:orchestrator-tracking issues).

The lint script is the executable counterpart to the prose rule; without
the test suite a future docs edit that silently widens the keyword list
(or a regex change that misses a keyword) would let the project-#2734
auto-close failure mode recur. Each test pins one of the script's
contract guarantees:

  1. The keyword list covers every variant GitHub auto-closes on.
  2. The regex catches the cross-product (case, separator, owner/repo).
  3. The regex does NOT catch substring matches (e.g. 'prefixes #1').
  4. A tracking-labeled issue triggers a violation (exit 1).
  5. A non-tracking issue triggers no violation (exit 0).
  6. Lookup failure with --fail-open is treated as no violation.
  7. Lookup failure without --fail-open is exit 2.
  8. Commit messages are scanned in addition to the PR body.
  9. Cache: repeated references to the same issue make one lookup, not N.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "lint_pr_body_auto_close.py"


def _import_lint_module():
	# Import the script as a module so we can call its functions directly
	# (subprocess-based testing would also work, but direct calls are 1000x
	# faster and let us inject a mock label_lookup without spinning up gh).
	spec = importlib.util.spec_from_file_location("lint_pr_body_auto_close", SCRIPT_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules["lint_pr_body_auto_close"] = module
	spec.loader.exec_module(module)
	return module


def test_keyword_list_covers_all_github_auto_close_variants():
	# GitHub's auto-close keyword list per
	# https://docs.github.com/articles/closing-issues-using-keywords —
	# the exhaustive set as of writing this lint. The script's list MUST
	# include every entry; if GitHub adds a new keyword the lint becomes
	# silently weaker until this test is updated.
	expected_minimum = {
		"close", "closes", "closed",
		"fix", "fixes", "fixed",
		"resolve", "resolves", "resolved",
	}
	mod = _import_lint_module()
	actual = set(k.lower() for k in mod.AUTO_CLOSE_KEYWORDS)
	missing = expected_minimum - actual
	assert not missing, (
		f"AUTO_CLOSE_KEYWORDS is missing GitHub auto-close keyword(s): {sorted(missing)}. "
		f"GitHub will still auto-close issues referenced by these keywords on merge to "
		f"the default branch, so without them in the list the lint cannot prevent the "
		f"project-#2734 failure mode."
	)


def test_regex_matches_canonical_cases():
	mod = _import_lint_module()
	cases = [
		("Fixes #2734", "Fixes", None, 2734),
		("fixes #2734", "fixes", None, 2734),
		("FIXES #2734", "FIXES", None, 2734),
		("Closes #100", "Closes", None, 100),
		("Resolves owner/repo#42", "Resolves", "owner/repo", 42),
		("fixed #1", "fixed", None, 1),
		("resolves #999999", "resolves", None, 999999),
		# `Close #N` with no plural / no separator is the GitHub-supported
		# minimal form and must still match.
		("Close #5", "Close", None, 5),
	]
	for text, keyword, referenced_repo, issue in cases:
		matches = mod._scan_text("test", text)
		assert len(matches) == 1, (
			f"regex failed to match canonical case {text!r}; got {matches!r}"
		)
		_, _, kw, repo_ref, iss = matches[0]
		assert kw.lower() == keyword.lower()
		assert repo_ref == referenced_repo
		assert iss == issue


def test_regex_matches_github_url_issue_form():
	# GitHub's auto-close logic fires equally on the URL form
	# (https://github.com/owner/repo/issues/N) and the short-form (#N).
	# The implement.yml flow uses URL form in its hardcoded PR body
	# ("Automated implementation. Closes ${ISSUE_URL}"), and the lint
	# must catch URL-form keyword references against tracking issues.
	# See https://docs.github.com/articles/closing-issues-using-keywords.
	mod = _import_lint_module()
	cases = [
		("Closes https://github.com/owner/repo/issues/2734", "Closes", "owner/repo", 2734),
		("Fixes https://github.com/owner/repo/issues/100", "Fixes", "owner/repo", 100),
		("Resolves https://github.com/owner/repo/issues/9", "Resolves", "owner/repo", 9),
		# Lowercase + http (no s) — GitHub treats both the same.
		("fixes http://github.com/owner/repo/issues/1", "fixes", "owner/repo", 1),
	]
	for text, keyword, referenced_repo, issue in cases:
		matches = mod._scan_text("test", text)
		assert len(matches) == 1, (
			f"regex failed to match URL-form case {text!r}; got {matches!r}. "
			f"GitHub auto-closes on this form too, so the lint must catch it."
		)
		_, _, kw, repo_ref, iss = matches[0]
		assert kw.lower() == keyword.lower()
		assert repo_ref == referenced_repo
		assert iss == issue


def test_regex_does_not_match_substring_or_unrelated_words():
	mod = _import_lint_module()
	false_positives = [
		"prefixes #1",        # substring of 'fixes' but not a word-boundary match
		"affixed #1",          # ends with 'fixed' but not a word boundary
		"reclose #1",          # prefix-glued
		"foreclose #1",        # contains 'close' but not a standalone keyword
		"some text #2734",     # no keyword
		"see issue #2734",     # 'see' is not an auto-close keyword
		"link to #2734",       # no keyword
		"Refs #2734",          # the recommended replacement — must NOT be flagged
		"Related to #2734",    # also the recommended replacement
		"Closes: #2734",       # GitHub does not auto-close on colon-form syntax
		"Resolves: https://github.com/owner/repo/issues/9",
	]
	for text in false_positives:
		matches = mod._scan_text("test", text)
		assert matches == [], (
			f"regex falsely matched {text!r}; got {matches!r}. "
			f"This would create noise for legitimate PR bodies and erode trust in the lint."
		)


def test_markdown_code_examples_in_pr_body_are_ignored():
	mod = _import_lint_module()
	text = (
		"Historical example: `Fixes #2734` should not be linted.\n\n"
		"```markdown\n"
		"Closes #2734\n"
		"```\n\n"
		"Real directive: Closes #99\n"
	)
	matches = mod._scan_text("PR body", text, markdown=True)
	assert len(matches) == 1, f"expected only the non-code-span directive to match, got {matches!r}"
	assert matches[0][4] == 99


def test_markdown_double_backtick_code_examples_are_ignored():
	mod = _import_lint_module()
	text = (
		"Historical example: ``Fixes #2734`` should not be linted.\n\n"
		"Real directive: Closes #99\n"
	)
	matches = mod._scan_text("PR body", text, markdown=True)
	assert len(matches) == 1, f"expected only the non-code-span directive to match, got {matches!r}"
	assert matches[0][4] == 99


def test_workflow_lints_pr_title_and_body_together():
	workflow = (REPO_ROOT / ".github/workflows/lint-pr-body-auto-close.yml").read_text(encoding="utf-8")
	assert 'PR_TITLE: ${{ github.event.pull_request.title }}' in workflow
	assert 'PR_BODY: ${{ github.event.pull_request.body }}' in workflow
	assert "printf '%s\\n\\n%s' \"${PR_TITLE:-}\" \"${PR_BODY:-}\" > /tmp/pr-body-lint/body.txt" in workflow


def test_implement_preflight_lints_title_and_body_together():
	workflow = (REPO_ROOT / ".github/workflows/implement.yml").read_text(encoding="utf-8")
	assert 'PR_TITLE_FILE="${RUNTIME_DIR}/pr-body-lint/title.txt"' in workflow
	assert 'PR_BODY_FILE="${RUNTIME_DIR}/pr-body-lint/body.txt"' in workflow
	assert 'PR_LINT_FILE="${RUNTIME_DIR}/pr-body-lint/lint-input.txt"' in workflow
	assert 'PR_TITLE="AI implementation for issue #${ISSUE_NUMBER}"' in workflow
	assert "printf '%s\\n\\n' \"${PR_TITLE}\" > \"${PR_LINT_FILE}\"" in workflow
	assert 'cat "${PR_BODY_FILE}" >> "${PR_LINT_FILE}"' in workflow


def test_cross_repo_reference_looks_up_referenced_repo_not_current_repo():
	mod = _import_lint_module()
	lookups: list[tuple[str, int]] = []

	def fake_lookup(repo: str, issue: int) -> list[str]:
		lookups.append((repo, issue))
		if (repo, issue) == ("other/repo", 2734):
			return ["ai:orchestrator-tracking"]
		return []

	violations, errors = mod.lint(
		pr_body="Fixes other/repo#2734\n",
		commit_messages=[],
		repo="owner/current",
		fail_open_on_lookup_error=False,
		label_lookup=fake_lookup,
	)
	assert errors == []
	assert lookups == [("other/repo", 2734)]
	assert len(violations) == 1
	assert violations[0].issue == 2734
	assert violations[0].issue_repo == "other/repo"


def test_tracking_labeled_issue_produces_violation():
	mod = _import_lint_module()
	def fake_lookup(repo: str, issue: int) -> list[str]:
		assert repo == "owner/repo"
		return ["ai:orchestrator-tracking", "ai:merged"] if issue == 2734 else []
	violations, errors = mod.lint(
		pr_body="Some description.\n\nFixes #2734\n",
		commit_messages=[],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		label_lookup=fake_lookup,
	)
	assert errors == []
	assert len(violations) == 1
	v = violations[0]
	assert v.issue == 2734
	assert v.keyword.lower() == "fixes"
	# The formatted message must reference §19 + the postmortem so the
	# developer knows where to look for the rule.
	formatted = v.format()
	assert "§19" in formatted
	assert "postmortem" in formatted.lower() or "2026-05-18" in formatted
	assert "Refs #" in formatted  # recommended replacement


def test_non_tracking_issue_produces_no_violation():
	mod = _import_lint_module()
	def fake_lookup(repo: str, issue: int) -> list[str]:
		# Issue exists, has labels, but NOT the tracking label.
		return ["bug", "good first issue"]
	violations, errors = mod.lint(
		pr_body="Fixes #100\n",
		commit_messages=[],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		label_lookup=fake_lookup,
	)
	assert errors == []
	assert violations == []


def test_lookup_failure_with_fail_open_is_silent_pass():
	mod = _import_lint_module()
	def fake_lookup(repo: str, issue: int) -> list[str] | None:
		return None  # simulate gh failure
	violations, errors = mod.lint(
		pr_body="Fixes #2734\n",
		commit_messages=[],
		repo="owner/repo",
		fail_open_on_lookup_error=True,
		label_lookup=fake_lookup,
	)
	assert violations == []
	# Errors are still recorded (operator visibility) but the lint
	# does not block the PR.
	assert len(errors) == 1
	assert "could not fetch labels" in errors[0]


def test_lookup_failure_without_fail_open_reports_error_for_exit_2():
	mod = _import_lint_module()
	def fake_lookup(repo: str, issue: int) -> list[str] | None:
		return None
	violations, errors = mod.lint(
		pr_body="Fixes #2734\n",
		commit_messages=[],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		label_lookup=fake_lookup,
	)
	# No violations (we can't prove the issue IS tracking-labeled) but
	# errors are populated so main() returns exit 2.
	assert violations == []
	assert len(errors) == 1


def test_lookup_failure_is_not_cached_across_later_references():
	mod = _import_lint_module()
	call_count = {"n": 0}
	def flaky_lookup(repo: str, issue: int) -> list[str] | None:
		call_count["n"] += 1
		if call_count["n"] == 1:
			return None
		return ["ai:orchestrator-tracking"]
	violations, errors = mod.lint(
		pr_body="Fixes #2734\nCloses #2734\n",
		commit_messages=[],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		label_lookup=flaky_lookup,
	)
	assert call_count["n"] == 2
	assert len(errors) == 1
	assert len(violations) == 1
	assert violations[0].keyword.lower() == "closes"


def test_commit_messages_are_scanned_in_addition_to_pr_body():
	mod = _import_lint_module()
	def fake_lookup(repo: str, issue: int) -> list[str]:
		return ["ai:orchestrator-tracking"] if issue == 2734 else []
	# PR body is clean; the violation is hidden in a commit message
	# (GitHub squash-merge would still use the PR body, but plain merge
	# commits to the default branch would auto-close on the keyword in the
	# commit message).
	violations, errors = mod.lint(
		pr_body="Some clean description with Refs #2734 only.\n",
		commit_messages=[(1, "fix: thing\n\nFixes #2734\n")],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		label_lookup=fake_lookup,
	)
	assert errors == []
	assert len(violations) == 1
	assert "commit message" in violations[0].source


def test_repeated_references_to_same_issue_cache_the_lookup():
	mod = _import_lint_module()
	call_count = {"n": 0}
	def counting_lookup(repo: str, issue: int) -> list[str]:
		call_count["n"] += 1
		return ["ai:orchestrator-tracking"] if issue == 2734 else []
	body = (
		"This PR Fixes #2734 and Closes #2734 and Resolves #2734.\n"
		"Also fixes #999 (not tracking).\n"
	)
	violations, errors = mod.lint(
		pr_body=body,
		commit_messages=[(1, "Resolves #2734\n")],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		label_lookup=counting_lookup,
	)
	# 4 matches against #2734 + 1 against #999 = 2 unique issues = 2 lookups.
	# Per CLAUDE.md §15 (GitHub API call hygiene), repeated refs to the same
	# issue must not multiply lookup calls.
	assert call_count["n"] == 2, (
		f"label_lookup was called {call_count['n']} times for 2 unique issues — "
		f"the per-issue cache regressed. CLAUDE.md §15 forbids per-iteration "
		f"gh calls; this lint must call gh once per unique issue, not once "
		f"per keyword reference."
	)
	# All four #2734 references should produce 4 violations (one per match).
	assert len([v for v in violations if v.issue == 2734]) == 4
	assert len([v for v in violations if v.issue == 999]) == 0


def test_empty_pr_body_is_silent_pass():
	mod = _import_lint_module()
	violations, errors = mod.lint(
		pr_body="",
		commit_messages=[],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		label_lookup=lambda r, i: [],
	)
	assert violations == []
	assert errors == []


def test_main_exit_codes():
	# End-to-end via the script's main() entry point.
	mod = _import_lint_module()
	tmp = Path(tempfile.mkdtemp(prefix="lint-pr-body-"))
	try:
		body_path = tmp / "body.txt"
		body_path.write_text("Fixes #2734\n", encoding="utf-8")
		# Inject a fake label lookup by monkey-patching the module-level
		# function the script's `lint()` uses by default.
		original = mod._fetch_issue_labels_gh
		mod._fetch_issue_labels_gh = lambda repo, issue: (
			["ai:orchestrator-tracking"] if issue == 2734 else []
		)
		try:
			# Simulate CLI args via sys.argv.
			old_argv = sys.argv
			sys.argv = [
				"lint_pr_body_auto_close.py",
				"--pr-body-file", str(body_path),
				"--repo", "owner/repo",
			]
			try:
				rc = mod.main()
			finally:
				sys.argv = old_argv
			assert rc == 1, f"expected exit 1 (violation), got {rc}"
		finally:
			mod._fetch_issue_labels_gh = original
	finally:
		import shutil
		shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
	try:
		import sys as _sys
		_sys.stdout.reconfigure(line_buffering=True)
	except Exception:
		pass
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}", flush=True)
			passed += 1
		except Exception as e:
			print(f"  FAIL  {name}: {e}", flush=True)
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total", flush=True)
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
