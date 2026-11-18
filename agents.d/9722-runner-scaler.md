<!-- agents: section="Workflow architecture" -->
Runner scaler (`tools/runner_scaler/`, `docs/plans/digitalocean-runners-for-consumer-pipelines-plan.md`, Phase 2). It is a supervisor (`docs/scripts-pending-removal.md`) on DigitalOcean App Platform, built from `tools/runner_scaler/Dockerfile` on every push to `stable` (`tools/runner_scaler/app.yaml`). It serves the `do-runner` jobs of consumers that set `PIPELINE_RUNS_ON=do-runner`.

Its state is the droplets tagged `gh-runner` / `gh-runner-repo-<slug>` plus GitHub's runner and job lists; the process keeps nothing durable, so a restart is safe.

- **Demand** is the queued `do-runner` job ids per repository. `workflow_job` webhooks keep it current, and a full scan replaces it every 10 minutes. A job a webhook reported in the last 60 s survives a scan that does not list it yet.
- **Supply** is the repository's droplets that are booting plus its runners that are online and idle. A droplet is booting until its runner is first seen online or picks up a job. GitHub lists a JIT runner as `offline` from creation, so a listed-but-never-online runner is booting, not registered. Droplets this process created that the DigitalOcean tag listing does not show yet also count as booting, and their runners are never treated as orphans.
- **Each pass** creates `min(demand - supply, cap - live)` droplets, 0 while the breaker is open and 1 trial when it is half-open, and destroys droplets as listed below.

Each droplet creation:

1. `generate-jitconfig` creates a runner named after the droplet.
2. `runner-boot.sh` is passed as user data, with the JIT config replacing its single placeholder.
3. If the droplet create fails, the JIT runner is deleted again.

A droplet is destroyed:

- on `completed`;
- when it powered off: after a job, or with its runner still listed offline and never online, which is a self-check boot failure. Off with the runner gone has no boot verdict;
- when its runner never came online within the register timeout (a boot failure);
- when it is idle without demand after the same window;
- when it is past its max lifetime.

Lost-runner re-runs (`POST actions/jobs/<id>/rerun`) need GitHub's lost-runner annotation and `run_attempt` 1. GitHub accepts them only after the run completes, so they are retried at most every 5 minutes per job, at most 10 per pass, for up to 6 hours (`rerun_deferred`, then `rerun` or `rerun_skipped reason=rerun_window_expired`). A job seen `in_progress` or `completed` is kept out of the queue for 10 minutes, so a full scan that read it as queued cannot add it back. Never edit the boot script so that it holds a credential: the droplet must stay credential-free apart from its single-use JIT config. Every droplet is created in the dedicated VPC `RUNNER_SCALER_VPC_UUID`; the service refuses to start without it while repositories are configured, so no runner shares the region's default VPC with the account's other droplets. `shubhodeep1/coding-workflows` is never served (`config_invalid reason=excluded_repo`): its CI stays on GitHub-hosted runners (plan decision Q24 C).

Onboarding: `/seed-repo` (step 6, item c) offers the fleet only for a private target and only when the agents file's `## DigitalOcean resources` table has a `runner-scaler` row, so the rollout must record the App Platform app under exactly that resource name. The order is fixed: GitHub App install (user), then `RUNNER_SCALER_REPOS` (session, §22.B), then `PIPELINE_RUNS_ON=do-runner` (session, §23.C).

<!-- agents: section="Stable log prefixes (contractual)" -->
- `RUNNER_SCALER` (`tools/runner_scaler/runner_scaler/log.py`, stdout of the App Platform service: `RUNNER_SCALER outcome=<started|stopping|stopped|pass|pass_failed|created|create_failed|create_lost|destroyed|destroy_failed|boot|breaker_open|skipped|scan_incomplete|rerun|rerun_deferred|rerun_skipped|runner_deleted|runner_delete_failed|alert|alert_not_sent|dry_run|config_invalid|config_error|webhook_rejected> key=value ...`; values are sanitised to `[A-Za-z0-9_.:/@+=,-]` and at most 200 characters; `reason=` names the decision, for example `job_completed`, `boot_failed`, `register_timeout`, `idle`, `max_lifetime`, `cap_reached`, `breaker_open`, `runner_lost`, `rate_limited`)
LOG_PREFIX.name=RUNNER_SCALER
