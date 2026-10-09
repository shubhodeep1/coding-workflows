<!-- changelog: fixed -->
- **A stable release now requires a passing CI run on the exact commit it releases, and publishing stays off until an operator turns it on.** Both release paths, `mark-stable.yml` and `test-and-mark-stable.yml`, check the CI `lint` check-run of the tested commit before they push anything. A new repository variable, `STABLE_RELEASE_ENABLED`, defaults to off.

Before this change the release job checked only the commit's combined status. GitHub Actions jobs report as check-runs, not statuses, so that check could never see `lint`, the aggregate job of `ci.yml`. A commit whose CI had failed could still be tagged as stable. The new step `Require successful CI lint check-run` runs right after the release job records the tested commit. It reads that commit's `lint` check-runs, counts only those created by the `github-actions` app, and lets the release continue only when every one finished with `success`. If CI is still running, it re-checks every 30 seconds for up to `STABLE_RELEASE_CI_WAIT_SECS` (600 by default). A missing, failed, cancelled, still-running or unreadable result fails the release, and the log line `STABLE_RELEASE_CI_GATE outcome=fail reason=<reason>` names the cause.

| The numbers that matter | Value |
| --- | --- |
| Release paths gated | 2 |
| Default CI wait | 600 seconds, checked every 30 seconds |
| `STABLE_RELEASE_ENABLED` default | off |

What this means for operators: until `STABLE_RELEASE_ENABLED` is set to `true`, every gate run still validates and runs the CI check but publishes nothing. It pushes no changelog, creates no tags or GitHub Release, sends no consumer dispatch and does not sync to `main`. It logs `STABLE_RELEASE_DISABLED` instead. The release job now also asks for the `checks: read` permission. While publishing is off, set `AUTO_RELEASE_STABLE_ENABLED=false` as well. Otherwise `auto-release-stable.yml` re-runs the full gate on the same tip every 6 hours.

### For contributors

`tests/test_mark_stable_release_tag_refspec_contract.py` runs the CI check against mocked check-run responses in both workflows, covering every failure reason and the bounded wait. It also checks that the switch counts only `true` as on, that every publishing step and `sync-to-main` are skipped while it is off, and that the `lint` job in `ci.yml` keeps its check-run name.
