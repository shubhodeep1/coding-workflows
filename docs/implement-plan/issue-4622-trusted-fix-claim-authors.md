# Implement-Plan Log — Count Claude fix claims and holds only from the PR's author or the workflow account

- Plan: docs/plans/issue-4622-trusted-fix-claim-authors-plan.md
- Source issue: shubhodeep1/coding-workflows#4622
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: main
- Project branch: claude/implement-plan-issue-4622-trusted-fix-claim-authors   Final PR: #4632 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (the PR carrying this log update)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-27
- Last note: phase 1 implemented and verified (claim authors restricted to the PR author and CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN; 130 targeted tests + 226 related tests pass); phase 1 PR opened against the project branch.

## Phases
1. [ ] Phase 1 — restrict claim authors to the PR author and the workflow account   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue) — plan header `Security pass: skip`.

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Which accounts may post a claim or hold that counts? — Picked: A — an owner/member/collaborator comment whose author is the PR's author or the configured workflow account `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`. Alternatives: B — only a dedicated GitHub App bot, as for fixer verdicts; C — keep association-only and give holds a lease. Why: A closes the spoofing path for every collaborator who does not own the PR while keeping the two identities that actually post claims; B would ignore every session claim because web sessions cannot post as a bot; C leaves the forgery in place for days. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] Should a hold also need verified cap state (`cap_reached`) to count? — Picked: A — no; bind holds by author identity only. Alternatives: B — honour a hold only when `cap_reached` is true. Why: `/fix-claude-pr` legitimately holds dead ends below the cap, so B would break documented holds; the author binding already stops forged holds and forged counted claims. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] Add a new env var listing extra trusted claim logins? — Picked: A — no. Alternatives: B — add `CLAUDE_FIX_CLAIM_AUTHOR_LOGINS`. Why: §5 minimal change; the PR author and the workflow account cover every writer today. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] Compare logins exactly or case-insensitively? — Picked: A — case-insensitively. Alternatives: B — exact. Why: GitHub logins are case-insensitive and unique; a mis-cased repo variable no longer drops sweep reservations. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] Should claims from untrusted authors still count toward `hand_backs`? — Picked: A — no. Alternatives: B — count them. Why: forged counted claims would otherwise push a PR to the cap and force a real fixer into a hold. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Started by the Claude issue dispatcher (`/implement-issue-claude`, issue mode) in session session_01T3kpGgTVrJhjefULKLUUqL, permission mode auto.
- Issue progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4622#issuecomment-5854270496
