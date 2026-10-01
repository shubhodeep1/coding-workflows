# Implement-Plan Log — Finalize tracking-issue lineage only from the orchestrator completion PR

- Plan: docs/plans/issue-5619-tracking-lineage-completion-pr-only-plan.md
- Source issue: shubhodeep1/coding-workflows#5619
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5619-tracking-lineage-completion-pr-only   Final PR: #5632 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5643
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01LHayV9tdHEAgtpejcbwf7v   safety net and hand-back re-armed after the round 3 intervention push (ids in the stage report)
- Last updated: 2026-10-01
- Last note: resumed on #5619 Q1: A after the OpenRouter credit outage; review round 3's only finding (the outage's failed `review / codex-agent` check) rejected, and the project branch merged into PR #5643 as `[claude-intervention]` for a fresh head.

## Phases
1. [ ] Phase 1 — completion-PR gate for tracking-issue lineage (issue_pr_status.yml + tests + README + changelog)   — PR #5643 open (waiting); review rounds: 3; interventions: 1 (2026-10-01: merged the project branch after the reviewer outage, Q1: A)

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which PR may finalize an orchestrator-tracking issue's lineage? — Picked: A — only its completion PR: merged, base the default branch, head `orchestrator/project-<T>` in this repository. Alternatives: B — none; never finalize tracking-issue lineage in `issue_pr_status.yml`; C — any PR merged into the default branch. Why: A is the audit's recommendation; B drops the legitimate path, C still lets an arbitrary linked PR finalize the project. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Does a PR closed without merging still write a tracking issue's lineage as `closed`? — Picked: A — no, skip it. Alternatives: B — keep writing `closed`. Why: an unmerged close never ends an orchestrator project (the poller reopens a fresh final PR), so `closed` is the same tampering as `merged`. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How is an issue treated whose classification lookups both failed (the step skips it as tracking)? — Picked: A — the same completion-PR rule as a tracking issue. Alternatives: B — keep #5227's rule (finalize on an unmerged close or a default-branch merge). Why: §1; the issue may be a tracking issue, and a missed standalone lineage costs only memory. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Should the step also derive the tracking issue from a completion PR's `orchestrator/project-<T>` head, so a body with only `Refs #<T>` still finalizes it? — Picked: A — no. Alternatives: B — yes, add `<T>` to the linked issues from the head ref. Why: §5; it adds a new linked-issue path into the label/close loop, which is out of scope for a tampering fix. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] What happens to the existing tests that assert the old tracking and unclassified lineage behaviour? — Picked: A — add accurately named tests and keep the old function names as aliases that call them (§6). Alternatives: B — rewrite the bodies under the old names; C — delete the old tests. Why: §6 forbids removal and repurposing; aliases keep names callable without misleading readers. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1, 2026-09-30] The REST fallback appends to `TRACKING_ISSUES` / `MANAGED_ISSUES` with `+=` after the payload list, which has no trailing newline, so `5` plus a failed lookup of `10` became `510` and tracking issue #5 was labelled `ai:merged` and closed on a default-branch merge (reproduced). Fix it in this phase? — Picked: A — yes, merge with the existing `merge_issue_number_list` and add a regression test. Alternatives: B — leave it and file a separate issue. Why: §1; it defeats the tracking skip this phase's gate relies on, and it is the #2760 incident class. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] A bash list built from `$(...)` output has no trailing newline, so appending with `+="<n>"$'\n'` fuses numbers (`5` + `10` → `510`) and silently breaks `grep -qxF` membership; merge list items with a helper such as `merge_issue_number_list`. (files: .github/workflows/issue_pr_status.yml)
- [source:intervention] When the reviewer provider fails (for example out of credits), the failed review check stays on the head and the next reviewer run reports it as a finding; no code change fixes it, so give the PR a new head (sync the base branch in) instead of a code edit. (files: .github/workflows/review_autofix.yml)

## Notes
- 2026-10-01 review round 3 (head 2b0a1f9, run 36794127294, ledger f59789f9…): one consensus finding, the failed `review / codex-agent` check from runs 36741838875 / 36752333871 / 36760232242, all "Insufficient credits" at OpenRouter. Rejected as not a code defect. Per the owner's Q1: A on #5619, merged the project branch into the phase branch (`[claude-intervention]`, brings #5304) for a fresh head.
- 2026-09-30 blocked: after round 2 every review run failed on the OpenRouter credit outage and the identical-failure cap labelled PR #5643 `ai:review-blocked`; asked on #5619 (Q1). The owner topped up the credits and answered Q1: A on 2026-10-01.
- 2026-09-30 review round 2 (head 614b026, catch-all fixer session_01EKhL3MUGUcSQzmGtsY67RV): 1 finding fixed (the rejected-finding count in this log).
- 2026-09-30 review round 1 (head 9b450c8, two reviewer runs, ledgers afd8b85b… and ae6fb495…): fixed the README `issue_pr_status.yml` row's unmerged-close wording; rejected the rest (helper scope, `+=` on a newline-terminated list, alias names kept per AD-5, `ORCH_INTEGRATION_BRANCH_PATTERN`, defensive `-n` guard, env defaults, body-URL-only test gap).
- Issue mode: the session started in `auto` permission mode (no start-up check needed).
- Base branch `claude/implement-plan-issue-4813-close-sweep-target-branch-merges` is project #4813's branch (final PR #4826, open draft into main at start).
