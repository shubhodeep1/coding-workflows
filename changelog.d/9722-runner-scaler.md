<!-- changelog: added -->
- **New runner scaler service for DigitalOcean runners.** `tools/runner_scaler/` starts one single-use droplet per queued `do-runner` job and destroys it when the job is done.

The service is Phase 2 of `docs/plans/digitalocean-runners-for-consumer-pipelines-plan.md`. A GitHub App sends it `workflow_job` webhooks. Every pass compares queued jobs with droplets still booting and idle runners, and creates only the missing droplets, up to 60 at once. Each droplet boots stock Ubuntu 26.04 with `tools/runner_scaler/bootstrap/runner-boot.sh`. The script installs the pipeline's tools and checks them before it registers a single-use runner, so a broken machine never runs a job.

Failures stay bounded:
- a circuit breaker stops creating droplets after repeated boot failures, then allows one trial droplet after a cooldown;
- a job whose droplet was lost is re-run once, only at `run_attempt` 1, as soon as GitHub accepts it after the run completes;
- Telegram alerts go out once per incident: breaker open, jobs queued without supply, cap reached, a droplet delete failed, DigitalOcean API errors, a lost-runner re-run given up.

Nothing runs until the App Platform app is created from `tools/runner_scaler/app.yaml`, and its first deploy is a dry run.

`/seed-repo` now offers the fleet when it seeds a new private consumer, once the rollout has recorded the `runner-scaler` app. It runs the three onboarding steps in a fixed order, so a job never queues with nothing to serve it: the user installs the GitHub App, then the session adds the repository to `RUNNER_SCALER_REPOS`, then sets `PIPELINE_RUNS_ON=do-runner`. Public repositories and coding-workflows are never offered.

| The numbers that matter | Value |
| --- | --- |
| Droplet size, region, image | `s-4vcpu-8gb` ($0.07143/hr), `nyc3`, `ubuntu-26-04-x64` |
| Droplets at once (cap) | 60, so at most $4.29/hr |
| Network | a dedicated VPC (`RUNNER_SCALER_VPC_UUID`, required), no inbound traffic |
| Register timeout, idle window, max lifetime | 15 min, 15 min, 300 min |
| Breaker | opens after 3 failures in a row or more than 50% failed in 15 min; one trial after 15 min |
| Lost-runner re-run | once, at `run_attempt` 1 only |
| GitHub API calls, 7 repositories | about 300 an hour when idle; at most about 3,450 an hour under continuous webhooks and full scan pages, plus at most 12 an hour per pending lost-runner re-run (installation limit 5,000) |

What this means for operators: nothing changes until rollout. The rollout covers the GitHub App, the scoped DigitalOcean token, the firewall, the App Platform app and the Uptime check, and each step is ask-first. Then a consumer sets `PIPELINE_RUNS_ON=do-runner`. To roll back, unset that variable.

### For contributors

The service is stdlib Python. PyJWT and cryptography are imported only to mint the GitHub App JWT, so `tests/test_runner_scaler_*.py` run with stubbed GitHub and DigitalOcean clients (`tests/runner_scaler_fakes.py`) and need neither. The service follows the `tools/claude-pool-broker` layout for deployed services. The plan named the path `runner_scaler/`; the code lives at `tools/runner_scaler/`. Refs #9722.
