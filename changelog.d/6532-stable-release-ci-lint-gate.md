<!-- changelog: fixed -->
- **Stable releases check CI on the tested commit in their first step, and a new switch can hold them.** Both release paths, `mark-stable.yml` and `test-and-mark-stable.yml`, check the CI `lint` check-run of the tested commit before they push anything. A new repository variable, `STABLE_RELEASE_ENABLED`, publishes when unset, empty or `true`; `false` holds the release.

Both release paths already ran `scripts/release_ci_gate.sh` (issue #6797), which fails the release unless the `lint` check-run of `ci.yml` on the tested commit succeeded. The new step `Require successful CI lint check-run` makes the same check first, right after the release job records the tested commit, whether or not publishing is on. It reads that commit's `lint` check-runs and counts only those the `github-actions` app created for a `ci.yml` run of that commit (other workflows also have `lint` jobs). Like the script, it lets the newest of them decide, so a re-run that passed outweighs an older failed attempt, and the release continues only when that run finished with `success`. If CI has not created the check-run yet or is still running, it re-checks every 30 seconds for up to `STABLE_RELEASE_CI_WAIT_SECS` (600 by default, at most 2400). A failed, cancelled or unreadable result, or one still missing or running when the wait runs out, fails the release, and the log line `STABLE_RELEASE_CI_GATE outcome=fail reason=<reason>` names the cause. Because this step runs first, its budget is the longest a release waits for CI: raising `RELEASE_CI_GATE_WAIT_SECS` alone does not extend it.

| The numbers that matter | Value |
| --- | --- |
| Release paths gated | 2 |
| Default CI wait | 600 seconds (at most 2400), checked every 30 seconds |
| `STABLE_RELEASE_ENABLED` default | on (unset publishes) |

What this means for operators: while `STABLE_RELEASE_ENABLED` is `false` (or any value other than unset, empty or `true`, which also logs a warning), every gate run still validates and runs the CI check but publishes nothing. It pushes no changelog, creates no tags or GitHub Release, sends no consumer dispatch and does not sync to `main`. It logs `STABLE_RELEASE_DISABLED` instead. The new step reads check-runs with the release job's existing `checks: read` permission. While publishing is off, set `AUTO_RELEASE_STABLE_ENABLED=false` as well. Otherwise `auto-release-stable.yml` re-runs the full gate on the same tip every 6 hours.

### For contributors

`tests/test_mark_stable_release_tag_refspec_contract.py` runs the CI check against mocked check-run responses in both workflows, covering every failure reason, the bounded wait for missing and running results, the 2400-second cap, re-runs where the newest check-run decides, and `lint` check-runs from other workflows. It also checks that the switch publishes when unset, empty or `true`, holds on `false` and warns on any other value, that every publishing step and `sync-to-main` are skipped while it is off, and that the `lint` job in `ci.yml` keeps its check-run name.
