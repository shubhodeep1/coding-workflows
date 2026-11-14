<!-- changelog: fixed -->
- **A failed `pull_request` review run is now reported to the workflow failure heal with its own head.** Before, the report carried the PR's current head. If the PR head moved while the run was still going, the heal intake rejected the run for a head mismatch, kept no runs and raised a CRITICAL `scope_marker_unresolved` refusal (binance-blessings run 42974302609, PR #534).

`scripts/workflow_failure_heal_autofix_report.sh` now reads `.pull_request.head.sha` from the Actions event file already on disk (`GITHUB_EVENT_PATH`). It does this only when `GITHUB_EVENT_NAME` is `pull_request`, the event's PR number is the reported PR and the value is 40 hex. It passes the head to the new optional `build-autofix-payload --run-head-sha` flag, and only when the staged helper knows that flag. `build_autofix_failure_payload` uses that head for `head_sha` and for the failure-marker lookup in `run_refs`; `head_branch` stays the PR's. A new optional `REPORT_RUN_HEAD_SHA` env var (default empty) overrides the head for tests. Dispatch runs, `pull_request_target` runs, `identical_failure_cap` reports (their counted failure markers carry the gate head) and runs without a usable event head keep the PR head, as before. The intake is unchanged: `verify_run` still requires the run's GitHub-reported head to equal the reported head, and the existing compare check (`--head-ancestor`) still requires the reported head to be the PR head or one of its ancestors. A force-push during the run therefore still ends in the CRITICAL. The `dispatched` log line gains `run_head=`, `run_head_source=` and `pr_head=` fields. There are no new API calls and the payload schema is unchanged.

| The numbers that matter | Value |
| --- | --- |
| New GitHub API calls | 0 |
| Payload schema changes | 0 (only the value of `head_sha` for `pull_request` runs) |

What this means for consumer repos: after the next `@stable` sync, a review failure on a PR whose head moved during the run reaches the heal intake with a verified run instead of a CRITICAL refusal. No consumer variables or wrapper changes are needed.

Refs #9250.
