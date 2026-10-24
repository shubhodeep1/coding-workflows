<!-- changelog: fixed -->
- **Self-repo validation tests the branch's own support scripts again.** The validation sandbox now stages the committed version of four support files instead of the trusted copies the host wrote over them, so a project that changes one of them, together with its tests, no longer fails validation with `harness_error`.

Self-repo validation (tracking issue #7913, run 40083109127) failed because support staging copies trusted versions of `scripts/validate_process.sh`, `scripts/validation_harness_sandbox.sh`, `scripts/codex_isolated_workspace.py` and `scripts/templates/slot_manifest.schema.json` over the validation checkout, and the sandbox's screened copy read the working tree. The branch's `tests/test_validate_process_template_mode.py` then ran against `main`'s older `validate_process.sh`. `STAGE_PY` in `scripts/validation_harness_sandbox.sh` now takes those four files from their `HEAD` blobs. The host still executes only trusted copies (#6865), and `scripts/validate_driver.sh` stays the trusted overlay because the sandbox runs it.

| The numbers that matter | Value |
| --- | --- |
| Support files staged from `HEAD` | 4 |
| Still the trusted overlay | `scripts/validate_driver.sh` |
| New log reason | `VALIDATION_HARNESS_SANDBOX phase=stage outcome=ok reason=support_from_head path=<path>` |

What this means for operators: after this lands on `main`, post `/revalidate` on #7913; the project's files need no change. A branch that changes `scripts/validate_driver.sh` together with tests that read it still sees the trusted driver.
