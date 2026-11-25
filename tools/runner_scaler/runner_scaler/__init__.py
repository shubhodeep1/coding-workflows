"""Runner scaler: single-use DigitalOcean droplets for `do-runner` GitHub Actions jobs.

docs/plans/digitalocean-runners-for-consumer-pipelines-plan.md, Phase 2. A
consumer that sets the repo variable PIPELINE_RUNS_ON=do-runner runs the
reusable pipeline workflows' jobs on runners this service creates: one droplet
per queued job, destroyed when the job completes. The service keeps no state of
its own; every reconcile pass recomputes from the droplets tagged `gh-runner`
and GitHub's job and runner lists, so a restart, a missed webhook or a replayed
event cannot pile up droplets.
"""
