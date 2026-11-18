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

- **Scripts:** Phase 1 only edits existing workflows and `scripts/workflow_failure_heal.py`. Phase 2 adds a new long-running supervisor, the `runner_scaler/` service, plus the droplet boot script it hands to every droplet. No script needs manual invocation (§18.A).
- **Scheduler / entry point:**
  - The scaler runs as a DigitalOcean App Platform service built from `runner_scaler/app.yaml`, with `deploy_on_push` from the `stable` branch and `source_dir: runner_scaler`. Every release that reaches `stable` (the `release` jobs of `.github/workflows/test-and-mark-stable.yml` and `.github/workflows/mark-stable.yml`) redeploys it, and no operator action is needed.
  - Its reconcile loop is an in-process timer; webhooks arrive from a GitHub App.
  - No GitHub Actions job, workflow or cron is added (§33).
- **Supervisor lifecycle (§18.C):**
  - Entry point: `python -m runner_scaler`.
  - Restart policy: App Platform restarts the container on crash or on a failing `GET /healthz`.
  - Shutdown: on SIGTERM the scaler stops accepting webhooks, finishes the reconcile pass in progress, and exits within 20 seconds. Droplets keep running, because runners talk to GitHub directly.
  - Crash recovery: the scaler keeps no state of its own. Its state is the droplets tagged `gh-runner` plus GitHub's job and runner lists, so a fresh instance picks up where the old one stopped.
  - External liveness: a DigitalOcean Uptime check on `/healthz` alerts by email when the service is down. The scaler's own Telegram watchdog cannot report its own death.
- **DB work:** none.
- **Removal registry (§18.F):** Phase 2 adds an entry for `runner_scaler/` to `docs/scripts-pending-removal.md` (see Implementation Steps, step 2.9).

## Goals

- **G1.** In every reusable workflow (17 files, 30 `runs-on:` lines) except `ci.yml`, `workflow_failure_heal.yml` and `check_failure_triage.yml`, every job's runner comes from the calling repository's variable `PIPELINE_RUNS_ON`:
  - the exact value `do-runner` selects the DigitalOcean runners;
  - any other value, or no value, selects the job's current runner (`ubuntu-latest`, or `ubuntu-24.04` in `security-audit.yml`).
- **G2.** With no consumer setting the variable, every job runs exactly as today. Phase 1 is a no-op on merge.
- **G3.** For each queued job labelled `do-runner` in a repository on the scaler's allowlist, the scaler creates one droplet, up to 60 at once. The droplet is destroyed when the job completes, when it fails to register within 15 minutes, or when it outlives 300 minutes.
- **G4.** A droplet whose boot self-check fails never registers a runner. It powers itself off, and the scaler destroys it and counts a boot failure.
- **G5.** The scaler's circuit breaker stops creating droplets after 3 boot failures in a row, or when more than half of boots in 15 minutes fail. It sends one Telegram alert per incident and tries a single droplet after a 15-minute cooldown.
- **G6.** A job that fails because its DigitalOcean runner was lost is re-run once by the scaler, only when the run's `run_attempt` is 1. The heal intake files no heal issue for such a failure.
- **G7.** A job labelled `do-runner` that waits more than 10 minutes with no droplet booting for it raises one Telegram alert per incident.
- **G8.** `workflow_failure_heal.yml`, `check_failure_triage.yml` and every coding-workflows job keep running on GitHub-hosted runners whatever the variable says.

## Non-goals

- Moving `coding-workflows`' own jobs, including `ci.yml` and the release workflows, off GitHub-hosted runners.
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

- **Chosen:** this repository under `runner_scaler/`, deployed by App Platform from `stable`.
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

## Constraints

- **§1 security:** droplets receive the job's secrets (`GH_PAT`, `OPENROUTER_API_KEY` and others) exactly as GitHub-hosted runners do, so each droplet:
  - runs one job only;
  - is destroyed afterwards;
  - holds no DigitalOcean or GitHub App credential of its own;
  - accepts no inbound traffic (cloud firewall on tag `gh-runner`).

  The scaler holds the only privileged credentials. Webhooks are rejected unless their `X-Hub-Signature-256` HMAC verifies.
- **§4:** every new env var and repo variable has a default. Unset `PIPELINE_RUNS_ON` means GitHub-hosted. Unset `RUNNER_SCALER_REPOS` means the scaler serves no repository.
- **§6:** no identifier is renamed or removed. The new names `PIPELINE_RUNS_ON`, `do-runner`, `gh-runner`, `runner_scaler/`, `RUNNER_SCALER_*` and the `RUNNER_SCALER` log prefix were checked against the repository and do not collide. Job ids and names stay unchanged, because log parsers, heal rules and required checks cite them (§33.B).
- **§14:** Phase 1 reaches consumers on their next `@stable` sync (`update_workflows.yml`). No consumer wrapper file changes. A consumer opts in only by setting its own variable.
- **§15:** the scaler's GitHub API use is webhook-driven, with bounded reconcile reads (see Approach). The heal-intake change adds at most 10 annotation reads per reported run, only for failed jobs labelled `do-runner`, and fails open.
- **§18:** see the §18.E section above.
- **§20 / §30:** each phase ships one `changelog.d/` fragment and one `agents.d/` fragment. README env var tables are edited in place.
- **§22:** creating the firewall, the App Platform app, its env vars and the Uptime check is provisioning, so each step is ask-first (§22.B). The resulting IDs go in the agents file's `## DigitalOcean resources` table through an `agents.d/` fragment (§22.C). Committed code never reads the session's `DIGITALOCEAN_ACCESS_TOKEN`: the scaler uses its own scoped token.
- **§23.C:** setting `PIPELINE_RUNS_ON` on consumer repositories is an administrative write, ask-first at rollout.
- **§27:** `implement.yml` is 440,172 bytes and `review_autofix.yml` is 409,603 bytes. Phase 1 adds about 80 bytes per `runs-on:` line (2 and 7 lines). Check both with `wc -c` after the edit; they must stay under 480,000.
- **§33:** no job, workflow, matrix leg or trigger is added. Jobs per triggering event are unchanged: before and after, every routed job is still one job. For an opted-in consumer, 0 of them hold a GitHub-hosted slot afterwards.

## Approach

### Runner selection (Phase 1)

Every `runs-on: ubuntu-latest` line in the 17 routed workflows becomes:

```yaml
runs-on: ${{ vars.PIPELINE_RUNS_ON == 'do-runner' && 'do-runner' || 'ubuntu-latest' }}
```

`security-audit.yml`'s job keeps its own fallback (`|| 'ubuntu-24.04'`). The `vars` context is available in `jobs.<id>.runs-on`, and inside a called workflow it resolves to the caller repository's variables.

| Workflow | `runs-on:` lines |
| --- | --- |
| `review_autofix.yml` | 7 |
| `clarify.yml`, `implement.yml`, `issue_pr_status.yml`, `orchestrate_poll.yml`, `plan.yml`, `update_workflows.yml`, `validate.yml` | 2 each |
| `cancel_on_pr_close.yml`, `cs_investigate.yml`, `memory_maintenance.yml`, `orchestrate.yml`, `orchestrate_clarify_respond.yml`, `security-audit.yml`, `sync_ai_labels.yml`, `unblock_judge.yml`, `workflow_integrity.yml` | 1 each |

### Heal-intake skip (Phase 1)

`scripts/workflow_failure_heal_intake.sh` already saves `repos/<source>/actions/runs/<id>/jobs` to `JOBS_FILE`, so the skip needs no new jobs call.

1. A new helper, `runner_lost_failure(jobs_doc, annotations_by_job)` in `scripts/workflow_failure_heal.py`, returns true when every failed job of the run has the label `do-runner` and one of its check-run annotations matches either message:
   - `The self-hosted runner: .* lost communication with the server`
   - `The runner has received a shutdown signal`
2. For each failed job labelled `do-runner` (at most 10 per run), the intake reads `GET repos/<source>/check-runs/<job_id>/annotations`.
3. `skip_reason()` then returns the new stable reason `runner_lost`, and no heal issue is filed.
4. If any read fails, or any failed job lacks the evidence, the run is reported as today (fail-open towards filing). The scaler handles the re-run (G6).

### Scaler service (Phase 2)

The package is `runner_scaler/`, Python 3.12, with dependencies pinned in `runner_scaler/requirements.txt`: `PyJWT` and `cryptography`, imported lazily so unit tests need neither.

**HTTP server**
- `POST /webhook` accepts `workflow_job` events from the GitHub App. It verifies `X-Hub-Signature-256` against `RUNNER_SCALER_WEBHOOK_SECRET` and drops events for repositories not in `RUNNER_SCALER_REPOS` or whose `labels` are not exactly `["do-runner"]`.
- `GET /healthz` returns 200 while the reconcile loop has completed a pass within 3 intervals.

**Reconcile, level-triggered.** One pass runs on every accepted webhook (debounced to one per 5 seconds) and every `RUNNER_SCALER_RECONCILE_SECONDS` (default 120). For each allowlisted repository:
- `queued` is the set of queued `do-runner` job ids. Webhook events maintain it in memory; a full scan replaces it every `RUNNER_SCALER_FULL_SCAN_SECONDS` (default 600) to catch missed or undelivered webhooks.
- `supply` is the number of that repository's droplets that are booting, plus its `do-runner` runners that are online and idle.
- The scaler creates `min(queued − supply, cap − total_droplets)` droplets, or none while the breaker is open.

Duplicate or replayed events cannot over-create, because each pass recomputes from state rather than reacting to single events.

**Droplet creation**
1. Get a single-use runner registration: `POST /repos/{owner}/{repo}/actions/runners/generate-jitconfig` with `name=<droplet name>`, `runner_group_id=1`, `labels=["do-runner"]`.
2. Create the droplet:
   - name `do-runner-<repo-slug>-<8 hex>`;
   - size `RUNNER_SCALER_DROPLET_SIZE` (default `s-4vcpu-8gb`), region `RUNNER_SCALER_REGION` (default `nyc3`), image `RUNNER_SCALER_IMAGE` (default `ubuntu-26-04-x64`);
   - tags `gh-runner` and `gh-runner-repo-<slug>`;
   - `user_data` = `runner_scaler/bootstrap/runner-boot.sh` with the JIT config inserted.

**Droplet destruction**
- On `workflow_job` `completed` for that runner's name.
- For a droplet whose status is `off` (boot self-check failed) or that has not registered within `RUNNER_SCALER_REGISTER_TIMEOUT_SECONDS` (default 900); both count as boot failures.
- For any `gh-runner` droplet older than `RUNNER_SCALER_MAX_LIFETIME_MINUTES` (default 300: the longest job timeout of 240 minutes plus margin).

**Circuit breaker**
- Opens after `RUNNER_SCALER_BREAKER_CONSECUTIVE` (default 3) boot failures in a row, or when the failure ratio over `RUNNER_SCALER_BREAKER_WINDOW_MINUTES` (default 15) exceeds `RUNNER_SCALER_BREAKER_FAIL_RATIO` (default 0.5), with at least 4 boots in the window.
- While open it creates no droplets. After `RUNNER_SCALER_BREAKER_COOLDOWN_MINUTES` (default 15) it allows one trial droplet: success closes the breaker, failure re-opens it.

**Lost-runner re-run (G6)**
- Applies on `workflow_job` `completed` with `conclusion: failure` for a `do-runner-*` runner, when either:
  - the scaler's droplet list showed the droplet gone or `off` before the job completed, or
  - the job's annotations match the lost-runner messages (one read).
- The scaler calls `POST /repos/{o}/{r}/actions/jobs/{job_id}/rerun` only when the run's `run_attempt` is 1 and `RUNNER_SCALER_RERUN_ENABLED` is `true` (default).
- It never re-runs at `run_attempt` 2 or more. GitHub's own counter is the bound, so a restart cannot reset it.

**Alerts**
- A Telegram message through the Bot API using `TG_BOT_SECRET` and `TG_ADMIN_CHAT_ID`, the same names as `scripts/tg_helpers.sh`, deduplicated by incident key with a 60-minute cooldown.
- Incident kinds: `breaker_open`, `queued_without_supply` (a `do-runner` job queued longer than `RUNNER_SCALER_QUEUE_ALERT_MINUTES`, default 10, with no booting droplet for its repo), `cap_reached`, `do_api_error`, `rerun_given_up` (a lost runner at `run_attempt` ≥ 2).

**Logging** is one structured line per decision, under the stable prefix `RUNNER_SCALER`:

```text
RUNNER_SCALER outcome=<created|destroyed|skipped|rerun|alert> reason=<...> repo=<owner/repo> droplet=<name> job=<id>
```

**Dry run.** `RUNNER_SCALER_DRY_RUN` (default `false`): `true` logs every decision and calls no write endpoint.

**Secrets** are App Platform encrypted env vars: `RUNNER_SCALER_DO_TOKEN`, `RUNNER_SCALER_GH_APP_ID`, `RUNNER_SCALER_GH_APP_PRIVATE_KEY`, `RUNNER_SCALER_WEBHOOK_SECRET`, `TG_BOT_SECRET` and `TG_ADMIN_CHAT_ID`. The DigitalOcean token is a custom-scoped token limited to droplet create, read and delete, plus tag create and read.

**API budget (§15)**

| Source | Calls |
| --- | --- |
| GitHub, per allowlisted repository per reconcile pass | 1 runners list |
| GitHub, per full scan | 1 queued-runs list, plus 1 jobs read per queued run, capped at 50 runs per repo per scan |
| GitHub, per droplet | 1 JIT config |
| GitHub, per lost-runner failure | at most 1 annotations read and 1 re-run |
| DigitalOcean, per reconcile pass | 1 droplets-by-tag list |
| DigitalOcean, per droplet | 1 create and 1 delete |

For 7 repositories at the defaults, steady state is under about 600 GitHub calls an hour. That is within the GitHub App installation limit of at least 5,000 an hour. A rate-limit response skips that pass and is logged (`reason=rate_limited`), with no tight retry loop.

### Droplet boot script (Phase 2)

`runner_scaler/bootstrap/runner-boot.sh` runs as cloud-init user data:

1. **Set up the runner user.** Create user `runner` with passwordless sudo and membership of the `docker` group, like GitHub-hosted runners.
2. **Install pinned packages.** Install, with 3 attempts and backoff:
   - Ubuntu packages: `docker.io`, `git`, `jq`, `curl`, `ca-certificates`, `unzip`, `build-essential`, `python3`, `python3-pip`, `python3-venv`, `ripgrep`, `gawk`, `shellcheck`;
   - `gh` from GitHub's apt repository;
   - the `actions/runner` release at a pinned version, checked against its SHA-256.

   The list comes from a static inventory of commands the 17 routed workflows and their scripts call (step 2.1).
3. **Run the self-check.** It confirms `docker run --rm hello-world`, `python3 -m venv`, `gh --version`, `jq`, `shellcheck`, `git` and a free-disk threshold. On any failure the script writes the reason to the serial console log and runs `shutdown -h now` without registering.
4. **Run one job.** Start the runner as `runner` with `./run.sh --jitconfig <config>`. When it exits, power off, and the scaler destroys the droplet.

## Phases & Merge Strategy

Every merge ships to production. The two phases are independent: either can merge first, and each is a complete, safe state on its own.

1. **Phase 1: runner switch in the reusable workflows, and the heal-intake skip.**
   - **Files:** the 17 routed workflow files under `.github/workflows/`, `scripts/workflow_failure_heal.py`, `scripts/workflow_failure_heal_intake.sh`, new tests, README env var row, `changelog.d/` and `agents.d/` fragments.
   - **Done when:**
     - with no consumer setting `PIPELINE_RUNS_ON`, every job resolves to its old runner;
     - the new test pins the expression in every routed job and its absence from `ci.yml`, `workflow_failure_heal.yml` and `check_failure_triage.yml`;
     - the `runner_lost` skip is covered by tests;
     - full CI is green.
   - **Rollback:** revert the PR. No consumer state depends on it, and a consumer can also unset its variable.
2. **Phase 2: scaler service, droplet boot script, App Platform spec and docs.**
   - **Files:** everything under `runner_scaler/`, new tests under `tests/`, `docs/scripts-pending-removal.md`, README, `changelog.d/` and `agents.d/` fragments.
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
- every job in the 17 routed files uses the exact expression with its previous runner as fallback;
- `ci.yml`, `workflow_failure_heal.yml`, `check_failure_triage.yml` and every non-reusable workflow contain no `PIPELINE_RUNS_ON`;
- `workflow-templates/*.yml` contain no `PIPELINE_RUNS_ON`.

The `tests-system-python` catch-all collects it, so no wiring edit is needed (`tests/test_ci_wires_every_test_file.py`).

1.4. In `scripts/workflow_failure_heal.py`, add `runner_lost_failure()` and the `runner_lost` branch in `skip_reason()`. Annotations come in through a new optional keyword argument with an empty default, so existing callers are unchanged.

1.5. In `scripts/workflow_failure_heal_intake.sh`, after the existing `JOBS_FILE` read (around line 1764), fetch annotations for at most 10 failed jobs labelled `do-runner` into a temp file and pass it to the skip check.
- Any API error leaves the file empty, so the run is reported as before.
- Log `WORKFLOW_FAILURE_HEAL_RUNNER_LOST outcome=<skipped|not_runner_lost|lookup_failed> run=<id>`.
- Add a comment above the call naming the existing calls audited, per §15.

1.6. Extend `tests/test_workflow_failure_heal.py` with cases for:
- all failed jobs lost on `do-runner` → `runner_lost`;
- one failed job on `ubuntu-latest` → not skipped;
- an annotation that doesn't match → not skipped;
- an empty annotations file → not skipped.

1.7. README: add a `PIPELINE_RUNS_ON` row to the repo-variable table (default unset; valid values `do-runner` and unset; behaviour; the heal and triage exception; the rollback).

1.8. Add a `changelog.d/<pr>-pipeline-runs-on-switch.md` fragment (`added`) and an `agents.d/<pr>-pipeline-runs-on-switch.md` fragment. The agents fragment covers the variable in the workflow architecture section, and the new log prefix in `## Stable log prefixes (contractual)` with its `LOG_PREFIX.name=` line.

### Phase 2

2.1. Inventory the commands the 17 routed workflows and the scripts they call depend on, beyond what their own setup steps install. Record the result as the package list and self-check list in `runner_scaler/bootstrap/runner-boot.sh`, with a comment naming the inventory method so it can be repeated.

2.2. Write `runner_scaler/bootstrap/runner-boot.sh` as described in Approach, using tab indentation and passing `shellcheck`. The JIT config placeholder is a single literal token that `runner_scaler` replaces; it never appears in logs.

2.3. Write the `runner_scaler/` package:
- `__main__.py`: startup, config parsing with the §4 defaults, SIGTERM handling;
- `server.py`: `/webhook` and `/healthz`;
- `reconcile.py`: desired-count logic and the cleanup passes;
- `breaker.py`;
- `rerun.py`;
- `github_app.py`: JWT, installation token, JIT config, runners, jobs, annotations, re-run;
- `do_client.py`: droplet create, list by tag and delete;
- `alerts.py`: Telegram with deduplication;
- `config.py`.

Every external call has a timeout and returns a typed error. No call retries in a tight loop.

2.4. Write `runner_scaler/app.yaml`, the App Platform spec:
- one service of the smallest instance size, built from `source_dir: runner_scaler` on branch `stable` with `deploy_on_push: true`;
- `run_command: python -m runner_scaler`;
- an HTTP health check on `/healthz`;
- env var declarations with the defaults above, and encrypted placeholders for the secrets (no values).

2.5. Write `runner_scaler/requirements.txt` with pinned versions.

2.6. Add tests under `tests/`, collected by `tests-system-python`, with stubbed GitHub and DigitalOcean clients:
- `test_runner_scaler_webhook.py`
- `test_runner_scaler_reconcile.py`
- `test_runner_scaler_breaker.py`
- `test_runner_scaler_rerun.py`
- `test_runner_scaler_bootstrap.py`: static checks for the pinned runner version and SHA-256, the self-check list, power-off on failure, and no credential variables

2.7. README: add a section "DigitalOcean runners for consumer pipelines" covering:
- the architecture;
- every `RUNNER_SCALER_*` env var with its default;
- the GitHub App permissions:
  - repository: Actions read and write, Administration read and write, Metadata read;
  - webhook events: Workflow job;
- the scoped DigitalOcean token scopes;
- the failure modes and the rollback.

2.8. Add an `agents.d/<pr>-runner-scaler.md` fragment (operational runbook, log prefix registry entry `RUNNER_SCALER`) and a `changelog.d/<pr>-runner-scaler.md` fragment (`added`).

2.9. Add the `docs/scripts-pending-removal.md` entry:

```markdown
### `runner_scaler/` + `runner_scaler/app.yaml` + `runner_scaler/bootstrap/runner-boot.sh`

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
- `runner_scaler/__init__.py` [new]
- `runner_scaler/__main__.py` [new]
- `runner_scaler/config.py` [new]
- `runner_scaler/server.py` [new]
- `runner_scaler/reconcile.py` [new]
- `runner_scaler/breaker.py` [new]
- `runner_scaler/rerun.py` [new]
- `runner_scaler/github_app.py` [new]
- `runner_scaler/do_client.py` [new]
- `runner_scaler/alerts.py` [new]
- `runner_scaler/app.yaml` [new]
- `runner_scaler/requirements.txt` [new]
- `runner_scaler/bootstrap/runner-boot.sh` [new]
- `tests/test_runner_scaler_webhook.py` [new]
- `tests/test_runner_scaler_reconcile.py` [new]
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
- **JIT config in droplet user data.** It is readable through the droplet's metadata service and by holders of the scaler's DigitalOcean token. ACCEPTED — it is single-use, registers one runner for one job, and expires.
- **Scaler outage.** Jobs queue and nobody is alerted from inside. Mitigations: App Platform health-check restarts; a DigitalOcean Uptime check with email alerts on `/healthz`; the rollback is unsetting the variable.
- **Cost runaway.** Mitigated by the cap of 60 droplets (at most $4.29/hr), the 300-minute lifetime cleanup and the 15-minute unregistered cleanup.
- **The lost-runner re-run repeats a real failure once.** The rule requires lost-runner evidence and `run_attempt` 1, so a real code failure is not re-run. ACCEPTED — one extra run at worst.
- **Leaked droplets after a scaler bug.** The cleanup pass works only on tags, so it removes any `gh-runner` droplet past its lifetime even when its job is unknown.
- **The stable-branch deploy redeploys the scaler on every release.** ACCEPTED — the scaler keeps no state and restarts cleanly; in-flight droplets are unaffected.
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
   - creates the cloud firewall on tag `gh-runner` (no inbound, all outbound);
   - creates the App Platform app from `runner_scaler/app.yaml` with `RUNNER_SCALER_REPOS` listing the 7 consumers and `RUNNER_SCALER_DRY_RUN=true`;
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
