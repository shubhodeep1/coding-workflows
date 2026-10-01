# Finalize tracking-issue lineage only from the orchestrator completion PR

Source issue: shubhodeep1/coding-workflows#5619 (https://github.com/shubhodeep1/coding-workflows/issues/5619)
Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
Security pass: skip (ai:security: automation-produced issue)

## Summary

`issue_pr_status.yml` writes an orchestrator-tracking issue's AI-memory lineage as `merged` (or `closed`) whenever any closed PR links it, whatever the PR's base or head. This plan narrows that to the one PR that really finishes an orchestrator project: its completion PR, merged from `orchestrator/project-<T>` in this repository into the default branch.

## Context

- Security audit finding #5619 (`STRIDE: Tampering`, medium): at `.github/workflows/issue_pr_status.yml:557` (step `Update linked issue labels when PR closes`), the tracking-issue branch of the label/close loop appends the issue to `LINEAGE_FINALIZE_ISSUE_NUMBERS` unconditionally. The next step, `Finalize linked issue lineage state`, then calls `memory_finalize_task --final-state merged` for it. A PR merged into an unrelated branch that links a tracking issue (closing keyword, repo-scoped URL) therefore records the project as finished while it is still active.
- Impact: `scripts/memory_maintenance_extract_learnings.py` samples every lineage with `state == "merged"` as a finished task, so a forged `merged` state feeds the learnings extraction.
- Prior work on the same step: #4813 (target-branch gate for labels and close), #4957 (managed status only from the label), #5226 (automation-head check off the default branch), #5227 (lineage only for issues the gate accepted). #5227's AD-1 left tracking-issue lineage unchanged and named "B — also stop finalizing orchestrator-tracking issues" as the alternative; this finding asks for that follow-up.
- The orchestrator's completion PR is the final integration PR `ensure_eager_final_pr` opens (`scripts/orchestrate_poll_process.sh`): head `orchestrator/project-<T>` (the integration branch `.github/workflows/orchestrate.yml` creates), base the default branch, body `Refs #<T>`.
- The label/close behaviour for tracking issues (skip, owned by `orchestrate_poll_process.sh`) is not affected (#1469 regression guard).

## Goals

- A tracking issue's lineage is finalized only when the closed PR is merged, its base is the default branch (`DEFAULT_BRANCH_NAME`), its head ref is `orchestrator/project-<T>` for that issue number `<T>`, and its head repository is this repository.
- Every other PR that links a tracking issue (a merge into another branch, a merge from another head or a fork, a close without merging) leaves the tracking issue's lineage alone and logs why.
- An issue the step could not classify (both lookups failed, so it is treated as tracking) follows the same rule (fail closed).
- Labels and issue state of tracking issues stay untouched on every path, as today.

## Non-goals

- Making the orchestrator's completion PR's `Refs #<T>` a linked issue (AD-4): `<T>` never enters the label/close loop. The final PR's review round 1 added a lineage-only path instead: `<T>` is derived from the verified completion head and added to `LINEAGE_FINALIZE_ISSUE_NUMBERS` alone (AD-7).
- Any change to standalone or managed-child issues, to the label/close gate, to the Telegram alert or cleanup steps (`LINKED_ISSUE_NUMBERS`), or to `close_merged_issues_sweep`.
- Any change to `scripts/ai_memory*.py` or `scripts/memory_helpers.sh`.

## Constraints

- §1: security first; the new rule fails closed (an unverified PR never finalizes a tracking issue's lineage).
- §5: the change stays inside the tracking-issue branch of the loop and the REST fallback's list appends (AD-6); no new step, and the gate itself makes no new API call (review round 2 added one tracker read, AD-8).
- §6: no identifier is renamed or removed. `LINEAGE_FINALIZE_ISSUE_NUMBERS`, `LINEAGE_UNCLASSIFIED_ISSUE_NUMBERS`, the `Skipping orchestrator-tracking issue #…` log line (pinned by `tests/test_issue_pr_status_payload_fallback_contract.py`), and the step names stay. Test functions whose asserted behaviour reverses keep their names as aliases (AD-5).
- §9: YAML keeps 2-space indentation; Python tests keep tabs.
- §15: zero new GitHub API calls. The head ref, head repository, base, and merged flag already come from the event payload (`PR_HEAD_REF`, `PR_HEAD_REPO_FULL_NAME`, `PR_BASE_REF`, `PR_MERGED`). The head-derived tracker (AD-7) costs one REST read of issue `<T>`, issued only on a merge that passed that gate, to confirm `<T>` is an `ai:orchestrator-tracking` issue (AD-8).
- §20: a `changelog.d/5619-…` fragment (section `security`).
- §7: the `issue_pr_status.yml` row in `README.md` is updated.
- §27: `issue_pr_status.yml` is ~41 KB, far below the 480,000-byte guard.

## Approach

In the tracking-issue branch of the loop (currently lines 550–560 of `issue_pr_status.yml` on the base branch), replace the unconditional append with a completion-PR check:

```bash
if [ "${PR_MERGED}" = "true" ] && [ "${PR_BASE_REF}" = "${DEFAULT_BRANCH_NAME}" ] \
  && [ -n "${PR_HEAD_REPO_FULL_NAME:-}" ] && [ "${PR_HEAD_REPO_FULL_NAME}" = "${REPOSITORY}" ] \
  && [ "${PR_HEAD_REF:-}" = "orchestrator/project-${issue_number}" ]; then
  LINEAGE_FINALIZE_ISSUE_NUMBERS+="${issue_number}"$'\n'
else
  echo "<why the lineage is skipped>"
fi
```

The existing unclassified-issue message is kept for the unclassified case, and a new message explains the classified-tracking skip. This replaces #5227's narrower unclassified rule with the same completion-PR rule (AD-3).

Alternatives considered: never finalize tracking lineage from this workflow (AD-1 B; drops the legitimate path the audit recommendation keeps), or accept any default-branch merge (AD-1 C; any PR that links the tracking issue and merges into the default branch could still finalize it).

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase per issue.

1. **Phase 1 — completion-PR gate for tracking-issue lineage.** Files: `.github/workflows/issue_pr_status.yml`, `tests/test_issue_pr_status_target_branch_gate.py`, `README.md`, `changelog.d/5619-tracking-lineage-completion-pr-only.md` [new]. Done when: the new and updated tests in `tests/test_issue_pr_status_target_branch_gate.py` pass (non-completion merges, unmerged closes, fork heads, and wrong bases leave a tracking issue unfinalized; the completion PR finalizes it as `merged`; unclassified issues follow the same rule; tracking labels and state stay untouched), `tests/test_issue_pr_status_payload_fallback_contract.py` still passes, and the workflow YAML parses. Rollback: revert the phase PR; the step returns to finalizing every linked tracking issue.

## Implementation Steps

### Phase 1

1. `.github/workflows/issue_pr_status.yml`, step `Update linked issue labels when PR closes`, tracking-issue branch of the loop: keep the `Skipping orchestrator-tracking issue …` echo; append the issue to `LINEAGE_FINALIZE_ISSUE_NUMBERS` only for the verified completion PR; otherwise log the reason (unclassified or not the completion PR) and skip. Update the `LINEAGE_FINALIZE_ISSUE_NUMBERS` / `LINEAGE_UNCLASSIFIED_ISSUE_NUMBERS` comments near the top of the step (lines 224–234) to describe the rule and cite #5619.
1b. Same step, per-issue REST fallback: merge failed and classified lookups into `TRACKING_ISSUES` / `MANAGED_ISSUES` with `merge_issue_number_list` instead of `+=` (AD-6, found while implementing).
2. `tests/test_issue_pr_status_target_branch_gate.py`: add tests for the completion PR (GraphQL and REST-fallback classification), a child merge into `orchestrator/project-<T>`, a default-branch merge from another head, a fork head named `orchestrator/project-<T>`, a completion head merged into a non-default base, an unmerged close, and unclassified issues; keep `test_tracking_issue_lineage_is_unchanged` and `test_unclassified_issue_keeps_default_merge_and_unmerged_lineage` as aliases of the new tests (AD-5); rewrite `test_classified_tracking_issue_keeps_lineage_when_rest_fallback_works` to the completion PR; call every new test from the `__main__` block.
3. `README.md` line 1184 (`issue_pr_status.yml` row): replace "orchestrator-tracking issues … are finalized as before, an issue the step could not classify … is finalized only on an unmerged close or a default-branch merge" with the completion-PR rule and cite #5619.
4. `changelog.d/5619-tracking-lineage-completion-pr-only.md` [new]: `<!-- changelog: security -->` entry per §20.

## Files & Modules

- `.github/workflows/issue_pr_status.yml`
- `tests/test_issue_pr_status_target_branch_gate.py`
- `README.md`
- `changelog.d/5619-tracking-lineage-completion-pr-only.md` [new]

## Tests

- Unit (runs the step's real `run:` script under bash with the stub `gh`, as the file already does): `tests/test_issue_pr_status_target_branch_gate.py`, run by `ci.yml` step at line 1034 as a script.
- Contract: `tests/test_issue_pr_status_payload_fallback_contract.py` (skip gate and log marker unchanged).
- YAML parse of `.github/workflows/issue_pr_status.yml`; `actionlint` when available.
- Runtime validation of the project branch runs in the chain's validation stage.

## Risks & Mitigations

- A completion PR that only carries `Refs #<T>` (today's final PR body) links no issue. MITIGATED by AD-7: `<T>` is derived from the verified `orchestrator/project-<T>` head for the lineage list only, so its lineage is finalized and its labels and state are untouched; `<T>` is queued only after one REST read confirms it is an `ai:orchestrator-tracking` issue (AD-8).
- A standalone issue whose classification lookups both failed no longer gets its lineage finalized on a default-branch merge or an unmerged close. ACCEPTED — needs two API failures in one run; fail closed per §1 (AD-3).
- A repo whose orchestrator integration branch is not named `orchestrator/project-<T>`. Mitigation: `orchestrate.yml` names every integration branch this way, and `issue_body_orchestrator_project_branch` in `scripts/gh_helpers.sh` already relies on the same name.

## Rollout

No flag and no new variable. The reusable workflow ships to consumer repos through their `ai-issue-pr-status.yml` wrapper on the next `@stable` sync; the wrapper already passes the event payload the check reads. Rollback: revert the PR.

## References

- Issue #5619; audit tracker #3576.
- #4813, #4957, #5226, #5227 (`docs/completed/issue-5227-lineage-only-target-branch-merges-plan.md`, AD-1), #1469.
- `.github/workflows/issue_pr_status.yml`, `scripts/orchestrate_poll_process.sh` (`ensure_eager_final_pr`), `scripts/memory_maintenance_extract_learnings.py`.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which PR may finalize an orchestrator-tracking issue's lineage? — Picked: A — only its completion PR: merged, base the default branch, head `orchestrator/project-<T>` in this repository. Alternatives: B — none; never finalize tracking-issue lineage in `issue_pr_status.yml`; C — any PR merged into the default branch. Why: A is the audit's recommendation; B drops the legitimate path, C still lets an arbitrary linked PR finalize the project. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Does a PR closed without merging still write a tracking issue's lineage as `closed`? — Picked: A — no, skip it. Alternatives: B — keep writing `closed`. Why: an unmerged close never ends an orchestrator project (the poller reopens a fresh final PR), so `closed` is the same tampering as `merged`. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How is an issue treated whose classification lookups both failed (the step skips it as tracking)? — Picked: A — the same completion-PR rule as a tracking issue. Alternatives: B — keep #5227's rule (finalize on an unmerged close or a default-branch merge). Why: §1; the issue may be a tracking issue, and a missed standalone lineage costs only memory. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Should the step also derive the tracking issue from a completion PR's `orchestrator/project-<T>` head, so a body with only `Refs #<T>` still finalizes it? — Picked: A — no. Alternatives: B — yes, add `<T>` to the linked issues from the head ref. Why: §5; it adds a new linked-issue path into the label/close loop, which is out of scope for a tampering fix. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] What happens to the existing tests that assert the old tracking and unclassified lineage behaviour? — Picked: A — add accurately named tests and keep the old function names as aliases that call them (§6). Alternatives: B — rewrite the bodies under the old names; C — delete the old tests. Why: §6 forbids removal and repurposing; aliases keep names callable without misleading readers. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1, 2026-09-30] The REST fallback appends to `TRACKING_ISSUES` / `MANAGED_ISSUES` with `+=` after the payload list, which has no trailing newline, so `5` plus a failed lookup of `10` became `510` and tracking issue #5 was labelled `ai:merged` and closed on a default-branch merge (reproduced). Fix it in this phase? — Picked: A — yes, merge with the existing `merge_issue_number_list` and add a regression test. Alternatives: B — leave it and file a separate issue. Why: §1; it defeats the tracking skip this phase's gate relies on, and it is the #2760 incident class. Applied in: phase 1 PR. Status: pending review
- AD-7 [final-merge — review round 1, 2026-10-01] The final PR's reviewer panel (6 of 6 reviewers, round 1 on head f11e889) found the completion-PR gate unreachable in production: the orchestrator's completion PR body says only `Refs #<T>`, so `<T>` is never a linked issue and its lineage is never finalized, and the positive-path test used a URL body that production never produces. How should the round answer it? — Picked: A — derive `<T>` from the verified completion head (merged into the default branch from `orchestrator/project-<T>` in this repository) and add it to `LINEAGE_FINALIZE_ISSUE_NUMBERS` only, never to the linked issues; test with the production body. Alternatives: B — reject the findings under AD-4 and keep the path unreachable; C — change `ensure_eager_final_pr` to also link the tracker with a repo-scoped URL. Why: A meets the issue's recommendation with the same trust rule as AD-1, zero API calls, and labels/state/alerts untouched (AD-4's concern was the label/close loop, which A never enters); B leaves dead code and needs a verdict bot this repo has not configured; C feeds `<T>` into the label/close loop and only helps PRs opened after it ships. Applied in: PR #5632 (review round 1). Status: pending review
- AD-8 [final-merge — review round 2, 2026-10-01] The final PR's reviewer panel (3 of 5 reviewers, round 2 on head 75bf650) found that the head-derived path (AD-7) trusts the `orchestrator/project-<T>` name without checking that `<T>` is an `ai:orchestrator-tracking` issue, so a same-repository branch with that name merged into the default branch can record an unrelated or missing issue's lineage as `merged`. How should the round answer it? — Picked: A — one REST read of issue `<T>`, only on a merge that passed the event gate; queue `<T>` only when it is an issue (not a pull request) carrying `ai:orchestrator-tracking`, and skip it with a warning when the read fails (fail closed). Alternatives: B — add a `<T>` alias to the existing closingIssuesReferences GraphQL query (no extra call); C — reject the finding (only a collaborator can push and merge such a branch). Why: §1; A costs one call per orchestrator project and leaves the classification payload alone, B turns a missing `<T>` into a payload error that sends every real closing reference to the per-issue REST fallback, and C leaves the gap and needs a verdict bot this repo has not configured. Applied in: PR #5632 (review round 2). Status: pending review

## Notes

- `.claude/scripts/security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5619` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
