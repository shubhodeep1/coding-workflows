<!-- changelog: added -->
- **Check-failure triage can now diagnose a CI run of a PR that has already closed, and does so once for PR #6611's failed shard run.** Activation gap of PR #7091 (#7093).

`.github/workflows/check_failure_triage.yml` gains three optional inputs, `historical_run_id`, `historical_job_id` and `historical_repro_artifact_prefix`, all empty by default. Left empty, triage behaves exactly as before. When both ids are set, `scripts/check_failure_triage.sh` changes only these rules:

- A closed PR is triaged instead of skipped with `pr_not_open`. The fork guard stays, and an unreadable PR head repository now fails the run.
- The fingerprint also covers the run and job ids, and the duplicate check counts closed issues, so the same run is filed only once.
- The base-branch gate is skipped (`base_gate outcome=bypassed reason=historical_mode`).
- Before reading any log, the run and job are checked against this repository, the CI workflow, the PR head SHA and branch, and a failed conclusion (`historical_binding_mismatch`).
- The job log is fetched with `GH_PAT` in the host-side collect step and redacted. Only the first shard failure, with 40 lines before and 80 after, is sent to the isolated diagnosis.
- If the log has expired (404 or 410), the run instead reads the caller's reproduction artifacts. The host recounts every result from the raw logs and reports `reproduced`, `complete_non_reproduction` or `incomplete`. A non-reproduction counts as complete only when every group, shard and step ran and no test was skipped for a missing `jq` or PyYAML.
- The filed issue adds a "Historical run follow-up" section. It lists the evidence and requires a regression test, a passing affected test and `CI / lint`, and an update to the earlier triage note.

The new single-use workflow `.github/workflows/check-failure-triage-historical.yml` runs on every push to `main` (and on `workflow_dispatch`) for run 37922279826 / job 113792812346. Its guard job skips once a trusted `ai:check-triage` issue with that run's fingerprint exists, or after three failed runs (dispatch bypasses the cap). When the log is gone, a credential-free `repro` job reruns the failing tree's `orchestrate-poll` CI steps for groups 0-3. It installs and checks `jq` and PyYAML first, and falls back to the merge commit if the head SHA cannot be fetched. Triage still never pushes code. The workflow is listed in `docs/scripts-pending-removal.md`.
