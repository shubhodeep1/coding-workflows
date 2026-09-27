<!-- changelog: fixed -->
- **Validation no longer stops before writing its status when the caller does not export the immutable-support paths.** `scripts/validate_process.sh` now defaults `SUPPORT_ROOT_DIR`, `SUPPORT_SCRIPTS_DIR` and `SUPPORT_PROMPTS_DIR` to the tree it runs from.

This branch's `validate.yml` exports the three variables from its immutable support bundle. Main's `validate.yml`, which runs a project branch's checkout while the branch is being validated, does not. The script then stopped at prompt resolution with "SUPPORT_PROMPTS_DIR is required" and never wrote `validation_status.json`, so every run was recorded as `codex_failure` with "Missing status artifact". The Codex launcher and self-heal stop on the same variables. Exported values are kept unchanged; only missing ones are filled in, and the script logs which ones.

| The numbers that matter | Value |
| --- | --- |
| Failed runs this explains (project #3965) | 36232071002, 36235618528, 36239049270 |
| Log line when defaults apply | `VALIDATE_SUPPORT_DEFAULTS applied=<names> root=<path>` |

What this means for operators: validating a project branch with an older `validate.yml` now produces a real verdict instead of a missing-status failure.

### For contributors

The defaults point at the directory `validate_process.sh` runs from (the same trust as the script) and are exported so the Codex launcher and `self_heal_validation.sh` see the same values. Tests: `tests/test_validate_process_support_defaults.py`.
