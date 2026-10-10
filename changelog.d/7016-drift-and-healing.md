<!-- changelog: added -->
- **The validation harness sandbox is checked every night, self-repo validation runs on main's harness scripts, and a workflow heal can fix the size guard it is filed for.** Runner-image drift now shows up within a day instead of when every project validation fails, and a stale integration branch no longer validates with its own old sandbox script.

`nightly-validation-selftest.yml` has a new `harness-sandbox-check` job. It provisions the sandbox, runs a probe through `checked-run` and checks log copy-back. A failed run on the default branch reaches the workflow failure heal intake, which opens an `ai:workflow-heal` issue. Self-repo validation runs without an explicit target now stage `validation_harness_sandbox.sh`, `codex_isolated_workspace.py` and `validate_driver.sh` from the verified support commit, the way #6940 did for the template renderer. Project #6664 had revalidated with an integration-branch sandbox script that could not provision. A heal filed for a failing guard test now includes the files the fix edits. For the workflow size guard that is the oversized workflow, `scripts/stage_workflow_support.sh`, `docs/INVENTORY.md` and that workflow's new `scripts/<workflow>_step_*.sh` files, instead of `tests/**` only.

| The numbers that matter | Value |
| --- | --- |
| Sandbox check schedule | daily, `15 2 * * *` (nightly self-test cron) |
| Log line | `VALIDATION_HARNESS_SANDBOX_DAILY outcome=ok\|fail` |
| Scripts staged from the support commit | 3 (`self_repo_trusted_scripts`) |
| Size-guard threshold the heal scope reads | 480,000 bytes |

What this means for operators: a broken sandbox arrives as an ordinary heal issue the next morning, self-repo projects validate with the current harness even before they sync main, and a red main from the size guard can be healed without a human.

### For contributors

New log prefixes: `VALIDATE_TRUSTED_SUPPORT_OVERRIDE`, `VALIDATE_TRUSTED_SUPPORT`, `VALIDATION_HARNESS_SANDBOX_DAILY`. Guard tests that map to other files go in `HEAL_SCOPE_GUARD_TEST_SUBJECTS` in `scripts/workflow_failure_heal.py`. `extract_failing_tests` now also reads tracebacks and unittest headers from tests that CI runs as scripts.
