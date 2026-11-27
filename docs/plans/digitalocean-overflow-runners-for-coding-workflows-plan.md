# Overflow coding-workflows' final CI to DigitalOcean runners when GitHub's 40 slots are full

## Summary

coding-workflows runs on GitHub-hosted runners, which are free for a public repository but limited to 40 jobs at once for the account. That limit was full all night on 2026-11-25/26 and jobs waited a median of 65 minutes. This plan keeps GitHub's 40 slots as the first choice and sends a pull request's final CI run to the existing DigitalOcean runner fleet only when the dispatching job has itself just waited for a runner, with a hard droplet cap and a monthly budget.

## Automation and supervisor summary (§18.E)

- **Scripts:** extends existing code only: the runner scaler service (`tools/runner_scaler/`), `scripts/pr_checks_lib.sh`, `scripts/review_final_ci_sweep.sh` and `.github/workflows/ci.yml`. No new script and nothing run by hand.
- **Entry points:**
  - `.github/workflows/ci.yml`, `workflow_dispatch` (the final CI run), new optional input;
  - `_pr_ensure_final_ci` and `_pr_base_sync_dispatch_final_ci` in `scripts/pr_checks_lib.sh`, called from the review, sweep and poller jobs that dispatch final CI today;
  - the final-CI sweep (`scripts/review_final_ci_sweep.sh`), which already runs on the review sweep's schedule, for the stuck-run fallback.
- **Supervisor:** no new one. The runner scaler (App Platform service from the first plan, restarted and health-checked by App Platform) is extended.
- **DB work:** none.
- **`docs/scripts-pending-removal.md`:** no new entry. The scaler's existing entry gains one line: its removal preflight must also show `RUNNER_SCALER_OVERFLOW_REPOS` empty and no queued `do-runner` job in coding-workflows.
- **Runner use (§33):** no new job, workflow or matrix leg. Jobs per final CI run are unchanged (47); what changes is where 9 of the 12 job definitions may run.

## Context

### What was measured

Job records of every run created 2026-11-25 22:00 to 2026-11-26 05:36 UTC in coding-workflows and three consumer repositories (4,128 jobs):

| The numbers that matter | Value |
| --- | --- |
| Jobs running at once, every 30-minute window from 23:30 | 40 (the account limit), mean 38 to 40 |
| Runner time used since 22:00 | 17,397 of 18,240 minutes (95%) |
| Slots held by coding-workflows / by each visible consumer | 35 to 40 / 1 to 2 |
| Final CI on pull requests (47 jobs, about 500 runner-minutes a run) | 46.9% of runner time |
| `CI` on push to `main` | 23.7% (issue #10156 reduces this) |
| Review and autofix | 16.3% |
| Jobs waiting at 05:36 | 247, median wait 65 minutes |
| Demand if nothing had waited | mean 44 concurrent, 90th percentile 101, peak 155 |
| Work above the 40-job line that night | about 1,000 runner-minutes an hour |

So nothing throttles the account below its limit. Demand exceeds it, in bursts: one final CI queues 46 jobs at once.

### What exists

- `docs/plans/digitalocean-runners-for-consumer-pipelines-plan.md` (the "first plan"). Its Phase 1 (PR #9723, `PIPELINE_RUNS_ON` routing in 17 reusable workflows) and Phase 2 (PR #9725, `tools/runner_scaler/`) are on `main`. Its rollout R1 to R5 has not started: no stable release since v1.47.6, and the GitHub App and DigitalOcean token do not exist yet.
- First-plan decision **D15**: coding-workflows never moves to the fleet. The `runs-on` expression excludes it by `github.repository`, and `tools/runner_scaler/runner_scaler/config.py` drops it from `RUNNER_SCALER_REPOS` (`EXCLUDED_REPOS`, `reason=self_repo_excluded`). Reasons given: hosted minutes are free, a full move would cost about $2,000 to 2,400 a month, and fork pull requests would need same-repo-only routing.
- The scaler serves one droplet per queued job whose labels are exactly `["do-runner"]`, in repositories on its allowlist (D6), size `s-4vcpu-8gb` at $0.07143 an hour (D7), at most 60 droplets (D8), in a dedicated VPC (D14).
- Final CI is dispatched by `_pr_ensure_final_ci` (`scripts/pr_checks_lib.sh`, three `gh workflow run` call sites: docs-only, sync-quick, full) and `_pr_base_sync_dispatch_final_ci`. A dispatched run uses the pull request branch's copy of `ci.yml`.
- `ci.yml` has 12 `runs-on: ubuntu-latest` lines. Eight jobs install Python 3.12 with `actions/setup-python`.

### Constraints that shape the design

- GitHub fixes a job's runner from `runs-on` when the job is queued. A job cannot move from a hosted runner to another runner type by itself, so the choice has to be made when the run is dispatched.
- Push and `pull_request` runs take no inputs, so they cannot be routed per run. They stay on GitHub-hosted runners.
- The repository is public. Anyone can open a pull request from a fork, and a workflow file on any ref can ask for any label. A droplet must never run a job that the pipeline did not dispatch.

## Goals

1. With the switch off (the default), nothing changes: every coding-workflows job runs on a GitHub-hosted runner and the scaler ignores the repository.
2. With the switch on, a full final CI run goes to droplets only when the dispatching job's own run shows a recent runner wait of at least `FINAL_CI_OVERFLOW_MIN_WAIT_SECONDS`. Otherwise it goes to GitHub-hosted runners as today.
3. The scaler creates a droplet for a coding-workflows job only when the job's run is a `workflow_dispatch` of an allowed workflow file, on a branch of this repository, started by an allowed account. Every other `do-runner` job in the repository gets no droplet and one alert.
4. Overflow spend is bounded twice: at most `RUNNER_SCALER_OVERFLOW_MAX_DROPLETS` droplets at once for overflow repositories, and no new overflow droplet once the month's overflow spend reaches `RUNNER_SCALER_OVERFLOW_MONTHLY_BUDGET_USD`.
5. A final CI run that was sent to droplets and gets no runner within `FINAL_CI_OVERFLOW_STUCK_MINUTES` is replaced by a run on GitHub-hosted runners, without a person acting.
6. The jobs that hold write tokens (`lint`, `final-ci-failed`) and `budget-watch` never run on a droplet.
7. The consumers' full move (first plan) is unchanged, and coding-workflows can never become a fully served repository through `RUNNER_SCALER_REPOS`.

## Non-goals

- Overflow for review, autofix, security audit, judge and poller runs. They carry model and GitHub credentials; see Future work.
- Overflow for `CI` on push to `main` and for `ci_pr_quick.yml`. They take no inputs.
- Moving coding-workflows to the fleet outright. D15 stays for `RUNNER_SCALER_REPOS`.
- Reducing demand. Project #10097 (lease plan, delta suite) and issue #10156 (delta suite on `main`) own that. Overflow is sized after them.
- A warm pool, a different droplet size, or a snapshot image.

## Decisions

The owner asked for overflow in these words (2026-11-26): "can we keep DO as an overflow that is used when the 40 runner capacity is full? so we maximise the runners from GH and just pass the overflow to DO? ... the consumers still move completely to DO. we keep the overflow here just because its a public repo and we can use GH runners for free." The owner approved writing this plan (Q202 A) and asked that each remaining choice take the recommended option and be recorded for review. Each decision below can be overridden on the plan PR.

### O1. Where the choice is made

- **Chosen:** at dispatch, by a new optional `ci.yml` input `overflow_runner` that the dispatcher sets to `do-runner`.
- **Alternatives considered:** registering droplets with the label `ubuntu-latest` so waiting hosted jobs could be picked up by droplets; a repository variable flipped by the poller.
- **Why:** a shared label would let any job in a public repository land on a droplet, including a fork's pull request run, and GitHub does not document which runner wins when labels collide. A variable is read once when a run starts and cannot route single runs.

### O2. What overflows

- **Chosen:** full final CI runs only. Docs-only and sync-quick final runs (2 to 3 jobs) stay on GitHub-hosted runners.
- **Alternatives considered:** review and audit runs too; only the 32 `tests-system-python` shards.
- **Why:** final CI is 47% of the load, has one dispatch path, and its test jobs use no secret beyond the job token. Splitting a run by job needs a second input and saves little.

### O3. Which jobs of the run move

- **Chosen:** `static-checks`, the seven gated test jobs and `tests-sync-quick` (9 `runs-on` lines). `lint`, `final-ci-failed` and `budget-watch` keep `ubuntu-latest`.
- **Alternatives considered:** all 12.
- **Why:** `lint` has `statuses: write` and posts `ci/final`; `final-ci-failed` can write issues and pull requests and dispatch workflows. Tokens that can change repository state stay on GitHub's runners (§1).

### O4. The saturation signal

- **Chosen:** the dispatching job reads its own run's jobs once (`GET repos/<repo>/actions/runs/<run_id>/jobs`) and takes the longest `started_at − created_at` among jobs of that run that started in the last `FINAL_CI_OVERFLOW_SAMPLE_MINUTES` (default 20). At or above `FINAL_CI_OVERFLOW_MIN_WAIT_SECONDS` (default 300) the run overflows. No sample in the window, or a failed read, means no overflow.
- **Alternatives considered:** counting running jobs across the account; an endpoint on the scaler; a flag written by the poller.
- **Why:** there is no account-level jobs listing, so a count costs one call per running run (§15). The dispatcher's own run is a fresh sample of the same queue for one call, with no new credential and no dependency on the scaler being reachable.

### O5. Fork safety

- **Chosen:** the scaler serves a job in an overflow repository only after it has read the job's run once and found all of: `event == "workflow_dispatch"`; `head_repository.full_name == repository.full_name`; `path` in `RUNNER_SCALER_OVERFLOW_WORKFLOWS` (default `.github/workflows/ci.yml`); `triggering_actor.login` in `RUNNER_SCALER_OVERFLOW_ACTORS` (default empty, which serves nothing). The owner also sets the repository's Actions setting to require approval for workflow runs from all outside contributors before the switch is turned on.
- **Alternatives considered:** trusting the label alone; an unguessable per-run label.
- **Why:** a just-in-time runner takes any queued job with its labels, so the label alone proves nothing. A per-run label is visible in the public jobs API as soon as the jobs are queued. The run's event, repository, workflow path and actor are set by GitHub and cannot be forged from a fork.

### O6. Spend bounds

- **Chosen:** `RUNNER_SCALER_OVERFLOW_MAX_DROPLETS` default `20` (20 × $0.07143 = $1.43 an hour, about $1,030 for a month pegged at the cap) and `RUNNER_SCALER_OVERFLOW_MONTHLY_BUDGET_USD` default `300`. Month-to-date spend comes from DigitalOcean's own invoice preview (`GET /v2/customers/my/invoices`, then the preview's items), summing `Droplets` items whose description starts with `do-runner-<overflow repo slug>-`, plus the scaler's own estimate for droplets created since the preview's `updated_at`.
- **Alternatives considered:** the droplet cap alone; a counter kept in the scaler's memory; a counter in a repository variable.
- **Why:** the owner asked for a monthly budget. The scaler restarts on every stable release and loses memory; the invoice preview is authoritative and survives restarts. It was read on 2026-11-26 and lists each droplet as its own item (`description`, `amount`, `start_time`, `end_time`; `updated_at` a few hours old).

### O7. Default state

- **Chosen:** `FINAL_CI_OVERFLOW_ENABLED` default `false`, listed under `off_on_purpose` in `scripts/stage_switches.json`. `RUNNER_SCALER_OVERFLOW_REPOS` default empty.
- **Alternatives considered:** default on with a capability probe.
- **Why:** with no scaler deployed, a run sent to `do-runner` would wait until GitHub fails it. Off is the only safe default until the rollout's canary has passed (§4).

### O8. When the fleet does not answer

- **Chosen:** the final-CI sweep replaces a final run whose `do-runner` jobs have been queued longer than `FINAL_CI_OVERFLOW_STUCK_MINUTES` (default 20) with a dispatch that omits the input. The `ci-final-pr-<n>` concurrency group cancels the stuck run.
- **Alternatives considered:** alert only (first plan D10); cancel and re-dispatch from the scaler.
- **Why:** the first plan left automatic fallback as future work because consumers have no second pool. Here the hosted pool is always there, and the sweep already inspects final runs each tick with the token that dispatches them.

### O9. Phase split

- **Chosen:** three phases (scaler, `ci.yml`, dispatcher). Each is inert alone.
- **Alternatives considered:** one PR.
- **Why:** each phase is small enough to review, and the security-critical part (O5) is reviewed alone.

## Constraints

- **§1.** Security first: O3 and O5 decide in favour of isolation over runner time.
- **§4.** Every new variable has a default; the defaults leave behaviour unchanged.
- **§6.** No identifier is renamed or removed. `EXCLUDED_REPOS` and `reason=self_repo_excluded` keep their meaning for `RUNNER_SCALER_REPOS`. The new `ci.yml` input is optional with an empty default. New names were checked against `main` on 2026-11-26: no match for `FINAL_CI_OVERFLOW`, `RUNNER_SCALER_OVERFLOW` or `overflow_runner`.
- **§14.** `scripts/pr_checks_lib.sh` and `scripts/review_final_ci_sweep.sh` also run in the 7 consumers listed in `.github/ai/consumer_repos.json`. There `FINAL_CI_OVERFLOW_ENABLED` is unset, so the new code is never reached; consumers move through `PIPELINE_RUNS_ON` as planned. `ci.yml` and the scaler are not synced to consumers.
- **§15.** One jobs read per full final-CI dispatch while the switch is on, none while it is off. The sweep's stuck check reuses the jobs data the sweep already loads for a final run, or adds one read per overflow run per tick, never one per job. The scaler's run read is cached per run id.
- **§22.** Every DigitalOcean change in the rollout is ask-first and performed by the session after approval. Resource ids go into `agents.md` through an `agents.d/` fragment.
- **§23.C.** Repository settings, variables and the GitHub App installation are the owner's steps.
- **§27.** `ci.yml` is 291,498 bytes; the change adds about 30 lines.
- **§33.** No job is added. `max-parallel` and concurrency groups are unchanged.

## Approach

### Scaler: overflow repositories (Phase 1)

`tools/runner_scaler/runner_scaler/config.py` gains:

| Variable | Default | Meaning |
| --- | --- | --- |
| `RUNNER_SCALER_OVERFLOW_REPOS` | empty | Comma-separated `owner/repo` list served only for eligible runs (O5). A repository listed in both this and `RUNNER_SCALER_REPOS` is treated as overflow. `EXCLUDED_REPOS` no longer applies to this list; it still applies to `RUNNER_SCALER_REPOS`. |
| `RUNNER_SCALER_OVERFLOW_WORKFLOWS` | `.github/workflows/ci.yml` | Workflow paths whose dispatch runs may be served. |
| `RUNNER_SCALER_OVERFLOW_ACTORS` | empty | Logins allowed as `triggering_actor`. Empty serves nothing in overflow repositories. |
| `RUNNER_SCALER_OVERFLOW_MAX_DROPLETS` | `20` | Cap on droplets for overflow repositories, inside `RUNNER_SCALER_MAX_DROPLETS`. |
| `RUNNER_SCALER_OVERFLOW_MONTHLY_BUDGET_USD` | `300` | Month-to-date spend at which no new overflow droplet is created. `0` disables overflow. |
| `RUNNER_SCALER_OVERFLOW_BUDGET_REFRESH_SECONDS` | `900` | Spacing of invoice-preview reads. |

Behaviour:

1. **Webhook and scan.** `workflow_job` events and scan results for an overflow repository enter a separate `candidates` set, not `queued`.
2. **Eligibility.** For each candidate job the reconcile pass reads the run once (`GET repos/<repo>/actions/runs/<run_id>`, cached by run id for the run's life) and applies O5. Eligible jobs move to `queued`. An ineligible job is dropped with `RUNNER_SCALER outcome=skipped reason=overflow_ineligible repo=<repo> job=<id> check=<event|head_repo|path|actor>` and raises the alert kind `overflow_ineligible` (one per run, existing cooldown).
3. **Caps.** Droplets to create for overflow repositories are `min(queued − supply, RUNNER_SCALER_OVERFLOW_MAX_DROPLETS − overflow_droplets, RUNNER_SCALER_MAX_DROPLETS − total_droplets)`, and zero while the budget is reached (`reason=overflow_budget_reached`, alert once a day) or the breaker is open.
4. **Budget.** `do_client.py` gains the two invoice reads. Spend = sum of `amount` for `Droplets` items whose `description` starts with `do-runner-<slug>-` for an overflow slug, plus `hours × hourly price` for overflow droplets the scaler created after the preview's `updated_at`. A failed read keeps the last value for up to 6 hours, then blocks overflow (`reason=overflow_budget_unknown`). Consumer repositories are never limited by this budget.
5. **Everything else** (droplet create, registration marker, destruction, breaker, lost-runner re-run, restart, dry run) is the first plan's code, unchanged.

The DigitalOcean token needs `billing:read` in addition to the first plan's scopes; the GitHub App must also be installed on coding-workflows. Both are rollout steps, not code.

### `ci.yml`: the input and the expression (Phase 2)

New `workflow_dispatch` input:

```yaml
overflow_runner:
  description: "do-runner sends this run's test jobs to the DigitalOcean fleet; anything else keeps GitHub-hosted runners"
  required: false
  default: ""
  type: string
```

The nine `runs-on` lines of O3 become:

```yaml
runs-on: ${{ github.event_name == 'workflow_dispatch' && inputs.overflow_runner == 'do-runner' && 'do-runner' || 'ubuntu-latest' }}
```

The run name gains a suffix so the route is visible and `CI [pr:<n>]` still names a full run exactly: `CI [pr:<n>] [do]`. `scripts/ci_sync_quick.sh full-ci-green` and every other reader of the run name are updated to accept the suffix as a full run. The suffix is added only when the input is `do-runner`, so no existing name changes (§6).

`lint`, `final-ci-failed` and `budget-watch` are not touched.

Droplet image: `tools/runner_scaler/bootstrap/runner-boot.sh` installs the tools the reusable workflows call. This phase adds a test that lists every command the nine jobs' steps call and fails when one is in neither `APT_PACKAGES`' provided commands, the jobs' own setup steps, nor an explicit allowlist, and adds the missing packages to the boot script.

### Dispatcher: decide and fall back (Phase 3)

`scripts/pr_checks_lib.sh`:

1. New helper `_pr_final_ci_overflow_decide <repo>`. It returns `do-runner` only when all hold: `FINAL_CI_OVERFLOW_ENABLED` is true; `GITHUB_RUN_ID` is set; the one jobs read succeeds; the O4 sample is at or above the threshold. It logs one line: `FINAL_CI_OVERFLOW pr=<n> outcome=<overflow|hosted> reason=<disabled|no_sample|below_threshold|read_failed|wait_seconds> wait_s=<n> threshold_s=<n>`. The docstring states the input, the output, the single API call and the fail-open rule (§15).
2. The full-run call site of `_pr_ensure_final_ci` (the third `gh workflow run`) and the full-run path of `_pr_base_sync_dispatch_final_ci` add `-f overflow_runner=do-runner` when the helper says so. The docs-only and sync-quick call sites are unchanged (O2).
3. If that dispatch is refused (a branch whose `ci.yml` predates Phase 2 rejects the unknown input), dispatch again without it, as the code already does for a refused `sync_quick`. Log `reason=input_refused`.

`scripts/review_final_ci_sweep.sh`:

4. For a final run whose name ends in `[do]` and whose jobs include a queued `do-runner` job older than `FINAL_CI_OVERFLOW_STUCK_MINUTES`, dispatch the same head without the input and log `FINAL_CI_OVERFLOW pr=<n> outcome=fallback_hosted reason=stuck_queued run=<id>`. One fallback per head: the replacement run has no suffix, so the rule cannot fire twice.
5. A cancelled `[do]` run must not send the pull request back to review. `final-ci-failed` already ignores cancelled runs. Issue #10131 (a cancelled run's `lint` can still post `ci/final failure`) must be merged before the switch is turned on; the rollout checks it.

`env:` lines for the four new variables are added to every step that sources `pr_checks_lib.sh` and can dispatch final CI (found with `git grep -n '_pr_ensure_final_ci\|_pr_base_sync_dispatch_final_ci\|pr_checks_lib.sh'` over `.github/workflows/` and `scripts/`; at the time of writing `review_autofix.yml`, `review_autofix_sweep.yml`, which also runs `review_final_ci_sweep.sh`, and `orchestrate_poll.yml`), each as `${{ vars.<NAME> || '<default>' }}`.

### What a run costs

| Scenario | Runner-minutes | Monthly cost at $0.0012 a minute |
| --- | --- | --- |
| One full final CI on droplets (46 jobs × about 9 min + 1 to 2 min boot each) | about 500 | $0.60 a run |
| Overflow, every hour as busy as the measured night | about 24,500 a day | about $900 |
| Overflow, half the day that busy | about 12,000 a day | about $450 |
| Ceiling at 20 droplets all month | | about $1,030 |
| Default budget | | $300 |
| Full move of coding-workflows (first plan, D15) | about 56,000 a day | $2,000 to 2,400 |

Only final CI overflows, which is 47% of the load, so the real figure is below the "overflow" rows. The default budget is set low on purpose: at $0.60 a run it buys 500 overflow final CI runs a month, and the owner raises it after the first report.

## Phases & Merge Strategy

Each phase is one PR. The phases can merge in any order; each leaves the system working because nothing reaches the new code until the rollout sets variables that all default to off or empty.

1. **Phase 1: scaler overflow repositories.**
   Files: `tools/runner_scaler/runner_scaler/{config,reconcile,do_client,server,alerts}.py`, `tools/runner_scaler/app.yaml`, `tests/test_runner_scaler_*.py`, `tests/runner_scaler_fakes.py`, README, fragments, `docs/scripts-pending-removal.md`.
   Done: with `RUNNER_SCALER_OVERFLOW_REPOS` empty every existing scaler test passes unchanged; the new tests show a droplet is created only for a run that passes all four O5 checks and never past either cap.
   Rollback: leave `RUNNER_SCALER_OVERFLOW_REPOS` empty, or revert the PR.
2. **Phase 2: `ci.yml` input and expression.**
   Files: `.github/workflows/ci.yml`, `tools/runner_scaler/bootstrap/runner-boot.sh`, `scripts/ci_sync_quick.sh`, tests, README, fragments.
   Done: a dispatch without the input resolves all 12 jobs to `ubuntu-latest`; with `do-runner`, nine resolve to `do-runner` and three to `ubuntu-latest`; push and `workflow_call` runs resolve to `ubuntu-latest`. Nothing passes the input yet.
   Rollback: revert the PR. No dispatcher sends the input while `FINAL_CI_OVERFLOW_ENABLED` is false.
3. **Phase 3: dispatcher decision and fallback.**
   Files: `scripts/pr_checks_lib.sh`, `scripts/review_final_ci_sweep.sh`, the three workflows' `env:` blocks, `scripts/stage_switches.json`, tests, README, fragments.
   Done: with the switch false the dispatch commands and log lines are byte-identical to `main` on the existing fixtures; with it true the helper's table of cases passes. Without Phase 2 on the branch a refused dispatch falls back to the plain one, so this phase is correct alone.
   Rollback: set `FINAL_CI_OVERFLOW_ENABLED=false` (its default), or revert the PR.

## Implementation Steps

### Phase 1

1. `config.py`: parse the six variables with the existing validators (`REPO_RE`, a new path pattern for workflows, GitHub login pattern for actors, integer and decimal bounds). Invalid values fall back to the default with `outcome=config_invalid`. Keep `EXCLUDED_REPOS` for `RUNNER_SCALER_REPOS`; a repository on the overflow list is not rejected by it.
2. `server.py`: route accepted `workflow_job` events for overflow repositories to the candidates set. Keep the "labels exactly `["do-runner"]`" rule.
3. `reconcile.py`: eligibility read and cache, the two caps, the log lines above. The full scan includes overflow repositories.
4. `do_client.py`: `invoice_preview()` and `invoice_items(uuid)` with pagination (`per_page=200`), plus `overflow_spend_usd(slugs)`.
5. `alerts.py`: alert kinds `overflow_ineligible` and `overflow_budget_reached`.
6. `app.yaml`: the six keys with their defaults.
7. Tests (see Tests), README table, `agents.d/` fragment for the new `RUNNER_SCALER` reasons, `changelog.d/` fragment, registry line in `docs/scripts-pending-removal.md`.

### Phase 2

1. `ci.yml`: add the input; change the nine `runs-on` lines; extend `run-name`.
2. `scripts/ci_sync_quick.sh` and any other reader of `CI [pr:<n>]` run names found by `git grep -n 'CI \[pr:'`: accept the `[do]` suffix as a full run.
3. `runner-boot.sh`: add the packages the new test reports missing, and their commands to `SELF_CHECK_COMMANDS`.
4. Tests for the expression table and the tool inventory; README; fragments.

### Phase 3

1. `pr_checks_lib.sh`: the helper, the two call sites, the refused-dispatch retry.
2. `review_final_ci_sweep.sh`: the stuck rule.
3. `env:` lines in `review_autofix.yml`, `review_autofix_sweep.yml`, `orchestrate_poll.yml`; check each file's size (§27).
4. `scripts/stage_switches.json`: `FINAL_CI_OVERFLOW_ENABLED` under `off_on_purpose`.
5. Tests; README rows for the four variables; `agents.d/` fragment for the `FINAL_CI_OVERFLOW` prefix; `changelog.d/` fragment.

## Files & Modules

- `tools/runner_scaler/runner_scaler/config.py`, `reconcile.py`, `do_client.py`, `server.py`, `alerts.py`
- `tools/runner_scaler/app.yaml`, `tools/runner_scaler/bootstrap/runner-boot.sh`
- `tests/test_runner_scaler_{reconcile,clients,webhook}.py`, `tests/runner_scaler_fakes.py` (extended)
- `.github/workflows/ci.yml`
- `.github/workflows/review_autofix.yml`, `review_autofix_sweep.yml`, `orchestrate_poll.yml` (`env:` only)
- `scripts/pr_checks_lib.sh`, `scripts/review_final_ci_sweep.sh`, `scripts/ci_sync_quick.sh`
- `scripts/stage_switches.json`
- `tests/test_runner_scaler_bootstrap.py` (tool inventory) and new or extended test files under `tests/` for Phases 2 and 3
- `README.md`, `docs/scripts-pending-removal.md`
- `agents.d/<n>-*.md` and `changelog.d/<n>-*.md`, one of each per phase `[new]`

## Tests

Unit, all runnable without network:

- **Scaler eligibility:** a run passes only with all four O5 fields right; each wrong field alone is refused with its `check=` value; a fork head repository is refused; an empty actor list refuses everything; the run read is made once per run id.
- **Scaler caps:** the overflow cap binds before the global cap; budget reached creates nothing and alerts once; a failed invoice read keeps the last value, then blocks after 6 hours; consumer repositories are unaffected by the budget; `RUNNER_SCALER_REPOS=shubhodeep1/coding-workflows` is still dropped with `self_repo_excluded`.
- **Spend:** invoice items for two overflow slugs, a consumer slug and other products sum to the overflow slugs only; droplets newer than `updated_at` are added once.
- **`ci.yml` expression:** the table under Phase 2 "Done", evaluated by the CI-structure tests.
- **Tool inventory:** every command the nine jobs call is provided.
- **Dispatcher:** switch off; no run id; read failure; no sample in the window; below and at the threshold; refused dispatch retried without the input; docs-only and sync-quick never carry the input.
- **Sweep:** a `[do]` run with a `do-runner` job queued past the limit gets one hosted replacement; a hosted run never does; the replacement does not trigger the rule again.

End to end, in the rollout: the canary in step 4 below.

## Risks & Mitigations

- **A fork or an unexpected workflow gets a droplet.** O5's four checks, the empty default for actors, and the owner's Actions setting. Covered by tests.
- **A droplet created for an eligible job is claimed by another queued `do-runner` job in the repository.** Such a job exists only if a workflow file on a ref of this repository asks for the label, or an approved fork run does. ACCEPTED: the Actions setting blocks unapproved fork runs, the droplet is single-use with no stored credential in its own VPC, and the ineligible job raises an alert.
- **CI jobs fail on droplets for a missing tool or a slower disk.** The tool-inventory test, the canary, and O8's fallback. A job that fails for this reason on a droplet would send the pull request back to review, so the switch stays off until the canary is green.
- **Droplet boot adds 1 to 2 minutes a job.** ACCEPTED: the alternative that night was a 65-minute median wait.
- **The invoice preview lags.** Bounded by the scaler's own estimate since `updated_at` and by the droplet cap: one day of lag at the cap is about $34.
- **Overflow masks the real problem.** The 24-hour and 7-day reports in the rollout show overflow minutes; project #10097 and issue #10156 stay the remedy for demand.
- **The measurement is one night.** ACCEPTED, pending the rollout's reports: the budget default is deliberately low and the owner resizes it from real numbers.

## Rollout

Each step that creates or changes an external resource is ask-first and performed by the session after approval (§22.B, §23.C).

1. **Prerequisites.** The first plan's R1 to R4 are done for the consumers, so the fleet is proven. Phases 1 to 3 of this plan are on `stable` (the scaler deploys from `stable`). Issue #10131 is merged.
2. **Owner steps.** Install the GitHub App on coding-workflows; add `billing:read` to the DigitalOcean token; set the repository's Actions setting to require approval for all outside contributors.
3. **Scaler settings (session, after approval).** Set `RUNNER_SCALER_OVERFLOW_REPOS=shubhodeep1/coding-workflows` and `RUNNER_SCALER_OVERFLOW_ACTORS` to the pipeline's account, with `RUNNER_SCALER_DRY_RUN=true` first; check the `RUNNER_SCALER` log lines; then turn dry run off.
4. **Canary (session, after approval).** Dispatch one full final CI for an open pipeline pull request with `overflow_runner=do-runner` while the switch is still off. Pass: all nine routed jobs run on `do-runner-*` runners and succeed, `lint` runs on a hosted runner and posts `ci/final success`, the droplets are destroyed.
5. **Turn on.** The owner sets `FINAL_CI_OVERFLOW_ENABLED=true`.
6. **Report.** After 24 hours and after 7 days an interactive session reports: overflow runs and runner-minutes, month-to-date spend from the invoice preview, median runner wait before and after, fallbacks and ineligible alerts.
7. **Rollback.** Set `FINAL_CI_OVERFLOW_ENABLED=false`; new final runs go to hosted runners at once. Set `RUNNER_SCALER_OVERFLOW_REPOS` empty once no `do-runner` job is queued in the repository.

## Future work

- **Review, audit and judge overflow.** They are 16% of the load and carry credentials. Needs its own plan: an input through the 17 routed workflows and their `internal-*.yml` wrappers, and a decision on credentials on droplets for a public repository.
- **Per-job overflow.** Send only the 32 `tests-system-python` shards when the wait is short.

## References

- `docs/plans/digitalocean-runners-for-consumer-pipelines-plan.md` (PRs #9722, #9723, #9725)
- `docs/plans/merge-lease-short-critical-section-plan.md` (project #10097)
- `docs/plans/cut-ci-runner-time-plan.md`
- Issue #10156 (delta suite on `main`), issue #10131 (cancelled final CI posts a failure), issue #10048 (priority lane)
- CLAUDE.md §1, §4, §6, §14, §15, §18, §22, §23, §27, §33
