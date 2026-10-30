<!-- agents: section="Pre-push affected-test gate" -->
- **Unfinished baseline re-runs.** A unit that fails on the change but whose
  baseline re-run times out or cannot start is reported as
  `unverified baseline_timeout` / `unverified baseline_error`, not as a failure
  caused by the change, so it never starts the implement repair turn or the
  second gate run. Only a baseline re-run that finishes and fails makes a unit
  `inherited`; a unit with no baseline copy of its test file still counts as
  caused by the change.
  Like every `unverified` result, a gate result whose only problems are
  these entries still counts as a passing stamp, so the push is not held;
  CI on the pushed head stays the check for such a test.
