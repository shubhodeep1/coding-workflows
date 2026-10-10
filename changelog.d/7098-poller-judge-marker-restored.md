<!-- changelog: fixed -->
- **Poller judges run again.** WAVE, STALL, INTEGRATION and SECURITY judges stopped deferring as `sandbox_helper_outdated`.

From 2026-10-10 13:19 UTC every non-review poller judge on this repository deferred instead of running. #7024 had reworded a comment in `scripts/review_untrusted_sandbox.sh` that `scripts/orchestrate_poll_process.sh` checks for, word for word, before it runs a judge in the isolated sandbox. Project #6664's wave judge deferred on three ticks and escalated to `ai:needs-human`. The comment line is restored verbatim, and a new test checks every line the poller looks for against the real helper, not a fake.

| The numbers that matter | Value |
| --- | --- |
| Judges affected | `WAVE_JUDGE`, `STALL_JUDGE`, `INTEGRATION_JUDGE`, `SECURITY_JUDGE` |
| Deferrals before escalation | 3 (`JUDGE_ISOLATION ... count=3 max=3`) |
| Broken by | #7024, merged 2026-10-10 13:19 UTC |

What this means for operators: projects whose judges escalated with "Judge isolation unavailable ... sandbox_helper_outdated" retry once `ai:needs-human` is removed from the tracking issue.
