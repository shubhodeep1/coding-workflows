#!/usr/bin/env python3
"""Tests for merge-precheck hardening in review_autofix.yml.

These tests verify that the workflow contains the required guardrails to prevent
untracked runtime-populated helper files (for example scripts/ai_memory.py and
bootstrapped support scripts) from causing git merge/reset failures, that
deterministic early merge-topology failures are gated before reviewers run, and
that failure classification is correct.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
# Shared cleanup line: intentionally covers both CI-generated helpers and
# support scripts copied into the working tree during review jobs.
FULL_RM_LINE = (
    "rm -f scripts/ai_memory.py scripts/ai_memory_lib.py "
    "scripts/memory_helpers.sh scripts/openrouter_prompt_cache.py "
    "scripts/cost_audit.py scripts/codex_heartbeat.sh "
    "scripts/review_run_reviewers.sh scripts/review_apply_fixes.sh "
    "scripts/review_rb_judge.sh scripts/pr_checks_lib.sh "
    "scripts/summarize_reviewer_consensus.sh "
    "scripts/check_external_branch_advance.sh 2>/dev/null || true"
)


def _workflow() -> str:
    return REVIEW_AUTOFIX_WF.read_text(encoding="utf-8")


def _section(start_marker: str, end_marker: str) -> str:
    wf = _workflow()
    start = wf.find(start_marker)
    assert start != -1, f"Expected section start marker: {start_marker!r}"
    end = wf.find(end_marker, start)
    assert end != -1, f"Expected section end marker after {start_marker!r}: {end_marker!r}"
    return wf[start:end]


# ---------------------------------------------------------------------------
# Fix 1: explicit removal of runtime-populated helper files before git reset/merge
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
    must explicitly remove the shared runtime-populated helper files so they
    cannot linger as untracked files and cause 'Untracked working tree file
    would be overwritten' errors on subsequent git merge invocations."""
    detect_step = _section(
        "- name: Detect merge conflicts",
        "\n      - name: Prepare merge-conflict resolver prompt and pre-snapshot",
    )
    # Match the actual git command (not a comment line) by requiring a newline
    # immediately before the indented command.
    reset_match = re.search(r"\n\s+git reset --hard HEAD\s*\n", detect_step)
    rm_pos = detect_step.find(FULL_RM_LINE)
    assert rm_pos != -1, (
        f"Expected {FULL_RM_LINE!r} to appear in the Detect merge conflicts step"
    )
    assert reset_match is not None, (
        "Expected 'git reset --hard HEAD' command in the Detect merge conflicts step"
    )
    reset_pos = reset_match.start()
    assert rm_pos < reset_pos, (
        "rm -f of the shared runtime-populated helper files must appear before "
        "git reset --hard HEAD inside the Detect merge conflicts step so the "
        "untracked files are gone before reset runs"
    )


def test_known_ci_artifacts_removed_in_resolve_step():
    """Every merge-performing review_autofix probe should remove the same set
    of runtime-populated helper files before running git merge."""
    pre_review_step = _section(
        "- name: Pre-review deterministic merge-topology gate",
        "\n      - name: Run reviewer models",
    )
    detect_step = _section(
        "- name: Detect merge conflicts",
        "\n      - name: Prepare merge-conflict resolver prompt and pre-snapshot",
    )
    assert FULL_RM_LINE in pre_review_step, (
        "Expected the pre-review merge-topology gate to remove the shared "
        "runtime-helper cleanup line before its git reset/merge probe"
    )
    assert FULL_RM_LINE in detect_step, (
        "Expected the late detect-conflicts step to remove the same shared "
        "runtime-helper cleanup line before its git reset/merge probe"
    )


def test_pre_review_gate_mktemp_fail_open_reachable_under_errexit():
    """The early gate's documented mktemp fail-open checks must remain
    reachable even though the step runs with set -euo pipefail."""
    section = _section(
        "- name: Pre-review deterministic merge-topology gate",
        "\n      - name: Run reviewer models",
    )
    assert 'MERGE_STASH="$(mktemp -d 2>/dev/null || printf \'\')"' in section, (
        "Expected the pre-review gate to keep mktemp -d fail-open under errexit"
    )
    assert 'MERGE_STDERR_FILE="$(mktemp 2>/dev/null || printf \'\')"' in section, (
        "Expected the pre-review gate to keep merge stderr mktemp fail-open under errexit"
    )


def test_pre_review_gate_preserves_pre_assembled_static_artifact():
    """The early merge probe must not delete the pre-assembled reviewer prompt
    artifact that reviewer fan-out consumes later in the same job."""
    section = _section(
        "- name: Pre-review deterministic merge-topology gate",
        "\n      - name: Run reviewer models",
    )
    clean_cmd = (
        "git clean -ffdx -e .codex-workflow-src -e .codex-workflow-src-main "
        "-e pre_assembled_static.txt"
    )
    assert section.count(clean_cmd) >= 3, (
        "Expected every pre-review git clean invocation to preserve "
        "pre_assembled_static.txt for the later reviewer step"
    )


# ---------------------------------------------------------------------------
# Fix 1b: deterministic merge-topology gate runs before reviewers
# ---------------------------------------------------------------------------

def test_pre_review_merge_topology_gate_present_before_reviewers():
    """A deterministic pre-review gate must run before reviewer fan-out so
    obviously stale / structurally unmergeable PRs stop early."""
    wf = _workflow()
    gate_name = "- name: Pre-review deterministic merge-topology gate"
    reviewer_name = "- name: Run reviewer models"
    gate_pos = wf.find(gate_name)
    reviewer_pos = wf.find(reviewer_name)
    assert gate_pos != -1, f"Expected workflow step: {gate_name!r}"
    assert reviewer_pos != -1, f"Expected workflow step: {reviewer_name!r}"
    assert gate_pos < reviewer_pos, (
        "The pre-review deterministic merge-topology gate must appear before "
        "Run reviewer models"
    )


def test_pre_review_gate_reuses_stale_base_classifier_and_merge_probe():
    """The early gate should reuse the existing stale-base classifier and a
    narrow git merge --no-commit probe rather than inventing a new mechanism."""
    section = _section(
        "- name: Pre-review deterministic merge-topology gate",
        "\n      - name: Run reviewer models",
    )
    assert "check_external_branch_advance.sh" in section, (
        "Expected the pre-review merge-topology gate to reuse "
        "check_external_branch_advance.sh"
    )
    assert 'git merge --no-commit --no-ff "origin/${BASE_BRANCH}"' in section, (
        "Expected the pre-review merge-topology gate to run a compact "
        "git merge --no-commit probe against origin/${BASE_BRANCH}"
    )


def test_run_reviewer_models_guarded_by_skip_flag():
    """Reviewer fan-out must be skipped when the early deterministic gate has
    already concluded the branch is stale or structurally unmergeable."""
    reviewer_step = _section(
        "- name: Run reviewer models",
        "\n      - name: Upload per-reviewer logs (always)",
    )
    assert "env.AUTOFIX_STALE_BASE_SKIP != 'true'" in reviewer_step, (
        "Expected Run reviewer models to be guarded by AUTOFIX_STALE_BASE_SKIP"
    )


def test_pre_editor_stale_base_gate_skipped_after_early_short_circuit():
    """If the early deterministic gate already set AUTOFIX_STALE_BASE_SKIP,
    the older pre-editor stale-base gate should not perform redundant work."""
    pre_editor_step = _section(
        "- name: Pre-editor stale-base gate",
        "\n      - name: Install project dependencies (best-effort)",
    )
    assert "env.AUTOFIX_STALE_BASE_SKIP != 'true'" in pre_editor_step, (
        "Expected Pre-editor stale-base gate to skip when the early gate "
        "already short-circuited the run"
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
    late_detect_step = _section(
        "- name: Detect merge conflicts",
        "\n      - name: Prepare merge-conflict resolver prompt and pre-snapshot",
    )
    # Find the outer guard and the inner exit-128 check positions
    outer_guard = '[ "${merge_exit}" -ne 0 ] && [ ! -f "$(git rev-parse --git-dir)/MERGE_HEAD" ]'
    inner_check = '[ "${merge_exit}" -eq 128 ]'
    outer_pos = late_detect_step.find(outer_guard)
    inner_pos = late_detect_step.find(inner_check)
    assert outer_pos != -1, f"Expected outer guard: {outer_guard!r}"
    assert inner_pos != -1, f"Expected inner check for: {inner_check!r}"
    assert inner_pos > outer_pos, (
        "The exit-128 inner check must appear after (inside) the outer "
        "merge_exit -ne 0 && !MERGE_HEAD guard"
    )


def test_late_detect_merge_conflicts_step_preserved():
    """The existing late merge-conflict detection step must remain in place so
    ordinary content conflicts still flow into the resolver path."""
    wf = _workflow()
    assert "- name: Detect merge conflicts" in wf, (
        "Expected the late Detect merge conflicts step to remain present in "
        "review_autofix.yml"
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
