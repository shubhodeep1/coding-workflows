# Implement-Plan Log — Stream every PR comment in the merge hold gate

- Plan: docs/plans/issue-5566-stream-merge-gate-comments-plan.md
- Source issue: shubhodeep1/coding-workflows#5566
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5566-stream-merge-gate-comments   Final PR: #5571 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (this PR)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 implemented and verified (44/44 gate tests, 173/173 in the ci.yml hand-back/sweep/gate step, ruff clean); phase PR opened against the project branch

## Phases
1. [ ] Phase 1 — stream the gate's comment read (scripts/claude_merge_hold_gate.py, tests/test_claude_merge_hold_gate.py, README.md, agents.md, changelog.d/5566-merge-gate-comment-flood.md) — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; security_pass_skip.py verified)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Where does the fix live? — Picked: A — a streaming reader inside `scripts/claude_merge_hold_gate.py`. Alternatives: B — remove the cap in `.claude/scripts/check_in_status.py` `gh_api_list`; C — both. Why: the merge refusal is the gate's; B changes the checker and sweep too and edits a protected `.claude/` path. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Should the stream have any page limit? — Picked: A — no limit; the read ends at the first short page. Alternatives: B — a much higher cap that still fails closed. Why: any fixed cap brings the same denial of service back at a higher comment count. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How is memory bounded? — Picked: A — keep only trusted-author comments with a claim-marker line, trimmed to the fields and marker lines `read_fix_claims` reads. Alternatives: B — keep every comment. Why: only trusted markers may grow the kept set; trimming keeps the marker count, so the decision is unchanged. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Retry policy for a page read? — Picked: A — 3 attempts, 1 s then 2 s back-off, only for read failures; shape errors fail closed at once. Alternatives: B — no retry; C — retry shape errors too. Why: a long stream makes one transient 5xx more likely; a malformed payload is not transient. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Fix the same 10-page cap in the checker and sweep? — Picked: A — no, out of scope; noted. Alternatives: B — also change `check_in_status.gh_api_list`. Why: those callers retry rather than refuse a merge, and the change needs the protected-path twin flow. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] Replace page-number pagination to avoid a skip when a comment is deleted mid-read? — Picked: A — keep page numbers; record the limitation. Alternatives: B — a second full pass to confirm; C — GraphQL cursors. Why: pre-existing and shared with `check_in_status.py`; B doubles calls on a flooded PR, C changes transport. Applied in: no code change. Status: pending review

## Lessons
- [source:plan-deviation] A test fake `gh` that returns the whole list for page 1 and `[]` for later pages cannot exercise pagination limits; slice list payloads by `per_page`/`page` so flood cases reach page 11+. (files: tests/test_claude_merge_hold_gate.py)

## Notes
- Base branch: claude/implement-plan-issue-5316-gate-auto-merge-on-hold-claims (open final PR #5323 into main).
- Security pass: `security_pass_skip.py` → skip (label ai:security, created and labelled by the issue automation).
