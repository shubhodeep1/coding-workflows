# Decide orchestrator-managed status from the label and project lineage, not issue-body text

Source issue: shubhodeep1/coding-workflows#4957 (https://github.com/shubhodeep1/coding-workflows/issues/4957)
Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
Security pass: skip (ai:security: automation-produced issue)

## Summary

Project #4813 made `close_merged_issues_sweep` and `issue_pr_status.yml` count a merged PR as an issue's finished work only on the issue's target branch, but it kept a bypass for orchestrator-managed children: any base counts, and "managed" is true when the issue body merely contains the text `Managed by: AI Orchestrator`. A standalone issue that quotes that text in prose can therefore be closed by a PR merged into an unrelated branch. This plan decides managed status from the automation-applied `ai:orchestrator-managed` label alone and limits a managed child's merge to its own project branch.

## Context

- Finding (issue #4957, `security-audit.yml` against the #4813 project branch): `scripts/orchestrate_poll_process.sh:4002` sets `_sweep_issue_managed=true` when `grep -qF "Managed by: AI Orchestrator"` matches anywhere in the body, and the sweep then accepts a candidate PR merged into **any** base (`scripts/orchestrate_poll_process.sh:4042`). Recommendation: "Determine managed status from automation-controlled labels and verified project lineage, not issue-body text; require a matching project base branch."
- The issue body of #4957 itself quotes the marker in prose, which is exactly the spoof shape.
- The same test drives the workflow path. `.github/workflows/issue_pr_status.yml` step `Update linked issue labels when PR closes` puts an issue in `MANAGED_ISSUES` on the label **or** `contains("Managed by: AI Orchestrator")` (lines 265-272, 425-436, 466-473), and the per-issue loop (lines 485-513) skips the #4813 target-branch gate for a managed child and closes it on a merge into any base. The sweep's own header comment names this as "the same test issue_pr_status.yml uses".
- Every orchestrator child is created with the `ai:orchestrator-managed` label by automation (`.github/workflows/orchestrate.yml:1186-1191`, `scripts/orchestrate_poll_process.sh` issue-creation paths at lines 5575, 7484, 14009, 19337, 21222, 21298, 21716, 22895, 23134, `scripts/validate_process.sh:4196`, `scripts/review_rb_judge.sh:2382`, `scripts/workflow_failure_heal_intake.sh:821`), and its body carries the metadata lines `- Tracking issue: #<T>` and `- Integration branch: <branch>` (bold forms `- **Tracking issue:** #<T>` / `- **Integration branch:** \`<branch>\`` in heal issues). The orchestrator's integration branch is always `orchestrator/project-<T>` (`.github/workflows/orchestrate.yml:1099`).
- Base branch: this issue names `claude/implement-plan-issue-4813-close-sweep-target-branch-merges` (the #4813 project branch, final PR #4826 still an open draft into `main`), so this project builds on it and its final PR targets it.

## Goals

- `close_merged_issues_sweep` treats an issue as orchestrator-managed only when it carries the `ai:orchestrator-managed` label. Body text never sets it.
- A managed child's merged implementation PR counts only when its `base.ref` is the default branch, the issue's declared integration branch (the #4813 rule), or `orchestrator/project-<T>` where `<T>` is the issue's single `Tracking issue: #<T>` metadata value. Any other base is logged as `rejected=non_target_base` and falls through to the existing no-merged-PR policy.
- `issue_pr_status.yml` applies the same rule in its label and close gate: `is_managed_child` needs the label, and a managed child's merge into a non-default base labels and closes it only on that same project branch. An issue that has the marker text but not the label is logged and handled as a standalone issue.
- `MANAGED_ISSUES` (the exported classification the `Send PR merged Telegram alert` step reads to suppress an alert) keeps its current label-or-body definition.
- No new GitHub API call (§15): the label and body come from payloads both paths already fetch.
- Tests prove: a standalone issue whose body quotes the marker is not closed by a merge into an unrelated branch (sweep and workflow); a labelled child still closes on its declared integration branch and on `orchestrator/project-<T>`; a labelled child does not close on an unrelated base.
- A `changelog.d/` fragment (§20, `security`).

## Non-goals

- `scripts/claude_issue_route.py` routing and the other readers of the marker (`plan.yml`, `orchestrate_clarify_respond.yml`, the stall-recovery orphan check). They pick a pipeline or a prompt, not whether an issue is finished; each is its own finding if it matters.
- The `Send PR merged Telegram alert` step's fallback lookup (label or body): it only suppresses a DEBUG alert, and `tests/test_issue_pr_status_payload_fallback_contract.py` pins it.
- Sibling findings on the same base: #4955 (large issue bodies disable the sweep, line 3918) and #4956 (`issue_body_integration_branch` regex cost). They are separate issues with their own projects.
- Verifying that `<T>` is an `ai:orchestrator-tracking` issue: that would add one API call per candidate (§15), and the label already comes from automation.

## Constraints

- §1: security first. The narrower rule may leave a legitimately managed child open in an edge case (Risks); a spoofed close is the worse failure.
- §5: change only the managed-status test and the base check that uses it, in the two places that close issues.
- §6: no identifier is renamed or removed. `_sweep_issue_managed`, `MANAGED_ISSUES`, `is_managed_child`, `record_issue_integration_branches`, the `CLOSE_MERGED_SWEEP` prefix and its existing keys stay; the rejection line only gains a trailing `project_base=` key. New identifiers checked for collisions: `issue_body_orchestrator_project_branch`, `_sweep_issue_project_branch`, `ISSUE_MANAGED_PROJECT_BRANCHES`, `issue_has_managed_label`, `issue_managed_project_branch_for`, `issue_project_branch`, `_orch_meta_project_branch`.
- §9: tabs in `scripts/gh_helpers.sh`; the poller and workflow keep their 2-space style; YAML stays 2-space.
- §14: `issue_pr_status.yml` and `scripts/gh_helpers.sh` reach consumers together through `@stable`. The workflow keeps a fail-closed stub for the new helper.
- §15: no new API call.
- §19: PR bodies use `Refs #4957`; the final PR targets a non-default base, so the final-merge stage closes the issue explicitly.
- §27: `issue_pr_status.yml` is about 40 KB, far below the 480,000-byte guard.

## Approach

1. **Lineage parser.** Add `issue_body_orchestrator_project_branch <body>` to `scripts/gh_helpers.sh`, next to `issue_body_integration_branch`. It reads the `Tracking issue: #<T>` metadata line (plain or bold, optional leading `- `, the whole line), and prints `orchestrator/project-<T>` only when exactly one distinct `<T>` is present; otherwise it prints nothing. It uses `sed -nE` and `sort -u`, with no API call and no `python3`.
2. **Sweep.** `_sweep_issue_managed` becomes label-only. For a managed issue, `_sweep_issue_project_branch` is `issue_body_orchestrator_project_branch` of its body. The accept condition becomes: base equals the default branch, or base equals the declared integration branch, or (managed and non-empty project branch and base equals it). The rejection line gains `project_base=<branch|none>`.
3. **Workflow.** `record_issue_integration_branches` also records, for each node that carries the `ai:orchestrator-managed` label, `<issue>\t<project branch>` in `ISSUE_MANAGED_PROJECT_BRANCHES`; the REST fallback records the same from `_orch_meta`. In the loop, `is_managed_child` needs membership in `MANAGED_ISSUES` **and** a recorded label; a body-only match logs `Issue #<n> has the "Managed by: AI Orchestrator" text but not the ai:orchestrator-managed label; treating it as a standalone issue.` For a merged PR into a non-default base, the gate accepts the declared integration branch for every issue and, for a managed child, also its project branch; anything else logs and skips (the existing message for standalone issues, a managed-child variant naming the project branch).

Alternatives: see AD-1 to AD-4.

## Phases & Merge Strategy

**Single phase.** Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue fixes the scope, and the sweep and workflow changes are one invariant (body text never makes an issue managed; a managed child finishes only on its own project branch). Shipping one half leaves the same spoof open through the other path.

1. **Phase 1 — label-only managed status with a project-branch match.**
   - Files: `scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/issue_pr_status.yml`, `tests/test_orchestrate_poll_process.py`, `tests/test_issue_pr_status_target_branch_gate.py`, `tests/test_gh_helpers_issue_body_integration_branch.py`, `README.md`, `changelog.d/4957-managed-status-from-label-and-lineage.md` [new]. No `.claude/**` path.
   - Done when: the new and updated tests pass together with the existing sweep, workflow-step and helper tests; `bash -n` is clean on the touched scripts; the workflow YAML parses; the README rows describe the rule.
   - Rollback: revert the phase PR. `ENABLE_CLOSE_MERGED_ISSUES=false` still stops the sweep without a revert.

## Implementation Steps

Phase 1:

1. `scripts/gh_helpers.sh`: add `issue_body_orchestrator_project_branch()` after `issue_body_integration_branch`, with a header comment naming both callers and issue #4957.
2. `scripts/orchestrate_poll_process.sh` `close_merged_issues_sweep`:
   - header comment: managed means the label, and a managed child's merge counts on its declared integration branch or `orchestrator/project-<T>`;
   - `local` line: add `_sweep_issue_project_branch`;
   - per issue: drop the body `grep` from `_sweep_issue_managed`; compute `_sweep_issue_project_branch` for a managed issue (guarded with `type … >/dev/null 2>&1`);
   - candidate loop: replace the unconditional managed accept with the project-branch match; append `project_base=` to the rejection line.
3. `.github/workflows/issue_pr_status.yml` step `Update linked issue labels when PR closes`:
   - `ISSUE_MANAGED_PROJECT_BRANCHES=""`, a fail-closed stub for `issue_body_orchestrator_project_branch`, and the helpers `issue_has_managed_label` / `issue_managed_project_branch_for`;
   - `record_issue_integration_branches`: record label-managed nodes before the existing `continue`;
   - REST fallback: record label-managed issues from `_orch_meta`;
   - loop: label-gated `is_managed_child`, then the base gate for every issue with the managed project-branch alternative;
   - update the three-bucket comment block to describe managed as label-based for this gate.
4. `tests/test_gh_helpers_issue_body_integration_branch.py`: cases for the new helper (plain, bold, bullet, two identical lines, two different lines, prose mention, none).
5. `tests/test_orchestrate_poll_process.py`:
   - give `test_close_merged_issues_sweep_closes_ready_to_merge_with_verified_merged_pr`'s #10 a realistic metadata body (`- Tracking issue: #192`, no `Integration branch:` line), so it exercises the lineage path;
   - new: body-marker spoof without the label, PR merged into an unrelated branch → not closed, `rejected=non_target_base`;
   - new: labelled child whose PR merged into an unrelated branch → not closed;
   - new: labelled child merged into its declared `Integration branch:` → closed.
6. `tests/test_issue_pr_status_target_branch_gate.py`: update the managed-child case to a realistic body, and add: marker-only spoof on an unrelated base → no label, no close; labelled child on an unrelated base → no label, no close; labelled child on `orchestrator/project-<T>` from its tracking line → label + close.
7. `README.md`: the `ENABLE_CLOSE_MERGED_ISSUES` row (line 200) and the `issue_pr_status.yml` row (line 1184).
8. `changelog.d/4957-managed-status-from-label-and-lineage.md` [new], section `security`.

## Files & Modules

- `scripts/gh_helpers.sh`
- `scripts/orchestrate_poll_process.sh`
- `.github/workflows/issue_pr_status.yml`
- `tests/test_gh_helpers_issue_body_integration_branch.py`
- `tests/test_orchestrate_poll_process.py`
- `tests/test_issue_pr_status_target_branch_gate.py`
- `README.md`
- `changelog.d/4957-managed-status-from-label-and-lineage.md` [new]

## Tests

- Unit/integration (pytest): the files above, plus `tests/test_issue_pr_status_payload_fallback_contract.py` and `tests/test_linked_pr_implementation_guard.py`, which touch the same step and sweep.
- Static: `bash -n scripts/gh_helpers.sh scripts/orchestrate_poll_process.sh`; the workflow YAML parses; `tests/test_workflow_file_size_limit.py`.
- End to end: the chain's conformance audit and runtime validation. The security pass is skipped for this automation-produced follow-up (plan header).

## Risks & Mitigations

- A labelled child whose body has neither an `Integration branch:` line naming its base nor a single `Tracking issue:` line, merged into a non-default base, is no longer closed by these two paths. Mitigation: every creation path writes both lines; the orchestrator's own `reconcile_managed_issue_labels` / wave logic stays the authoritative close path, and the sweep's no-merged-PR policy alerts on a stranded `ai:merged` issue.
- An issue that lost its label (a human removed it) stops getting the managed close. ACCEPTED: the label is the automation's own record; its absence is exactly the case the finding asks not to trust.
- A consumer on an older `gh_helpers.sh`: the stub prints nothing, so a managed child without a matching `Integration branch:` line fails closed (not closed) rather than open.
- Merge conflicts with sibling projects #4955 / #4956 on the same base (same function / helper file). Mitigation: the chain syncs the base into the project branch at every stage and resolves conflicts per §12.

## Rollout

Lands on the #4813 project branch with this project's final PR, then reaches `main` with #4826 and consumers on the next `@stable` release (§14). No flag: it narrows when an issue is closed. `ENABLE_CLOSE_MERGED_ISSUES=false` stays the emergency stop for the sweep.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Does the fix cover `issue_pr_status.yml`'s close gate as well as the sweep line the finding names? — Picked: A — both paths. Alternatives: B — the sweep only, as the finding's location reads. Why: the workflow's managed bypass uses the same body test and closes the issue on any base, so B leaves the reported exploit live through the PR-close event (§1). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What is "verified project lineage" for a labelled child? — Picked: A — its declared `Integration branch:` line, or `orchestrator/project-<T>` from a single `Tracking issue: #<T>` line. Alternatives: B — the declared integration branch only; C — any base matching `^orchestrator/project-[0-9]+$`. Why: A requires the base to be that child's own project branch and keeps children that lack an `Integration branch:` value closing; B could strand such children; C lets a labelled child close on another project's branch. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Should `MANAGED_ISSUES` (used by the merged-alert step) become label-only too? — Picked: A — no; only the label and close gate changes. Alternatives: B — make the classification label-only everywhere in the workflow. Why: the alert step only suppresses a DEBUG message, and its contract test pins the label-or-body lookup; §5. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Should the sweep confirm `<T>` is an `ai:orchestrator-tracking` issue? — Picked: A — no. Alternatives: B — one `issues/<T>` read per managed candidate. Why: §15 forbids a per-item call here, and the automation-applied label plus the exact project-branch match already bind the child to one project. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-29] Issue #4957's body quotes `Managed by: AI Orchestrator` in prose; is it routed away (implement-issue-claude step 2)? — Picked: A — no; the gate reads a `Managed by: AI Orchestrator` line, and the issue carries neither that line nor `ai:orchestrator-managed`. Alternatives: B — treat the prose as the marker and stop. Why: the prose mention is the finding's own subject, not metadata. Applied in: no code change. Status: pending review

## References

- Issue #4957 (this finding); audit tracker #3576.
- Issue #4813, its plan `docs/plans/issue-4813-close-sweep-target-branch-merges-plan.md`, and final PR #4826.
- Sibling findings #4955, #4956.
