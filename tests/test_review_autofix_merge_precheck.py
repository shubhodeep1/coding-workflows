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


# ---------------------------------------------------------------------------
# Fix 5: blobless partial-clone promisor-fetch recovery in the late detect step
#
# The codex-agent checkout uses `filter: blob:none`, so the 3-way merge
# lazy-fetches blobs from the promisor remote and can fail with exit 128 on a
# "not our ref" / "could not fetch ... from promisor remote" signature even
# when the merge itself is well-formed.  The pre-review merge-topology gate and
# scripts/review_conflict_prepare.sh already backfill and retry on that
# signature; the late detect step must do the same, and must not report an
# infrastructure failure as a hard ::error:: merge outcome.
# ---------------------------------------------------------------------------

PROMISOR_SIGNATURE_GREP = (
    "grep -qiE 'promisor remote|not our ref|could not fetch|fetch-pack|"
    "remote error: upload-pack'"
)


def _detect_step() -> str:
    return _section(
        "- name: Detect merge conflicts",
        "\n      - name: Prepare merge-conflict resolver prompt and pre-snapshot",
    )


def test_detect_step_detects_promisor_fetch_failure_signature():
    """The late detect step must classify the promisor-fetch stderr signature
    instead of treating it as an ordinary merge failure."""
    step = _detect_step()
    assert "merge_promisor_fetch_failure_seen=false" in step, (
        "Expected the detect step to initialise merge_promisor_fetch_failure_seen "
        "before the merge probe"
    )
    assert PROMISOR_SIGNATURE_GREP in step, (
        "Expected the detect step to grep git merge stderr for the partial-clone "
        "promisor fetch failure signature"
    )


def test_detect_step_backfills_blobs_and_retries_merge_once():
    """On the promisor signature the detect step must drop the partial filter,
    refetch the merge inputs, and retry the merge exactly once."""
    step = _detect_step()
    assert "git config --unset-all remote.origin.partialclonefilter" in step, (
        "Expected the detect step to unset remote.origin.partialclonefilter "
        "before refetching blobs"
    )
    assert "git fetch --no-tags --prune --refetch origin" in step, (
        "Expected the detect step to run a --refetch blob backfill"
    )
    # Exactly two merge invocations: the initial probe and the single retry.
    merge_cmd = 'git merge --no-commit --no-ff "origin/${BASE_BRANCH}"'
    assert step.count(merge_cmd) == 2, (
        "Expected exactly two git merge invocations in the detect step (initial "
        f"probe + one blob-backfill retry), found {step.count(merge_cmd)}"
    )
    assert "git merge stderr (after blob backfill): " in step, (
        "Expected the retry to label its stderr so the two probes are "
        "distinguishable in the raw CI log"
    )


def test_detect_step_backfill_refspecs_defined_outside_shallow_branch():
    """The backfill refspec array must be built inside the recovery block, not
    reused from the shallow-clone deepen path — `_merge_base_refspecs` is only
    defined when .git/shallow exists and would be unset under `set -u`."""
    step = _detect_step()
    array_init = '_merge_backfill_refspecs=("+refs/heads/${BASE_BRANCH}:refs/remotes/origin/${BASE_BRANCH}")'
    assert array_init in step, (
        "Expected the recovery block to build its own _merge_backfill_refspecs array"
    )
    assert '"${_merge_backfill_refspecs[@]}"' in step, (
        "Expected the --refetch call to expand _merge_backfill_refspecs"
    )
    assert '"${_merge_base_refspecs[@]}"' not in step.split(array_init)[1], (
        "The blob-backfill refetch must not reuse the shallow-only "
        "_merge_base_refspecs array"
    )


def test_surviving_promisor_failure_downgraded_to_warning():
    """A promisor fetch failure that survives the retry is infrastructure, not a
    merge outcome: it must produce a ::warning:: fail-open, while every other
    exit-128 cause keeps its hard ::error::."""
    step = _detect_step()
    warning = "::warning::Merge precheck: exit 128 from a partial-clone promisor fetch failure"
    error = "::error::Merge precheck failed (exit 128)"
    warn_pos = step.find(warning)
    err_pos = step.find(error)
    assert warn_pos != -1, (
        "Expected a ::warning:: fail-open for a promisor fetch failure surviving "
        "the blob-backfill retry"
    )
    assert err_pos != -1, (
        "The hard ::error:: must be preserved for non-promisor exit-128 causes"
    )
    # The warning is the guarded branch; the error is its else.
    assert warn_pos < err_pos, (
        "Expected the promisor ::warning:: branch to be evaluated before falling "
        "back to the generic exit-128 ::error::"
    )
    assert step.count(PROMISOR_SIGNATURE_GREP) >= 2, (
        "Expected the exit-128 classifier to re-check the promisor signature in "
        "addition to the post-merge detection"
    )


def test_surviving_promisor_failure_still_sets_merge_conflict_true():
    """Fail-open here means handing the run to the conflict resolver, which runs
    its own merge replay with its own backfill and clears MERGE_CONFLICT when
    that replay finds nothing to resolve — so MERGE_CONFLICT must stay true."""
    step = _detect_step()
    warn_pos = step.find(
        "::warning::Merge precheck: exit 128 from a partial-clone promisor fetch failure"
    )
    assert warn_pos != -1, "promisor fail-open warning not found"
    conflict_pos = step.find('echo "MERGE_CONFLICT=true" >> "$GITHUB_ENV"', warn_pos)
    assert conflict_pos != -1, (
        "Expected MERGE_CONFLICT=true to still be written after the promisor "
        "fail-open warning so scripts/review_conflict_prepare.sh gets the run"
    )


def test_initial_promisor_stderr_preserved_in_annotation():
    """After a retry the second probe's stderr may not carry the signature, so
    the original promisor stderr must be appended to the annotation."""
    step = _detect_step()
    assert 'merge_promisor_initial_stderr=""' in step, (
        "Expected the detect step to initialise merge_promisor_initial_stderr"
    )
    assert "| initial promisor stderr: ${merge_promisor_initial_stderr}" in step, (
        "Expected the annotation to append the initial promisor stderr when the "
        "retry produced a different message"
    )


def test_retry_exit_128_classifier_requires_retry_evidence():
    """A retry-side exit 128 must not be downgraded just because the initial
    probe saw the promisor signature; the retry must either repeat the
    signature or emit no stderr at all."""
    pre_review_step = _section(
        "- name: Pre-review deterministic merge-topology gate",
        "\n      - name: Run reviewer models",
    )
    detect_step = _detect_step()
    for label, section in (
        ("pre-review merge-topology gate", pre_review_step),
        ("late detect-conflicts step", detect_step),
    ):
        classifier = section.split('elif [ "${merge_exit}" -eq 128 ]; then', 1)[1]
        classifier_copy = '_merge_classifier_stderr_oneline="${_merge_stderr_oneline}"'
        append_line = (
            '_merge_stderr_oneline="${_merge_stderr_oneline} | initial promisor '
            'stderr: ${merge_promisor_initial_stderr}"'
        )
        assert "merge_promisor_retry_stderr_matches=false" in section, (
            f"Expected the {label} to track whether the retry itself still "
            "matched the promisor signature"
        )
        assert "merge_promisor_retry_stderr_matches=true" in section, (
            f"Expected the {label} to record a promisor-signature match from "
            "the retry stderr"
        )
        assert classifier_copy in section, (
            f"Expected the {label} to snapshot the retry stderr before "
            "appending the initial promisor stderr"
        )
        assert section.find(classifier_copy) < section.find(append_line), (
            f"Expected the {label} to preserve an unmutated retry-stderr copy "
            "for the exit-128 classifier"
        )
        assert '[ "${merge_promisor_retry_stderr_matches}" = "true" ]' in classifier, (
            f"Expected the {label} exit-128 classifier to key off the retry's "
            "own promisor signature"
        )
        assert '[ "${_merge_classifier_stderr_oneline}" = "<git merge produced no stderr>" ]' in classifier, (
            f"Expected the {label} to fall back to the initial promisor signal "
            "only when the retry produced no stderr"
        )
        assert '[ "${_merge_stderr_oneline}" = "<git merge produced no stderr>" ]' not in classifier, (
            f"Expected the {label} classifier to avoid comparing against the "
            "mutated annotation string"
        )
        assert '|| grep -qiE \'promisor remote|not our ref|could not fetch|fetch-pack|remote error: upload-pack\'' not in classifier, (
            f"The {label} must not treat the initial promisor flag alone as "
            "enough to downgrade an unrelated retry-side exit 128"
        )


def test_all_merge_probes_share_the_promisor_recovery():
    """Both merge probes in review_autofix.yml must carry the recovery — a probe
    without it misreads a lazy-fetch failure as a merge outcome."""
    wf = _workflow()
    pre_review_step = _section(
        "- name: Pre-review deterministic merge-topology gate",
        "\n      - name: Run reviewer models",
    )
    detect_step = _detect_step()
    for label, section in (
        ("pre-review merge-topology gate", pre_review_step),
        ("late detect-conflicts step", detect_step),
    ):
        assert "git config --unset-all remote.origin.partialclonefilter" in section, (
            f"Expected the {label} to carry the partial-clone promisor recovery"
        )
    # No third, unguarded probe has crept in elsewhere in the workflow.
    assert wf.count('git merge --no-commit --no-ff "origin/${BASE_BRANCH}"') == 4, (
        "Expected exactly four merge invocations in review_autofix.yml (initial "
        "probe + backfill retry, in each of the two merge-performing steps). A "
        "new probe must carry the promisor recovery too."
    )
