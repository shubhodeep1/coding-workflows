# Mark poller-managed issues merged only on merges into their target branch

Source issue: shubhodeep1/coding-workflows#5618 (https://github.com/shubhodeep1/coding-workflows/issues/5618)
Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
Security pass: skip (ai:security: automation-produced issue)

## Summary

The orchestrator poller has four paths that turn "a linked PR is merged" into merged state for an issue, and none of them checks which branch the PR merged into or who pushed its head. An unrelated PR whose body says `Fixes #<n>` and that merged into the wrong branch can therefore force `ai:merged` on child `<n>` or mark it `merged` in `check-wave-status`, which can advance its wave. `close_merged_issues_sweep` rejects exactly that merge since issues #4813 and #5226. This plan applies the same fail-closed target-branch and identity rule, through one shared predicate, to all four paths.

## Context

- Finding: `[security-audit] poller-reconciles-wrong-base-pr-as-merged` (issue #5618, STRIDE Tampering, high, `scripts/orchestrate_poll_process.sh:19639`), filed by `security-audit.yml` cycle 3 of the #4813 project, whose branch this plan builds on. The #4813 conformance run 1 already flagged the same code as a HYPOTHESIS (`_reconcile_merged_pr_issue` labels `ai:merged` without a base check), and that project recorded the lesson: "A base-branch gate added to one close path must be applied to every path that marks an issue merged … in the same change, or the next audit finds the unguarded one."
- The rule already in force (`close_merged_issues_sweep`, base branch ~L3875-4120): a verified implementation PR counts when its `base.ref` equals `.base.repo.default_branch` of the same PR JSON (empty fails closed); or when `base.ref` is the issue's target branch **and** its head is an automation branch for the issue (`pr_head_ref_is_issue_automation_branch`, `scripts/gh_helpers.sh`) **and** `.head.repo.full_name` is this repository. Anything else is `rejected=non_target_base` or `rejected=unverified_identity`. The issue's target branch is its body's `Integration branch:` / `Target branch:` line (`issue_body_integration_branch`) or, for an `ai:orchestrator-managed` child, `orchestrator/project-<T>` from its `Tracking issue:` line (`issue_body_orchestrator_project_branch`).
- The four unguarded paths (base branch line numbers):
  1. **Current-wave reconcile loop** (~L19597-19650, the flagged line). Accepts the first merged candidate that passes `_pr_json_is_issue_implementation_pr` (head `ai/issue-<n>` **or** a closing keyword). `PR_MERGED=true` goes to `reconcile_managed_issue_labels` (forces `ai:merged`) and to `PR_STATES_JSON` for `orchestrate_lib.py check-wave-status`, where `reconcile_wave_issue_status` returns `merged` ahead of every label.
  2. **`refresh_validation_dispatch_wave_gate`** (~L2686-2748). Builds `pr_states_json` from `_fetch_candidate_issue_details_graphql`'s `linked_pr`, the newest cross-reference with `willCloseTarget == true`: a closing keyword on any PR, any base, any head.
  3. **Stall recovery `_reconcile_merged_pr_issue`** (~L15535-15560, six callers in `run_standalone_stall_recovery` and `recover_stalled_issue`). When `_check_merged_pr_guard` sees a merged linked PR (from several cache shapes, some with no base at all), it adds `ai:merged`, which `check-wave-status` then reads as `merged` (`label_ai_merged`) and which feeds the close sweep.
  4. **Backward scan of prior waves** (~L19225-19245). A prior-wave child in `ai:ready-to-merge` whose linked PR (branch name, body reference, or `_subissue_closing_pr_number`) is merged gets `ai:merged` and `status: "merged"` in the state file.
- Wave children are created by the poller with `- Integration branch: $(jq -r '.integration_branch' STATE_FILE)`, and `compute_cycle_integration_ahead_by` sets `CWS_INTEGRATION_BRANCH` from that field before paths 1 and 4 run, so their target is known without reading editable issue text.
- Sibling follow-ups of the same audit, not in scope here: #5617 and #5619 (`issue_pr_status.yml`), #5620 (`pr_head_ref_is_issue_automation_branch` accepts any writer's branch name). Whatever #5620 changes in that helper applies here automatically, because this plan calls the helper rather than copying it.

## Goals

- One predicate decides, for all four paths, whether a merged PR finished an issue: default-branch merge of a verified implementation PR, or a merge into one of the issue's target branches from a same-repository automation head for that issue. Every other merge is rejected with reason `non_target_base` or `unverified_identity`, and unknown inputs fail closed.
- Path 1: a rejected merged candidate is logged as `LINKED_PR_CROSS_REF_REJECTED issue=<n> pr=<p> reason=<reason> …` and the next candidate is tried; it never sets `PR_MERGED=true`.
- Path 2: a rejected `merged: true` link becomes `{state: "unknown", merged: false}` in the gate's `pr_states_json`, logged the same way with `path=validation_dispatch_gate`.
- Path 3: `_reconcile_merged_pr_issue` adds `ai:merged` only for a merge the predicate accepts; otherwise it logs `STALL_MERGED_LABEL_REJECTED issue=<n> pr=<p> reason=<reason> …` and adds no label. The stall-recovery skip itself is unchanged.
- Path 4: a merged but rejected PR neither adds `ai:merged` nor marks the child `merged`; it is logged `[backward-scan] … rejected=<reason>`.
- Runtime tests cover the predicate's accept and reject cases and `_reconcile_merged_pr_issue`'s gate; structural tests pin paths 1, 2 and 4 to the predicate.

## Non-goals

- `close_merged_issues_sweep` and `issue_pr_status.yml`: already enforce the rule (#4813, #5226); not refactored onto the new predicate (§5).
- `validation_fix_issue_has_merged_pr_evidence` → `backfill_validation_fix_issue_merged_label` (validation and security fix-up issues, not wave children): recorded in the log's Notes as a follow-up (AD-7).
- Whether a stall recovery should still be skipped when the only merged PR is a rejected one: unchanged, recorded as a note (AD-8).
- Unmerged candidates (open, closed without merge): `pr_state` is not a decision input downstream, so their handling is unchanged (AD-4).
- Stronger proof of automation authorship than the branch name: #5620's scope.

## Constraints

- §1: security first; an empty default branch, an empty target list, a missing helper, a missing head repository, or a failed fetch all mean "not merged".
- §5: only the four producers above change; the sweep keeps its own inline copy of the rule.
- §6: no identifier is renamed. `LINKED_PR_CROSS_REF_REJECTED` keeps its fields and gains reason values and context keys. New identifiers (`_pr_json_merged_into_issue_target`, `ISSUE_TARGET_MERGE_REJECT_REASON`, `STALL_MERGED_LABEL_REJECTED`) are unique across `scripts/`, `tests/`, and `.github/`.
- §9: new bash follows the surrounding code (2-space indentation inside the poller's functions).
- §15: paths 1 and 4 already hold the full `pulls/<n>` JSON and use the state file for the target, so they add no call. Path 2 adds two fields to the existing GraphQL PR fragment instead of a per-issue REST read. Path 3 adds one `pulls/<n>` and one `issues/<n>` REST read, and only when a merged linked PR is found (the cache shapes its callers pass lack the base, head repository, and issue body); the audit is written in the function's comment.
- §20: security fix, so a `changelog.d/5618-…` fragment.

## Approach

Add `_pr_json_merged_into_issue_target <issue> <pr_json> [<target branch> …]` next to `_pr_json_is_issue_implementation_pr`. It reads the REST `pulls/<n>` shape (`.base.ref`, `.base.repo.default_branch`, `.head.ref`, `.head.repo.full_name`), issues no API call, and returns 0 when:

1. `base.ref` is non-empty and equals a non-empty `.base.repo.default_branch`; or
2. `base.ref` is non-empty and equals one of the non-empty `<target branch>` arguments, `.head.repo.full_name` equals `GITHUB_REPOSITORY`, `pr_head_ref_is_issue_automation_branch` is defined and accepts `(<issue>, head.ref)`.

Otherwise it returns 1 with `ISSUE_TARGET_MERGE_REJECT_REASON` set to `unverified_identity` (the base was a target branch, the identity failed) or `non_target_base`. Callers check `_pr_json_is_issue_implementation_pr` first, as the sweep does.

- **Path 1:** targets = `${CWS_INTEGRATION_BRANCH:-}`. Compute the candidate's merged flag before adopting it; merged and rejected → log and `continue`.
- **Path 2:** extend the GraphQL `linked_pr` object with `head_repo` (`headRepository { nameWithOwner }`) and `default_branch` (`baseRepository { defaultBranchRef { name } }`); after `pr_states_json` is built, map each merged link to the REST shape with jq and check it with targets = the gate's `integration_branch`; downgrade rejected entries.
- **Path 3:** `_reconcile_merged_pr_issue` fetches the PR (`_fetch_pr_json`) and the issue (`issues/<n>`: body and labels), derives the targets exactly as the sweep does (the body's integration branch; the managed child's project branch only with the `ai:orchestrator-managed` label), and adds the label only when `_pr_json_is_issue_implementation_pr` and the predicate both pass. A failed fetch adds no label.
- **Path 4:** targets = the state file's `.integration_branch`; check `_pr_json_is_issue_implementation_pr` and the predicate before promoting.

Alternatives considered: fixing only path 1 (the audit would find paths 2-4 next, as the #4813 lesson predicts); accepting the child body's `Integration branch:` line for paths 1, 2 and 4 (the state file is authoritative and not editable by issue authors).

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan. The fix is one predicate and its four call sites; splitting it would ship a gate that the remaining paths still bypass.

1. **Phase 1 — gate every poller path that marks an issue merged on the issue's target branch.**
   - Files: `scripts/orchestrate_poll_process.sh`, `tests/test_linked_pr_implementation_guard.py`, `README.md`, `changelog.d/5618-reconcile-target-branch-merges.md`.
   - Done when: the predicate's runtime tests pass (default-branch accept; target-branch automation-head accept; wrong-base reject; target-branch foreign-head reject; fork-head reject; empty default branch, empty target, and missing helper fail closed); `_reconcile_merged_pr_issue` runtime tests show a label edit only for an accepted merge; structural tests show paths 1, 2 and 4 call the predicate before recording merged state; `bash -n`, `tests/test_linked_pr_implementation_guard.py`, and the relevant `tests/test_orchestrate_poll_process.py` subsets pass.
   - Rollback: revert the phase PR; the four paths go back to adopting any merged closing-keyword PR.

## Implementation Steps

1. `scripts/orchestrate_poll_process.sh`: add `_pr_json_merged_into_issue_target` and `declare -g ISSUE_TARGET_MERGE_REJECT_REASON=''` directly after `_pr_json_is_issue_implementation_pr`, with a comment citing #5618, #4813, #5226.
2. Path 1, current-wave reconcile loop: read the candidate's merged flag before assigning `LINKED_PR_NUM`; when merged and the predicate rejects it, emit `LINKED_PR_CROSS_REF_REJECTED issue=<n> pr=<p> base=<ref> head=<ref> head_repo=<repo> project_base=<b> reason=<reason>` to stderr and `continue`.
3. Path 2: add the two GraphQL fields and the two `linked_pr` keys in `_fetch_candidate_issue_details_graphql` (documented in its comment); in `refresh_validation_dispatch_wave_gate`, re-check each merged link and downgrade rejected ones.
4. Path 3: gate the label write in `_reconcile_merged_pr_issue` as in the Approach; keep its healing note and Telegram alert for an accepted merge, and write a healing note without the "tagged" claim for a rejected one.
5. Path 4: in the backward scan, gate the ready-to-merge promotion on `_pr_json_is_issue_implementation_pr` and the predicate.
6. `tests/test_linked_pr_implementation_guard.py`: runtime tests for the predicate (extracting `pr_head_ref_is_issue_automation_branch` from `scripts/gh_helpers.sh`) and for `_reconcile_merged_pr_issue` with stubbed `gh_retry` / `_fetch_pr_json`; structural tests for paths 1, 2 and 4.
7. `README.md`: extend the `ENABLE_STALL_MERGED_PR_GUARD` row and the orchestrate_poll reconciliation text with the rule (issue #5618).
8. `changelog.d/5618-reconcile-target-branch-merges.md` (`security`).

## Files & Modules

- `scripts/orchestrate_poll_process.sh`
- `tests/test_linked_pr_implementation_guard.py`
- `README.md`
- `changelog.d/5618-reconcile-target-branch-merges.md` [new]

## Tests

- `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_linked_pr_implementation_guard.py` (already a `ci.yml` step).
- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_orchestrate_poll_process.py -k <subsets touching reconcile, stall, merged, backward, validation gate>` (the full file takes over an hour serially).
- `bash -n scripts/orchestrate_poll_process.sh`.

## Risks

- A legitimate PR into a project branch whose head is not an automation branch (a hand-made PR) no longer marks the issue merged through these paths. That matches the sweep and `issue_pr_status.yml` since #5226; the issue still resolves through its own chain or labels.
- Path 3 adds two REST reads per merged-PR hit; a rejected hit repeats them on each stall cycle until the issue leaves the stall set. Rejections are rare (an attack or an unusual flow).
- A GraphQL error from the new fields would drop that batch (the helper already fails open to `{}`); both fields are standard `PullRequest` fields.

## Rollout

Ships to consumers with the next `@stable` release. No data migration. The target-branch rule itself has no flag. The heal of merged state written before the rule (added in final-merge review round 2 on PR #5633) is on by default and can be turned off with the `ENABLE_MERGED_STATE_HEAL` repository variable (`false` only logs and alerts once); the poller exports it from `orchestrate_poll.yml`.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which producers of merged state does the fix cover? — Picked: A — all four poller paths (current-wave reconcile loop, validation-dispatch wave gate, stall-recovery `_reconcile_merged_pr_issue`, backward-scan promotion) through one predicate. Alternatives: B — only the reconcile loop the finding names; C — paths 1 and 2 (direct wave-status producers). Why: paths 3 and 4 write `ai:merged`, which `check-wave-status` reads as merged, so B and C leave the exploit open; the #4813 project's own lesson says to gate every path in the same change. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Which branches count as a wave child's target in paths 1, 2 and 4? — Picked: A — the default branch (from the PR JSON, empty fails closed) and the project's integration branch from the state file. Alternatives: B — also the child's body `Integration branch:` line, as the sweep accepts. Why: the state file is authoritative and the poller writes the same value into every child body; body text is editable. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What happens to a merged candidate the rule rejects in path 1? — Picked: A — log it and try the next candidate, never keeping it as the fallback PR. Alternatives: B — keep it as an unmerged fallback (`state: closed`). Why: mirrors the sweep; a rejected PR is not the issue's work. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Are unmerged candidates also gated on base? — Picked: A — no; only merged evidence is gated. Alternatives: B — gate every candidate. Why: `pr_state` is not a decision input downstream, so B changes nothing observable and widens the diff (§5). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] Where does the validation-dispatch gate get the head repository and default branch? — Picked: A — two extra fields on the existing GraphQL PR fragment. Alternatives: B — one REST `pulls/<n>` read per merged link. Why: §15. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Where does the predicate live? — Picked: A — `scripts/orchestrate_poll_process.sh`, next to `_pr_json_is_issue_implementation_pr`. Alternatives: B — `scripts/gh_helpers.sh`. Why: every caller is in the poller, and the existing guard tests extract helpers from it. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Is `validation_fix_issue_has_merged_pr_evidence` (base check only when a base is passed, no identity check) fixed here? — Picked: A — no; record it in the log's Notes. Alternatives: B — add the rule there too. Why: it serves validation and security fix-up issues, not wave children, and is outside the finding; §5. Applied in: no code change. Status: changed to B (2026-10-02, PR #5633)
- AD-8 [plan, 2026-09-30] In path 3, does a rejected merged PR still skip the stall recovery? — Picked: A — yes; only the label write is gated. Alternatives: B — let the recovery action run. Why: the skip is existing behaviour outside the finding (it prevents the #1074 `/reclarify` loop), and changing it is a separate availability question. Applied in: phase 1 PR. Status: changed to B (2026-10-02, PR #5633; with Q7: A, ai:done / ai:ready-to-merge count the attempt instead of acting on the rejected PR)
- AD-9 [plan, 2026-09-30] Where does path 3 get the base, head repository, and target branches, given its callers pass several cache shapes? — Picked: A — `_reconcile_merged_pr_issue` fetches `pulls/<n>` and `issues/<n>` (two REST reads, only on a merged hit) and derives the targets as the sweep does. Alternatives: B — thread full PR JSON through all six callers; C — accept only default-branch merges in path 3. Why: A is one change in the single label writer; B touches six call sites and three cache shapes; C would stop tagging legitimate project-branch merges. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` result: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
