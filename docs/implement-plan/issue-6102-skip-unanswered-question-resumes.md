# Implement-Plan Log — Usage-limit resumes: skip sessions waiting on an unanswered question

- Plan: docs/plans/issue-6102-skip-unanswered-question-resumes-plan.md
- Source issue: shubhodeep1/coding-workflows#6102 (progress comment 5956408433)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-5660-resume-usage-limit-stops
- Project branch: claude/implement-plan-issue-6102-skip-unanswered-question-resumes   Final PR: #6105 draft
- Status: BLOCKED
- Stage: phase 1/1 — review round 1 (twin sync)
- Activation: not started
- Waiting on: PR #6112: twin sync (review round 1 fixes)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01HKnvTyY9wdNoZoKqLisFzv (idle, reused on resume)   safety net none   hand-back none
- Last updated: 2026-10-02
- Last note: review round 1 on ed72c11 fixed twin-first (vocabulary-only limit waits, needs_input before too_old, tests, changelog/handbook wording); selector suite 181 passed, only test_template_parity red until the twin sync; held for the twin sync

## Phases
1. [ ] Phase 1 — skip unanswered requests on both signals (selector twin, tests, README, agents.md, changelog fragment)   — protected paths: .claude/scripts/usage_limit_resumes.py (via workflow-templates/.claude/scripts/usage_limit_resumes.py)   — PR #6112 open; twin sync ed72c11 ([claude-twin-sync] issue-6102 phase 1, by the operator's driver session_01Db6EvqsPiaDHDTp8fUMeV8, answer Q1: A in issue comment of 2026-10-02 17:05Z) copied the twin to .claude/scripts/usage_limit_resumes.py, sha256 f83055de…7758778d verified, root and twin byte-equal, 230 passed across the selector, stale-routine, and changelog suites; review round 1 (ed72c11) fixed twin-first, held for the second twin sync; review rounds: 1; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py --issue 6102` printed `"skip": true`)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-02] How does the selector recognise an unanswered request on the `text` signal? — Picked: A — a non-empty `needs_action` in either summary copy that is not only a limit wait (no Q-ID, `?`, or reply/answer/decide/confirm/choose/approve; the limit error text, or text that starts with the wait or the limit and names a limit plus a wait word), on both signals; a `need_input` summary with an empty `needs_action` still resumes. Alternatives: B — skip every `need_input` category on both signals; C — skip only a `needs_action` matching a list of question phrasings. Why: B would likely stop every limit-stopped session (the #5660 contract says a failed turn can still show `need_input`), and C fails open on wording it does not know. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-02] Should the resume prompts also say they are not an answer? — Picked: A — yes, one fixed sentence in both prompts. Alternatives: B — selector change only. Why: it covers the case the snapshot cannot see (an empty `needs_action` after a question) at the cost of one sentence. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-02] Which skip reason does the new rule report? — Picked: A — the existing `needs_input`. Alternatives: B — a new `unanswered_request` reason. Why: same meaning, no new identifier (§6), and the docs already name it. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-02] Update `.claude/commands/claude-issue-pickup.md`'s list of skip reasons? — Picked: A — no. Alternatives: B — add `needs_input` to it through the twin-sync blocker. Why: the file has no twin, its list already omits `needs_input` and is a summary (the script decides), and B adds a protected-path diff for wording only (§5). Applied in: no code change. Status: pending review
- AD-5 [phase 1/1 — resume after twin sync, 2026-10-02] Commit the progress-log record of the twin sync to PR #6112's branch while its review run on ed72c11 is in flight? — Picked: A — no; carry it in this resume block and the issue progress comment, and the next stage that pushes commits it. Alternatives: B — push a log-only commit now. Why: a log-only push supersedes the reviewer panel running on ed72c11 and costs a round (#5660's AD-20). Applied in: no code change. Status: pending review
- AD-6 [phase 1/1 — review round 1, 2026-10-02] How to close the mixed-text bypass the reviewers found in `is_limit_wait` (a request riding on the limit error text or a wait prefix)? — Picked: A — a limit wait may hold only limit-wait words (`LIMIT_WAIT_VOCABULARY`: error wording, wait words, times, connectives; digits pass, symbols are not words); any other word makes it a request. Alternatives: B — add more action verbs to `HUMAN_REQUEST_PATTERN`; C — treat every non-empty `needs_action` as a request. Why: A fails closed on wording it does not know (§1); B fails open on any verb it does not list; C would stop limit-stopped sessions whose summary copies the error text into `needs_action` (the #5660 contract). Applied in: PR #6112 (review round 1). Status: pending review

## Lessons
- [source:intervention] A check that a text is "only X" must hold the whole text to an allow-list of X's words; a substring match plus a deny-list of request words fails open on any wording the list misses. (files: workflow-templates/.claude/scripts/usage_limit_resumes.py)

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-02)
- Phase 1 verification (2026-10-02): the new tests fail on the unfixed root copy (every new case) and pass on the twin; selector suite 159 passed, 1 failed (test_template_parity, until the twin sync); ruff clean. Real-data simulation on this account's live list_sessions page (100 sessions, limit text injected into every summary): the 16 sessions with a pending request are skipped (15 needs_input, 1 permission_prompt), the 84 without one are still selected.
- Permission mode: auto (session started by the Claude issue dispatcher, `dispatch shubhodeep1/coding-workflows#6102: start`).
- Issue base `claude/implement-plan-issue-5660-resume-usage-limit-stops` is the #5660 project branch; its final PR #5678 (draft, into `main`) is open, so the base has not merged.
- Sibling follow-up #6101 edits the same selector on the same base as its own issue-mode project.
- ai:claude-blocked removed from #6102 on 2026-10-02 17:10Z.
- Review round 1 (2026-10-02, ed72c11, ledger 39869c8b…): 6 consensus findings, all valid and fixed twin-first: `is_limit_wait` mixed-text bypass (twin and root), `too_old` before `needs_input` (twin and root), missing mixed-text tests, and the changelog's manual-resume pointer (also fixed in `docs/operations/master-session.md`). Every new case fails on the unfixed root copy; replay on this account's live list_sessions page: no real `needs_action` verdict changed, one old session with a pending request now reads `needs_input` instead of `too_old`.
