<!-- changelog: fixed -->
- **The synced-file security heal intake now logs the real scope-marker status when it skips a finding with `reason=scope_unresolved`.**

`scripts/workflow_failure_heal_intake.sh` used to log `status=missingunknown` for a finding on a managed path with no source at HEAD, because the `heal-scope check-marker` pipeline appended a fallback value after the marker's own status. The intake now reads the status from the captured `check-marker` output, the same way the main scope-marker guard already does, so the line reads `status=missing` (or `invalid`, …) and falls back to `unknown` only when nothing usable came back. The skip decision and the log line's keys are unchanged.

What this means for operators: searches and alerts on `skip reason=scope_unresolved ... status=missing` in the `workflow-failure-heal-intake.yml` logs now match the synced-file security path too.

### For contributors

`tests/test_workflow_failure_heal_synced_security.py` now covers this skip, and also the defensive `warn empty_group_file` guard, which validated findings never reach in production.
