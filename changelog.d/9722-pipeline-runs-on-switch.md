<!-- changelog: added -->
- **Consumer pipeline jobs can run on DigitalOcean runners instead of GitHub-hosted ones.** A consumer opts in by setting the repo variable `PIPELINE_RUNS_ON` to `do-runner`.

Every job of the 17 reusable workflows that consumers call now reads the calling repository's `PIPELINE_RUNS_ON`. Only the exact value `do-runner` sends a job to the runner scaler's single-use droplets. Unset, or any other value, keeps each job's previous runner (`ubuntu-latest`, or `ubuntu-24.04` for `security-audit.yml`), so nothing changes until a consumer opts in. `ci.yml`, `workflow_failure_heal.yml` and `check_failure_triage.yml` always stay on GitHub-hosted runners, so a broken droplet fleet cannot break the heal intake itself. The `heal-report` jobs inside `clarify.yml`, `plan.yml`, `implement.yml` and `validate.yml` do follow the variable, so while the fleet is down their reports wait with the rest of the run until the variable is unset. The heal intake now drops a run whose every failed job lost its `do-runner` droplet, logs `WORKFLOW_HEAL skip reason=runner_lost`, and files no heal issue for it, because the runner scaler re-runs such a job once instead.

| The numbers that matter | Value |
| --- | --- |
| Reusable workflows that read the variable | 17 (30 `runs-on:` lines) |
| Reusable workflows that never read it | `ci.yml`, `workflow_failure_heal.yml`, `check_failure_triage.yml` |
| Annotation reads the heal intake may add per reported run | at most 10, only when every failed job ran on `do-runner` |

What this means for consumer operators: nothing changes on the next `@stable` sync. Set `PIPELINE_RUNS_ON=do-runner` only after the runner scaler serves your repository, or the jobs wait in the queue until the variable is unset. Unsetting it sends new jobs back to GitHub-hosted runners at once.

### For contributors

The plan is `docs/plans/digitalocean-runners-for-consumer-pipelines-plan.md` (Phase 1, Refs #9722). `tests/test_pipeline_runs_on_switch.py` pins the expression in every routed job and its absence everywhere else, including `workflow-templates/`. The lost-runner rule lives in `runner_lost_candidate_job_ids()` and `runner_lost_failure()` in `scripts/workflow_failure_heal.py`, behind the `runner-lost-candidates` and `runner-lost` subcommands. It fails open: an unreadable or non-matching annotation, or a job with any other failure-level annotation (a failed step's exit code), reports the run as before. Each annotations read is a single attempt.
