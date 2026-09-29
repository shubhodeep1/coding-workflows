# Implement-Plan Log — Decide orchestrator-managed status from the label and project lineage, not issue-body text

- Plan: docs/completed/issue-4957-managed-status-from-label-and-lineage-plan.md (moved from docs/plans/ by the completion PR)
- Source issue: shubhodeep1/coding-workflows#4957
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4957-managed-status-from-label-and-lineage   Final PR: #4999 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4813-close-sweep-target-branch-merges)
- Waiting on: completion PR (branch claude/implement-plan-issue-4957-managed-status-from-label-and-lineage-complete)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01T1YB4i2LsJMe7tn8ohxfW3
- Last updated: 2026-09-29
- Last note: conformance run 1 CONFORMANT (no fixes); validation skipped by Q1: A (standing decision Q17: A); completion PR opened.

## Phases
1. [x] Phase 1 — label-only managed status with a project-branch match (sweep + issue_pr_status.yml + helper + tests + docs + changelog) — PR #5011 merged 2026-09-29 (42c307a); review rounds: 1; interventions: 0

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — no fixes (pre-security). Implemented: COMPLETE; Correctness: PASS. Checks: `bash -n` on scripts/gh_helpers.sh and scripts/orchestrate_poll_process.sh; issue_pr_status.yml parses; 45 targeted tests (helper, target-branch gate, payload-fallback contract, linked-PR guard, workflow size limit) and 20 sweep tests in tests/test_orchestrate_poll_process.py passed.

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified).

## Validation
- Skipped (covered by #4813's project validation). validate.yml binds `target_ref` only to a final PR into the default branch, and final PR #4999 targets claude/implement-plan-issue-4813-close-sweep-target-branch-merges. Blocked 2026-09-29 before dispatch; Q1 asked on #4957 and answered Q1: A by the owner (master session, standing decision Q17: A) at https://github.com/shubhodeep1/coding-workflows/issues/4957#issuecomment-5885304516. Long-term fix: #4734.

## Completion
- Completion PR (branch claude/implement-plan-issue-4957-managed-status-from-label-and-lineage-complete) — doc moved to docs/completed/issue-4957-managed-status-from-label-and-lineage-plan.md
- Final PR #4999 draft (base claude/implement-plan-issue-4813-close-sweep-target-branch-merges)

## Activation
- n/a: the issue base is the #4813 project branch, so this change goes live with #4813's lifecycle (Issue Mode).

## Auto-decisions
- AD-1 [plan, 2026-09-29] Does the fix cover `issue_pr_status.yml`'s close gate as well as the sweep line the finding names? — Picked: A — both paths. Alternatives: B — the sweep only. Why: the workflow's managed bypass uses the same body test and closes the issue on any base (§1). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What is "verified project lineage" for a labelled child? — Picked: A — its declared `Integration branch:` line, or `orchestrator/project-<T>` from a single `Tracking issue: #<T>` line. Alternatives: B — the declared integration branch only; C — any `^orchestrator/project-[0-9]+$` base. Why: A binds the child to its own project branch and keeps children without an `Integration branch:` value closing. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Should `MANAGED_ISSUES` (used by the merged-alert step) become label-only too? — Picked: A — no; only the label and close gate changes. Alternatives: B — label-only everywhere in the workflow. Why: the alert step only suppresses a DEBUG message and its contract test pins it; §5. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Should the sweep confirm `<T>` is an `ai:orchestrator-tracking` issue? — Picked: A — no. Alternatives: B — one `issues/<T>` read per managed candidate. Why: §15; the label plus the exact project-branch match already bind the child. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-29] Issue #4957's body quotes `Managed by: AI Orchestrator` in prose; is it routed away? — Picked: A — no; the gate reads a metadata line and the issue has neither that line nor `ai:orchestrator-managed`. Alternatives: B — stop as Codex-owned. Why: the prose is the finding's subject, not metadata. Applied in: no code change. Status: pending review

## Lessons
- [source:plan-deviation] An issue-mode project whose base is another project's branch cannot run runtime validation, because validate.yml authorizes `target_ref` only through an open final PR into the default branch; its plan should state up front that the parent project's validation covers it. (files: .github/workflows/validate.yml)

## Notes
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4957#issuecomment-5883218912
- Security pass: skip (`security_pass_skip.py`: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- This issue is one of the #4813 project's security-pass cycle 1 follow-ups (#4955, #4956, #4957); that project's checker waits for all three to be closed or `ai:merged`.
- Session: started by the Claude issue dispatcher routine as session_01KWUFqLvyEZEvWAY5eURL5s (auto mode). No `mcp__github__*` tools in this session: GitHub writes go through the REST API.
- Phase 1 PR #5011 opened 2026-09-29 against the project branch; final PR #4999 (draft, base claude/implement-plan-issue-4813-close-sweep-target-branch-merges); project checker session_01T1YB4i2LsJMe7tn8ohxfW3.
- Review round 1 (2026-09-29, session_017ufc3dG2wiwamEgTTaBDmm, head 7fc0073): 1 finding (minimax, low) — the MANAGED_ISSUES comment block said managed children merge "never main". Valid: fixed, and the standalone-bucket wording was corrected to the #4813 rule in the same comment block.
- PR #5011 merged 2026-09-29T05:43:26Z (merge commit 42c307a).
- Conformance 1/3 stage (2026-09-29, session_01AvoNBswiUwSKCmYNNZXtwz): CONFORMANT; the issue base had not merged and the project branch was already in sync with it. Validation could not be dispatched (see ## Validation): blocked comment https://github.com/shubhodeep1/coding-workflows/issues/4957#issuecomment-5885067849, `ai:claude-blocked` added, then removed after the owner's Q1: A. The same session continued to the completion PR on the master session's wake.
