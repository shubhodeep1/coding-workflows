<!-- changelog: fixed -->
- **Failing CI on pull requests now reaches check-failure triage, and a failing CI on `main` now opens a heal issue.** Check-failure triage had never run, because GitHub sends no `check_run` event for checks created by GitHub Actions.

`internal-check-failure-triage.yml` and the consumer wrapper `ai-check-failure-triage.yml` gain a `triage-workflow-run` job on `workflow_run: completed`. Each failed (`failure` or `timed_out`) pull-request run is evaluated, but repeated failures of the same workflow on a PR share one open triage issue. The diagnosis reads every failing check on the PR head, so a roll-up job that only fails because another job did gets no separate issue. This repo listens to `CI`; consumer repos listen to every workflow and skip only the shipped pipeline wrapper paths, not custom workflows with matching names or case variants. The exclusion uses the workflow definition path rather than the run path, which may carry an `@refs/heads/...` suffix. `workflow-failure-heal-intake.yml` now also takes failed `CI` runs on pushes to the default branch and files the fix against that branch. The `check_run` job stays for checks from apps other than GitHub Actions.

| The numbers that matter | Value |
| --- | --- |
| Check-failure triage runs in this repo before the change | 0 (since the workflow landed in June) |
| Open triage issues per PR and failing workflow | At most 1 |
| `main` CI push runs that failed, 2026-10-03 09:51 to 2026-10-04 17:56 UTC | 35 of 35 |

What this means for operators: a red CI on a PR now opens an `ai:check-triage` issue when no matching one is open, and a red `main` now opens an `ai:workflow-heal` issue against `main`, so a broken base no longer sits unnoticed. A fix PR for a CI-derived heal issue must pass the head-bound security audit; release-workflow heal issues retain their existing exemption. In consumer repos every finished workflow leaves a skipped `AI Check Failure Triage` run in the Actions tab; `CHECK_FAILURE_TRIAGE_ENABLED=false` still turns triage off.
