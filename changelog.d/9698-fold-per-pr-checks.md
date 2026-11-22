<!-- changelog: changed -->
- **The integration-PR readiness check and the PR-body auto-close lint now share one runner job on every push and body edit.** A new workflow `PR checks` (`.github/workflows/pr-checks.yml`) replaces `lint-pr-body-auto-close.yml`, and `integration-pr-readiness.yml` now runs on label events only.

A pull request into `main` or `stable` used to start two jobs on every push, each holding a runner slot for under a minute of work after 20 to 70 minutes in the queue. Both checks are now step groups of the single job `pr-body-lint`. The check-run name `pr-body-lint` and the commit status `orchestrator/integration-pr-not-ready` report as before, so the pipeline's merge gate keeps working unchanged. Only the lint can fail the job: a readiness step that cannot read the tracking issue or post its status shows a warning and leaves the status to speak for itself. Each check runs on the same events as before.

| The numbers that matter | Value |
| --- | --- |
| Runs in 2026-11-10 to 2026-11-17, readiness check | 1,192 |
| Runs in 2026-11-10 to 2026-11-17, PR-body lint | 1,015 |
| Jobs per `opened` / `synchronize` / `reopened` / `ready_for_review` event | 2 before, 1 after |
| Jobs per `edited` event | 1 before, 1 after |
| Jobs per `ai:override-incomplete-merge` label event | 1 before, 1 after |
| Jobs for any other label event | 0 before, 0 after (one skipped `readiness` run) |
| Job timeout | 10 minutes (was 5 + 5) |
| `ORCH_FINAL_MERGE_REQUIRED_CHECKS` default | unchanged |

What this means for operators: nothing to change by hand. An open pull request switches to `PR checks` at its next push or body edit, and until then keeps the check-run and status already on its head. The workflow name `Lint PR body for auto-close keywords against orchestrator-tracking issues` no longer appears in the Actions list; look for `PR checks`. `Integration PR readiness check` still appears, for label changes only. Consumer repos never received these workflows; they get only the updated file name in CLAUDE.md §19.

### For contributors

Label events stay out of `pr-checks.yml` on purpose. A job skipped by its `if:` reports a `skipped` check-run, and GitHub and `_pr_ruleset_required_check_blockers` read the newest check-run of a name, so a label change after a failed lint would have hidden the failure behind a newer `skipped` `pr-body-lint`. The job therefore has no job-level `if:`. The readiness steps skip `edited`, as the readiness check always did, so the fold adds no API call. The job has no concurrency group; the comment above its steps gives the reasons. `lint-plan-archival.yml` is not folded yet and still has a job named `lint`. The three workflow-name entries in `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` stay; they match no check-run, and the README row now says so. `tests/test_pr_checks_workflow.py` pins the shape of the fold.
