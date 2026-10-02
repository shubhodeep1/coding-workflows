# Usage-limit resumes: skip sessions waiting on an unanswered question

Source issue: shubhodeep1/coding-workflows#6102 (https://github.com/shubhodeep1/coding-workflows/issues/6102)
Base branch: claude/implement-plan-issue-5660-resume-usage-limit-stops
Security pass: skip (ai:security: automation-produced issue)

## Summary

The usage-limit selector (`.claude/scripts/usage_limit_resumes.py`, issue #5660) skips a session that waits on a human answer only on the `rate_limit_info` signal. On the `text` signal, an idle session with an unanswered Q/A request and the usage-limit text in its summary gets a resume prompt telling it to continue, which can carry it past an ask-first decision. This plan makes both signals skip a session whose summary still records an unanswered request, and makes every resume prompt say it is not an answer to one.

## Automation (CLAUDE.md §18.E)

- **Scripts:** no new script. One existing helper changes: `.claude/scripts/usage_limit_resumes.py` (edited through its `workflow-templates/.claude/` twin).
- **Scheduler entry point:** unchanged. The Claude issue pickup's existing step 1a runs the selector on every `start`, `— wake.`, and `— wake. — catch-up` wake (`.claude/commands/claude-issue-pickup.md`).
- **Supervisor:** none new. The pickup supervisor is unchanged.
- **DB work:** none.
- **Future-removal registry:** no new entry (no new script).

## Context

- Security audit run 37027496021 (`security-audit.yml`, `ref` = the #5660 project branch) filed this finding (tracker #3576): `STRIDE: Elevation of Privilege`, medium, confidence 8/10, `.claude/scripts/usage_limit_resumes.py:410`.
- The code: `_skip_reason` returns `needs_input` only when `signal == "rate_limit_info"` and `status_category == "need_input"`. The `text` signal (`post_turn_summary.status_detail` carries the usage-limit error) never checks for a pending question. `test_text_signal_still_resumes_a_need_input_summary` pins that on purpose: a turn that failed on the limit can still show `need_input`, and the error text decides.
- What the summaries hold (live `list_sessions` page, 2026-10-02 16:10Z, 100 sessions): every one of the 16 `need_input` sessions carries a non-empty `needs_action` naming what the human must do (`Reply: \`Q1: A\` …`, `decide: reissue #4374 or drop it?`, `Approve or deny Bash`). The other 84 have an empty or missing `needs_action`. `needs_action` sits in both `post_turn_summary` and `external_metadata.post_turn_summary`, and the top-level copy can omit the key.
- No session on the page stopped on the limit, so what a limit-stopped summary puts in `needs_action` is not observed. The existing fixtures model it as empty.
- The resume prompts (`CHECKER_PROMPT`, `OTHER_PROMPT`) say "The limit has reset" and "Continue from your latest instructions" or "Repeat the steps …". Neither says the message is not an answer to a pending question.
- Sibling finding #6101 (`usage_limit_resumes.py:443`, sessions from another repository or origin) is a separate issue-mode project on the same base branch. It is out of scope here.

## Goals

- On both signals, a candidate whose `needs_action` (either summary copy) is non-empty and is not only a wait for the usage limit is skipped with reason `needs_input`.
- On the `rate_limit_info` signal, `status_category == "need_input"` still skips, as today.
- On the `text` signal, a `need_input` summary with an empty `needs_action`, or with a `needs_action` that only says to wait for the limit or carries the limit error, is still resumed.
- A `needs_action` that names a Q-ID, ends in a question mark, or asks to reply, answer, decide, confirm, choose, or approve is never treated as a limit wait.
- Every resume prompt says the message is not an answer to any question or approval request of the session's, and that an unanswered one means waiting, not acting.
- `tests/test_usage_limit_resumes.py` pins each rule above.

## Non-goals

- #6101 (repository or origin filtering).
- Changing `.claude/commands/claude-issue-pickup.md` (a protected path with no twin). Its list of skip reasons is a summary, and the script decides.
- Verifying that a human answered (the selector reads snapshots and makes no API calls).
- New skip-reason names or output fields.

## Constraints

- §1: security first. Unknown `needs_action` wording fails closed (skipped, listed under `skipped`, where the handbook's manual fallback covers it).
- §5: the smallest change that closes the finding. One helper and its tests, plus the docs that describe the skip reasons.
- §6: no identifier is renamed or removed. The reason stays `needs_input`, and the JSON output shape is unchanged. New module-level names (`LIMIT_WORD_PATTERN`, `LIMIT_WAIT_WORD_PATTERN`, `LIMIT_WAIT_START_PATTERN`, `HUMAN_REQUEST_PATTERN`, `_needs_action_texts`, `is_limit_wait`, `has_unanswered_request`, `NOT_AN_ANSWER_TEXT`) are checked against the module first.
- §9: tabs in Python.
- §20: a `changelog.d/` fragment (security fix).
- §28.C and the interim twin-first default: `.claude/**` is edited only through `workflow-templates/.claude/scripts/usage_limit_resumes.py`. The root copy is synced by the operator from the twin-sync blocker.

## Approach

1. Read `needs_action` from both summary copies (`post_turn_summary` and `external_metadata.post_turn_summary`), stripped.
2. A `needs_action` is a **limit wait** when it carries no human-request marker (`Q<n>`, `?`, or the words reply, answer, decide, confirm, choose, approve) and either matches the usage-limit error text (`has_limit_text`) or starts with the wait or the limit and names a limit (`limit`) together with a wait word (reset, wait, retry, resume, resend, try again), so `merge PR #N once the limit resets` stays a request.
3. A session **has an unanswered request** when any copy is non-empty and is not a limit wait.
4. `_skip_reason` returns `needs_input` (after the existing `permission_prompt` check) when the session has an unanswered request, on either signal, or when the signal is `rate_limit_info` and `status_category` is `need_input`.
5. Both prompts gain one fixed sentence: this message is not an answer to any question or approval request of yours; if one is still unanswered, keep waiting for the human's answer and end the turn without acting on it.

Alternatives considered (see Auto-decisions): skipping every `need_input` category on both signals would likely stop every limit-stopped session, because the existing contract says a failed turn can still show `need_input`. A pattern list of question phrasings alone would fail open on wording it does not know.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue fixes the scope, and the selector change, its tests, and its docs cannot ship apart without leaving the docs wrong.

1. **Phase 1 — skip unanswered requests on both signals.**
   - Files: `workflow-templates/.claude/scripts/usage_limit_resumes.py`, `tests/test_usage_limit_resumes.py`, `README.md`, `agents.md`, `changelog.d/6102-skip-unanswered-question-resumes.md`.
   - Protected paths: `.claude/scripts/usage_limit_resumes.py` (via its twin).
   - Done: the selector suite passes against the twin with the new tests, `ruff check` is clean, and the root `.claude/` copy differs from the twin only until the twin sync.
   - Rollback: revert the phase PR; the selector returns to the #5660 behaviour.

## Implementation Steps

Phase 1:
1. `workflow-templates/.claude/scripts/usage_limit_resumes.py`: add the four patterns, `_needs_action_texts`, `is_limit_wait`, and `has_unanswered_request`; change the `needs_input` rule in `_skip_reason`; add `NOT_AN_ANSWER_TEXT` to both prompts; update the module docstring (signals and skip reasons).
2. `tests/test_usage_limit_resumes.py`: add tests for each goal; keep `test_text_signal_still_resumes_a_need_input_summary` and `test_rejected_snapshot_waiting_on_a_human_is_skipped`.
3. `README.md` (pickup step 1a summary) and `agents.md` (selector skip reasons): describe the `needs_input` rule on both signals.
4. `changelog.d/6102-skip-unanswered-question-resumes.md` (`security`).

## Files & Modules

- `workflow-templates/.claude/scripts/usage_limit_resumes.py`
- `.claude/scripts/usage_limit_resumes.py` (through the twin sync, not edited by the session)
- `tests/test_usage_limit_resumes.py`
- `README.md`
- `agents.md`
- `changelog.d/6102-skip-unanswered-question-resumes.md` [new]

## Tests

- Unit (`tests/test_usage_limit_resumes.py`, runs in its own `ci.yml` step):
  - text signal with a Q/A `needs_action` → `needs_input`;
  - the same request only in `external_metadata.post_turn_summary` → `needs_input`;
  - a `need_input` summary whose `needs_action` waits for the limit to reset, or carries the error text → resumed;
  - a limit-worded `needs_action` that also names a Q-ID or asks to decide → `needs_input`;
  - a checker on the `rate_limit_info` signal with a `review_ready` category and a pending request → `needs_input`;
  - whitespace-only `needs_action` → resumed;
  - both prompts carry the not-an-answer sentence;
  - `is_limit_wait` / `has_unanswered_request` unit cases.
- The template-parity test stays red until the twin sync copies the twin to `.claude/`.

## Risks & Mitigations

- A limit-stopped session whose `needs_action` uses wording the limit-wait rule does not know is skipped and stays stopped. ACCEPTED — security first (§1); it is listed under `skipped` as `needs_input`, and the handbook's manual fallback covers it.
- A `need_input` summary with an empty `needs_action` from a turn that had asked a question is still resumed. Mitigation: the prompt's not-an-answer sentence keeps the session waiting.
- #6101 edits the same file on the same base branch. Mitigation: whichever lands second merges the base and resolves the conflict in its own project.

## Rollout

Ships with the #5660 project (final PR #5678 into `main`) after the twin sync. No flag, no migration. Rollback is a revert of the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-10-02] How does the selector recognise an unanswered request on the `text` signal? — Picked: A — a non-empty `needs_action` in either summary copy that is not only a limit wait (no Q-ID, `?`, or reply/answer/decide/confirm/choose/approve; the limit error text, or text that starts with the wait or the limit and names a limit plus a wait word), on both signals; a `need_input` summary with an empty `needs_action` still resumes. Alternatives: B — skip every `need_input` category on both signals; C — skip only a `needs_action` matching a list of question phrasings. Why: B would likely stop every limit-stopped session (the #5660 contract says a failed turn can still show `need_input`), and C fails open on wording it does not know. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-02] Should the resume prompts also say they are not an answer? — Picked: A — yes, one fixed sentence in both prompts. Alternatives: B — selector change only. Why: it covers the case the snapshot cannot see (an empty `needs_action` after a question) at the cost of one sentence. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-02] Which skip reason does the new rule report? — Picked: A — the existing `needs_input`. Alternatives: B — a new `unanswered_request` reason. Why: same meaning, no new identifier (§6), and the docs already name it. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-02] Update `.claude/commands/claude-issue-pickup.md`'s list of skip reasons? — Picked: A — no. Alternatives: B — add `needs_input` to it through the twin-sync blocker. Why: the file has no twin, its list already omits `needs_input` and is a summary (the script decides), and B adds a protected-path diff for wording only (§5). Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py --issue 6102` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #6102; tracker #3576; audit run https://github.com/shubhodeep1/coding-workflows/actions/runs/37027496021
- Parent project #5660 (plan `docs/plans/issue-5660-resume-usage-limit-stops-plan.md`, final PR #5678); sibling finding #6101
