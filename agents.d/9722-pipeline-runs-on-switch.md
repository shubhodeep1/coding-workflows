<!-- agents: section="Workflow architecture" -->
Consumer runner selection (`docs/plans/digitalocean-runners-for-consumer-pipelines-plan.md`, Phase 1). Every job of the reusable workflows that consumers call reads the calling repository's `vars.PIPELINE_RUNS_ON`:

```yaml
runs-on: ${{ vars.PIPELINE_RUNS_ON == 'do-runner' && 'do-runner' || 'ubuntu-latest' }}
```

`security-audit.yml` falls back to `ubuntu-24.04`. Only the exact value `do-runner` routes a job to the runner scaler's single-use DigitalOcean droplets; anything else keeps the GitHub-hosted runner. The rules:

- `ci.yml`, `workflow_failure_heal.yml` and `check_failure_triage.yml` never read the variable, and neither do non-reusable workflows or `workflow-templates/` jobs.
- `tests/test_pipeline_runs_on_switch.py` pins all of this. A new job in a routed workflow must use the same expression, with its previous runner as the fallback.
- coding-workflows never sets the variable, so its own runs of these workflows stay GitHub-hosted.
- The `heal-report` jobs inside `clarify.yml`, `plan.yml`, `implement.yml` and `validate.yml` are routed too. Only the heal intake itself (`workflow_failure_heal.yml`) is independent of the fleet; while the fleet is down a failed phase's report waits with its run until the variable is unset.

The heal intake (`scripts/workflow_failure_heal_intake.sh`, evidence collection) handles a lost droplet like this:

1. `workflow_failure_heal.py runner-lost-candidates` lists a run's failed job ids only when every failed job carries the `do-runner` label (and its `check_run_url`, when present, names the job id), and at most 10 of them.
2. For each listed job, the intake reads `repos/<source>/check-runs/<job id>/annotations` once (`GH_RETRY_MAX_ATTEMPTS=1`).
3. `runner-lost` prints `true` when each of those jobs has a failure-level annotation starting with GitHub's lost-runner wording (`The self-hosted runner: ... lost communication with the server` or `The runner has received a shutdown signal`), and every other failure-level annotation of the job is the same wording or `The operation was canceled.`. Any other failure annotation (a step's exit code) means the job also failed for real.
4. The run is then left out of the evidence (`WORKFLOW_HEAL runner_lost outcome=skipped run=<id>`). When no other run remains, the report is skipped (`WORKFLOW_HEAL skip reason=runner_lost ... runs=<n>`).

Any read failure, a non-matching annotation, or a mixed failure (one failed job on a GitHub-hosted runner) reports the run as before. That is fail-open towards filing.
