#!/usr/bin/env python3
"""Tests for scripts/check_integration_pr_readiness.py (P3 from
docs/postmortems/2026-05-18-project-2734-stall.md).

Tests focus on the pure logic (branch derivation, checkbox counting,
state decision tree). The commit-status POST is exercised separately
via the workflow itself; we test the function shape rather than the
HTTP call.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_integration_pr_readiness.py"


def _import():
	spec = importlib.util.spec_from_file_location("check_integration_pr_readiness", SCRIPT_PATH)
	assert spec is not None and spec.loader is not None
	mod = importlib.util.module_from_spec(spec)
	sys.modules["check_integration_pr_readiness"] = mod
	spec.loader.exec_module(mod)
	return mod


def test_derive_tracking_issue_from_canonical_branch():
	mod = _import()
	assert mod._derive_tracking_issue("orchestrator/project-2734") == 2734
	assert mod._derive_tracking_issue("orchestrator/project-1") == 1
	assert mod._derive_tracking_issue("orchestrator/project-999999") == 999999


def test_derive_tracking_issue_rejects_non_orchestrator_branches():
	mod = _import()
	# Branches that aren't orchestrator integration branches must return
	# None so the readiness check no-ops on them (a feature PR from
	# claude/foo or main is not subject to this check).
	for branch in (
		"main",
		"stable",
		"claude/fix-something",
		"feature/whatever",
		"orchestrator/project-",        # missing number
		"orchestrator/project-abc",     # non-numeric
		"orchestrator/foo-100",         # wrong prefix
		"orchestrator/project-2734-x",  # suffix
	):
		assert mod._derive_tracking_issue(branch) is None, f"expected None for {branch!r}"


def test_count_checkboxes_canonical_body():
	mod = _import()
	body = """## Project: Some project

### Wave 1
- [ ] **issue-a**: First sub-issue
- [x] **issue-b**: Second sub-issue (done)

### Wave 2
- [ ] **issue-c**: Third (priority 1)
- [X] **issue-d**: Fourth (capital X also counts as checked)

Some prose with `- [ ]` mid-line (not a real task list item).
"""
	unchecked, total = mod._count_checkboxes(body)
	# 4 task list items at line start (`^\s*-\s*\[...]` matches);
	# the in-backtick mid-line literal is correctly ignored because
	# its dash is not at the line start.
	assert total == 4, f"expected 4 total checkboxes, got {total}"
	# 'issue-a' and 'issue-c' are unchecked; 'issue-b' (lowercase x)
	# and 'issue-d' (capital X) are both checked.
	assert len(unchecked) == 2, f"expected 2 unchecked, got {unchecked!r}"
	assert any("issue-a" in t for t in unchecked)
	assert any("issue-c" in t for t in unchecked)


def test_count_checkboxes_no_checkboxes_in_body():
	mod = _import()
	body = "Just some prose.\n\nNo checkboxes at all."
	unchecked, total = mod._count_checkboxes(body)
	assert total == 0
	assert unchecked == []


def test_count_checkboxes_blank_checkbox_item_counts_as_unchecked():
	mod = _import()
	body = "- [ ]\n- [x] done\n"
	unchecked, total = mod._count_checkboxes(body)
	assert total == 2
	assert unchecked == ["<blank checkbox item>"]


def test_count_checkboxes_handles_project_2734_body_shape():
	# Pin against the actual project-#2734 body shape (the orchestrator-
	# emitted template). If this regresses, the script silently
	# mis-counts on real tracking issues.
	mod = _import()
	body = """## Project: Implement the resolver self-heal plan

**Total issues:** 9 | **Waves:** 7

### Wave 1
- [ ] **phase1-verifier-baseline-delta**: Phase 1A (priority 1)
- [ ] **phase6-subissue-test-runs-spike**: Phase 6 spike (priority 4)

### Wave 2
- [ ] **phase1-resolver-bootstrap-wiring**: Phase 1B (priority 1)

### Wave 3
- [ ] **phase2-retry-state-escalation**: Phase 2 (priority 2)

### Wave 4
- [ ] **phase3-tiered-verification**: Phase 3 (priority 2)

### Wave 5
- [ ] **phase4-quarantine-core**: Phase 4A (priority 3)

### Wave 6
- [ ] **phase4-drift-audit-job**: Phase 4B (priority 4)
- [ ] **phase5-branch-rebuild**: Phase 5 (priority 4)

### Wave 7
- [ ] **docs-closeout-and-plan-move**: Close out (priority 5)
"""
	unchecked, total = mod._count_checkboxes(body)
	assert total == 9, f"expected 9 (project #2734 had 9 sub-issues), got {total}"
	assert len(unchecked) == 9, f"expected 9 unchecked, got {len(unchecked)}"


def test_main_posts_noop_success_for_non_orchestrator_branch():
	mod = _import()
	posted: list[tuple[str, str]] = []
	original_post = mod._post_commit_status
	original_fetch = mod._fetch_issue
	old_argv = sys.argv
	try:
		mod._post_commit_status = lambda repo, sha, state, description, target_url="": posted.append((state, description)) or True
		mod._fetch_issue = lambda repo, n: (_ for _ in ()).throw(AssertionError("_fetch_issue should not run for non-orchestrator branches"))
		sys.argv = [
			"check_integration_pr_readiness.py",
			"--head-ref", "claude/fix-something",
			"--head-sha", "deadbeef",
			"--repo", "owner/repo",
		]
		rc = mod.main()
		assert rc == 0
		assert posted == [(
			"success",
			"head ref 'claude/fix-something' is not an orchestrator/project-* branch — readiness check does not apply",
		)]
	finally:
		sys.argv = old_argv
		mod._post_commit_status = original_post
		mod._fetch_issue = original_fetch


def test_main_errors_when_non_orchestrator_status_post_fails():
	mod = _import()
	posted: list[tuple[str, str, str]] = []
	original_post = mod._post_commit_status
	original_fetch = mod._fetch_issue
	old_argv = sys.argv
	try:
		mod._post_commit_status = lambda repo, sha, state, description, target_url="": posted.append((state, description, target_url)) or False
		mod._fetch_issue = lambda repo, n: (_ for _ in ()).throw(AssertionError("_fetch_issue should not run for non-orchestrator branches"))
		sys.argv = [
			"check_integration_pr_readiness.py",
			"--head-ref", "claude/fix-something",
			"--head-sha", "deadbeef",
			"--repo", "owner/repo",
		]
		stderr = io.StringIO()
		with contextlib.redirect_stderr(stderr):
			rc = mod.main()
		assert rc == 1
		assert posted == [(
			"success",
			"head ref 'claude/fix-something' is not an orchestrator/project-* branch — readiness check does not apply",
			"",
		)]
		assert "::error::[integration-pr-readiness] failed to post readiness commit status" in stderr.getvalue()
	finally:
		sys.argv = old_argv
		mod._post_commit_status = original_post
		mod._fetch_issue = original_fetch


def test_main_fails_closed_when_tracking_issue_has_no_checkboxes():
	mod = _import()
	posted: list[tuple[str, str]] = []
	original_post = mod._post_commit_status
	original_fetch = mod._fetch_issue
	old_argv = sys.argv
	try:
		mod._post_commit_status = lambda repo, sha, state, description, target_url="": posted.append((state, description)) or True
		mod._fetch_issue = lambda repo, n: {"labels": [mod.TRACKING_LABEL], "body": "Just prose."}
		sys.argv = [
			"check_integration_pr_readiness.py",
			"--head-ref", "orchestrator/project-2734",
			"--head-sha", "deadbeef",
			"--repo", "owner/repo",
		]
		stderr = io.StringIO()
		with contextlib.redirect_stderr(stderr):
			rc = mod.main()
		assert rc == 0
		assert posted == [(
			"failure",
			"tracking issue #2734 has no checkbox items in its body; readiness check cannot verify completeness",
		)]
		assert "::error::[integration-pr-readiness] tracking issue #2734 has no checkbox items in its body; readiness check cannot verify completeness" in stderr.getvalue()
	finally:
		sys.argv = old_argv
		mod._post_commit_status = original_post
		mod._fetch_issue = original_fetch


def test_main_posts_success_when_all_tracking_boxes_are_ticked():
	mod = _import()
	posted: list[tuple[str, str]] = []
	original_post = mod._post_commit_status
	original_fetch = mod._fetch_issue
	old_argv = sys.argv
	try:
		mod._post_commit_status = lambda repo, sha, state, description, target_url="": posted.append((state, description)) or True
		mod._fetch_issue = lambda repo, n: {
			"labels": [mod.TRACKING_LABEL],
			"body": "- [x] shipped A\n- [X] shipped B\n",
		}
		sys.argv = [
			"check_integration_pr_readiness.py",
			"--head-ref", "orchestrator/project-2734",
			"--head-sha", "deadbeef",
			"--repo", "owner/repo",
		]
		rc = mod.main()
		assert rc == 0
		assert posted == [(
			"success",
			"all 2 sub-issue(s) on #2734 are ticked — integration PR is ready",
		)]
	finally:
		sys.argv = old_argv
		mod._post_commit_status = original_post
		mod._fetch_issue = original_fetch


def test_main_fails_when_any_tracking_checkbox_remains_unchecked():
	mod = _import()
	posted: list[tuple[str, str]] = []
	original_post = mod._post_commit_status
	original_fetch = mod._fetch_issue
	old_argv = sys.argv
	try:
		mod._post_commit_status = lambda repo, sha, state, description, target_url="": posted.append((state, description)) or True
		mod._fetch_issue = lambda repo, n: {
			"labels": [mod.TRACKING_LABEL],
			"body": "- [x] shipped A\n- [ ] outstanding B\n- [X] shipped C\n",
		}
		sys.argv = [
			"check_integration_pr_readiness.py",
			"--head-ref", "orchestrator/project-2734",
			"--head-sha", "deadbeef",
			"--repo", "owner/repo",
		]
		stderr = io.StringIO()
		with contextlib.redirect_stderr(stderr):
			rc = mod.main()
		assert rc == 0
		assert posted == [(
			"failure",
			"1/3 sub-issues on #2734 still unchecked: outstanding B",
		)]
		assert "::warning::[integration-pr-readiness] 1/3 sub-issues on #2734 still unchecked: outstanding B" in stderr.getvalue()
		assert f"::notice::To merge anyway (rare; e.g. de-scoping the remaining work), apply the {mod.OVERRIDE_LABEL!r} label to this PR." in stderr.getvalue()
		assert "::error::[integration-pr-readiness] 1/3 sub-issues on #2734 still unchecked: outstanding B" not in stderr.getvalue()
	finally:
		sys.argv = old_argv
		mod._post_commit_status = original_post
		mod._fetch_issue = original_fetch


def test_main_errors_when_failure_status_cannot_be_posted():
	mod = _import()
	posted: list[tuple[str, str, str]] = []
	original_post = mod._post_commit_status
	original_fetch = mod._fetch_issue
	old_argv = sys.argv
	try:
		mod._post_commit_status = lambda repo, sha, state, description, target_url="": posted.append((state, description, target_url)) or False
		mod._fetch_issue = lambda repo, n: {
			"labels": [mod.TRACKING_LABEL],
			"body": "- [ ] outstanding work\n",
		}
		sys.argv = [
			"check_integration_pr_readiness.py",
			"--head-ref", "orchestrator/project-2734",
			"--head-sha", "deadbeef",
			"--repo", "owner/repo",
		]
		stderr = io.StringIO()
		with contextlib.redirect_stderr(stderr):
			rc = mod.main()
		assert rc == 1
		assert posted == [(
			"failure",
			"1/1 sub-issues on #2734 still unchecked: outstanding work",
			"https://github.com/owner/repo/issues/2734",
		)]
		assert "::error::[integration-pr-readiness] failed to post readiness commit status" in stderr.getvalue()
	finally:
		sys.argv = old_argv
		mod._post_commit_status = original_post
		mod._fetch_issue = original_fetch


def test_post_commit_status_logs_subprocess_failures():
	mod = _import()
	original_run = mod.subprocess.run
	try:
		mod.subprocess.run = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("gh missing"))
		stderr = io.StringIO()
		with contextlib.redirect_stderr(stderr):
			ok = mod._post_commit_status("owner/repo", "deadbeef", "success", "desc")
		assert ok is False
		assert "::warning::[integration-pr-readiness] commit status POST failed: gh missing" in stderr.getvalue()
	finally:
		mod.subprocess.run = original_run


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
