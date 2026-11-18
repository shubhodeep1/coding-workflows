# Run consumer pipeline jobs on autoscaled DigitalOcean runners

## Summary

Consumer repositories run the AI pipeline's reusable workflows on GitHub-hosted runners. Those runners take slots from the account's 40-job concurrency limit and are billed per minute in private repositories. This plan lets each consumer move those jobs onto single-use DigitalOcean droplets that are created per job and destroyed afterwards, by setting one repo variable. `coding-workflows` itself stays on GitHub-hosted runners.

## Context

- The account's Actions queue has been jammed since 2026-11-11 (`docs/plans/reduce-actions-run-volume-plan.md`). On 2026-11-18, 377 runs were queued in coding-workflows, and the last 30 `CI` runs took 1 to 2.5 hours from creation to finish for about 20 minutes of work each.
- Both earlier plans (`docs/plans/cut-ci-runner-time-plan.md`, #9528, and the run-volume plan) list runner capacity as a non-goal. This plan covers it.
- Managed runner vendors were ruled out:
  - Depot, Blacksmith and WarpBuild runners, and GitHub larger runners, need a GitHub organization, and `shubhodeep1` is a personal account.
  - Namespace works with personal accounts. At about 0.75 to 1.2 million minutes a month, though, its overage pricing is roughly $6,000 to $12,000 a month for 4 vCPU machines.
  - A DigitalOcean droplet costs about 2.5 to 5 times less per minute.
- All 7 consumers in `.github/ai/consumer_repos.json` are private, so GitHub bills their hosted minutes beyond the plan's included minutes. Self-hosted runner minutes are free: the $0.002/min platform charge announced for March 2026 was postponed.
- Consumers call this repository's reusable workflows (`uses: shubhodeep1/coding-workflows/.github/workflows/<name>.yml@stable`). Those workflows already read the caller's repository variables (for example `vars.WORKFLOW_PROFILE`). A `runs-on:` that reads a variable therefore lets each consumer opt in without any change to its wrapper files.
- GitHub's `ubuntu-latest` is Ubuntu 26.04 today (`tests-system-python` notes in `ci.yml`). DigitalOcean offers the `ubuntu-26-04-x64` distribution image in every region.
- The pipeline runs Docker sandboxes (`scripts/clarify_isolated_run.sh`, `scripts/review_untrusted_sandbox.sh`, `scripts/codex_isolated_exec.sh` and others), installs packages with `apt-get`, and uses `actions/setup-python` and `actions/setup-node`.
- DigitalOcean account facts (read 2026-11-18): droplet limit 2,000, with 20 droplets in use. Droplets have been billed per second since 2026-01-01, with a minimum charge of 60 seconds or $0.01.

## Automation and supervisor summary (§18.E)

- **Scripts:** Phase 1 only edits existing workflows, `scripts/workflow_failure_heal.py` and `scripts/workflow_failure_heal_intake.sh`. Phase 2 adds a new long-running supervisor, the `tools/runner_scaler/` service, plus the droplet boot script it hands to every droplet. No script needs manual invocation (§18.A).
- **Layout (D16):** `tools/runner_scaler/` is the App Platform build context. It holds `app.yaml`, `requirements.txt`, `bootstrap/runner-boot.sh` and the Python package `runner_scaler/` (so the package path is `tools/runner_scaler/runner_scaler/`, and `python -m runner_scaler` resolves from the build context root). Every path in this plan uses that layout.
- **Scheduler / entry point:**
  - The scaler runs as a DigitalOcean App Platform service built from `tools/runner_scaler/app.yaml`, with `deploy_on_push` from the `stable` branch and `source_dir: tools/runner_scaler`. Every release that reaches `stable` (the `release` jobs of `.github/workflows/test-and-mark-stable.yml` and `.github/workflows/mark-stable.yml`) redeploys it, and no operator action is needed.
  - Its reconcile loop is an in-process timer; webhooks arrive from a GitHub App.
  - No GitHub Actions job, workflow or cron is added (§33).
- **Supervisor lifecycle (§18.C):**
  - Entry point: `python -m runner_scaler`, run from `tools/runner_scaler/`.
  - Restart policy: App Platform restarts the container on crash or on a failing `GET /healthz`.
  - Shutdown: on SIGTERM the scaler stops accepting webhooks, finishes the reconcile pass in progress, and exits within 20 seconds. Droplets keep running, because runners talk to GitHub directly.
  - Crash recovery: the durable state is the droplets tagged `gh-runner` (with the `gh-runner-registered` tag the scaler adds once a droplet's runner is seen online) plus GitHub's job, run and runner lists. The scaler also keeps in-process state that a restart loses: the queued-job set, the breaker history and the alert cooldowns. A fresh instance rebuilds the queued set with a full scan at startup, starts the breaker half-open (one trial droplet before scaling out, so a restart during a failing fleet costs at most one droplet), and may repeat at most one alert per open incident. That loss is accepted; see Approach, "Restart behaviour".
  - External liveness: a DigitalOcean Uptime check on `/healthz` alerts by email when the service is down. The scaler's own Telegram watchdog cannot report its own death.
- **DB work:** none.
- **Removal registry (§18.F):** Phase 2 adds an entry for `tools/runner_scaler/` to `docs/scripts-pending-removal.md` (see Implementation Steps, step 2.9).

## Goals

- **G1.** In every reusable workflow (17 files, 30 `runs-on:` lines) except `ci.yml`, `workflow_failure_heal.yml` and `check_failure_triage.yml`, every job's runner comes from the calling repository's variable `PIPELINE_RUNS_ON`:
  - the value `do-runner` selects the DigitalOcean runners, except when the calling repository is `shubhodeep1/coding-workflows` (G8, D15). GitHub compares expression strings case-insensitively, so `DO-RUNNER` also matches; any other spelling falls back;
  - any other value, or no value, selects the job's current runner (`ubuntu-latest`, or `ubuntu-24.04` in `security-audit.yml`, the only routed workflow not on `ubuntu-latest`; `drift-audit.yml` and `review_autofix_sweep.yml` also use `ubuntu-24.04` but are not reusable workflows and are not routed).
- **G2.** With no consumer setting the variable, every job runs exactly as today. Phase 1 is a no-op on merge.
- **G3.** For each queued job labelled `do-runner` in a repository on the scaler's allowlist, the scaler creates one droplet, up to 60 at once (`RUNNER_SCALER_MAX_DROPLETS`). The droplet is destroyed when the job completes, when it fails to register within 15 minutes, when its runner sits online and idle for 5 minutes with no queued job for it (the job was cancelled before assignment), or when it outlives 300 minutes.
- **G4.** A droplet whose boot self-check fails never registers a runner. It powers itself off, and the scaler destroys it and counts a boot failure. A droplet that powers off after its runner registered (a normal end of job) is destroyed without counting a boot failure.
- **G5.** The scaler's circuit breaker stops creating droplets after 3 boot failures in a row, or when more than half of boots in 15 minutes fail. It sends one Telegram alert per incident and tries a single droplet after a 15-minute cooldown.
- **G6.** When a run's only failures are jobs whose DigitalOcean runner was lost, the scaler re-runs the failed jobs once, after the whole run has completed and only when its `run_attempt` is 1. The heal intake files no heal issue for such a run. A run that also has an ordinary failure is neither re-run nor skipped: it is reported to the heal intake as today.
- **G7.** A job labelled `do-runner` that waits more than 10 minutes with no droplet booting for it raises one Telegram alert per incident.
- **G8.** `workflow_failure_heal.yml`, `check_failure_triage.yml` and every coding-workflows job keep running on GitHub-hosted runners whatever the variable says. Two independent guards enforce this: the `runs-on` expression never yields `do-runner` when `github.repository` is `shubhodeep1/coding-workflows` (Phase 1), and the scaler drops that repository from its allowlist at config load (Phase 2). Both are pinned by tests (steps 1.3 and 2.6).

## Non-goals

- Moving `coding-workflows`' own jobs, including `ci.yml` and the release workflows, off GitHub-hosted runners (D15, Q24 C: never).
- Jobs defined directly in consumer wrapper templates (`workflow-templates/ai-implement.yml`, `ai-plan.yml`, `ai-review.yml`: 5 small gate and sweep jobs). These are not reusable-workflow jobs (Q3), and droplet boot time would outweigh their sub-minute run time.
- Consumers' own CI workflows, which this repository does not manage.
- Automatic fallback to GitHub-hosted runners when the fleet fails (Q14 B). This is recorded as future work. The rollback in this plan is unsetting the variable.
- A prebuilt droplet snapshot (Q4). This becomes a later plan, only if boot overhead proves too high.
- Measuring consumer runner minutes before building (Q2 B).

## Decisions

User answers, 2026-11-18.

### D1 — Which repositories move (Q1 A)

- **Chosen:** the 7 consumers in `.github/ai/consumer_repos.json`.
- **Alternatives considered:** every private repository running the pipeline.
- **Why:** these are the registered consumers. Other repositories can be added to the scaler's allowlist later without code changes.

### D2 — Measure first (Q2 B)

- **Chosen:** build without a baseline measurement.
- **Alternatives considered:** measure each consumer's runner minutes and slot share first.
- **Why:** the user's choice. The R5 rollout check measures the result instead.

### D3 — Which jobs move (Q3 A, narrowed by Q17 A)

- **Chosen:** every job of the reusable workflows, switched by the calling repo's variable. The exceptions are `workflow_failure_heal.yml` and `check_failure_triage.yml`, which always stay on GitHub-hosted runners.
- **Alternatives considered:** long AI-engine jobs only; heal and triage moving too.
- **Why:** the repair path must never depend on the fleet it may be repairing, so that a broken fleet cannot start a heal loop.

### D4 — How droplets get their tools (Q4 A)

- **Chosen:** the stock `ubuntu-26-04-x64` image plus a versioned boot script in this repository, run as cloud-init user data.
- **Alternatives considered:** a snapshot image from day one.
- **Why:** there is no image to maintain by hand. A scheduled snapshot bake can be added later if boot time hurts.

### D5 — Where the scaler runs (Q5 A)

- **Chosen:** a DigitalOcean App Platform service.
- **Alternatives considered:** a droplet running the scaler as a system service.
- **Why:** App Platform includes managed restarts and an HTTPS endpoint for the webhook.

### D6 — Warm pool (Q6 A)

- **Chosen:** no pre-booted droplets; one droplet per job.
- **Alternatives considered:** 2–3 waiting droplets.
- **Why:** nothing is paid for idle time. The rollout check (R5) measures boot overhead before a warm pool is considered.

### D7 — Droplet size (Q11 C)

- **Chosen:** `s-4vcpu-8gb` (4 vCPU / 8 GB, $0.07143/hr, about $0.0012/min).
- **Alternatives considered:** `s-2vcpu-8gb-amd`, `s-2vcpu-4gb`.
- **Why:** it has the same 8 GB as GitHub's private-repo runner, with twice its vCPUs, for about 15% more per minute than `s-2vcpu-8gb-amd`.

### D8 — Concurrency cap (Q12 C)

- **Chosen:** at most 60 scaler droplets at once.
- **Alternatives considered:** 30, 15.
- **Why:** 60 × $0.07143/hr is $4.29/hr, so even pegged at the cap for a whole month the fleet stays under about $3,100.

### D9 — Scaler code location and deploy (Q13 A)

- **Chosen:** this repository under `tools/runner_scaler/` (D16), deployed by App Platform from `stable`.
- **Alternatives considered:** a new private repository deploying on every push to `main`.
- **Why:** a scaler change ships only after `test-and-mark-stable` passes.

### D10 — Failure handling (Q14 A, Q18 A)

- **Chosen:**
  - a queued-job watchdog that alerts on Telegram, with rollback by unsetting `PIPELINE_RUNS_ON`;
  - a bounded re-run of jobs whose runner was lost (`run_attempt` 1 only);
  - a heal-intake skip for those failures.
- **Alternatives considered:** automatic fallback to GitHub-hosted runners (future work); alert only, with no re-run.
- **Why:** every automatic action is bounded by GitHub's own `run_attempt` counter, and the repair path never runs on the fleet, so no failure can feed itself.

### D11 — Rollout (Q15 B)

- **Chosen:** all 7 consumers at once.
- **Alternatives considered:** one consumer first for 7 days.
- **Why:** the user's choice. The per-consumer variable keeps the rollback immediate.

### D12 — Phase split (Q16 A)

- **Chosen:** two PRs, each independently mergeable.
- **Alternatives considered:** one PR with all of it.
- **Why:** Phase 1 is a no-op until a consumer sets the variable, and Phase 2 is a no-op until the app is deployed, so neither depends on the other.

### D13 — Assumptions taken without override (Q7–Q10, Q19–Q21)

- **Chosen:**
  - Python for the scaler (Q7).
  - A GitHub App for webhooks and runner registration (Q8).
  - Region `nyc3` (Q9).
  - Rollout checked by an interactive Actions API and billing read, with no committed reporting code (Q10).
  - Only the exact value `do-runner` routes to droplets, so a typo falls back to GitHub (Q19).
  - The plan PR goes on the session branch (Q20).
  - Phase 1 touches 17 reusable workflows with 30 `runs-on:` lines: the 19 that consumers call (every `workflow_call` workflow except `ci.yml`) minus the two Q17 exceptions (Q21).
- **Alternatives considered:** Go (Q7); a PAT with one webhook per repo (Q8); another region (Q9); committed reporting code (Q10); passing any value through (Q19); a `claude/write-plan-*` branch (Q20).
- **Why:** each follows from repo conventions or from an earlier answer. All were listed as assumptions and none was overridden.

### D14 — Runner network (Q23 A)

- **Chosen:** every runner droplet is created in a dedicated VPC: the droplet-create request always passes `vpc_uuid` = `RUNNER_SCALER_VPC_UUID`. The variable defaults to empty (§4); with `RUNNER_SCALER_REPOS` also empty (its default) the scaler serves nothing, so the default is safe. While repositories are configured and the VPC is empty, the scaler refuses to start (it logs `RUNNER_SCALER outcome=skipped reason=vpc_unset` and exits non-zero), so it fails closed instead of using the default VPC.
- **Alternatives considered:** the region's default VPC.
- **Why:** the account already runs other droplets. A compromised job on a runner in the default VPC could reach them over private IPs.

### D15 — coding-workflows itself (Q24 C)

- **Chosen:** coding-workflows never moves to the droplet fleet. Its `ci.yml`, release workflows and its own runs of the reusable workflows stay on GitHub-hosted runners. The `runs-on` expression excludes this repository by `github.repository` (so its jobs never request `do-runner`, whatever the variable says), and the scaler's `config.py` drops it from `RUNNER_SCALER_REPOS` at load with a `reason=self_repo_excluded` log line, even if it is listed.
- **Alternatives considered:** moving it after 7 days of consumer use; moving it now in a Phase 3.
- **Why:** it is public, so its hosted minutes cost nothing; its `CI` at about 56,000 runner-minutes a day would cost about $2,000–2,400 a month on droplets; and its fork PRs would need same-repo-only routing. Run-volume reduction stays the remedy for its share of the queue.

### D16 — Scaler location (implementation)

- **Chosen:** the service lives at `tools/runner_scaler/`, not `runner_scaler/`.
- **Alternatives considered:** a top-level `runner_scaler/` directory.
- **Why:** deployed services already live under `tools/` (`tools/claude-pool-broker/`). The App Platform build context is then the service directory alone.

## Constraints

- **§1 security:** droplets receive the job's secrets (`GH_PAT`, `OPENROUTER_API_KEY` and others) exactly as GitHub-hosted runners do, so each droplet:
  - runs one job only;
  - is destroyed afterwards;
  - holds no DigitalOcean or GitHub App credential of its own;
  - accepts no inbound traffic (cloud firewall on tag `gh-runner`);
  - sits in a dedicated VPC, never the region's default one (D14).

  The scaler holds the only privileged credentials. Webhooks are rejected unless their `X-Hub-Signature-256` HMAC verifies.
- **§4:** every new env var and repo variable has a default. Unset `PIPELINE_RUNS_ON` means GitHub-hosted. Unset `RUNNER_SCALER_REPOS` means the scaler serves no repository. Every `RUNNER_SCALER_*` variable and its default is listed once in Approach, "Scaler configuration"; `RUNNER_SCALER_VPC_UUID` defaults to empty and fails closed as D14 describes.
- **§6:** no identifier is renamed or removed. The new names `PIPELINE_RUNS_ON`, `do-runner`, `gh-runner`, `gh-runner-registered`, `tools/runner_scaler/` (with its package `runner_scaler`), `RUNNER_SCALER_*` and the `RUNNER_SCALER` log prefix were checked against the repository and do not collide. Job ids and names stay unchanged, because log parsers, heal rules and required checks cite them (§33.B).
- **§14:** Phase 1 reaches consumers on their next `@stable` sync (`update_workflows.yml`). No consumer wrapper file changes. A consumer opts in only by setting its own variable.
- **§15:** the scaler's GitHub API use is webhook-driven, with bounded reconcile reads (see Approach). The heal-intake change adds at most 10 annotation reads per reported run, only for failed jobs labelled `do-runner`, reuses the jobs document the intake already saves, and fails open towards filing.
- **§18:** see the §18.E section above.
- **§20 / §30:** each phase ships one `changelog.d/` fragment and one `agents.d/` fragment. README env var tables are edited in place.
- **§22:** creating the firewall, the App Platform app, its env vars and the Uptime check is provisioning, so each step is ask-first (§22.B). The resulting IDs go in the agents file's `## DigitalOcean resources` table through an `agents.d/` fragment (§22.C). Committed code never reads the session's `DIGITALOCEAN_ACCESS_TOKEN`: the scaler uses its own scoped token.
- **§23.C:** setting `PIPELINE_RUNS_ON` on consumer repositories is an administrative write, ask-first at rollout.
- **§27:** `implement.yml` is 440,172 bytes and `review_autofix.yml` is 409,603 bytes. Phase 1 adds about 130 bytes per `runs-on:` line (2 and 7 lines, so under 1,000 bytes per file). Check both with `wc -c` after the edit; they must stay under 480,000.
- **§33:** no job, workflow, matrix leg or trigger is added. Jobs per triggering event are unchanged: before and after, every routed job is still one job. For an opted-in consumer, 0 of them hold a GitHub-hosted slot afterwards.

## Approach

### Runner selection (Phase 1)

Every `runs-on: ubuntu-latest` line in the 17 routed workflows becomes:

```yaml
runs-on: ${{ vars.PIPELINE_RUNS_ON == 'do-runner' && github.repository != 'shubhodeep1/coding-workflows' && 'do-runner' || 'ubuntu-latest' }}
```

`security-audit.yml`'s job keeps its own fallback (`|| 'ubuntu-24.04'`). The `vars` and `github` contexts are both available in `jobs.<id>.runs-on`, and inside a called workflow both resolve to the caller: `vars` to the caller repository's variables and `github.repository` to the caller repository. The `github.repository` guard is what holds G8 for this repository's own `internal-*.yml` wrappers, which call the routed workflows: even with `PIPELINE_RUNS_ON=do-runner` set here, every job resolves to its hosted runner. Unset or any other value makes the first operand false, so the expression yields the previous runner (G2).

| Workflow | `runs-on:` lines |
| --- | --- |
| `review_autofix.yml` | 7 |
| `clarify.yml`, `implement.yml`, `issue_pr_status.yml`, `orchestrate_poll.yml`, `plan.yml`, `update_workflows.yml`, `validate.yml` | 2 each |
| `cancel_on_pr_close.yml`, `cs_investigate.yml`, `memory_maintenance.yml`, `orchestrate.yml`, `orchestrate_clarify_respond.yml`, `security-audit.yml`, `sync_ai_labels.yml`, `unblock_judge.yml`, `workflow_integrity.yml` | 1 each |

### Heal-intake skip (Phase 1)

`scripts/workflow_failure_heal_intake.sh` already saves `repos/<source>/actions/runs/<id>/jobs` to `JOBS_FILE` inside its per-run loop (`while ... done < <(jq -r '.run_refs[] | ...')`, around lines 1760–1837), so the skip needs no new jobs call.

The existing `skip_reason()` calls (around lines 618 and 627) run before any jobs data exists, so the `runner_lost` decision is **not** made there and `skip_reason()` is left unchanged. It is a separate, later check:

1. A new helper, `runner_lost_failure(jobs_doc, annotations_by_job)` in `scripts/workflow_failure_heal.py`, returns true when the run has at least one failed job, every failed job has the label `do-runner`, and each of them has a check-run annotation matching either message (plain `re.search`; an exception in the matcher counts as no match):
   - `The self-hosted runner: .* lost communication with the server`
   - `The runner has received a shutdown signal`
2. A new CLI subcommand, `runner-lost --jobs-json <file> --annotations-json <file>`, wraps it and prints `runner_lost` or nothing.
3. Inside the per-run loop, right after `JOBS_FILE` passes its existing validity check, the intake reads `GET repos/<source>/check-runs/<job_id>/annotations` for each failed job labelled `do-runner` (at most 10 per run) into `${LOG_DIR}/run-<id>-annotations.json`, and records the `runner-lost` result for that run.
4. After the loop ends, and before the heal issue is filed, the intake skips with `log "skip reason=runner_lost ..."` and `exit 0` only when every kept `run_refs` entry returned `runner_lost`. A payload with several `run_refs` where any run lacks the evidence is reported as today.
5. A run that mixes a lost `do-runner` job with any other failed job (on any runner) is reported as today, and the scaler does not re-run it (G6), so the two sides apply the same rule.
6. If any annotations read fails, or any failed job lacks the evidence, the run is reported as today (fail-open towards filing). Earlier kind-specific routes (synced findings, validation routing, the non-retryable PR diagnosis) are unchanged.

### Scaler service (Phase 2)

The package is `tools/runner_scaler/runner_scaler/`, Python 3.12, with dependencies pinned in `tools/runner_scaler/requirements.txt`: `PyJWT` and `cryptography`, imported lazily so unit tests need neither.

**Scaler configuration.** Every variable has a default (§4). `config.py` parses them once at startup, and `app.yaml`, the README (step 2.7) and the tests (step 2.6) list the same names.

| Variable | Default | Meaning |
| --- | --- | --- |
| `RUNNER_SCALER_REPOS` | empty | Comma-separated `owner/repo` allowlist. Empty serves nothing. `shubhodeep1/coding-workflows` is always dropped at load (D15). |
| `RUNNER_SCALER_VPC_UUID` | empty | VPC passed as `vpc_uuid` on every droplet create (D14). Empty while `RUNNER_SCALER_REPOS` is non-empty: refuse to start. |
| `RUNNER_SCALER_MAX_DROPLETS` | `60` | Cap on `gh-runner` droplets at once (D8). |
| `RUNNER_SCALER_DROPLET_SIZE` / `_REGION` / `_IMAGE` | `s-4vcpu-8gb` / `nyc3` / `ubuntu-26-04-x64` | Droplet shape (D7, D13, D4). |
| `RUNNER_SCALER_RECONCILE_SECONDS` | `120` | Timer pass over all repositories. |
| `RUNNER_SCALER_WEBHOOK_DEBOUNCE_SECONDS` | `30` | Minimum spacing of webhook-triggered passes, per repository. |
| `RUNNER_SCALER_FULL_SCAN_SECONDS` | `600` | Backstop scan interval; one scan also runs at startup. |
| `RUNNER_SCALER_SCAN_MAX_JOB_READS` | `20` | Jobs reads per repository per full scan. |
| `RUNNER_SCALER_REGISTER_TIMEOUT_SECONDS` | `900` | Unregistered droplet is a boot failure after this. |
| `RUNNER_SCALER_IDLE_TIMEOUT_SECONDS` | `300` | Registered runner idle with no queued job for its repo is removed after this. |
| `RUNNER_SCALER_MAX_LIFETIME_MINUTES` | `300` | Hard lifetime of any `gh-runner` droplet. |
| `RUNNER_SCALER_BREAKER_CONSECUTIVE` / `_WINDOW_MINUTES` / `_FAIL_RATIO` / `_COOLDOWN_MINUTES` | `3` / `15` / `0.5` / `15` | Circuit breaker. |
| `RUNNER_SCALER_RERUN_ENABLED` | `true` | Lost-runner re-run (G6). |
| `RUNNER_SCALER_QUEUE_ALERT_MINUTES` | `10` | Queued-without-supply alert threshold (G7). |
| `RUNNER_SCALER_DRY_RUN` | `false` | Log decisions, call no write endpoint. |

The secrets (below) have no value in code; with `RUNNER_SCALER_REPOS` non-empty and any of them empty, the scaler refuses to start, like the VPC rule.

**HTTP server**
- `POST /webhook` accepts `workflow_job` and `workflow_run` events from the GitHub App. It verifies `X-Hub-Signature-256` against `RUNNER_SCALER_WEBHOOK_SECRET`, caps bodies at 1 MiB, and drops events for repositories not in the allowlist. `workflow_job` events whose `labels` are not exactly `["do-runner"]` are dropped too.
- `GET /healthz` returns 200 while the reconcile loop has completed a pass within 3 intervals.

**Reconcile, level-triggered.** Passes are serialised by one in-process lock, so two webhooks never run overlapping passes. A timer pass covers every allowlisted repository every `RUNNER_SCALER_RECONCILE_SECONDS`. An accepted webhook triggers a pass for **its own repository only**, debounced per repository to one per `RUNNER_SCALER_WEBHOOK_DEBOUNCE_SECONDS`. For each repository in the pass:
- `queued` is the set of queued `do-runner` job ids, keyed by job id, so duplicate or replayed `queued` deliveries collapse. Webhook events add ids (`queued`) and remove them (`in_progress`, `completed`).
- The full scan, every `RUNNER_SCALER_FULL_SCAN_SECONDS` and once at startup, lists the repository's runs with `status=queued` **and** with `status=in_progress` (a run whose first jobs started still has queued jobs), then reads jobs for those runs, oldest first, up to `RUNNER_SCALER_SCAN_MAX_JOB_READS`. It **merges** into `queued`: it adds every queued `do-runner` job it saw and removes ids it read as no longer queued; ids it did not read are kept. When runs are left unread it logs `reason=scan_truncated` and continues from the next run on the following scan (round-robin cursor), so a backlog is covered over successive scans.
- `supply` is the number of that repository's droplets that are booting (created but not yet registered, including droplets this instance created in the current or previous pass that the droplet list does not show yet), plus its `do-runner` runners that are online and idle.
- The scaler creates `min(queued − supply, RUNNER_SCALER_MAX_DROPLETS − total_droplets)` droplets, or none while the breaker is open.

**Droplet creation**
1. Get a single-use runner registration: `POST /repos/{owner}/{repo}/actions/runners/generate-jitconfig` with `name=<droplet name>`, `runner_group_id=1`, `labels=["do-runner"]`.
2. Create the droplet:
   - name `do-runner-<repo-slug>-<8 hex>`;
   - size `RUNNER_SCALER_DROPLET_SIZE`, region `RUNNER_SCALER_REGION`, image `RUNNER_SCALER_IMAGE`;
   - `vpc_uuid` = `RUNNER_SCALER_VPC_UUID` (always passed; D14);
   - tags `gh-runner` and `gh-runner-repo-<slug>`;
   - `user_data` = `tools/runner_scaler/bootstrap/runner-boot.sh` with the JIT config inserted.

**Registration marker.** The first time a pass sees a droplet's runner online in the runners list, or a `workflow_job` event names the droplet as `runner_name`, the scaler adds the tag `gh-runner-registered` to that droplet. The marker lives on the droplet, so it survives a scaler restart.

**Droplet destruction**
- On `workflow_job` `completed` for that runner's name. Not a boot failure.
- For a droplet whose status is `off`: before classifying, the pass handles all pending webhook events. If the droplet carries `gh-runner-registered`, it ended normally (boot step 4) and is destroyed without counting anything. Without the marker, the self-check failed (boot step 3) and it counts as a boot failure.
- For a droplet without `gh-runner-registered` older than `RUNNER_SCALER_REGISTER_TIMEOUT_SECONDS`: destroyed, and counted as a boot failure.
- For a droplet whose runner has been online and idle for longer than `RUNNER_SCALER_IDLE_TIMEOUT_SECONDS` while its repository has no queued `do-runner` job (the job was cancelled before assignment): the scaler deletes the runner (`DELETE /repos/{o}/{r}/actions/runners/{id}`), then destroys the droplet. Not a boot failure.
- For any `gh-runner` droplet older than `RUNNER_SCALER_MAX_LIFETIME_MINUTES` (the longest job timeout of 240 minutes plus margin).

A residual case remains: a job short enough to start and finish between two passes, with every webhook for it lost, leaves an `off` droplet without the marker and counts one false boot failure. One such count cannot open the breaker on its own (3 in a row or a majority of at least 4 boots); this is accepted.

**Circuit breaker**
- Opens after `RUNNER_SCALER_BREAKER_CONSECUTIVE` boot failures in a row, or when the failure ratio over `RUNNER_SCALER_BREAKER_WINDOW_MINUTES` exceeds `RUNNER_SCALER_BREAKER_FAIL_RATIO`, with at least 4 boots in the window.
- While open it creates no droplets. After `RUNNER_SCALER_BREAKER_COOLDOWN_MINUTES` it allows one trial droplet: success (the droplet gets `gh-runner-registered`) closes the breaker, failure re-opens it.

**Lost-runner re-run (G6).** GitHub accepts a re-run only after the whole run has completed, so the scaler acts on the run, not the job:
- Trigger: `workflow_run` `completed` with `conclusion: failure` and `run_attempt` 1 in an allowlisted repository. As a backstop for missed webhooks, each full scan also lists that repository's failed runs created in the last 30 minutes (1 call) and handles any still at `run_attempt` 1.
- Evidence: one jobs read for the run. If any failed job ran on a `do-runner-*` runner, the scaler reads the annotations of those jobs (at most 10).
- Action: only when every failed job of the run ran on a `do-runner-*` runner and has a matching lost-runner annotation (the same rule as the heal intake), and `RUNNER_SCALER_RERUN_ENABLED` is `true`, the scaler calls `POST /repos/{o}/{r}/actions/runs/{run_id}/rerun-failed-jobs`. A run that mixes a lost job with any other failure is not re-run (`reason=mixed_failures`); the heal intake reports it as today.
- Bound: it never re-runs at `run_attempt` 2 or more, and logs `rerun_given_up` instead. The re-run itself moves the run to attempt 2, so `run_attempt` is also the idempotency key: a duplicate event, the backstop scan or a restart cannot re-run the same run twice.

**Restart behaviour.** The in-process state is the queued set, the breaker history and the alert cooldowns, and a restart (every `stable` release redeploys the scaler) loses it:
- the queued set is rebuilt by the startup full scan;
- the breaker starts half-open: one trial droplet, and scale-out resumes once it registers. A restart during a failing fleet therefore costs at most one droplet, and a healthy restart delays scale-out by one boot;
- alert cooldowns start empty, so an incident still open may be alerted once more. Accepted.

**Alerts**
- A Telegram message through the Bot API using `TG_BOT_SECRET` and `TG_ADMIN_CHAT_ID`, the same names as `scripts/tg_helpers.sh`, deduplicated by incident key with a 60-minute cooldown.
- Incident kinds: `breaker_open`, `queued_without_supply` (a `do-runner` job queued longer than `RUNNER_SCALER_QUEUE_ALERT_MINUTES` with no booting droplet for its repo), `cap_reached`, `do_api_error`, `rerun_given_up` (a lost runner at `run_attempt` ≥ 2).

**Logging** is one structured line per decision, under the stable prefix `RUNNER_SCALER`:

```text
RUNNER_SCALER outcome=<created|destroyed|skipped|rerun|alert> reason=<...> repo=<owner/repo> droplet=<name> job=<id>
```

**Dry run.** `RUNNER_SCALER_DRY_RUN=true` logs every decision and calls no write endpoint.

**Secrets** are App Platform encrypted env vars: `RUNNER_SCALER_DO_TOKEN`, `RUNNER_SCALER_GH_APP_ID`, `RUNNER_SCALER_GH_APP_PRIVATE_KEY`, `RUNNER_SCALER_WEBHOOK_SECRET`, `TG_BOT_SECRET` and `TG_ADMIN_CHAT_ID`. The DigitalOcean token is a custom-scoped token limited to droplet create, read and delete, plus tag create and read (tag create also covers adding `gh-runner-registered` to a droplet).

**API budget (§15)**, worst case per hour for 7 repositories at the defaults:

| Source | Calls | Worst case / hour |
| --- | --- | --- |
| GitHub, webhook-triggered pass (own repository only, ≤ 1 per 30 s per repository) | 1 runners list | 840 |
| GitHub, timer pass (every 120 s, all repositories) | 1 runners list per repository | 210 |
| GitHub, full scan (every 600 s, per repository) | 2 runs lists (queued, in_progress) + ≤ 20 jobs reads + 1 failed-runs list | 966 |
| GitHub, per droplet | 1 JIT config, at most 1 runner delete | ≤ 480 (60 droplets turning over every 15 minutes) |
| GitHub, per failed run with a `do-runner` failure (webhook or backstop) | 1 jobs read + ≤ 10 annotations reads + ≤ 1 re-run | ≤ 600 (assumes ≤ 50 such runs an hour) |
| DigitalOcean, per pass | 1 droplets-by-tag list | ≤ 870 |
| DigitalOcean, per droplet | 1 create, ≤ 1 tag, 1 delete | ≤ 720 |

The GitHub worst case is about 3,100 calls an hour and steady state far less, within the GitHub App installation limit of at least 5,000 an hour; DigitalOcean stays within its 5,000-an-hour limit. A rate-limit response skips that pass and is logged (`reason=rate_limited`), with no tight retry loop.

### Droplet boot script (Phase 2)

`tools/runner_scaler/bootstrap/runner-boot.sh` runs as cloud-init user data:

1. **Set up the runner user.** Create user `runner` with passwordless sudo and membership of the `docker` group, like GitHub-hosted runners.
2. **Install packages.** Install, with 3 attempts and backoff:
   - Ubuntu packages from the image's signed Ubuntu archive (not version-pinned): `docker.io`, `git`, `jq`, `curl`, `ca-certificates`, `unzip`, `build-essential`, `python3`, `python3-pip`, `python3-venv`, `ripgrep`, `gawk`, `shellcheck`;
   - `gh` from GitHub's signed apt repository (not version-pinned);
   - the `actions/runner` release at a pinned version, checked against its SHA-256. This is the only version-pinned, checksummed artefact, and the only one the static test checks for a pin.

   The list comes from a static inventory of commands the 17 routed workflows and their scripts call (step 2.1).
3. **Run the self-check.** It confirms `docker run --rm hello-world`, `python3 -m venv`, `gh --version`, `jq`, `shellcheck`, `git` and a free-disk threshold. On any failure the script writes the reason to the serial console log and runs `shutdown -h now` without registering.
4. **Run one job.** Add an `iptables -I OUTPUT -d 169.254.169.254 -m owner --uid-owner runner -j DROP` rule so job code running as `runner` cannot re-read the user data (and the JIT config) from the metadata service. This is defence in depth only: `runner` has passwordless sudo, so a job can remove the rule, and the risk stays accepted (Risks). Then start the runner as `runner` with exactly `./run.sh --jitconfig "${JIT_CONFIG}"` (GitHub's documented just-in-time invocation; there is no separate `config.sh` step). When it exits, power off; the droplet carries `gh-runner-registered`, so the scaler destroys it without counting a boot failure.

## Phases & Merge Strategy

Every merge ships to production. The two phases are independent: either can merge first, and each is a complete, safe state on its own.

1. **Phase 1: runner switch in the reusable workflows, and the heal-intake skip.**
   - **Files:** the 17 routed workflow files under `.github/workflows/`, `scripts/workflow_failure_heal.py`, `scripts/workflow_failure_heal_intake.sh`, new tests, README env var row, `changelog.d/` and `agents.d/` fragments.
   - **Done when:**
     - with no consumer setting `PIPELINE_RUNS_ON`, every job resolves to its old runner;
     - the new test pins the expression (with its coding-workflows guard) in every routed job, its resolution for the unset, `do-runner`, typo and coding-workflows cases, and its absence from `ci.yml`, `workflow_failure_heal.yml` and `check_failure_triage.yml`;
     - the `runner_lost` skip is covered by tests;
     - full CI is green.
   - **Rollback:** revert the PR. No consumer state depends on it, and a consumer can also unset its variable.
2. **Phase 2: scaler service, droplet boot script, App Platform spec and docs.**
   - **Files:** everything under `tools/runner_scaler/`, new tests under `tests/`, `docs/scripts-pending-removal.md`, README, `changelog.d/` and `agents.d/` fragments.
   - **Done when:**
     - unit tests cover signature checks, reconcile arithmetic, the cap, idempotency under duplicate and replayed events, restart, the breaker, the re-run bound, the cleanup passes and the boot script's static contract;
     - `shellcheck` passes on the boot script;
     - full CI is green.

     Merging deploys nothing. The App Platform app does not exist until rollout step R3, and with `RUNNER_SCALER_REPOS` unset the scaler serves no repository.
   - **Rollback:** revert the PR. If the app is already deployed, the next `stable` release redeploys the reverted tree, or the app is scaled to zero (ask-first).

## Implementation Steps

### Phase 1

1.1. In each of the 17 routed workflows, replace every job's `runs-on: ubuntu-latest` with the expression from Approach. In `security-audit.yml`, replace `runs-on: ubuntu-24.04` with the same expression, falling back to `'ubuntu-24.04'`. Change no job id, name, `if:`, `timeout-minutes:` or step.

1.2. Run `wc -c` on `implement.yml` and `review_autofix.yml` and confirm both stay under 480,000 bytes (§27).

1.3. Add `tests/test_pipeline_runs_on_switch.py`. It parses every `.github/workflows/*.yml` that has `workflow_call`, and asserts:
- every job in the 17 routed files uses the exact expression, including the `github.repository != 'shubhodeep1/coding-workflows'` guard, with its previous runner as fallback (`ubuntu-24.04` for `security-audit.yml` only);
- a table-driven case mirrors the expression in Python and asserts the resolved runner for: variable unset or empty (previous runner, G2), `do-runner` (`do-runner`), a typo such as `do_runner` (previous runner), and `do-runner` with `github.repository` = `shubhodeep1/coding-workflows` (previous runner, G8). The test also asserts that the workflow text matches the mirrored form, so the two cannot drift;
- `ci.yml`, `workflow_failure_heal.yml`, `check_failure_triage.yml` and every non-reusable workflow (for example `drift-audit.yml` and `review_autofix_sweep.yml`, which keep `ubuntu-24.04`) contain no `PIPELINE_RUNS_ON`;
- `workflow-templates/*.yml` contain no `PIPELINE_RUNS_ON`.

The `tests-system-python` catch-all collects it, so no wiring edit is needed (`tests/test_ci_wires_every_test_file.py`).

1.4. In `scripts/workflow_failure_heal.py`, add `runner_lost_failure(jobs_doc, annotations_by_job)` and the `runner-lost` CLI subcommand next to `skip-reason`. `skip_reason()` and its callers are unchanged: they run before any jobs data exists (Approach, "Heal-intake skip").

1.5. In `scripts/workflow_failure_heal_intake.sh`:
- inside the per-run loop, right after the existing `JOBS_FILE` validity check (around line 1776), fetch annotations for at most 10 failed jobs labelled `do-runner` into `${LOG_DIR}/run-<id>-annotations.json`, run `runner-lost` on the pair, and record the result per run;
- after the loop ends (around line 1837) and before the heal issue is filed, skip with `skip reason=runner_lost` and `exit 0` only when every kept `run_refs` entry returned `runner_lost`;
- any API error leaves that run's annotations file empty, so the payload is reported as before;
- log `WORKFLOW_FAILURE_HEAL_RUNNER_LOST outcome=<skipped|not_runner_lost|lookup_failed> run=<id>`;
- add a comment above the annotations call naming the existing calls audited, per §15.

1.6. Extend `tests/test_workflow_failure_heal.py` with cases for:
- all failed jobs lost on `do-runner` → `runner_lost`;
- one lost `do-runner` job plus one failed job on `ubuntu-latest` → not skipped;
- one lost `do-runner` job plus one ordinary `do-runner` failure → not skipped;
- an annotation that doesn't match → not skipped;
- an empty annotations file → not skipped;
- a run with no failed job → not skipped.

Add an intake-level case (in the existing intake test that stubs `gh`) for a payload with two `run_refs` where only one is `runner_lost` → reported, not skipped.

1.7. README: add a `PIPELINE_RUNS_ON` row to the repo-variable table (default unset; valid values `do-runner` and unset; behaviour; the heal and triage exception; the rollback).

1.8. Add a `changelog.d/<pr>-pipeline-runs-on-switch.md` fragment (`added`) and an `agents.d/<pr>-pipeline-runs-on-switch.md` fragment. The agents fragment covers the variable in the workflow architecture section, and the new log prefix in `## Stable log prefixes (contractual)` with its `LOG_PREFIX.name=` line.

### Phase 2

2.1. Inventory the commands the 17 routed workflows and the scripts they call depend on, beyond what their own setup steps install. Record the result as the package list and self-check list in `tools/runner_scaler/bootstrap/runner-boot.sh`, with a comment naming the inventory method so it can be repeated.

2.2. Write `tools/runner_scaler/bootstrap/runner-boot.sh` as described in Approach, using tab indentation and passing `shellcheck`. The JIT config placeholder is a single literal token that `runner_scaler` replaces; it never appears in logs.

2.3. Write the `runner_scaler` package under `tools/runner_scaler/runner_scaler/`:
- `__main__.py`: startup (config load, startup full scan, breaker half-open), SIGTERM handling;
- `server.py`: `/webhook` (`workflow_job` and `workflow_run`) and `/healthz`;
- `reconcile.py`: the serialised pass, per-repository debounce, the merging full scan, desired-count logic, the registration marker and the cleanup passes (off, unregistered, idle, lifetime);
- `breaker.py`;
- `rerun.py`: the run-level re-run rule and its backstop;
- `github_app.py`: JWT, installation token, JIT config, runners list and delete, runs, jobs, annotations, `rerun-failed-jobs`;
- `do_client.py`: droplet create (with `vpc_uuid`), list by tag, tag and delete;
- `alerts.py`: Telegram with deduplication;
- `config.py`: every variable in Approach, "Scaler configuration", with its default; the coding-workflows exclusion (D15); the refuse-to-start rules for an empty VPC or secret while repositories are configured (D14).

Every external call has a timeout and returns a typed error. No call retries in a tight loop.

2.4. Write `tools/runner_scaler/app.yaml`, the App Platform spec:
- one service of the smallest instance size, built from `source_dir: tools/runner_scaler` on branch `stable` with `deploy_on_push: true`;
- `run_command: python -m runner_scaler`;
- an HTTP health check on `/healthz`;
- env var declarations for every variable in Approach, "Scaler configuration" (including `RUNNER_SCALER_MAX_DROPLETS` and `RUNNER_SCALER_VPC_UUID`) with its default, and encrypted placeholders for the secrets (no values).

2.5. Write `tools/runner_scaler/requirements.txt` with pinned versions.

2.6. Add tests under `tests/`, collected by `tests-system-python`, with stubbed GitHub and DigitalOcean clients:
- `test_runner_scaler_webhook.py`: signature checks, body cap, allowlist and label filtering
- `test_runner_scaler_reconcile.py`: the cap (`RUNNER_SCALER_MAX_DROPLETS`), duplicate and replayed `queued` deliveries creating one droplet, a just-created droplet counted as supply before the list shows it, the full scan finding a queued job inside an `in_progress` run and merging rather than replacing, scan truncation and the cursor, `vpc_uuid` on every create, an `off` droplet with `gh-runner-registered` not counted as a boot failure (including when `off` is seen before `completed`), an `off` droplet without it counted, and the idle rule for a job cancelled before assignment
- `test_runner_scaler_config.py`: every default, coding-workflows dropped from `RUNNER_SCALER_REPOS`, refuse-to-start on an empty VPC or secret while repositories are configured, and start with nothing configured
- `test_runner_scaler_breaker.py`: opening rules, cooldown trial, half-open at startup
- `test_runner_scaler_rerun.py`: re-run only on `workflow_run` `completed` at `run_attempt` 1 with every failed job lost; mixed failures not re-run; `run_attempt` 2 gives `rerun_given_up`; a duplicate event or the backstop does not re-run twice
- `test_runner_scaler_bootstrap.py`: static checks for the pinned runner version and SHA-256, the self-check list, power-off on failure, the exact `./run.sh --jitconfig` invocation, the metadata `iptables` rule before it, and no credential variables

2.7. README: add a section "DigitalOcean runners for consumer pipelines" covering:
- the architecture;
- every `RUNNER_SCALER_*` env var with its default;
- the GitHub App permissions:
  - repository: Actions read and write, Administration read and write, Checks read (the check-run annotations reads), Metadata read;
  - webhook events: Workflow job, Workflow run;
- the scoped DigitalOcean token scopes;
- the failure modes and the rollback.

2.8. Add an `agents.d/<pr>-runner-scaler.md` fragment (operational runbook, log prefix registry entry `RUNNER_SCALER`) and a `changelog.d/<pr>-runner-scaler.md` fragment (`added`).

2.9. Add the `docs/scripts-pending-removal.md` entry:

```markdown
### `tools/runner_scaler/` + `tools/runner_scaler/app.yaml` + `tools/runner_scaler/bootstrap/runner-boot.sh`

- **Introduced in:** #<pr> (<date>)
- **Type:** supervisor
- **Removal trigger:** when no repository has set `PIPELINE_RUNS_ON=do-runner` for 30 days; otherwise permanent — review annually.
- **Removal preflight checks:**
  - `gh api repos/<repo>/actions/variables/PIPELINE_RUNS_ON` returns 404 for every repository in `RUNNER_SCALER_REPOS`.
  - `GET /v2/droplets?tag_name=gh-runner` returns `meta.total` 0.
  - No `workflow_job` with label `do-runner` in any of those repositories in the last 30 days (Actions jobs API).
- **Owner:** @shubhodeep1
```

## Files & Modules

Phase 1:
- `.github/workflows/cancel_on_pr_close.yml`
- `.github/workflows/clarify.yml`
- `.github/workflows/cs_investigate.yml`
- `.github/workflows/implement.yml`
- `.github/workflows/issue_pr_status.yml`
- `.github/workflows/memory_maintenance.yml`
- `.github/workflows/orchestrate.yml`
- `.github/workflows/orchestrate_clarify_respond.yml`
- `.github/workflows/orchestrate_poll.yml`
- `.github/workflows/plan.yml`
- `.github/workflows/review_autofix.yml`
- `.github/workflows/security-audit.yml`
- `.github/workflows/sync_ai_labels.yml`
- `.github/workflows/unblock_judge.yml`
- `.github/workflows/update_workflows.yml`
- `.github/workflows/validate.yml`
- `.github/workflows/workflow_integrity.yml`
- `scripts/workflow_failure_heal.py`
- `scripts/workflow_failure_heal_intake.sh`
- `tests/test_pipeline_runs_on_switch.py` [new]
- `tests/test_workflow_failure_heal.py`
- `README.md`
- `changelog.d/<pr>-pipeline-runs-on-switch.md` [new]
- `agents.d/<pr>-pipeline-runs-on-switch.md` [new]


Phase 2:
- `tools/runner_scaler/runner_scaler/__init__.py` [new]
- `tools/runner_scaler/runner_scaler/__main__.py` [new]
- `tools/runner_scaler/runner_scaler/config.py` [new]
- `tools/runner_scaler/runner_scaler/server.py` [new]
- `tools/runner_scaler/runner_scaler/reconcile.py` [new]
- `tools/runner_scaler/runner_scaler/breaker.py` [new]
- `tools/runner_scaler/runner_scaler/rerun.py` [new]
- `tools/runner_scaler/runner_scaler/github_app.py` [new]
- `tools/runner_scaler/runner_scaler/do_client.py` [new]
- `tools/runner_scaler/runner_scaler/alerts.py` [new]
- `tools/runner_scaler/app.yaml` [new]
- `tools/runner_scaler/requirements.txt` [new]
- `tools/runner_scaler/bootstrap/runner-boot.sh` [new]
- `tests/test_runner_scaler_webhook.py` [new]
- `tests/test_runner_scaler_reconcile.py` [new]
- `tests/test_runner_scaler_config.py` [new]
- `tests/test_runner_scaler_breaker.py` [new]
- `tests/test_runner_scaler_rerun.py` [new]
- `tests/test_runner_scaler_bootstrap.py` [new]
- `docs/scripts-pending-removal.md`
- `README.md`
- `changelog.d/<pr>-runner-scaler.md` [new]
- `agents.d/<pr>-runner-scaler.md` [new]

## Data Model / Index Changes

None.

## Tests

- **Unit:** steps 1.3, 1.6 and 2.6, all in the existing `tests-system-python` catch-all. No new CI job.
- **Static:** `shellcheck` and actionlint already run in `static-checks`. Confirm that actionlint accepts the `vars` expression in `runs-on`.
- **End to end:** during rollout step R4, with `RUNNER_SCALER_DRY_RUN=true` and then `false`, one manually triggered pipeline run in one consumer:
  - its jobs land on `do-runner-*` runners;
  - the droplets are destroyed after completion.

  This is an operations check, not committed code (Q10).

## Risks & Mitigations

- **Missing tools on the stock image.** The boot inventory may miss a tool that GitHub's image has, making jobs fail for environment reasons. Mitigations:
  - the self-check covers the inventory;
  - those failures are not lost-runner failures, so the existing heal pipeline may see them, but heal and triage run on GitHub-hosted runners (Q17), so they cannot loop on the fleet;
  - the rollback is unsetting the variable.

  ACCEPTED — Q4 A and Q15 B (all consumers at once).
- **Docker images pulled on every job.** Per-job droplets have no layer cache, which adds minutes to sandbox jobs. Mitigation: measure in the rollout check; a scheduled snapshot bake is the follow-up plan.
- **Boot overhead.** An estimated 1–3 minutes per job, longer than many short jobs take. ACCEPTED — Q6 A; re-evaluate in the rollout check.
- **JIT config in droplet user data.** It is readable through the droplet's metadata service and by holders of the scaler's DigitalOcean token. ACCEPTED — it is single-use, registers one runner for one job, and expires. The boot script's `iptables` rule (boot step 4) blocks re-reads by job code running as `runner`, but `runner` has passwordless sudo, so that rule is defence in depth only.
- **Scaler outage.** Jobs queue and nobody is alerted from inside. Mitigations: App Platform health-check restarts; a DigitalOcean Uptime check with email alerts on `/healthz`; the rollback is unsetting the variable.
- **Cost runaway.** Mitigated by the cap of 60 droplets (`RUNNER_SCALER_MAX_DROPLETS`, at most $4.29/hr), the 300-minute lifetime cleanup, the 15-minute unregistered cleanup and the 5-minute idle cleanup for runners whose job was cancelled before assignment.
- **The lost-runner re-run repeats a real failure once.** The rule requires lost-runner evidence on every failed job of a completed run and `run_attempt` 1, so a run with a real code failure is not re-run. ACCEPTED — one extra run at worst.
- **Leaked droplets after a scaler bug.** The cleanup pass works only on tags, so it removes any `gh-runner` droplet past its lifetime even when its job is unknown.
- **The stable-branch deploy redeploys the scaler on every release.** ACCEPTED — in-flight droplets are unaffected and the registration marker lives on the droplets. The in-process queued set, breaker history and alert cooldowns are lost; the startup full scan rebuilds the queued set, the breaker starts half-open, and an open incident may be alerted once more (Approach, "Restart behaviour").
- **Webhook endpoint exposed to the internet.** Requests fail unless the HMAC verifies, and request bodies are capped at 1 MiB.
- **Secrets on droplets.** The same exposure as GitHub-hosted runners, plus single use, no inbound traffic, and no scaler credentials on the droplet. ACCEPTED — Q1.

## Rollout

Each step that creates or changes an external resource is ask-first and performed by the session after approval (§22.B, §23.C).

1. **R1.** Merge Phases 1 and 2, in either order, and release to `stable` with the existing release workflows. Consumers receive Phase 1 on their next `@stable` sync, which is still a no-op.
2. **R2.** You create a GitHub App on the `shubhodeep1` account:
   - the permissions and events listed in step 2.7;
   - the webhook URL left blank until R3;
   - installed only on the 7 consumer repositories.

   You also create the custom-scoped DigitalOcean token. Both are one-time steps in the browser.
3. **R3.** The session, after approval:
   - creates a dedicated VPC in `nyc3` for the runners (D14);
   - creates the cloud firewall on tag `gh-runner` (no inbound, all outbound);
   - creates the App Platform app from `tools/runner_scaler/app.yaml` with `RUNNER_SCALER_REPOS` listing the 7 consumers, `RUNNER_SCALER_VPC_UUID` set to that VPC and `RUNNER_SCALER_DRY_RUN=true`;
   - sets the encrypted secrets;
   - creates the Uptime check on `/healthz`;
   - records every resource ID through an `agents.d/` fragment in a small PR (§22.C).

   You then set the App's webhook URL to the app's `/webhook`.
4. **R4.** Check the dry-run logs (`RUNNER_SCALER outcome=...`), then set `RUNNER_SCALER_DRY_RUN=false` (ask-first). Set `PIPELINE_RUNS_ON=do-runner` on all 7 consumers (Q15 B, ask-first) and run the end-to-end check under Tests.
5. **R5.** After 24 hours and after 7 days, an interactive session reports per consumer:
   - job failure rate, duration and queue wait (Actions API);
   - droplet spend (DigitalOcean billing);
   - the coding-workflows queue depth.
6. **Rollback.**
   - Unset `PIPELINE_RUNS_ON` on the affected consumers; new jobs go to GitHub-hosted runners at once.
   - Keep the scaler running until no queued job carries `do-runner`, then set `RUNNER_SCALER_REPOS` to empty.
   - Jobs already queued with `do-runner` while the fleet is broken are cancelled and re-run, and pick up the GitHub-hosted runner on re-run.

## Future work

- **Automatic fallback (Q14 B).** When the breaker is open, or jobs are queued without supply past a threshold, cancel the affected runs and re-dispatch them with GitHub-hosted runners. This needs its own plan, covering loop bounds, a dispatch budget (§33) and run attribution for the poller's concurrency caps.
- **Scheduled snapshot bake (Q4).** A weekly job bakes `runner-boot.sh` into a DigitalOcean snapshot, to be used only if R5 shows boot overhead or Docker pulls dominate.
- **Consumer-owned CI and template-level jobs.** Opt in the same way once the fleet is proven.

## References

- `docs/plans/reduce-actions-run-volume-plan.md`
- `docs/plans/cut-ci-runner-time-plan.md` (orchestrator project #9528, Refs #9528)
- CLAUDE.md §1, §4, §6, §14, §15, §18, §20, §22, §23, §27, §30, §33
- [GitHub: autoscaling with self-hosted runners](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/autoscaling-with-self-hosted-runners)
- [GitHub REST: create a JIT runner configuration](https://docs.github.com/en/rest/actions/self-hosted-runners#create-configuration-for-a-just-in-time-runner-for-a-repository)
- [DigitalOcean Droplet pricing (per-second billing)](https://docs.digitalocean.com/products/droplets/details/pricing/)
- [DigitalOcean: per-second billing announcement](https://www.digitalocean.com/blog/dropletplans-persecbilling-byoip-natgateway)
- [Depot GitHub Actions runners (organizations only)](https://depot.dev/docs/github-actions/overview)
- [Namespace pricing](https://namespace.so/pricing)
