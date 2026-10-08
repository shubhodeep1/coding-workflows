<!-- changelog: security -->
- **Synthesised behavioural smoke tests no longer run with pipeline credentials.** A test the review synthesiser writes (`validation/tests/synth_round_*.sh`) is model output shaped by the PR and restored from a PR-scoped cache, and validation used to run it on the host with `GH_TOKEN` and `OPENROUTER_API_KEY` in its environment. `scripts/validate_driver.sh` now runs each one in a `python:3.12-slim` container. The container has no network, a read-only root and no capabilities. It gets only `HOME`, `TMPDIR` and `BEHAVIOURAL_SMOKE_SANDBOXED=1`, and a `git archive` of `HEAD` with no `.git`. If Docker or the image is unavailable, the test is reported as TAP `ok ... # SKIP` and is never run on the host.

The wrapper the synthesiser writes now runs its body only when `BEHAVIOURAL_SMOKE_SANDBOXED=1`, so an older driver reports it instead of running it. `scripts/validate_process.sh` copies only files named `synth_round_<n>_<slug>.sh` and `synth_round_<n>_manifest.json`, and never a symlink, so a poisoned manifest cannot plant a host-run test or overwrite the canary. It also starts the whole validation harness without `GH_TOKEN`, `GITHUB_TOKEN`, `GH_PAT`, `OPENROUTER_API_KEY`, `TG_BOT_SECRET`, `CHECK_TRIAGE_ISSUES_TOKEN` or the Actions OIDC request variables.

- **Line ownership in the project security pass marks fewer findings advisory.** A finding on a line older than the project now stays blocking in three more cases:
  - the project added lines anywhere in the cited file (`reason=added_lines_in_file`), because an override appended far from the line can still change how it runs;
  - a file that links the cited module to a module that lost lines exists only at the head, such as a router the project added (module references are now read at both base and head);
  - a file the project changed, other than documentation or tests, names the cited module at the head (`reason=changed_file_references_cited_module`).

`SECURITY_AUDIT_LINE_OWNERSHIP_HUNK_WINDOW` is still accepted, but it now only picks the logged reason (`changed_hunk_within_window` or `added_lines_in_file`).

| The numbers that matter | Value |
| --- | --- |
| Extra `git grep` calls per module stem | 1 (head, alongside base) |
| Default synthesised-test timeout (`VALIDATION_SYNTH_SANDBOX_TIMEOUT_SECS`) | 300 s |
| Extra GitHub API calls | 0 |

What this means for operators: more findings block, so projects may spend more security-pass fix cycles; the cycle budget and the exhaustion judge still bound them. Synthesised smoke tests no longer reach compose services or the network. They were already advisory and never changed the validation result. On a runner without Docker or the `python:3.12-slim` image they are skipped, and `BEHAVIOURAL_SMOKE_SANDBOX ... outcome=skipped` in the validation log says why. Harness tests that are not synthesised still run on the host; only their environment credentials were removed.

### For contributors

New stable log prefix `BEHAVIOURAL_SMOKE_SANDBOX`. Findings `smoke-synth-credentialed-test-exec`, `security-pass-deleted-guard-advisory` and `security-pass-distant-override-advisory`; Refs #6664. Tests: `tests/test_security_audit_workflow_contract.py`, `tests/test_review_synthesise_smoke.py`, `tests/test_validate_driver_synthesised_filter.py`.
