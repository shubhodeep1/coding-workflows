<!-- changelog: security -->
- **Validation of an orchestrator integration branch now runs the default branch's credential scrub, sandbox and harness driver, not the branch's own copies.**

Validation in this repository checks out the project's integration branch and used that checkout's `validate_process.sh`, `validate_driver.sh` (the per-test credential scrub), `validation_harness_sandbox.sh` (the isolation boundary) and the renderer's slot schema (the `project_name` allowlist). `validate_process.sh` runs on the host, where the GitHub token and the model key are available. A project PR that weakened one of these files therefore took effect in that project's own validation run. These four files now come from the same verified support commit as the validation templates and the renderer, and the support step fails if a trusted copy is missing. Consumer repositories and validation runs with an explicit `target_ref` already staged them from a trusted copy and are unchanged.

The shell-quoting, credential-scrub and sandbox regression tests now also run in CI and in both release gates (`test-and-mark-stable.yml` and `mark-stable.yml`). Before this, a release could ship a regression in any of them.

What this means for operators: a branch that changes one of these four files is validated with the default branch's copy until it merges. Its PR CI still tests the branch's copy. The run log names each replaced file with `VALIDATE_TRUSTED_SUPPORT_OVERRIDE` and the total with `VALIDATE_TRUSTED_SUPPORT staged=<n>`.
