<!-- changelog: security -->
- **Usage-limit resumes no longer wake a session that is waiting on a human answer.** The Claude issue pickup's selector now skips such a session on both of its signals, and every resume prompt says it is not an answer.

Before this fix, `.claude/scripts/usage_limit_resumes.py` checked for a pending question only on its `rate_limit_info` signal. An idle session whose summary carried the usage-limit error text was sent a prompt to continue even while its own summary still asked a human to reply, decide, or confirm, so it could act past an ask-first decision. The selector now reads `needs_action` from both summary copies. It skips the session as `needs_input` when that text asks for anything other than waiting for the limit to reset: a limit wait may hold only the error wording, wait words, times, and connectives, so any other word, such as merge or push, makes it a request, and a retry, resend, or resume counts only when the text defers it to the reset (`Usage limit reached. Please retry the request now.` is a request). A Q-ID such as `Q1`, a question mark, or the words reply, answer, decide, confirm, choose, or approve always count as a request. A session older than 3 days in this state is listed as `needs_input`, not `too_old`. Both resume prompts now carry one more fixed sentence: the message is not an answer to any question or approval request, so an unanswered one means waiting.

| The numbers that matter | Value |
| --- | --- |
| Signals the `needs_input` skip covers | 2 of 2 (was 1: `rate_limit_info` only) |
| Live sessions with a pending request (2026-10-02 sample of 100) that a simulated limit stop would now skip | 16 of 16 (15 `needs_input`, 1 `permission_prompt`) |
| Live sessions without one that still resume | 84 of 84 |

What this means for operators: after a usage-limit stop, a session that asked you a question stays stopped until you answer it, and it is listed under `skipped` with reason `needs_input` in the pickup's selector output. A `need_input` summary whose `needs_action` is empty or only says to wait for the limit is still resumed. Wording the selector does not recognise counts as a request, so such a session stays stopped too. Answer the request in that session, which resumes it; do not resume a `needs_input` session by hand (`docs/operations/master-session.md`, "Usage limit stops").

### For contributors

The rule lives in `is_limit_wait` (with `LIMIT_WAIT_VOCABULARY`) and `has_unanswered_request`. The reason name `needs_input` and the JSON output shape are unchanged. `tests/test_usage_limit_resumes.py` pins the rule. The root `.claude/scripts/usage_limit_resumes.py` follows its `workflow-templates/.claude/` twin through the twin sync (security follow-up #6102 of #5660).
