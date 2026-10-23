<!-- changelog: added -->
- **Validation failures caused by the harness are reported to workflow failure heal.** When a runtime validation run fails because of the validation harness coding-workflows ships, such as a failed canary or a broken harness template, the project used to get fix-up issues it could not act on, and the project stalled. `validate.yml` now has a `heal-report` job that sends such a failure to the workflow failure heal intake as one `validation_failure` report, so the harness is fixed upstream. Refs #7913.

- New `workflow_call` outputs: `failure_owner` (`harness`, `project`, `mixed` or `none`), `harness_report`, `harness_stage`, `harness_reason` and `harness_fp`. The existing outputs are unchanged.
- The `heal-report` job runs only after a failed validate job whose failure is harness-owned, for a tracking issue other than `0`. It takes its support scripts from the commit the caller pinned, like `implement.yml`'s `heal-report` job, and is granted only `id-token: write`.
- The validate job logs `VALIDATE_SUPPORT_SHA sha=<commit>`, the commit its support scripts came from.
- `VALIDATION_HARNESS_HEAL_ENABLED` (default `true`) turns this off when set to `false`.
