<!-- changelog: fixed -->
- **A red final CI now goes to a repair, not to a judge `merge` the check then refuses.** The review-blocked judge also sees which jobs, steps and tests failed.

PR #9679 sat for days. Its final CI failed after a base update, the judge answered `merge` twice without seeing why CI failed, the red check refused the merge each time, and nothing else could act because the autofix rounds and judge fixes were used up. There were three causes:

- The final-CI repair gate only read `ai:final-ci-failed:v1`. A base-update head posts `ai:freshness-ci-failed:v1`, so its repair never ran. The gate now accepts both.
- The judge prompt had no CI data. It now has a `CI FAILURES ON THE JUDGED HEAD` section with each failed job's name, its failed steps, its failing test ids and its last error lines. The section is built from the check-run snapshot the editor already gets, is bounded and sanitised, and is marked as untrusted data.
- A judge `merge` on a red head ended as `checks_failed`, which only paged. Now the judge posts `ai:final-ci-repair-request:v1` and dispatches a review run, and the gate then runs the final-CI repair for that head even when every autofix and judge budget is used up. Once the repair's caps are used up (one attempt per head, two repaired heads per PR), the judge posts one `ai:final-ci-handover:v1` comment that names the failures, adds `ai:needs-human` and sends one ERROR alert. Later judge runs on that head skip without a comment or an alert.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls, judged head green | 0 |
| Extra API calls, judged head red | at most 3 `GET actions/runs/<id>/jobs`, plus 1 comment and 1 dispatch (or 1 label edit per linked issue) |
| API calls saved | 1 `GET commits/<head>/statuses` when the gate repairs on the judge's request |
| New repo variable | `RB_JUDGE_RED_CHECK_REPAIR_ENABLED` (default `true`) |

What this means for operators: a PR whose final CI fails is repaired or handed to you with the failing tests named. Set `RB_JUDGE_RED_CHECK_REPAIR_ENABLED=false` to restore the old `checks_failed` page.
