<!-- changelog: fixed -->
- **The isolated heal editor no longer throws away valid YAML edits.** Its sandbox image now ships PyYAML, and a missing YAML checker is reported as a validator outage instead of a syntax error.

Issue #6982 failed with "no actionable output" because `scripts/validate_changed_files_syntax.sh` ran inside the `clarify_sandbox` image, where `python3` could not import `yaml`. Every edited workflow file was flagged as a syntax error, the repair loop could not clear it, and both edit attempts were discarded. The image now installs `python3-yaml`. When the checker is still missing, the validator exits 3 and `scripts/heal_isolated_implement.sh` stops with `reason=validator_unavailable` instead of spending repair attempts. The validator's `::error` lines now appear in the run log, prefixed with `HEAL_ISOLATED_EDITOR validation:`.

| The numbers that matter | Value |
| --- | --- |
| Validator exit code for an unavailable checker | 3 |
| Validation lines echoed to the run log | up to 20, 300 characters each |

What this means for operators: heal runs that edit YAML now land their edits, and a validator failure names its cause in the run log instead of disappearing with the temp directory.
