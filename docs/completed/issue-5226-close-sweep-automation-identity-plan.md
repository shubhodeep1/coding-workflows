# Require an automation-owned PR before closing an issue on a non-default branch merge

Source issue: shubhodeep1/coding-workflows#5226 (https://github.com/shubhodeep1/coding-workflows/issues/5226)
Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
Security pass: skip (ai:security: automation-produced issue)

## Summary

Since #4813, `close_merged_issues_sweep` and `issue_pr_status.yml` count a merged PR as an issue's finished work when the PR merged into the issue's target branch, and they accept the PR as the issue's own when its body carries a closing keyword (`Fixes #N`). On the default branch that matches what GitHub does anyway. On any other target branch (the branch the issue body names on its `Integration branch:` / `Target branch:` line, or a managed child's `orchestrator/project-<T>`) nothing but the PR author's own text ties the PR to the issue. So an unrelated merged PR that writes `Fixes #N` and targets that branch gets the issue labelled `ai:merged` and closed.

This plan keeps the closing-keyword identity for default-branch merges and requires an automation-owned PR for every other merge: the head branch follows the pipeline's naming for that issue (`ai/issue-<n>`, or the orchestrator judge's `fix/<n>-followup-<epoch>`), and the head lives in the same repository, so only someone with write access could have created it.

## Context

- Finding (issue #5226, from the #4813 project's security audit): `scripts/orchestrate_poll_process.sh:4073` accepts an implementation PR on its base alone. `_pr_json_is_issue_implementation_pr` (line 15121) accepts either the `ai/issue-<n>` head or a closing-keyword body, and a PR author controls the body. The issue body's `Integration branch:` line is editable by the issue's author.
- Attack chain today: a PR with `Fixes #N` merges into the issue's integration branch → `issue_pr_status.yml` (step `Update linked issue labels when PR closes`) finds the issue in `closingIssuesReferences` and applies `ai:merged` (standalone issue) or closes it directly (labelled managed child) → the sweep picks up `ai:merged`, accepts the same PR, and closes the issue.
- Who legitimately closes an issue on a non-default merge:
  - Codex-built follow-ups and orchestrator children: implement PRs with head `ai/issue-<n>` (`implement.yml`), pushed to the same repository.
  - The orchestrator judge's follow-up PR for a merged review-blocked PR: head `fix/<n>-followup-<epoch>` (`scripts/orchestrate_poll_process.sh:20469`), body `Closes #<n>`, base the integration branch.
  - Claude issue-mode projects on a non-default base: their PRs say `Refs #N`, and the `final-merge` stage closes the issue itself with `ai:merged` (`.claude/commands/implement-plan-claude.md`, Issue Mode). Neither gate is involved.
- `issue_pr_status.yml` already parses the issue from an `ai/issue-<n>([-/]…)?` head (`BRANCH_ISSUE_NUMBER`), so a suffixed head is already treated as that issue's branch there.

## Goals

- `close_merged_issues_sweep`: a verified implementation PR merged into the default branch counts as today. One merged into the issue's integration branch or a managed child's project branch counts only when its head is an automation branch for that issue in this repository. Otherwise the sweep logs `CLOSE_MERGED_SWEEP issue=<n> origin=<o> candidate_pr=<p> rejected=unverified_identity base=<ref> head=<ref> head_repo=<repo>` and tries the next candidate; with none left, the existing no-merged-PR policy of the label class applies.
- `issue_pr_status.yml`: on a merged PR into a non-default target branch, label (and, for a managed child, close) only when the same identity holds; otherwise log and leave the issue's labels and state unchanged.
- One shared predicate in `scripts/gh_helpers.sh`, used by both.
- No new GitHub API call (§15): the sweep reads `.head.ref` / `.head.repo.full_name` from the `pulls/<n>` JSON it already fetches, and the workflow reads them from the event payload.
- Tests for both gates, a README update, and a `changelog.d/` fragment (§20, `security`).

## Non-goals

- `_pr_json_is_issue_implementation_pr` does not change: stall recovery, validation fix-up evidence and linked-PR adoption also call it and never close issues on their own (the sweep is the closer).
- Default-branch merges keep the closing-keyword identity: GitHub closes the issue on such a merge by itself, so the sweep and the workflow grant nothing extra there.
- PRs closed without merging (`ai:closed` path) stay as they are (out of scope since #4813).
- No `.claude/**` change.

## Constraints

- §1: security first; the new rule fails closed (missing head repo, missing helper → a non-default merge does not count).
- §5: the gate is added where the two decisions are made; nothing else changes.
- §6: no identifier renamed; existing log lines (`rejected=non_target_base`, `PR merged into … which is not issue #…'s target branch …`) keep their text. New names checked for collisions: `pr_head_ref_is_issue_automation_branch`, `_sweep_pr_head_ref`, `_sweep_pr_head_repo`, `PR_HEAD_REPO_FULL_NAME`, `rejected=unverified_identity`.
- §9: tabs in shell where the file uses tabs, YAML 2-space.
- §14: `issue_pr_status.yml` fetches `gh_helpers.sh` from the same support ref; the workflow keeps a fail-closed stub when the helper is missing.
- §15: no new API call.
- §19: phase, fix and completion PRs use `Refs #5226`; the final PR targets a non-default base, so it uses `Refs #5226` too and the `final-merge` stage closes the issue.
- §27: `issue_pr_status.yml` stays far below 480,000 bytes.

## Approach

1. `scripts/gh_helpers.sh`: add `pr_head_ref_is_issue_automation_branch <issue> <head_ref>`, returning 0 when the head is `ai/issue-<n>`, `ai/issue-<n>-…` / `ai/issue-<n>/…` (the shape `issue_pr_status.yml` already maps to the issue), or `fix/<n>-followup-<digits>`; 1 otherwise (a non-numeric issue included).
2. Sweep: after the base matches a non-default target, require `pr_head_ref_is_issue_automation_branch` on `.head.ref` and `.head.repo.full_name == GITHUB_REPOSITORY` (both non-empty). A default-branch base keeps today's acceptance. When the helper is missing, a non-default merge does not count.
3. `issue_pr_status.yml`: add `PR_HEAD_REPO_FULL_NAME: ${{ github.event.pull_request.head.repo.full_name }}`. In the per-issue loop, after the existing target-branch check passes for a merged PR on a non-default base, require the same identity; otherwise log `PR #<p> merged into <base> is not an automation PR for issue #<n> (head <ref>, head repo <repo|none>); leaving its labels and state unchanged.` and continue. Fail-closed stub when `gh_helpers.sh` lacks the helper.

Alternatives considered: AD-1 B (sweep only) leaves the workflow closing labelled managed children on the same text; AD-2 B (trust issue author association) needs an extra call per issue and still trusts PR text; AD-3 B (exact `ai/issue-<n>` only) drops the orchestrator judge's follow-up PR, which merges into the integration branch with `Closes #<n>`.

## Phases & Merge Strategy

**Single phase.** Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the sweep and the workflow are the two halves of one invariant ("a non-default merge closes an issue only through an automation-owned PR"); shipping one half leaves the finding exploitable through the other.

1. **Phase 1 — require an automation-owned PR for non-default target merges.**
   - Files: `scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/issue_pr_status.yml`, `tests/test_orchestrate_poll_process.py`, `tests/test_issue_pr_status_target_branch_gate.py`, `tests/test_gh_helpers_issue_automation_branch.py` [new], `README.md`, `changelog.d/5226-close-sweep-automation-identity.md` [new]. No `.claude/**` path.
   - Done when: the sweep and workflow-gate tests (new and existing) pass, the helper test passes, `bash -n` is clean on touched scripts, the workflow YAML parses, and the README rows and changelog fragment describe the rule.
   - Rollback: revert the phase PR; `ENABLE_CLOSE_MERGED_ISSUES=false` stops the sweep without a revert.

## Implementation Steps

Phase 1:

1. `scripts/gh_helpers.sh`: add `pr_head_ref_is_issue_automation_branch()` next to `issue_body_orchestrator_project_branch`, with a comment naming both callers and the incident.
2. `scripts/orchestrate_poll_process.sh` `close_merged_issues_sweep`: header comment documents the identity rule; in the candidate loop split the accept condition into "default branch" (accept) and "integration / project branch" (accept only with identity, else log `rejected=unverified_identity` and `continue`); the existing `rejected=non_target_base` line is unchanged.
3. `.github/workflows/issue_pr_status.yml` step `Update linked issue labels when PR closes`: env `PR_HEAD_REPO_FULL_NAME`; fail-closed stub; identity check after the target-branch check for merged non-default PRs; update the step's comment block.
4. `tests/test_orchestrate_poll_process.py`: the `pulls/<n>` mock carries `head.repo.full_name` (default: the test repository; override per PR) and `base.repo.full_name`; new tests: a closing-keyword PR with a non-automation head merged into the declared integration branch leaves the issue open with `rejected=unverified_identity`; a fork head named `ai/issue-10` is rejected; the judge's `fix/10-followup-<epoch>` head closes; existing integration-branch tests keep passing with `ai/issue-10…` heads.
5. `tests/test_issue_pr_status_target_branch_gate.py`: `_run_step` takes `pr_head_repo`; new tests: non-automation head on the integration branch → no label, no close; fork head on a managed child's project branch → no label, no close; `fix/10-followup-<epoch>` → label; `test_target_branch_alias_counts` uses the `ai/issue-10` head real heal PRs carry.
6. `tests/test_gh_helpers_issue_automation_branch.py` [new]: accept/reject table for the helper.
7. `README.md`: extend the `ENABLE_CLOSE_MERGED_ISSUES` row and the `issue_pr_status.yml` row.
8. `changelog.d/5226-close-sweep-automation-identity.md` [new], section `security`.
9. Confirm `ci.yml` collects the new test file.

## Files & Modules

- `scripts/gh_helpers.sh`
- `scripts/orchestrate_poll_process.sh`
- `.github/workflows/issue_pr_status.yml`
- `tests/test_orchestrate_poll_process.py`
- `tests/test_issue_pr_status_target_branch_gate.py`
- `tests/test_gh_helpers_issue_automation_branch.py` [new]
- `README.md`
- `changelog.d/5226-close-sweep-automation-identity.md` [new]

## Tests

- pytest: the `close_merged_issues_sweep` tests in `tests/test_orchestrate_poll_process.py`, `tests/test_issue_pr_status_target_branch_gate.py`, `tests/test_issue_pr_status_payload_fallback_contract.py`, `tests/test_linked_pr_implementation_guard.py`, `tests/test_gh_helpers_issue_body_integration_branch.py`, the new helper test, `tests/test_workflow_file_size_limit.py`.
- Static: `bash -n` on the two scripts; YAML parse of the workflow.
- End to end: the chain's conformance and validation stages (security pass skipped per the header).

## Risks & Mitigations

- A legitimate non-default merge from a hand-named branch (a human's `Fixes #N` PR into a project branch) no longer closes its issue. Mitigation: the `ai:merged`-origin fall-through raises the existing Telegram WARNING, and a human closes the issue; Claude issue-mode projects close their own issues. ACCEPTED as the safer default (§1).
- A consumer running an older `gh_helpers.sh` with the new workflow: the stub fails closed, so only default-branch merges count until the helper arrives; the sweep (same repo, same ref) remains the backstop.

## Rollout

Ships with this project's final PR into its base (#4813's project branch), then to `main` with #4813's final PR, then to consumers on the next `@stable` release. No flag: it narrows a destructive action. `ENABLE_CLOSE_MERGED_ISSUES=false` remains the sweep's emergency stop.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which paths get the identity rule? — Picked: A — both the sweep and `issue_pr_status.yml`, through one helper in `scripts/gh_helpers.sh`. Alternatives: B — the sweep only, as the finding locates it; C — change `_pr_json_is_issue_implementation_pr` for every caller. Why: B leaves the workflow closing labelled managed children on the same PR text; C changes stall recovery and fix-up evidence, which close nothing. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What proves a PR is the issue's assigned fix on a non-default base? — Picked: A — the head branch follows the automation's naming for that issue and the head repository is this repository. Alternatives: B — trust the PR's closing keyword when the issue author is a collaborator (needs an extra call per issue and still trusts PR text); C — record the issue-to-PR mapping in a new marker comment (new state and writers across pipelines). Why: A needs no call and no new state; creating a same-repo branch already requires write access. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which head names count as automation branches? — Picked: A — `ai/issue-<n>`, `ai/issue-<n>-…` / `ai/issue-<n>/…`, and `fix/<n>-followup-<digits>`. Alternatives: B — exact `ai/issue-<n>` only. Why: `issue_pr_status.yml` already maps the suffixed shape to the issue, and the orchestrator judge's follow-up PR merges into the integration branch with `Closes #<n>`. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Does a default-branch merge also need the identity? — Picked: A — no, keep the closing-keyword identity there. Alternatives: B — require it everywhere. Why: GitHub itself closes the issue on that merge, so B would only stop `ai:merged` labelling and break standalone PRs with no gain. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Should the editable `Integration branch:` line itself be authorized (issue author check)? — Picked: A — no separate check; with the identity rule a changed line can only point the close at a branch where the issue's own automation PR merged. Alternatives: B — trust the line only from automation-authored issues. Why: B needs author data the sweep's `gh issue list` does not return in a usable form and adds nothing once A of AD-2 holds. Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py`: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #5226 (finding), #4813 (target-branch rule), #4957 (managed label only), #3817 / PR #3825 (implementation-PR guard).
