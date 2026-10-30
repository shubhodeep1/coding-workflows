<!-- changelog: fixed -->
- **A PR-close cleanup run can no longer be dropped because another PR closed a minute later.** Each `pull_request.closed` run of `AI Cancel Runs on PR Close` now waits in its own concurrency group instead of one group shared by the whole repository.

GitHub keeps only one waiting run per concurrency group and cancels the older one when a newer run joins. With one shared group, closing two PRs close together cancelled the first PR's cleanup run before it started. Its branch runs were left to the next 5-minute scheduled sweep, which waited behind the same queue. Release gate run 40794181365 failed this way: the smoke PR's close run 40814653574 was cancelled after 51 seconds when the close run for `ai/issue-8306` joined, and Phase 7 ran out of its 10-minute budget. Scheduled sweeps still share the original group `cancel-on-pr-close-<repo>`, so they keep running one at a time.

| The numbers that matter | Value |
| --- | --- |
| PR-close run group | `cancel-on-pr-close-<repo>-pr-<N>` |
| Scheduled sweep group | `cancel-on-pr-close-<repo>` (unchanged) |
| Workflow | `.github/workflows/cancel_on_pr_close.yml`, job `cancel-active-runs` |

What this means for consumer repos: on the next `@stable` sync, a closed PR's queued and running pipeline runs are cancelled by that PR's own close run, even when several PRs close at once. Nothing needs to change in the wrapper `ai-cancel-on-pr-close.yml`.

### For contributors

Close runs for different PRs and a scheduled sweep can now overlap. That is safe: a cancel request for a run that has already finished or been cancelled fails softly and is logged. `scripts/review_merge_train.sh release` claims each queued PR by deleting its label, so only one caller can release it. The orchestrate poller already ran `release` outside this group. `tests/test_review_merge_train.py::test_cancel_on_close_event_runs_get_their_own_concurrency_group` pins the group expression.
