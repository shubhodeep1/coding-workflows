<!-- changelog: changed -->
- **Fewer idle Actions runs: the poller runs every 15 minutes, the closed-PR sweep hourly, and consumer check-failure triage ignores default-branch runs.** Together these remove several thousand runs a day that did no work but crowded a shared, jammed runner pool.

On 2026-11-14 an idle consumer such as `digital_pa` created 624 runs, and 583 of them did nothing. 312 were `AI Check Failure Triage` runs that skipped themselves, because `workflow_run: ["*"]` fires for every run in the repository. 271 were `*/5` poller and closed-PR sweep runs cancelled while they waited for a runner. `workflow-templates/ai-check-failure-triage.yml` now has `branches-ignore: [main, master]` on its `workflow_run` trigger. It only ever triaged `pull_request` runs, and those run on the PR head branch, so crons, comment wrappers and pushes no longer create a triage run at all. `ai-orchestrate-poll.yml` / `internal-orchestrate-poll.yml` move from `*/5` to `*/15`, and `ai-cancel-on-pr-close.yml` / `internal-cancel-on-pr-close.yml` from `*/5` to `23 * * * *`.

| The numbers that matter | Value |
| --- | --- |
| Poller cron | `*/5 * * * *` → `*/15 * * * *` |
| Closed-PR sweep cron | `*/5 * * * *` → `23 * * * *` |
| Triage `workflow_run` filter | `branches-ignore: [main, master]` |
| Idle consumer runs that did no work (`digital_pa`, 2026-11-14) | 583 of 624 |

What this means for operators and consumer repos: a project that makes progress still gets its next poll at once (the follow-up poll after progress is unchanged), and merge-train releases still run on every poll tick and every PR close. A closed PR with merge conflicts can keep orphaned runs for up to an hour instead of five minutes. The Claude pool-health Telegram note, which only a poll in the first minutes of the hour sends, now arrives in fewer hours. A PR whose head branch is itself named `main` or `master` is no longer triaged. Consumers receive all of this on their next `@stable` sync.

### For contributors

The plan is `docs/plans/reduce-actions-run-volume-plan.md`. The triage template stays byte-identical across consumers, so release-manifest attestation is unaffected. `tests/test_ci_failure_workflow_run_triggers.py` and `tests/test_review_merge_train.py` pin the new trigger and crons.
