#!/usr/bin/env python3
"""Tests for merge-precheck hardening in review_autofix.yml.

These tests verify that the workflow contains the required guardrails to prevent
untracked CI-generated files (e.g. scripts/ai_memory.py) from causing
git merge/reset failures and that failure classification is correct.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"


def _workflow() -> str:
    return REVIEW_AUTOFIX_WF.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fix 1: explicit removal of known CI-generated files before git reset/merge
# ---------------------------------------------------------------------------

def test_workflow_checks_out_pr_head_ref_for_judge_context():
    """Review/autofix must evaluate PR context from the PR head ref."""
    wf = _workflow()
    assert "uses: actions/checkout@v5" in wf, (
        "Expected review_autofix workflow to use actions/checkout@v5"
    )
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in wf, (
        "Expected checkout ref to prefer pull_request.head.sha (PR head context)"
    )


def test_known_ci_artifacts_removed_before_git_reset_in_detect_step():
    """Before git reset --hard HEAD in the detect-conflicts step, the workflow
    must explicitly remove known CI-generated files so they cannot linger as
    untracked files and cause 'Untracked working tree file would be overwritten'
    errors on subsequent git merge invocations."""
    wf = _workflow()
    rm_line = "rm -f scripts/ai_memory.py scripts/ai_memory_lib.py scripts/memory_helpers.sh"
    # Match the actual git command (not a comment line) by requiring a newline
    # immediately before the indented command.
    reset_match = re.search(r"\n\s+git reset --hard HEAD\s*\n", wf)
    rm_pos = wf.find(rm_line)
    assert rm_pos != -1, (
        f"Expected {rm_line!r} to appear in review_autofix.yml"
    )
    assert reset_match is not None, (
        "Expected 'git reset --hard HEAD' command in review_autofix.yml"
    )
    reset_pos = reset_match.start()
    assert rm_pos < reset_pos, (
        "rm -f of known CI artifacts must appear before the first "
        "git reset --hard HEAD so the untracked files are gone before reset runs"
    )


def test_known_ci_artifacts_removed_in_resolve_step():
    """The 'Resolve merge conflicts' step must also remove the same set of
    CI-generated files before running git merge, since it performs its own
    merge invocation."""
    wf = _workflow()
    # There must be at least two occurrences of the rm -f line (one per merge-
    # performing step).
    count = wf.count(
        "rm -f scripts/ai_memory.py scripts/ai_memory_lib.py scripts/memory_helpers.sh"
    )
    assert count >= 2, (
        "Expected at least 2 occurrences of the known-CI-artifact rm -f "
        f"(one per merge step), found {count}"
    )


# ---------------------------------------------------------------------------
# Fix 2: pre-merge guardrail diagnostics
# ---------------------------------------------------------------------------

def test_pre_merge_diagnostics_present():
    """The workflow must emit git status --porcelain and the untracked file list
    before running git merge --no-commit, so future triage is straightforward."""
    wf = _workflow()
    assert "=== pre-merge working tree state ===" in wf, (
        "Expected '=== pre-merge working tree state ===' diagnostic header "
        "before git merge --no-commit"
    )
    assert "git status --porcelain" in wf, (
        "Expected 'git status --porcelain' in the pre-merge diagnostics block"
    )
    assert "git ls-files --others --exclude-standard" in wf, (
        "Expected 'git ls-files --others --exclude-standard' in pre-merge diagnostics"
    )


# ---------------------------------------------------------------------------
# Fix 3: correct failure classification for exit code 128
# ---------------------------------------------------------------------------

def test_exit_128_classified_as_error_not_no_conflicts():
    """When git merge exits 128 (dirty/untracked working tree), the workflow
    must log a clear ::error:: message and NOT fall through to the misleading
    'No merge conflicts detected' path."""
    wf = _workflow()
    assert '[ "${merge_exit}" -eq 128 ]' in wf, (
        "Expected explicit exit-128 check '[ \"${merge_exit}\" -eq 128 ]' "
        "in the failure-classification block"
    )
    # The error annotation must be present
    assert "::error::Merge precheck failed (exit 128)" in wf, (
        "Expected '::error::Merge precheck failed (exit 128)' annotation "
        "so the CI log clearly identifies untracked-file collisions"
    )


def test_exit_128_check_inside_merge_command_failed_guard():
    """The exit-128 branch must be nested inside the 'merge_exit -ne 0 AND
    no MERGE_HEAD' guard so it never fires on a successful merge."""
    wf = _workflow()
    # Find the outer guard and the inner exit-128 check positions
    outer_guard = '[ "${merge_exit}" -ne 0 ] && [ ! -f "$(git rev-parse --git-dir)/MERGE_HEAD" ]'
    inner_check = '[ "${merge_exit}" -eq 128 ]'
    outer_pos = wf.find(outer_guard)
    inner_pos = wf.find(inner_check)
    assert outer_pos != -1, f"Expected outer guard: {outer_guard!r}"
    assert inner_pos != -1, f"Expected inner check for: {inner_check!r}"
    assert inner_pos > outer_pos, (
        "The exit-128 inner check must appear after (inside) the outer "
        "merge_exit -ne 0 && !MERGE_HEAD guard"
    )


# ---------------------------------------------------------------------------
# Fix 4: backward compatibility — MERGE_CONFLICT env var preserved
# ---------------------------------------------------------------------------

def test_merge_conflict_env_var_set_on_exit_128():
    """When exit 128 is detected, MERGE_CONFLICT=true must still be written to
    GITHUB_ENV so downstream steps behave as if there were a conflict."""
    wf = _workflow()
    # The MERGE_CONFLICT=true assignment must appear after the exit-128 check
    # and before exit 0 in that branch.
    idx_128 = wf.find("::error::Merge precheck failed (exit 128)")
    idx_conflict = wf.find('echo "MERGE_CONFLICT=true" >> "$GITHUB_ENV"', idx_128)
    assert idx_128 != -1, "::error:: for exit 128 not found"
    assert idx_conflict != -1, (
        "Expected 'MERGE_CONFLICT=true' to be written to GITHUB_ENV after the "
        "exit-128 error annotation"
    )
