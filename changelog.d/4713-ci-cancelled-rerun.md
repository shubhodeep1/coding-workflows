<!-- changelog: fixed -->
- **Cancelled CI no longer leaves a PR stuck indefinitely.** The 30-minute review sweep can re-run failed jobs for eligible cancelled or startup-failed `ci.yml` runs on the current PR head.

Open, non-draft PRs in coding-workflows are checked during the existing review sweep. The sweep refreshes PR heads before requesting a rerun so a newer push supersedes the cancelled run. Real test failures do not trigger this recovery, and a failed-jobs rerun is not replaced by a full workflow rerun. Set `CI_CANCELLED_AUTO_RERUN_ENABLED=false` to disable the recovery path without stopping review dispatch.

| Recovery limit | Value |
| --- | --- |
| Sweep cadence | 30 minutes |
| Eligible attempts | First attempt only |
| CI lookups per tick | One bounded completed listing, three active-status listings |
| PR freshness check | One paginated open-PR listing |

What this means for maintainers: cancelled CI gets one automated failed-jobs retry when its PR head is still current; otherwise the sweep logs why it skipped the run.
