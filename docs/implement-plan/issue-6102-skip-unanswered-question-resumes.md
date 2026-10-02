# Implement-Plan Log — Usage-limit resumes: skip sessions waiting on an unanswered question

- Plan: docs/plans/issue-6102-skip-unanswered-question-resumes-plan.md
- Source issue: shubhodeep1/coding-workflows#6102 (progress comment 5956408433)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-5660-resume-usage-limit-stops
- Project branch: claude/implement-plan-issue-6102-skip-unanswered-question-resumes   Final PR: #6105 draft
- Status: BLOCKED
- Stage: phase 1/1 — review round 2 (twin sync)
- Activation: not started
- Waiting on: PR #6112: twin sync (review round 2 fixes)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01HKnvTyY9wdNoZoKqLisFzv (idle, reused on resume)   safety net none   hand-back none
- Last updated: 2026-10-02
- Last note: review round 2 on 68d802e fixed twin-first (a retry, resend, or resume must be deferred to the reset; "now" is not a limit-wait word; plan-doc task gaps); selector suite 196 passed, only test_template_parity red until the twin sync; held for the third twin sync

## Phases
1. [ ] Phase 1 — skip unanswered requests on both signals (selector twin, tests, README, agents.md, changelog fragment)   — protected paths: .claude/scripts/usage_limit_resumes.py (via workflow-templates/.claude/scripts/usage_limit_resumes.py)   — PR #6112 open; twin sync ed72c11 ([claude-twin-sync] issue-6102 phase 1, by the operator's driver session_01Db6EvqsPiaDHDTp8fUMeV8, answer Q1: A in issue comment of 2026-10-02 17:05Z) copied the twin to .claude/scripts/usage_limit_resumes.py, sha256 f83055de…7758778d verified, root and twin byte-equal, 230 passed across the selector, stale-routine, and changelog suites; review round 1 (ed72c11) fixed twin-first; second twin sync 68d802e ([claude-twin-sync] issue-6102 review round 1, by the operator's driver session_01Db6EvqsPiaDHDTp8fUMeV8, answer Q2: A relayed 2026-10-02 19:07Z) copied the twin to .claude/scripts/usage_limit_resumes.py, sha256 2dfbbd65…fe0940d0 verified, root and twin byte-equal, 231 passed across the selector and stale-routine suites on 68d802e (parity included); review round 2 (68d802e) fixed twin-first, held for the third twin sync; review rounds: 2; interventions: 0

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
- AD-7 [phase 1/1 — review round 2, 2026-10-02] How to close the vocabulary-only bypass the reviewers found in `is_limit_wait` ("Usage limit reached. Please retry the request now." and "Wait for the limit. Please retry now." are made only of limit-wait words)? — Picked: A — drop "now" from `LIMIT_WAIT_VOCABULARY`, and count a retry, resend, resume, or try only when the text defers it to the reset (a wait word, until, till, later, resets, or resetting, not negated by a preceding "not": `LIMIT_RETRY_WORD_PATTERN`, `LIMIT_DEFERRAL_PATTERN`). Alternatives: B — accept only text that matches a fixed list of whole limit-wait sentences; C — reject the finding (a retry is what the resume does). Why: A fails closed on an immediate or undeferred retry while keeping every observed error text and deferred-wait wording a limit wait (no verdict changed on the live 100-session page); B would stop limit-stopped sessions whose wording varies, and C leaves an instruction the summary still asks the human to act on. Applied in: PR #6112 (review round 2). Status: pending review

## Lessons
- [source:intervention] A check that a text is "only X" must hold the whole text to an allow-list of X's words; a substring match plus a deny-list of request words fails open on any wording the list misses. (files: workflow-templates/.claude/scripts/usage_limit_resumes.py)
- [source:intervention] An allow-list of words is not enough when the allowed words can form an instruction: action verbs in it (retry, resend, resume) must also be tied to the condition they wait on, and words of immediacy (now) left out. (files: workflow-templates/.claude/scripts/usage_limit_resumes.py)

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-02)
- Phase 1 verification (2026-10-02): the new tests fail on the unfixed root copy (every new case) and pass on the twin; selector suite 159 passed, 1 failed (test_template_parity, until the twin sync); ruff clean. Real-data simulation on this account's live list_sessions page (100 sessions, limit text injected into every summary): the 16 sessions with a pending request are skipped (15 needs_input, 1 permission_prompt), the 84 without one are still selected.
- Permission mode: auto (session started by the Claude issue dispatcher, `dispatch shubhodeep1/coding-workflows#6102: start`).
- Issue base `claude/implement-plan-issue-5660-resume-usage-limit-stops` is the #5660 project branch; its final PR #5678 (draft, into `main`) is open, so the base has not merged.
- Sibling follow-up #6101 edits the same selector on the same base as its own issue-mode project.
- ai:claude-blocked removed from #6102 on 2026-10-02 17:10Z.
- Review round 1 (2026-10-02, ed72c11, ledger 39869c8b…): 6 consensus findings, all valid and fixed twin-first: `is_limit_wait` mixed-text bypass (twin and root), `too_old` before `needs_input` (twin and root), missing mixed-text tests, and the changelog's manual-resume pointer (also fixed in `docs/operations/master-session.md`). Every new case fails on the unfixed root copy; replay on this account's live list_sessions page: no real `needs_action` verdict changed, one old session with a pending request now reads `needs_input` instead of `too_old`.
- ai:claude-blocked removed from #6102 on 2026-10-02 19:08Z (after the second twin sync); the log record of 68d802e was carried in the resume block under AD-5 and is committed here.
- Review round 2 (2026-10-02, 68d802e, ledger e876cf1e…; the workflow's hand-off numbers it round 1): consensus findings judged — the vocabulary-only bypass (twin and root, both branches of `is_limit_wait`) and its missing tests are valid and fixed twin-first (AD-7); the three plan-doc task gaps (Approach step 2, the §6 identifier list, the §1 and Risks manual-fallback wording) are valid and fixed; the `status_category` permission-prompt finding is rejected (the check is unchanged #5660 code, a pending permission prompt leaves the session `REQUIRES_ACTION`, which is skipped as `not_idle` first, and "approve" in `needs_action` is a request marker); the hardening note to inline `needs_input` is rejected (no defect; `too_old` needs it). Every new fail-closed case fails on the unfixed root copy; replay on this account's live list_sessions page: no `needs_action` verdict changed (28 texts, 14 sessions with an unanswered request before and after).
