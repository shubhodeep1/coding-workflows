# Change issue state on an unmerged PR close only for the issue's own automation PR

Source issue: shubhodeep1/coding-workflows#5617 (https://github.com/shubhodeep1/coding-workflows/issues/5617)
Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
Security pass: skip (ai:security: automation-produced issue)

## Summary

When a PR closes without merging, `issue_pr_status.yml` labels every issue the PR links `ai:closed`, closes it, and finalizes its AI-memory lineage as `closed`. It does not check that the PR was the issue's own implementation, so anyone whose PR reaches a PAT-backed run can close an arbitrary issue by writing `Fixes #N` in the body and closing the PR. This plan applies the #5226 automation-identity check to unmerged closes: the step changes an issue's labels, state, or lineage only when the PR's head is a same-repository automation branch for that issue; otherwise it logs and leaves the issue alone.

## Context

- The security audit reported this against the #4813 project branch as `unmerged-pr-closes-linked-issue` (A01:2021-Broken Access Control, high, confidence 9/10, `.github/workflows/issue_pr_status.yml:597`). Recommendation: for unmerged closes, require a trusted issue-to-PR record and verified automation provenance before changing issue or lineage state; otherwise skip.
- Step `Update linked issue labels when PR closes` builds `ISSUE_NUMBERS` from the PR's `closingIssuesReferences`, the body/title closing-keyword fallback, and an `ai/issue-<n>` head. The target-branch gate (#4813, #4957, #5226) only runs when `PR_MERGED=true` and the base is not the default branch. For `PR_MERGED=false` every non-tracking issue falls through to `set_issue_phase_label_resilient <n> ai:closed`, `gh issue close <n>`, and is added to `LINEAGE_FINALIZE_ISSUE_NUMBERS`. Tracking-bucket issues (orchestrator-tracking, and issues whose classification lookups both failed) are not labelled, but are still added to `LINEAGE_FINALIZE_ISSUE_NUMBERS`, so `Finalize linked issue lineage state` writes `memory_finalize_task --final-state closed` for them.
- The caller wrappers (`workflow-templates/ai-issue-pr-status.yml`, `.github/workflows/internal-issue-pr-status.yml`) trigger on `pull_request: [closed]` with `secrets: inherit`, so a run is PAT-backed whenever the event gets secrets (same-repository PRs, and fork PRs where the repository sends secrets to forks).
- `scripts/gh_helpers.sh` already has `pr_head_ref_is_issue_automation_branch <n> <head>` (issue #5226): true for `ai/issue-<n>`, `ai/issue-<n>-…`, `ai/issue-<n>/…`, and `fix/<n>-followup-<epoch>`. The step already has a fail-closed stub for older helper copies, and the event already provides `PR_HEAD_REF` and `PR_HEAD_REPO_FULL_NAME`. A same-repository head can only be pushed by an account with write access, which is the provenance #5226 accepted for non-default merges.
- The orchestrator's own unmerged closes of child PRs use `ai/issue-<n>` heads in the same repository, so they keep today's `ai:closed` + close behaviour. The orchestrator poller (`reconcile_managed_issue_labels`, `close_merged_issues_sweep`) remains the backstop for managed children.

## Goals

- On a PR closed without merging, an issue gets no `ai:closed` label, no close, and no lineage finalization unless the PR's head is in this repository and `pr_head_ref_is_issue_automation_branch` accepts it for that issue.
- The same rule covers tracking-bucket issues' lineage on an unmerged close (orchestrator-tracking issues and issues whose classification failed) (AD-2).
- An automation PR for the issue closed without merging keeps today's behaviour exactly: `ai:closed`, close, lineage `closed`.
- Merged PRs are unchanged (default-branch merges, the #4813 / #4957 / #5226 target-branch gate, and #5227 lineage).
- Each skip logs one searchable line naming the PR, the issue, the head, and the head repository.

## Non-goals

- Changing the merged-PR paths, the classification, or which issues are linked (AD-4).
- Adding a lineage-record lookup (ai-memory read) as a second provenance source (AD-1).
- Treating `claude/implement-plan-issue-<n>-…` heads as automation heads (AD-3).
- Changing the Telegram alert or cleanup steps (they run only on merged PRs).

## Constraints

- §1: security first; the change fails closed (no provenance → no mutation).
- §5: one workflow step, its tests, the helper's doc comment, README row, changelog fragment.
- §6: existing step names, env vars (`LINKED_ISSUE_NUMBERS`, `LINEAGE_FINALIZE_ISSUE_NUMBERS`), log lines, test function names, and `pr_head_ref_is_issue_automation_branch` are unchanged. The one new shell function `pr_is_issue_automation_pr` is unique in the repository (checked with `git grep`).
- §9: YAML stays 2-space indented; Python tests and shell helpers use tabs.
- §15: no new GitHub API call; the check reads event fields only.
- §20: security fix, so one `changelog.d/5617-unmerged-close-automation-identity.md` fragment (`<!-- changelog: security -->`).
- §27: `issue_pr_status.yml` is ~900 lines, far below the 480,000-byte guard.

## Approach

In step `Update linked issue labels when PR closes`, next to the existing `pr_head_ref_is_issue_automation_branch` stub, define `pr_is_issue_automation_pr <n>`: true only when `PR_HEAD_REPO_FULL_NAME` is non-empty and equals `REPOSITORY`, and `pr_head_ref_is_issue_automation_branch <n> "${PR_HEAD_REF}"` succeeds. In the per-issue loop:

1. Tracking bucket: on `PR_MERGED != true` without `pr_is_issue_automation_pr`, log `PR #<p> closed without merging is not an automation PR for issue #<n> (head …, head repo …); skipping its lineage finalization.` and do not add it to `LINEAGE_FINALIZE_ISSUE_NUMBERS`. The existing unclassified-merge check stays first.
2. Every other issue: before the label call, on `PR_MERGED != true` without `pr_is_issue_automation_pr`, log `PR #<p> closed without merging is not an automation PR for issue #<n> (head …, head repo …); leaving its labels, state, and lineage unchanged.` and `continue`.

An older `gh_helpers.sh` without the identity helper hits the existing stub (`return 1`), so unmerged closes then change nothing (fail closed, like non-default merges today).

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan; the change is one workflow step plus its tests and docs.

1. **Phase 1 — automation identity for unmerged closes.** Files: `.github/workflows/issue_pr_status.yml`, `tests/test_issue_pr_status_target_branch_gate.py`, `scripts/gh_helpers.sh` (doc comment only), `README.md` (the `issue_pr_status.yml` row), `changelog.d/5617-unmerged-close-automation-identity.md`. Done when: a non-automation same-repo head and a fork head closed unmerged cause no label, close, or lineage call for a standalone, a labelled managed child, a tracking, or an unclassified issue; an `ai/issue-<n>` / `fix/<n>-followup-<epoch>` same-repo head closed unmerged labels `ai:closed`, closes, and finalizes `closed` as today; every existing test in the file and the other `issue_pr_status` / `gh_helpers` tests pass; the workflow parses as YAML. Rollback: revert the phase PR.

## Implementation Steps

1. `issue_pr_status.yml`, step `Update linked issue labels when PR closes`: add `pr_is_issue_automation_pr` after the helper stub, with a comment citing #5617; add the tracking-bucket lineage check and the unmerged-close gate in the loop; update the bucket comment block (buckets 1–3) to say unmerged closes need the automation identity.
2. `scripts/gh_helpers.sh`: extend the `pr_head_ref_is_issue_automation_branch` doc comment to name the unmerged-close use (#5617). No code change.
3. `tests/test_issue_pr_status_target_branch_gate.py`: keep every existing test name; the three existing unmerged-close tests (`test_unmerged_close_behaviour_is_unchanged`, `test_unmerged_close_still_finalizes_as_closed`, `test_unclassified_issue_keeps_default_merge_and_unmerged_lineage`) pass an `ai/issue-10` head so they keep asserting today's behaviour for automation PRs, with docstrings updated; add runtime tests for: non-automation same-repo head and fork head on a standalone issue (no label, close, lineage); a labelled managed child (same); a tracking issue and an unclassified issue (no lineage); `fix/<n>-followup-<epoch>` head (labels and closes); a PR linking two issues where the head is automation for only one (only that one changes); register each new test in the `__main__` block.
4. `README.md` `issue_pr_status.yml` row: one sentence on the unmerged-close rule, and fix the "PRs closed without merging are finalized as before" clause.
5. `changelog.d/5617-unmerged-close-automation-identity.md` (§20).

## Files & Modules

- `.github/workflows/issue_pr_status.yml`
- `scripts/gh_helpers.sh` (comment only)
- `tests/test_issue_pr_status_target_branch_gate.py`
- `README.md`
- `changelog.d/5617-unmerged-close-automation-identity.md` [new]

## Testing

- `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_issue_pr_status_target_branch_gate.py` (how `ci.yml` runs it) and under `pytest`.
- `tests/test_issue_pr_status_payload_fallback_contract.py`, `tests/test_gh_helpers_issue_automation_branch.py`, `tests/test_ai_label_precreation_contract.py`, `tests/test_workflow_checkout_integration_ref_audit.py`, and a YAML parse of the workflow.

## Risks

- A human's abandoned same-repository PR with `Fixes #N` no longer closes issue #N: intended; GitHub itself never closes an issue on an unmerged PR, and the issue stays open for a human to close.
- A Claude issue-mode project's final PR (`claude/implement-plan-issue-<n>-…`) closed without merging no longer closes its issue `ai:closed` (AD-3): the issue stays open, which is the safe outcome for abandoned work.
- Orchestrator-tracking lineage is no longer finalized `closed` by an unmerged PR that mentions the tracker (AD-2); the orchestrator owns the tracking issue's lifecycle, and §19 already forbids closing keywords against tracking issues.

## Rollout

Ships with the #4813 project's final PR into `main`, then to consumers on the next `@stable` sync through the `ai-issue-pr-status.yml` wrapper. No variable, no migration, no new API call.

## Auto-decisions

- AD-1 [plan, 2026-09-30] What counts as verified automation provenance for an unmerged close? — Picked: A — a same-repository head that `pr_head_ref_is_issue_automation_branch` accepts for that issue (the #5226 rule). Alternatives: B — also require an ai-memory lineage record naming the PR; C — accept any same-repository head. Why: A reuses a tested, API-free identity that only write-access accounts can satisfy (§15, §5); B adds a network read to the label step with its own failure modes; C lets any collaborator branch with `Fixes #N` close an issue. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Does the gate also cover tracking-bucket lineage on an unmerged close? — Picked: A — yes, orchestrator-tracking and unclassified issues are finalized on an unmerged close only for an automation PR of that issue. Alternatives: B — keep their lineage finalization unchanged. Why: the finding's recommendation covers lineage state, and §1 puts security before compatibility; the tracker's lifecycle is owned by the orchestrator. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Should `claude/implement-plan-issue-<n>-…` heads count as automation heads? — Picked: A — no. Alternatives: B — extend `pr_head_ref_is_issue_automation_branch` to accept them. Why: §5; the helper is shared with `close_merged_issues_sweep`, and leaving an issue open after its abandoned PR is the safe failure. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-30] Should the merged-PR paths change? — Picked: A — no. Alternatives: B — also require the automation identity on default-branch merges. Why: GitHub closes the issue on a default-branch merge anyway, and non-default merges already require it (#5226). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] How are the three existing unmerged-close tests kept? — Picked: A — keep their names (§6) and give them an `ai/issue-10` head, so they still assert today's behaviour for automation PRs; add new tests for the rejected heads. Alternatives: B — rename them. Why: §6 forbids renames without asking. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Does this need a changelog fragment? — Picked: A — yes, `changelog.d/5617-unmerged-close-automation-identity.md` under `security`. Alternatives: B — none. Why: §20.A lists security fixes. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` result: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- The issue base's own final PR (#4826, draft, into `main`) was open at planning time, so the base has not moved.
