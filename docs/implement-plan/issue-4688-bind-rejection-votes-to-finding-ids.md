# Implement-Plan Log — Bind review rejection votes to per-run finding IDs, so quoted REJECTED_FINDING text cannot demote a finding

- Plan: docs/plans/issue-4688-bind-rejection-votes-to-finding-ids-plan.md
- Source issue: shubhodeep1/coding-workflows#4688
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason
- Project branch: claude/implement-plan-issue-4688-bind-rejection-votes-to-finding-ids   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-28
- Last note: project branch opened from the #4586 project branch; phase 1 in progress.

## Phases
1. [ ] Phase 1 — ID-bound rejection votes (issuer, header, gate, resume persistence, docs, tests)

## Conformance

## Security pass
- Skipped: plan header `Security pass: skip (ai:security: automation-produced issue)`.

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] How are rejection votes bound so quoted text cannot count? — Picked: A — per-run random finding IDs, issued after pass 1 into a manifest, and only manifest IDs count. Alternatives: B — keep file/line matching and only harden parsing (column 0, no fences, required reason); C — remove the rejection path. Why: B still counts a column-0 quote in prose; C reopens #4586; A is what the finding recommends. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-28] Where does ID issuing live? — Picked: A — a new `--issue-ids` mode of `scripts/review_claude_fixer_nonblocking.py`. Alternatives: B — a new script. Why: already bootstrapped and owns the ledger parser, so issuer and gate cannot drift. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-28] What happens to legacy-format `REJECTED_FINDING` lines without an ID? — Picked: A — ignored; the entry stays blocking. Alternatives: B — accept them during a transition. Why: B keeps the exploit open. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-28] What is the vote line shape? — Picked: A — `REJECTED_FINDING: <ID> | <file>:<line> | flagged_by: <slug> | reason: <one sentence>`, only the ID and a non-empty reason required, echoed location ignored by the gate. Alternatives: B — ID and reason only. Why: A keeps the summariser's informational `rejected_by:` mapping. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-28] How do IDs survive a same-head resume? — Picked: A — reuse the manifest when its `ledger_sha256` matches, and persist it with the partial-finalize artifacts. Alternatives: B — always issue new IDs. Why: B drops cached votes on every resume. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-28] The unreleased #4586 changelog fragment describes file/line/flagger matching. — Picked: A — correct that row and add this issue's own `security` fragment. Alternatives: B — add only this issue's fragment. Why: B ships a stale statement. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-28] Defend against a prompt-injected majority writing valid votes? — Picked: A — no; accepted residual risk. Alternatives: B — disable demotion. Why: an injected majority can already suppress a finding by not reporting it. Applied in: no code change. Status: pending review
- AD-8 [plan, 2026-09-28] Which pass-1 entries get an ID? — Picked: A — `CONSENSUS FINDINGS` entries with a parseable location and exactly one flagger. Alternatives: B — every entry. Why: only single-reviewer entries can be demoted. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A); started by the Claude issue dispatcher in session session_01REL9XiCRyzMYVeALbEuUdc.
- Issue progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4688#issuecomment-5862298363
- No phase touches `.claude/**`, so no protected-path approval is needed.
