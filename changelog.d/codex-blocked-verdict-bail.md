<!-- changelog: fixed -->
- **The implement retry loop now stops on a deliberate `BLOCKED: <reason>` verdict instead of retrying it five times under a "you must modify files" nudge.**

`prompts/mode-implement.txt` tells the implement model that `BLOCKED: <reason>` is a valid terminal deliverable, but `.github/workflows/implement.yml` treated that answer as an anonymous "returned output but produced no file changes" attempt. Every remaining attempt re-ran with the retry nudge, whose "the implementation plan requires repository modifications" wording contradicts issues that forbid file edits, and the issue received a generic "Codex implement failed after 5 attempts" diagnostics comment. The "Run Codex implementation" step now detects any final-output line starting with optional whitespace followed by `BLOCKED:` when the worktree delta is empty, even when summary text precedes it. It logs the model's reason, writes it to `${RUNTIME_DIR}/codex_blocked.flag`, emits the `Failed` substate, and breaks out of the loop on that attempt, the same shape as the existing `request_user_input` bail. The diagnostics comment on the source issue now opens with "Codex bailed on attempt N: deliberate BLOCKED verdict" so an operator can tell a model verdict from a stuck loop at a glance.

| The numbers that matter | Value |
| --- | --- |
| Motivating run | shubhodeep1/multi-user-ai-agent issue #246, run 33470149029 |
| Attempts spent on one deterministic verdict before this fix | 5 of 5 |
| Tokens spent on those attempts | ~201K |
| Attempts spent after this fix in the motivating case | 1 |

What this means for operators: an implement run that receives a `BLOCKED:` verdict stops on the first attempt that returns it. The issue comment identifies the deliberate verdict and points to the final assistant output captured in the workflow log; the included per-attempt tails contain stderr only. Downstream handling is unchanged: the step still exits 1 and the run takes the existing implementation-failed path, so orchestrator recovery behaves as before, just without four wasted attempts in the motivating cycle.

### For contributors

The check sits in the empty-delta branch ahead of the success-no-op regex (Guard 0, README 10f) and never fires when the attempt also produced real file changes. `tests/test_implement_post_codex_recovery.py::test_codex_blocked_verdict_bail_and_flag` pins the anchor regex, its position relative to the delta check and the success-no-op regex, the flag-then-break ordering, the stale-flag cleanup before the loop, and the `diag_reason` branch; `test_codex_blocked_verdict_regex_matches_line_start_only` exercises the live grep pattern against positive and negative fixtures.
