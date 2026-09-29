# Implement-Plan Log — Decide orchestrator-managed status from the label and project lineage, not issue-body text

- Plan: docs/plans/issue-4957-managed-status-from-label-and-lineage-plan.md
- Source issue: shubhodeep1/coding-workflows#4957
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4957-managed-status-from-label-and-lineage   Final PR: #4999 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5011
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01T1YB4i2LsJMe7tn8ohxfW3
- Last updated: 2026-09-29
- Last note: review round 1 on PR #5011: fixed the one finding (stale MANAGED_ISSUES / standalone bucket comment in issue_pr_status.yml); pushed a [claude-autofix] commit.

## Phases
1. [ ] Phase 1 — label-only managed status with a project-branch match (sweep + issue_pr_status.yml + helper + tests + docs + changelog) — PR #5011 open (waiting); review rounds: 1; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Does the fix cover `issue_pr_status.yml`'s close gate as well as the sweep line the finding names? — Picked: A — both paths. Alternatives: B — the sweep only. Why: the workflow's managed bypass uses the same body test and closes the issue on any base (§1). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What is "verified project lineage" for a labelled child? — Picked: A — its declared `Integration branch:` line, or `orchestrator/project-<T>` from a single `Tracking issue: #<T>` line. Alternatives: B — the declared integration branch only; C — any `^orchestrator/project-[0-9]+$` base. Why: A binds the child to its own project branch and keeps children without an `Integration branch:` value closing. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Should `MANAGED_ISSUES` (used by the merged-alert step) become label-only too? — Picked: A — no; only the label and close gate changes. Alternatives: B — label-only everywhere in the workflow. Why: the alert step only suppresses a DEBUG message and its contract test pins it; §5. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Should the sweep confirm `<T>` is an `ai:orchestrator-tracking` issue? — Picked: A — no. Alternatives: B — one `issues/<T>` read per managed candidate. Why: §15; the label plus the exact project-branch match already bind the child. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-29] Issue #4957's body quotes `Managed by: AI Orchestrator` in prose; is it routed away? — Picked: A — no; the gate reads a metadata line and the issue has neither that line nor `ai:orchestrator-managed`. Alternatives: B — stop as Codex-owned. Why: the prose is the finding's subject, not metadata. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4957#issuecomment-5883218912
- Security pass: skip (`security_pass_skip.py`: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- This issue is one of the #4813 project's security-pass cycle 1 follow-ups (#4955, #4956, #4957); that project's checker waits for all three to be closed or `ai:merged`.
- Session: started by the Claude issue dispatcher routine as session_01KWUFqLvyEZEvWAY5eURL5s (auto mode). No `mcp__github__*` tools in this session: GitHub writes go through the REST API.
- Phase 1 PR #5011 opened 2026-09-29 against the project branch; final PR #4999 (draft, base claude/implement-plan-issue-4813-close-sweep-target-branch-merges); project checker session_01T1YB4i2LsJMe7tn8ohxfW3.
- Review round 1 (2026-09-29, session_017ufc3dG2wiwamEgTTaBDmm, head 7fc0073): 1 finding (minimax, low) — the MANAGED_ISSUES comment block said managed children merge "never main". Valid: fixed, and the standalone-bucket wording was corrected to the #4813 rule in the same comment block.
