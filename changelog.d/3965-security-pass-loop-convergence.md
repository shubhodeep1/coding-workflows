<!-- changelog: fixed -->
- **The security-pass loop now converges on its own, and advisories parked in `ai:blocked` before a merge re-plan themselves.** The exhaustion judge may grant another fix cycle at most `MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS` times (default 2); later rounds turn `keep_fixing` into deferred advisories, and the final merge re-answers follow-ups the planner had parked.

Project #3965 finished its one-issue plan on 2026-09-03 and then spent sixteen days in the security pass: seven consolidated fix cycles across three resets, because the exhaustion judge chose `keep_fixing` in both of its rounds and `MAX_SECURITY_PASS_JUDGE_ROUNDS=0` left the sequence unbounded. Its two waived findings (#4090, #4091) were filed before the integration branch merged, the planner answered `BLOCKED: PR #3968 is still open`, and standalone stall recovery skips `ai:blocked` by design, so both waited for a human `/answer`. Two changes in `scripts/orchestrate_poll_process.sh` close both gaps. `security_pass_exhaustion_judge` now tells the judge when `keep_fixing_available` is `false` and, past the cap, rewrites any `keep_fixing` decision to `accept_with_followup` (`SECURITY_PASS_JUDGE_KEEP_FIXING_CAPPED`), so the remaining findings become deferred `ai:security` advisories and completion continues; `fail` verdicts are unchanged. `security_pass_unblock_filed_advisory_followups`, called from every site that records `final_merge_status = "merged"`, posts one `/answer [auto-answered-by-poller]` on each already-filed follow-up that is open and labelled `ai:blocked`, and records each issue in `security_pass_followups_merge_checked` so it is read at most once (`SECURITY_PASS_ADVISORY_FOLLOWUP_UNBLOCKED`).

| The numbers that matter | Value |
| --- | --- |
| New repo variable | `MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS` (default `2`; `0` restores the unbounded loop) |
| Judge rounds spent on #3965 before this shipped | 2, both `keep_fixing` (cycles 6 and 7 on a 5-cycle budget) |
| API cost of the re-plan step | one GET per follow-up over the project's lifetime, plus one POST per `ai:blocked` follow-up |
| Issues this reproduces | #4090, #4091 (`ai:blocked`), #4113 (cycle 7 of #3965) |

What this means for operators: a security-pass project that reaches the exhaustion judge a third time no longer gets a further fix cycle; its remaining findings become non-blocking advisories filed after the merge, and the project completes. Follow-ups that were already parked in `ai:blocked` re-enter planning when the integration branch lands on the default branch, with a `🔓 Security-pass advisory follow-ups re-planned` comment on the tracking issue. A human is still needed only for a project-wide `fail` verdict (`ai:security-pass-failed`), which the judge reserves for findings the automated pipeline cannot land.

### For contributors

State gains `security_pass_followups_merge_checked` (issue numbers, deduped, last 100; follow-ups the filer creates after the merge are added at creation); the judge diagnostics JSON gains `max_keep_fixing_rounds` and `keep_fixing_available`, and `prompts/mode-judge-security-pass-exhaustion.txt` carries the matching rule. Converted decisions keep the judge's justification behind the prefix `[keep_fixing capped after <c> judge round(s); converted to advisory follow-up]`, and the accept-all judge comment says how many were converted.
