<!-- changelog: fixed -->
- **Planning no longer loops on a CI-triage issue whose pull request was closed without merging.** Plan now hands such an issue to the unblock judge (`ai:blocked`) instead of leaving it at `ai:planning`.

A check-failure triage issue names a pull request. When that pull request is closed or merged and the superseded check in `.github/workflows/plan.yml` cannot settle the issue, plan skips the model run, which is correct, but it left the `ai:planning` label in place. Stall recovery then re-posted `/answer` about once an hour and every planning run skipped again, each time sending a Telegram WARNING. Issue #7995 (PR #7988, closed by the E2E smoke cleanup) went round this loop for about 40 hours. Plan now moves the issue from `ai:planning` to `ai:blocked` and posts one comment with an `ai:plan-pr-not-open:v1` marker, which is what implement already does for the same case (#7540).

| The numbers that matter | Value |
| --- | --- |
| Stall-recovery `/answer` re-posts on #7995 (2026-10-23 18:12 to 2026-10-25 09:40 UTC) | 33 |
| Hand-off comments per issue | 1, posted only after the label change lands |
| Telegram level after a successful hand-off | `DEBUG` (`WARNING` only if the label change fails) |

What this means for operators: a triage issue for an abandoned pull request now stops at `ai:blocked` after one planning run, and the unblock judge closes or re-issues it. No more hourly "Planning skipped" warnings for the same issue.
