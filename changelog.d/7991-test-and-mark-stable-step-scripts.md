<!-- changelog: changed -->
- **The release gate workflow is back under its file size guard.** `.github/workflows/test-and-mark-stable.yml` had reached 441,071 bytes. The `budget-watch` job warns at 440,000 bytes, `tests/test_workflow_file_size_limit.py` fails at 480,000, and GitHub stops starting runs above 512,000 (#7991).

The two largest step bodies in the `e2e-smoke-test` job now live in `scripts/`. They moved verbatim, so the gate behaves exactly as before:

| Step | Script | Bytes moved |
| --- | --- | --- |
| Phase 4: Wait for review & autofix to complete (`wait-review`) | `scripts/stable_e2e_step_wait_review.sh` | about 48,600 |
| Phase 4b: Verify editor restored canary (`verify-bait-removed`) | `scripts/stable_e2e_step_verify_bait_removed.sh` | about 34,000 |

The workflow is now 361,147 bytes. Each step keeps its name, id, `if:` and `env:`. Its `run:` block sources the script from a copy taken from a sparse checkout pinned to the run's own commit (`github.sha`), never from the `main` checkout. A new step checks that checkout's commit and refuses symlinks before it copies the scripts to `RUNNER_TEMP`. If the scripts are missing or unverified, the step fails. No inputs, outputs or GitHub API calls change.

What this means for consumer repos: nothing. This workflow runs only in coding-workflows.

### For contributors

Tests read the moved bodies through `expanded_test_and_mark_stable_text()` in `tests/review_autofix_step_scripts.py`, which puts them back inline. The registry `TEST_AND_MARK_STABLE_STEP_SCRIPTS` and `tests/test_test_and_mark_stable_step_scripts_contract.py` pin the wrapper and the pinned checkout. The contract test also requires the workflow to stay at or below 430,000 bytes.
