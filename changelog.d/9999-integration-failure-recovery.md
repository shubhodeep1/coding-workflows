<!-- changelog: fixed -->
- **Orchestrator projects no longer fail for good after many resolved integration conflicts, and a project ended by integration self-healing recovers once its final PR is mergeable.**

`INTEGRATION_CONFLICT_LIFETIME_MAX` (default 10) used to count every resolver and judge dispatch over a project's whole life, so a long project on a busy `main` reached it even when each conflict was resolved. On 2026-10-11 project #6664 failed this way after ten separate conflicts. Now each dispatch records the `main` commit it was asked to merge in, and once the integration branch contains it the count starts again (`INTEGRATION_CONFLICT_EPISODE_RESOLVED`). A conflict the resolver never lands still ends the project at the cap. A project that the cap or a failed judge escalation ended is also no longer stuck: when its final PR has been merged, or is open, mergeable on GitHub and merges cleanly locally, the poller restores the status it failed from and continues (`INTEGRATION_FAILED_RECOVERY`). That brings back #6664, and closes out #6902, whose final PR #6905 was merged while the poller skipped the project.

| The numbers that matter | Value |
| --- | --- |
| Lifetime cap | 10 dispatches per unresolved conflict run, unchanged default |
| Statuses a recovery restores | `in_progress`, `validating`, `validation-fixing`, `security-pass`, `security-pass-fixing` |
| Extra GitHub API calls | 1 REST read of the final PR per tick, only for a project in that failed state |

What this means for operators: an `❌ Integration self-healing capped` or `exhausted` alert no longer needs a hand edit of the tracking issue's state once the final PR is mergeable; watch for the `🔁 Integration recovered` comment instead.
