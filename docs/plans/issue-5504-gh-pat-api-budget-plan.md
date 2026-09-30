# GH_PAT API budget: measure per job, cut the top poller and review consumers, re-queue failed /reclarify routing

Source issue: shubhodeep1/coding-workflows#5504 (https://github.com/shubhodeep1/coding-workflows/issues/5504)
Base branch: main
Security pass: run

## Summary

The `GH_PAT` account ran out of its 5,000-per-hour REST budget twice in 70 minutes on 2026-09-30, and two `/reclarify` comments were never routed. This plan adds a searchable per-job budget log and a daily top-consumer report, cuts the per-PR REST loops the evidence already points at, and makes a failed `/reclarify` routing re-queue itself instead of disappearing.

## Context

- Issue #5504 has the incident (Clarify runs 36670937896 and 36676584568), the four requested changes, and the tests it wants. #5495 (the `gh_retry` stdout leak behind the "Invalid format 'null'" crash) and #5500 (the Claude-fixer hand-off post) are separate open issues. This plan does not touch either fix.
- Evidence gathered while planning (2026-09-30, through the session's own App token, not the PAT):
  - Actions runs since 05:00Z: about 180 `Internal: AI Review & Autofix` runs in about 1.5 hours (pull_request 68+, push 50+, workflow_dispatch 42+). The four comment-triggered workflows (Clarify, Plan, Implement, Clarify Respond) ran about 93 times an hour each, but almost all were `skipped` at the job `if:`, so they cost nothing.
  - 96 open PRs, 32 of them non-draft.
  - Static call inventory of the PAT jobs:
    - The poller's standalone conflict sweep (`scripts/orchestrate_poll_process.sh`, "Standalone PR conflict sweep") issues one REST `pulls/<n>` read per open PR on every tick, and reads draft and non-conflicted PRs only to skip them. Up to 100 calls a tick.
    - The noop-suspicious sweep right after it reads every open PR's paginated issue comments on every tick, to find the few PRs with an `Editor no-op suspicious` warning. At least one call per PR, and more for long `claude/*` PRs.
    - `scripts/review_collect_pr_metadata.sh` paginates PR comments at the API default of 30 per page instead of 100, on every codex-agent run.
    - The review `gate` job is already well batched (about 5-8 calls).
- The PAT budget is per GitHub user, so every repository that uses the same `GH_PAT` shares it (this repo and the 14 consumers in `.github/ai/consumer_repos.json`). `GITHUB_TOKEN` has its own budget per repository, and the search API has its own separate limit.
- Clarify is triggered only by `issues: opened` and `issue_comment` events. Comments written with `GITHUB_TOKEN` never trigger workflows, so a durable retry needs a scheduled job that re-posts `/reclarify` with the PAT.

## Goals

1. Each instrumented PAT job logs one `GH_PAT_BUDGET phase=start …` line and one `GH_PAT_BUDGET phase=end …` line. The end line carries the account-wide `remaining`, a `used_in_job` delta, and an exact per-job `gh_calls` count. Instrumented jobs:
   - review `gate` and `codex-agent`;
   - clarify;
   - the orchestrator poller;
   - the review sweep and the Claude PR catch-all;
   - the Claude issue intake;
   - the new re-queue sweep.
2. A daily scheduled report ranks the top PAT consumers per hour over the last 24 hours, by workflow and job. It publishes to the job summary, a JSON artifact, and `GH_PAT_BUDGET_REPORT` log lines.
3. The poller's conflict sweep reads a PR over REST only when the existing list call says the PR is conflicted (`mergeStateStatus` `DIRTY`, or the field is missing).
4. The poller's noop sweep narrows its candidates with one search-API call per tick, and falls back to the current per-PR scan on any search failure.
5. The review metadata collector paginates with `per_page=100`.
6. A human `/reclarify` whose clarify run fails before routing completes is labelled `ai:reclarify-requeue`, using `GITHUB_TOKEN`, with a visible comment. An hourly sweep re-posts `/reclarify` with the PAT once the budget is healthy. Each human `/reclarify` gets at most one automatic retry.
7. Separate credentials for critical and high-volume paths are proposed in `agents.md` but not implemented.

## Non-goals

- The `gh_retry` stdout leak (#5495) and the hand-off post robustness (#5500).
- Refactoring the six per-reviewer watchdog pollers, the check-run poll loop, or the orchestrator's per-wave timeline re-reads. The daily report measures them first (§8), and any later cut is a separate issue.
- Creating or rotating credentials, or changing secrets (§23.C).
- Re-queueing failures on `issues: opened` events. Re-posting those as `/reclarify` would change orchestrator fast-path semantics.
- Instrumenting the rare review jobs (`post-merge-validate-dispatch`, `post-merge-force-poll`, `deterministic-skip-merge`, `claude-fixer-auto-merge`, `fingerprint-cap-block`).

## Constraints

- §5: every change is additive. The budget steps are `continue-on-error` and fail open. The poller filters only drop reads whose result the current code already discards.
- §6: no rename. The new identifiers are verified unique:
  - `scripts/gh_pat_budget.sh`, `scripts/gh_pat_budget_report.py`, `scripts/reclarify_requeue_sweep.py`;
  - `.github/workflows/gh-pat-budget-report.yml`;
  - the `GH_PAT_BUDGET*` and `RECLARIFY_REQUEUE*` log keys and env vars, and `NOOP_SEARCH_PREFILTER_ENABLED`;
  - the label `ai:reclarify-requeue`.
  Existing step names stay. The Claude handoff step only gains an `id`.
- §4: new env vars and repo vars have defaults:
  - `GH_PAT_BUDGET_LOG_ENABLED` = true;
  - `NOOP_SEARCH_PREFILTER_ENABLED` = true;
  - `RECLARIFY_REQUEUE_MIN_BUDGET` = 500;
  - `RECLARIFY_REQUEUE_MAX_PER_REPO` = 20;
  - `GH_PAT_BUDGET_REPORT_SAMPLE_RUNS` = 20.
- §15: the budget helper reads `GET /rate_limit`, which GitHub does not count against the limit. The report runs on `GITHUB_TOKEN` (the repo's budget, not the PAT's). The re-queue sweep costs one list call per repository plus two calls per re-queued issue.
- §18: the report and the re-queue sweep run on schedules (a new daily workflow, and the existing hourly `17 * * * *` tick of `review_autofix_sweep.yml`). No manual script. Neither is single-use or long-running, so §18.F adds no registry entry.
- §19: phase, fix, and completion PRs use `Refs #5504`. The final PR uses `Fixes #5504`. #5504 is not an `ai:orchestrator-tracking` issue.
- §20: add the changelog fragment `changelog.d/5504-gh-pat-budget.md`.
- §27: `review_autofix.yml` is at 455,228 bytes. The added steps stay small, and the file must end under 480,000 bytes (checked with `wc -c`).
- §9: YAML uses 2 spaces, shell and Python use tabs.

## Approach

**Budget helper (`scripts/gh_pat_budget.sh start|end`).** A self-contained bash script that needs only `gh` and `jq`, so it can run in any job, including the review gate, which stages nothing except a sparse, identity-verified checkout.
- `start`:
  - reads `/rate_limit` for the token in `GH_TOKEN` and prints `GH_PAT_BUDGET phase=start repo=… workflow=… job=… run_id=… attempt=… remaining=… limit=… reset=…`;
  - writes the start values to `$GITHUB_ENV`;
  - installs a counting `gh` shim into `$RUNNER_TEMP` and prepends it with `$GITHUB_PATH`. The shim appends the subcommand to a counter file, then `exec`s the real `gh`.
- `end`:
  - reads `/rate_limit` again and prints `GH_PAT_BUDGET phase=end … remaining=… used_in_job=<delta|na> window_rolled=<0|1> gh_calls=<n>`.
  - `used_in_job` is the account-wide drop during the job, so it includes concurrent jobs. `gh_calls` is this job's own `gh` invocations (API subcommands only). It undercounts `--paginate` pages and calls made without `gh`.
- Both subcommands always exit 0, and any failure becomes a `status=` field on the line.

**Wiring.** Each instrumented job gets a start step as early as its copy of the script exists, and an end step with `if: always()`. Both are gated by `vars.GH_PAT_BUDGET_LOG_ENABLED != 'false'`, use `continue-on-error: true`, and set `GH_TOKEN: ${{ secrets.GH_PAT }}`. The gate gets one extra sparse checkout of `scripts/gh_pat_budget.sh` from the gate-verified support commit. codex-agent and clarify use their verified `.codex-workflow-src` checkout. The poller stages the script with its gh retry helper. The sweep, catch-all, and intake jobs use their checkout of this repo (the `sweep` job gains a `persist-credentials: false` checkout).

**Report (`scripts/gh_pat_budget_report.py`, workflow `gh-pat-budget-report.yml`, daily at 05:41 UTC plus `workflow_dispatch`).** Using `GITHUB_TOKEN` with `actions: read`:
1. List the last 24 hours of runs of the instrumented workflows in this repo. Split any window whose `total_count` exceeds 1,000.
2. Count non-skipped runs per hour.
3. Sample up to `GH_PAT_BUDGET_REPORT_SAMPLE_RUNS` completed runs per workflow, spread over the day, and read their jobs' logs for `GH_PAT_BUDGET phase=end` lines.
4. Estimate calls per hour per workflow and job as runs × mean `gh_calls`. The report also carries the lowest observed `remaining` per hour.
5. Write a Markdown table to `$GITHUB_STEP_SUMMARY`, a JSON artifact, and `GH_PAT_BUDGET_REPORT` lines. Emit a warning annotation when an hour's lowest `remaining` fell under 10% of the limit.
6. Fail open: a read error is logged and the report covers what it could read.

**Cuts.**
- *Conflict sweep:* add `isDraft,mergeStateStatus` to the existing `gh pr list`. Skip a draft `claude/*` PR, and skip a PR whose `mergeStateStatus` is present and not `DIRTY`, before the REST read. The REST read, and everything after it, is unchanged for `DIRTY` PRs and for a missing field.
- *Noop sweep:* before the loop, run one `search/issues` query (`repo:<r> is:pr is:open in:comments "Editor no-op suspicious"`) into a PR-number set. Skip PRs not in the set. The full scan runs as before when the search fails, returns `incomplete_results`, exceeds one page, or `NOOP_SEARCH_PREFILTER_ENABLED=false`.
- *Metadata collector:* append `per_page=100` to its three paginated reads.

**Routing protection.**
- In `clarify.yml`, a new failure-only step runs only when all of these hold:
  - the run failed (not cancelled; a cancel means a newer `/reclarify` superseded it);
  - the trigger was a human `/reclarify` comment;
  - routing never completed: `steps.clarify_route.outcome != 'success'`, or the new `claude_issue_handoff` step failed;
  - the comment body does not already carry the `<!-- ai:reclarify-requeued:v1 -->` marker.
- It then adds `ai:reclarify-requeue` with `GITHUB_TOKEN` and posts one comment with `<!-- ai:reclarify-requeue:v1 run=<id> -->` that explains the automatic retry. It uses the event's issue number, never the possibly corrupted `ISSUE_NUMBER` env.
- `scripts/reclarify_requeue_sweep.py`, as a new `reclarify-requeue` job on the hourly tick, walks this repo and every consumer repo:
  - it skips the cycle when the PAT's `core.remaining` is under `RECLARIFY_REQUEUE_MIN_BUDGET`;
  - it lists open issues with the label;
  - for each one (at most `RECLARIFY_REQUEUE_MAX_PER_REPO`), it posts `/reclarify` plus the requeued marker with the PAT, then removes the label.
  - A failed post keeps the label for the next hour. A failed label removal is logged; the next hour's re-post is harmless because clarify's concurrency group cancels the duplicate.
  - Dry-run follows the workflow's `dry_run` input.

**Credential split.** A proposal paragraph in `agents.md` and the final report. It names the paths, the secrets it would add, and why it is an operator decision.

Alternatives considered are recorded in the auto-decisions below.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase for one standalone issue.

1. **Phase 1: budget logging, daily report, poller and collector cuts, `/reclarify` re-queue.**
   - Files: everything under [Files & Modules](#files--modules).
   - Done when:
     - the tests below pass, and the repo's CI checks pass locally;
     - `review_autofix.yml` is under 480,000 bytes;
     - every instrumented job has a start step and an `always()` end step;
     - the report and re-queue jobs are wired to their schedules.
   - Rollback: revert the phase PR. For runtime control without a revert, set `GH_PAT_BUDGET_LOG_ENABLED=false` (removes the budget steps' effect) or `NOOP_SEARCH_PREFILTER_ENABLED=false` (restores the full noop scan). The report and re-queue jobs only add scheduled reads and an optional label.

## Implementation Steps

Phase 1:
1. Add `scripts/gh_pat_budget.sh` (start and end, the shim, fail-open) and `tests/test_gh_pat_budget.py`, using a fake `gh` on PATH.
2. Wire the start and end steps into:
   - `review_autofix.yml` (`gate`, with a sparse helper checkout; `codex-agent`);
   - `clarify.yml`;
   - `orchestrate_poll.yml` (stage the script beside `gh_helpers.sh`);
   - `review_autofix_sweep.yml` (`sweep`, `claude-pr-catch-all`);
   - `claude-issue-intake.yml`.
   Add `tests/test_gh_pat_budget_workflow_contract.py`.
3. Add `scripts/gh_pat_budget_report.py`, `.github/workflows/gh-pat-budget-report.yml`, and `tests/test_gh_pat_budget_report.py`.
4. In `scripts/orchestrate_poll_process.sh`, add the conflict-sweep list fields and the pre-read filter, and the noop-sweep search prefilter with its fail-open. Extend `tests/test_orchestrate_poll_process.py` and `tests/test_orchestrate_poll_noop_suspicious_recovery.py`. Pass `NOOP_SEARCH_PREFILTER_ENABLED` through `orchestrate_poll.yml`.
5. Add `per_page=100` in `scripts/review_collect_pr_metadata.sh`, with a test assertion.
6. In `clarify.yml`, add the Claude handoff step id and the "Queue failed /reclarify for automatic retry" step. Add the `ai:reclarify-requeue` label to `.github/ai/label_contract.v1.json`. Add `scripts/reclarify_requeue_sweep.py`, its `reclarify-requeue` job in `review_autofix_sweep.yml`, and `tests/test_reclarify_requeue_sweep.py`.
7. Documentation:
   - README and `agents.md` sections: the budget log, the report, the re-queue flow, the new vars, failure modes, and the credential-split proposal;
   - `docs/INVENTORY.md` rows for the new scripts and workflow;
   - the changelog fragment;
   - `ci.yml` registration of the new tests.

## Files & Modules

- `scripts/gh_pat_budget.sh` [new]
- `scripts/gh_pat_budget_report.py` [new]
- `scripts/reclarify_requeue_sweep.py` [new]
- `.github/workflows/gh-pat-budget-report.yml` [new]
- `.github/workflows/review_autofix.yml`
- `.github/workflows/clarify.yml`
- `.github/workflows/orchestrate_poll.yml`
- `.github/workflows/review_autofix_sweep.yml`
- `.github/workflows/claude-issue-intake.yml`
- `.github/workflows/ci.yml`
- `.github/ai/label_contract.v1.json`
- `scripts/orchestrate_poll_process.sh`
- `scripts/review_collect_pr_metadata.sh`
- `tests/test_gh_pat_budget.py` [new]
- `tests/test_gh_pat_budget_workflow_contract.py` [new]
- `tests/test_gh_pat_budget_report.py` [new]
- `tests/test_reclarify_requeue_sweep.py` [new]
- `tests/test_orchestrate_poll_process.py`
- `tests/test_orchestrate_poll_noop_suspicious_recovery.py`
- `README.md`, `agents.md`, `docs/INVENTORY.md`
- `changelog.d/5504-gh-pat-budget.md` [new]

## Tests

- Unit:
  - budget helper: line format, `$GITHUB_ENV` export, shim counting and pass-through (exit code, stdout), window roll, fail-open when `gh` is missing or errors;
  - report aggregation (window splitting, sampling, estimates, the low-remaining warning, fail-open);
  - re-queue sweep (budget floor, per-repo cap, post-then-unlabel, a failed post keeps the label, dry-run, registry loading).
- Poller behaviour through the existing `_run_poller` fake-`gh` harness:
  - conflict sweep: no `pulls/<n>` read for a non-`DIRTY` or draft `claude/*` PR, and an unchanged path for `DIRTY` and missing-field PRs;
  - noop sweep: the search prefilter skips non-matching PRs, and falls back to the full scan on search failure, incomplete results, or the kill switch.
- Contract:
  - every instrumented job has both budget steps, with the end step at `always()`;
  - the clarify re-queue step's `if:` and token;
  - the sweep job and the report workflow wiring;
  - the label contract entry;
  - `per_page=100` in the collector;
  - `review_autofix.yml` stays under 480,000 bytes (the existing `tests/test_workflow_file_size_limit.py`).
- CI: new tests registered in `ci.yml`. Inventory parity and actionlint/yamllint, as in the repo's standard checks.

## Risks & Mitigations

- The shim could break `gh` in a job. Mitigation: it `exec`s the absolute real path resolved before the PATH change, it is not installed when no real `gh` is found, and it has the kill switch `GH_PAT_BUDGET_LOG_ENABLED=false`.
- `used_in_job` is noisy under concurrency. ACCEPTED: `gh_calls` gives the per-job attribution, and the report uses it.
- Search index lag can hide a new noop warning for a tick. ACCEPTED: the next tick catches it, and search failure falls back to the full scan.
- A `mergeStateStatus` of `UNKNOWN` is skipped without a REST read. ACCEPTED: the current code already skips a REST `unknown`, and the next tick re-checks.
- A triage collaborator could add `ai:reclarify-requeue` by hand. ACCEPTED: the same people can already comment `/reclarify` themselves, so the label grants nothing new.
- The report's own reads could exhaust the repo's `GITHUB_TOKEN` budget. Mitigation: sampling is capped (about 300-500 calls a day), and it runs once a day.

## Rollout

Everything lands on the project branch, then in `main` with the final PR. Consumers get the reusable-workflow changes (review, clarify, poller) on the next `@stable` sync. The label reaches consumers through `sync_ai_labels.yml`. The report and re-queue sweep run only in this repo. The re-queue sweep walks consumer repos with the PAT. The first report covers a full day about 24 hours after the merge.

## Auto-decisions

- AD-1 [plan, 2026-09-30] How much of "cut the biggest consumers" belongs in this single phase, given that measured data cannot exist before the project merges into `main`? — Picked: A — Cut the consumers the current evidence already identifies (the poller's per-PR conflict and noop reads, and the collector's pagination), and let the daily report rank the rest. Alternatives: B — Measure and protect only, with no cuts; C — Also refactor the reviewer watchdogs, check-run polling, and orchestrator timeline re-reads now. Why: acts on verified §15 defects without speculative refactors (§8). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] Where does the budget logic live? — Picked: A — A standalone `scripts/gh_pat_budget.sh` that each job runs from its existing verified checkout (the gate via one sparse checkout). Alternatives: B — Functions in `scripts/gh_helpers.sh`, which the gate does not stage; C — A composite action referenced `@stable`, which would not resolve until the next release. Why: the only option that works in every instrumented job today. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] How are calls attributed to a job, when `remaining` is account-wide and about 40 projects run concurrently? — Picked: A — Log `remaining` and `used_in_job` as the issue asks, plus an exact `gh_calls` count from a counting `gh` shim. Alternatives: B — Only `remaining` and `used_in_job`; C — `GH_DEBUG=api` tracing. Why: without per-job counts, a "top consumers" report would mostly measure concurrency; C is noisy and risks leaking headers. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] Which jobs are instrumented? — Picked: A — Review `gate` and `codex-agent`, clarify, the poller, the review sweep, the Claude PR catch-all, the intake, and the new re-queue job. Alternatives: B — Also the five rare review jobs; C — Only review and clarify. Why: covers the high-volume paths the issue names while keeping `review_autofix.yml` well under the §27 guard. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Where does the daily report run, and where is it published? — Picked: A — A new internal daily workflow `gh-pat-budget-report.yml` on `GITHUB_TOKEN`, publishing to the job summary, a JSON artifact, and `GH_PAT_BUDGET_REPORT` log lines. Alternatives: B — A third cron on `review_autofix_sweep.yml`, which would need its `sweep` job's `if` rewritten; C — The weekly `workflow-log-analysis.yml`, which only runs weekly; D — Also update a tracking-issue comment. Why: a daily cadence with no change to existing schedules, and no PAT use. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-30] How does a failed `/reclarify` routing retry later instead of being lost? — Picked: A — The failure step labels `ai:reclarify-requeue` and comments with `GITHUB_TOKEN`; the hourly `reclarify-requeue` job re-posts `/reclarify` with the PAT once `core.remaining` is at least 500, then removes the label; one automatic retry per human `/reclarify`. Alternatives: B — A marker comment only, with the sweep scanning comments (more calls); C — A budget pre-check that defers the run without routing. Why: durable, visible, bounded, and it spends PAT calls only once the budget has recovered. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-30] Which PRs does the conflict sweep still read over REST? — Picked: A — Only those whose listed `mergeStateStatus` is `DIRTY` or missing, never draft `claude/*` PRs. Alternatives: B — Also `UNKNOWN`; C — Replace the REST read entirely. Why: identical outcomes to the current code, which drops non-dirty and unknown PRs after the read, and the REST read still gives the fresh head SHA for update-branch. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-30] How is the noop sweep's per-PR comment scan cut? — Picked: A — One search-API prefilter per tick, with fail-open to the full scan and the kill switch `NOOP_SEARCH_PREFILTER_ENABLED`. Alternatives: B — Skip `claude/*` heads when Claude-fixer mode is on; C — Aliased GraphQL comment batches. Why: the largest saving with unchanged decisions for every candidate PR; search has its own limit. Applied in: phase 1. Status: pending review
- AD-9 [plan, 2026-09-30] What is done about separate credentials? — Picked: A — Document a proposal in `agents.md` and the final report, with no code or secret changes. Alternatives: B — Add optional secret inputs now. Why: secrets are a §23.C operator decision, as the issue says. Applied in: no code change. Status: pending review
- AD-10 [plan, 2026-09-30] Which clarify failures are re-queued? — Picked: A — Only failures of a human `/reclarify` run before routing completed (`clarify_route` did not succeed, or the Claude handoff failed), never cancellations, and never a run already triggered by an automatic re-post. Alternatives: B — Every failed `/reclarify` run, including later LLM failures; C — Failures and cancellations. Why: protects exactly the routing path, avoids repeating LLM clarify runs, and respects the intentional cancel-in-progress. Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py` returned `{"skip": false, "reason": "no skip label"}`.

## References

- #5504, #5495, #5500
- CLAUDE.md §8, §15, §18, §20, §23.C, §27
- `scripts/orchestrate_poll_process.sh`, "Standalone PR conflict sweep" and "Standalone PR noop-suspicious recovery sweep"
