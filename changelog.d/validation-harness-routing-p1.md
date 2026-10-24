<!-- changelog: added -->
- **Validation failures caused by the harness are reported to workflow failure heal.** Refs #7913.

When a runtime validation run failed because of the validation harness coding-workflows ships, such as a failed canary or a broken harness template, the project got fix-up issues it could not act on, and the project stalled. `validate.yml` now has a `heal-report` job that sends such a failure to the workflow failure heal intake as one `validation_failure` report, so the harness is fixed upstream. It runs only after a failed validate job whose failure is harness-owned, for a tracking issue other than `0`. Projects whose failures are their own keep getting fix-up issues as before.

What this means for operators: a project no longer waits on fix-up issues for a harness defect it cannot repair. Set `VALIDATION_HARNESS_HEAL_ENABLED=false` (default `true`) to turn the report off and keep the old behaviour.

### For contributors

- New `workflow_call` outputs: `failure_owner` (`harness`, `project`, `mixed` or `none`), `harness_report`, `harness_stage`, `harness_reason` and `harness_fp`. The existing outputs are unchanged.
- The `heal-report` job takes its support scripts from the commit the caller pinned, like `implement.yml`'s `heal-report` job, and is granted only `id-token: write`. The error signature reaches it as a base64 job output of at most 1024 bytes.
- The validate job logs `VALIDATE_SUPPORT_SHA sha=<commit>`, the commit its support scripts came from.
