## Executive Summary

- **Fix the self-repo review bootstrap path first.** Three of the four failures in the window (`28074811452`, `28075817505`, `28075833147`) were `review_autofix` runs that the collector tagged as `Create Codex config`, but the logs actually fail on `stage_workflow_support.sh not found...` immediately before AI work starts. Fixing that single path would likely cut observed repo failure rate from `4/181` to `1/181` (~`2.2%` → `0.6%`). **Estimated impact:** high. **Confidence:** high.
- **Almost all AI review cost is concentrated in nine long `review_autofix` runs.** Exactly `9` runs produced **all `108` OpenRouter calls** in the sample; each run made `12` OR calls and lasted `2859-3306s`. This is the highest-leverage speed/cost target. **Estimated impact:** high. **Confidence:** high.
- **Idle orchestrator polls are overpaying for “no work.”** Eight recent `orchestrate_poll` runs (`28126727284` through `28137850019`) logged “No active orchestrator projects” yet still took `93-108s` (avg `100s`). In run `28137850019`, no-work was known by `00:08:36`, but the run continued until `00:10:00`. **Estimated impact:** high. **Confidence:** high.
- **A deterministic docs drift check is failing late in CI.** CI run `28078672186` failed after `405s` in `lint / Inventory parity` because `docs/INVENTORY.md` omitted `scripts/pr_checks_lib.sh`. Moving or automating that check would save minutes on each recurrence. **Estimated impact:** medium. **Confidence:** high.
- **Semble is operationally healthy; cache telemetry is not.** Observed Semble activity was light (`11` queries, avg `506ms`), and all `40` fallbacks were `context=contract-test` with `semble_runtime_fallbacks=0`. By contrast, `cache_hit_rate` was null in all telemetry-bearing runs and all OpenRouter token/cache totals stayed `0` despite `108` OR calls. **Estimated impact:** medium. **Confidence:** high.

## Speed Optimizations

1. **[Critical path] Short-circuit the second reviewer pass on the small-diff branch**
   - **Evidence:** `review_autofix` had `54` runs with `p50=11s` but `p95=3100.1s`. All `9` OR-using runs were long (`2859-3306s`) and each recorded `12` OR calls. In run `28085768807`, pass 2 started at `08:56:00` and finished at `09:12:48`; the log says `diff is 0 LOC < 200 threshold`. In run `28083293818`, pass 2 ran from `08:08:09` to `08:25:04` on the same small-diff branch (`diff is 69 LOC < 200`).
   - **Root cause:** the workflow still runs a full six-model second pass even when the pass-2 size gate has already classified the diff as “small.”
   - **Exact change:** in `scripts/review_run_reviewers.sh`, skip pass 2 when the small-diff branch is active and pass 1 is clean/low-severity; alternatively, reduce pass 2 to a 1-2 model subset on that branch.
   - **Estimated time savings:** about `16-17 minutes` per impacted long run.
   - **Implementation risk:** medium; keep the current full pass 2 for sensitive-path matches and high-severity pass-1 findings.

2. **[Critical path] Fast-exit no-work orchestrator polls**
   - **Evidence:** `orchestrate_poll` was `27/27` successful, but `p50=102s`. In the recent deep-dive sample, `8` no-work poll runs still took `93-108s` (avg `100s`). Run `28137850019` logged `Found 0 active tracking issue(s)` and `No active orchestrator projects` at `00:08:36`, then still ran `Checkout repository` and two memory event writes, finishing at `00:10:00`—about `83s` after no-work was known.
   - **Root cause:** `.github/workflows/orchestrate_poll.yml` keeps doing post-detection work after `has_work=false`, including unconditional repo checkout, git auth setup, a second support-checkout block, and `Record poll run start` (`if: always()`).
   - **Exact change:** gate all post-`find_tracking` checkout/setup/processing steps on `has_work == 'true'`; if idle-cycle telemetry must remain, emit one lightweight no-work event before those heavy steps.
   - **Estimated time savings:** about `60-80s` per no-work poll.
   - **Implementation risk:** medium; verify desired idle-cycle telemetry semantics before removing the extra steps.

3. **[Fail-fast] Move `Inventory parity` earlier in CI**
   - **Evidence:** CI run `28078672186` failed after `405s` in `lint / Inventory parity` with:
     - `docs/INVENTORY.md: Scripts: missing scripts/pr_checks_lib.sh`
     - `README.md:142: references scripts/pr_checks_lib.sh but docs/INVENTORY.md does not document it`
     - `agents.md:424: references scripts/pr_checks_lib.sh but docs/INVENTORY.md does not document it`
   - **Root cause:** a deterministic documentation drift check runs late in `.github/workflows/ci.yml`, after longer coverage and contract-test blocks.
   - **Exact change:** move `tests/inventory_parity.py` up next to JSON schema / prompt validation, before coverage gates.
   - **Estimated time savings:** about `5-6 minutes` on each docs-drift failure.
   - **Implementation risk:** low.

4. **[Micro-optimization] Early-exit no-op autofix sweeps**
   - **Evidence:** `18/25` `Internal: AI Review Autofix Sweep` runs logged `candidates=0`; those no-op sweeps still averaged `8.3s` (`6-13s`).
   - **Root cause:** `review_autofix_sweep.yml` computes `total`, then still snapshots active runs for two workflows before it can exit.
   - **Exact change:** return immediately when `total == 0`.
   - **Estimated time savings:** small per run; larger API savings than latency savings.
   - **Implementation risk:** low.

## Cost Optimizations

1. **Reduce the fixed 12-call reviewer fan-out**
   - **Evidence:** all `108` OpenRouter calls in the window came from `9` `review_autofix` runs: `28073874849`, `28076849154`, `28076850500`, `28078681639`, `28078707904`, `28080859361`, `28083293818`, `28085768807`, `28088634622`. Each logged `or_calls=12`. Run `28085768807` used six reviewer models (`minimax`, `moonshot`, `deepseek`, `mistral`, `qwen`, `grok`) across two passes; the summariser stayed on `openai/gpt-5.4-mini` with `reasoning=medium`.
   - **Root cause:** a fixed six-model, two-pass reviewer panel dominates cost; the mini summariser is not the main driver.
   - **Exact change:** keep the current summariser/editor settings, but shrink reviewer fan-out first:
     - short-circuit pass 2 on the small-diff branch, and/or
     - enable the existing reviewer-tier mechanism for non-sensitive PRs.
   - **Estimated savings:** lower-bound `50%` OR-call reduction on qualifying small-diff reruns (`12` calls → `6` if pass 2 is skipped); larger reductions are possible if tiered reviewer subsets are used.
   - **Quality-risk notes:** medium; preserve full review on sensitive-path matches and on high-severity pass-1 output.

2. **Make Semble replace prompt expansion instead of adding to it**
   - **Evidence:** total Semble activity was `11` queries / `144,401` logged bytes. `9` were `target=reviewer-context` (`125,680` bytes total, `~505ms` avg). In `28085768807` and `28083293818`, `SEMBLE_QUERY target=reviewer-context chunks=12 bytes≈14KB` happened immediately before `Reviewer iteration scoping: full-diff...` and a full two-pass review.
   - **Root cause:** on sampled long review runs, Semble is additive: it fetches retrieval context, but the main reviewer prompt still stays in full-diff/full-context mode.
   - **Exact change:** when reviewer scoping or targeted context succeeds, reduce or omit the broadest first-pass context blocks (`ORIGINAL_PR_DIFF`, oversized comment dumps, broad PR-wide fallback sections) instead of appending Semble on top of them.
   - **Estimated savings:** medium token savings and better cacheability; exact dollar impact is unquantified because OR token telemetry is missing.
   - **Quality-risk notes:** low-medium; keep direct file reads and conflict-resolver Semble retrieval as fallback paths.

3. **Cut no-op sweep GH API spend**
   - **Evidence:** `18` no-op sweep runs still execute the active-run snapshot loop. In code, that is `2 workflows × 2 statuses`, so the lower-bound avoidable cost is `72` extra `/actions/workflows/*/runs` calls in this sample window.
   - **Root cause:** no `total==0` short-circuit in `review_autofix_sweep.yml`.
   - **Exact change:** exit immediately after logging `AUTOFIX_SWEEP_START` when `candidates=0`.
   - **Estimated savings:** `72` lower-bound GH API calls in-window, plus minor runner time.
   - **Quality-risk notes:** low.

4. **Fix prompt-cache telemetry before doing deeper cache tuning**
   - **Evidence:** across `115` telemetry-bearing runs, `or_calls=108`, but `or_prompt_tokens=0`, `or_completion_tokens=0`, `or_total_tokens=0`, `or_cache_write_tokens=0`, `or_cache_read_tokens=0`, and `cache_hit_rate` was null for every run. In `28085768807`, per-call log lines still showed `cache_enabled=true` while cache token fields were `na`.
   - **Root cause:** OpenRouter usage/cache metrics are not being normalized into the collector reliably.
   - **Exact change:** ensure every OR response is routed through the usage normalizer and emitted into `cost_audit.py` fields; only then tune cache policy or prompt layout based on data.
   - **Estimated savings:** none immediately; this is instrumentation required to unlock the next round of real savings.
   - **Quality-risk notes:** none.

5. **Avoidable reruns are mostly runner-cost, not AI-token cost**
   - **Evidence:** all four failures were first-attempt runs (`retries=0`), and the three bootstrap failures plus the one docs-parity failure had zero OR usage.
   - **Root cause:** support bootstrap drift and manual inventory drift.
   - **Exact change:** fix the bootstrap path and fail inventory parity earlier.
   - **Estimated savings:** runner-minutes, not AI-token dollars.
   - **Quality-risk notes:** low.

**Other cost observations**
- **`CONTEXT_BUDGET_WARN`**: `0` actual events in the window.
- **`BREAK_GLASS`**: `0` actual events in the window.
- **Serena**: no actual `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` telemetry was observed, so there is no Serena cost center to tune yet.

## Reliability Improvements

1. **Repair the self-repo support bootstrap fallback**
   - **Failure evidence:** runs `28074811452`, `28075817505`, and `28075833147` all failed in `review_autofix`; the run metadata says `Create Codex config`, but the logs show the underlying error was `stage_workflow_support.sh not found in checked-out support sources`.
   - **Root cause category:** bootstrap/ref-resolution drift.
   - **Exact fix:** in `.github/workflows/review_autofix.yml`, after `.codex-workflow-src` and `.codex-workflow-src-main` miss, fall back to the already-checked-out workspace copy `scripts/stage_workflow_support.sh` when `github.repository == shubhodeep1/coding-workflows`; also add an immediate post-checkout assertion so the failure surfaces before downstream setup.
   - **Expected reliability impact:** removes `3/4` observed failures; `review_autofix` failure count would drop from `3/54` to `0/54`, and repo-level failures from `4/181` to `1/181` if no replacement failure is introduced.
   - **Rollback / fail-open:** keep the current hard-fail for consumer repos; make the local fallback self-repo only.

2. **Eliminate manual inventory drift as a CI failure mode**
   - **Failure evidence:** CI run `28078672186` failed because `docs/INVENTORY.md` did not include `scripts/pr_checks_lib.sh`, while both `README.md` and `agents.md` referenced it.
   - **Root cause category:** manual documentation parity drift.
   - **Exact fix:** either generate `docs/INVENTORY.md` from the actual repo inventory or add a dedicated update script/PR checklist item that is run alongside script additions; keep `tests/inventory_parity.py` as the guardrail.
   - **Expected reliability impact:** removes the only non-review failure class seen in the window.
   - **Rollback / fail-open:** none needed; keep the parity test even if generation is added.

3. **Keep Semble fail-open behavior, but separate contract-test fallbacks from runtime health**
   - **Failure evidence:** repo totals show `semble_fallbacks=40`, `semble_contract_test_fallbacks=40`, `semble_runtime_fallbacks=0`. The CI logs in `28078672186` and `28088631420` show repeated `SEMBLE_FALLBACK ... context=contract-test` from intentionally missing fake binaries.
   - **Root cause category:** test-harness noise, not production MCP instability.
   - **Exact fix:** keep the current fail-open runtime behavior, but report `context=contract-test` fallbacks separately in dashboards/alerts so they do not mask true runtime regressions.
   - **Expected reliability impact:** lower false-alarm rate; no change to production behavior.
   - **Rollback / fail-open:** none; the current runtime fail-open path is healthy.

**Reliability signals that were *not* problems in this window**
- No actual `BREAK_GLASS` events (`0`).
- No actual `CONTEXT_BUDGET_WARN` events (`0`).
- No Serena rollout failures: `serena_query_calls=0`, `serena_fallbacks=0`, `serena_probe_ok=0`, `serena_probe_failed=0`, `serena_probe_skipped=0`.
- The CI `Integration fingerprint verification FAILED` messages in slow run `28088631420` are protective fail-closed tests; they are blocking unsafe merge-resolve output, which is healthy behavior.

## AI Memory Health

- **Telemetry coverage:** `58` `AI_MEMORY_TELEMETRY` lines were found in the deep-dive logs:
  - `record-run-event`: `40`
  - `record-candidate`: `9`
  - `retrieve`: `9`
- **Retrieve effectiveness:** `5/9` retrieves returned records (**`55.6%` hit rate**).
  - Hit example: run `28085768807` recorded `records_selected=5`, `estimated_tokens=197`, `token_budget=1400`, `keyword_method=llm`.
  - Miss examples: runs `28083293818`, `28078681639`, `28078707904`, and `28080859361` all returned `records_selected=0`.
- **Token budget health:** average retrieve `estimated_tokens` was **`109.4`**, comfortably below the observed `1400` reviewer budget on hit cases.
- **Keyword selection:** `keyword_method=llm` for **all 9 retrieves**; no `plain` or `none` keyword mode was observed.
- **Negative health signals not observed:**
  - `fail_open: true` entries: `0`
  - `enabled: false` entries: `0`
  - retrieve pushes with retry count `>=3`: `0`
- **Missing operation coverage:** no `finalize-task`, `promote`, `compact`, `processed-command-claim`, or `processed-command-complete` operations were observed in the deep-dive subset.

**Recommendation**
- **Collapse idle poll memory writes.** Run `28137850019` had `has_work=false` yet still wrote both `poll_started` and `poll_completed` events, and each push took about `25s` wall-clock. The smallest safe change is to emit a single idle-cycle event—or only the completed event—when `has_work=false`.

## GH API Call Audit

_No raw per-run GH API counters were emitted in `workflow_log_report.json`, so this audit uses workflow code plus run/log evidence._

1. **Highest-redundancy pattern: no-op autofix sweeps**
   - **Evidence:** `18/25` sweep runs had `candidates=0`, yet `review_autofix_sweep.yml` still snapshots active runs for `internal-review.yml` and `review_autofix.yml` across `queued` and `in_progress`.
   - **Current pattern:** one open-PR list call, then `4` active-run list calls even when no PR is dispatchable.
   - **Recommended change:** return immediately on `total==0`.
   - **Estimated call-count reduction:** **lower bound `72` calls** in this window.
   - **Rate-limit benefit:** low-to-medium; this removes steady background churn.

2. **Existing good hygiene to preserve**
   - `review_autofix_sweep.yml` already snapshots active runs **once per workflow** instead of doing `N×2` per-PR workflow-run API fan-out.
   - `scripts/gh_helpers.sh` documents `autofix_retrigger_has_inflight_peer()` as **exactly one** `/actions/runs` call per invocation.
   - `scripts/orchestrate_poll_process.sh` already batches some issue label lookups via GraphQL instead of looping per issue.
   - **Recommendation:** keep these patterns; they align with the repo’s own API-hygiene comments.

3. **Lower-priority unbatched loop: cross-referenced PR lookups in the poller**
   - **Evidence:** `scripts/orchestrate_poll_process.sh::_subissue_closing_pr_number()` explicitly documents its tier-2 path as “1 timeline fetch plus up to one `gh api pulls/<n>` per cross-referenced PR.”
   - **Current pattern:** acceptable today because the code comments classify it as non-hot-path, but it is still a per-item REST loop.
   - **Recommended change:** if this path becomes frequent, cache PR bodies within a poll cycle or switch the tier-2 fetch to one GraphQL batch by PR number.
   - **Estimated call-count reduction:** variable; only worth doing if this path becomes visible in future telemetry.
   - **Rate-limit benefit:** low today.

4. **No evidence of rate-limit pressure**
   - **Evidence:** no sampled run summary or deep-dive log showed 429/secondary-rate-limit handling, and no GH API retry/backoff incidents stood out in the selected logs.
   - **Recommendation:** prioritize redundancy cuts over retry logic changes.

## Prompt Cache & Memory System

- **Prompt-cache observability is currently missing.**
  - `cache_hit_rate` was non-null in **`0/115`** telemetry-bearing runs.
  - `or_prompt_tokens`, `or_completion_tokens`, `or_total_tokens`, `or_cache_write_tokens`, and `or_cache_read_tokens` were all `0` repo-wide despite `108` OR calls.
  - Reviewer logs still show `cache_enabled=true`, so this is an instrumentation gap, not proof that caching is unused.
- **There is already a good static-prefix optimization in place.**
  - `review_autofix.yml` writes `pre_assembled_static.txt`, which is the right direction for stable-prefix prompt caching.
- **The remaining fragmentation appears to come from volatile prompt sections.** _Inference._
  - Full PR diffs
  - untrusted comment dumps
  - check-run log tails
  - full-diff reviewer mode on first or fail-open iterations (`28085768807` first iteration; `28083293818` full-diff because `LEDGER_STATUS_FILE` was missing)
- **Semble is not yet replacing enough volatile prompt material.**
  - In the long review runs, `SEMBLE_QUERY target=reviewer-context` happened, but the reviewer still stayed in `full-diff` mode.
- **No prompt-pressure alarms fired.**
  - `context_budget_warn_count=0`
  - `break_glass_count=0`

**Concrete improvements**
1. Fix OR usage/cache metric emission first.
2. Keep `pre_assembled_static.txt`, but push volatile sections as late in the prompt as possible.
3. When reviewer scoping/targeted context succeeds, use it to **remove** broad PR-wide sections rather than only appending Semble context.
4. Preserve current fail-open behavior; there is no evidence that prompt pressure is causing runtime failures yet.

## Orchestrator Health

- **Core orchestrator success rate looks good.**
  - `orchestrate_poll`: `27/27` success, `p50=102s`, `p95=156.4s`
  - `clarify`: `13` runs, all skipped, `p50=6s`
  - `implement`: `13` runs, all skipped, `p50=1s`
  - `orchestrate_clarify_respond`: `13` runs, all skipped, `p50=1s`
- **There is no evidence of clarification loops or stuck terminal states in this window.**
  - The sampled `clarify` / `implement` / `respond` runs were mostly expected guard-path skips, not retries or loopbacks.
- **The main orchestrator pain point is idle-cycle overhead, not correctness.**
  - Recent idle polls still cost ~`100s`.
  - No-work polls also performed memory writes and extra checkout/setup after the no-work condition was known.
- **Protective merge/conflict guardrails appear healthy.**
  - In slow CI run `28088631420`, `Integration fingerprint verification FAILED` correctly refused to create an `[ai-merge-resolve]` commit after sub-issue intent regressions. That is fail-closed behavior, not orchestrator instability.
- **Recommended observable indicators**
  - `%` of poll runs with `has_work=false`
  - average duration of `has_work=false` polls
  - `review_autofix` `p95` duration
  - count of `stage_workflow_support.sh not found` failures
  - AI memory retrieve hit rate
  - `semble_runtime_fallbacks` (should stay `0`)

## Pipeline Flow Bottlenecks

1. **Review/autofix compute is the dominant end-to-end bottleneck**
   - Evidence: `review_autofix` `p95=3100.1s`; the `9` OR-using runs averaged `3039.6s` and accounted for all `108` OR calls.
   - Phase location: review → summarise → re-review.
   - Fix first: shorten or subset pass 2.

2. **Idle poll setup/telemetry is the next biggest waste**
   - Evidence: recent no-work `orchestrate_poll` runs averaged `100s`.
   - Phase location: orchestrate polling loop.
   - Overhead type: queue/setup + duplicated checkout + memory write overhead.

3. **Late deterministic CI gates waste runner time**
   - Evidence: `28078672186` failed after `405s` on inventory parity.
   - Phase location: validate/lint.
   - Overhead type: fail-late validation.

4. **Queueing overhead exists on lightweight maintenance/status workflows, but it is low priority**
   - Evidence: `issue_pr_status` ran in `11-16s`; `cancel_on_pr_close` ran in `10-15s`; recent logs explicitly show hosted-runner wait dominating these flows.
   - Phase location: status/cancel side flows.
   - Overhead type: queueing, not compute.
   - Recommendation: defer until the review/poll bottlenecks are fixed.

5. **Clarify and implement are not current bottlenecks**
   - Evidence: both were entirely skip-path dominated in this sample (`13` runs each, all skipped, `p50` `6s` and `1s` respectively).
   - Recommendation: do not spend optimization effort there yet.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - Long `review_autofix` runs (`p95=3100.1s`) driven by fixed two-pass reviewer fan-out
  - Idle `orchestrate_poll` cycles (`p50=102s`) even with no active tracking issues
  - Late deterministic CI docs-parity failure
- **Top failure modes**
  - Support bootstrap failure: `stage_workflow_support.sh not found...` in runs `28074811452`, `28075817505`, `28075833147`
  - Inventory drift failure: `scripts/pr_checks_lib.sh` missing from `docs/INVENTORY.md` in run `28078672186`
- **Highest-cost drivers**
  - All `108` OpenRouter calls came from `9` long `review_autofix` runs
  - All `11` Semble queries came from the same long review cluster
  - Idle polls and no-op sweeps mostly burn runner/setup/API overhead, not AI tokens
- **Top 3 prioritized actions**
  1. Add a self-repo fallback for `stage_workflow_support.sh` in `review_autofix.yml`
  2. Fast-exit `orchestrate_poll` when `has_work=false`, including collapsing idle memory writes
  3. Trim the small-diff second reviewer pass in `scripts/review_run_reviewers.sh`

## Metrics Appendix

**Scope note:** run counts come from `workflow_log_report.json`; run-specific evidence comes from deep-dive logs under `/home/runner/work/_temp/workflow-log-output/{errors,slow,recent}`.

### Overall and key workflow-family metrics

| Scope | Runs | Success | Failure | Cancelled | Skipped | Failure rate | p50 dur (s) | p95 dur (s) | wall p50 (ms) | wall p99 (ms) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Repo total | 181 | 117 | 4 | 7 | 53 | 2.2% | 10.0 | 1818.0 | 12000 | 3167080 |
| `review_autofix` | 54 | 45 | 3 | 5 | 1 | 5.6% | 11.0 | 3100.1 | 11000 | 3252000 |
| `ci` | 12 | 9 | 1 | 2 | 0 | 8.3% | 1727.5 | 1817.4 | 1727500 | 1815900 |
| `orchestrate_poll` | 27 | 27 | 0 | 0 | 0 | 0.0% | 102.0 | 156.4 | 101000 | 111000 |
| `issue_pr_status` | 4 | 4 | 0 | 0 | 0 | 0.0% | 12.0 | 15.5 | n/a | n/a |
| `cancel_on_pr_close` | 4 | 4 | 0 | 0 | 0 | 0.0% | 11.5 | 14.7 | n/a | n/a |

### AI / cache / control-plane telemetry totals

| Metric | Value | Notes |
|---|---:|---|
| Runs with parsed log telemetry | 115 | repo-wide |
| `codex_calls` | 1 | all repo runs |
| `codex_tokens_used` | 2026 | one observed codex-usage run |
| `or_calls` | 108 | all from 9 long `review_autofix` runs |
| `or_prompt_tokens` | 0 | telemetry gap |
| `or_completion_tokens` | 0 | telemetry gap |
| `or_total_tokens` | 0 | telemetry gap |
| `or_cache_write_tokens` | 0 | telemetry gap |
| `or_cache_read_tokens` | 0 | telemetry gap |
| `cache_hit_rate` | null in 115/115 runs | no non-null emissions observed |
| `break_glass_count` | 0 | actual events only |
| `context_budget_warn_count` | 0 | actual events only |

### Long review/autofix OpenRouter cluster

| Run ID | Duration (s) | OR calls | Semble calls | Semble bytes |
|---|---:|---:|---:|---:|
| 28073874849 | 3306 | 12 | 1 | 14641 |
| 28085768807 | 3171 | 12 | 3 | 32767 |
| 28078707904 | 3143 | 12 | 1 | 12357 |
| 28078681639 | 3077 | 12 | 1 | 13776 |
| 28080859361 | 3036 | 12 | 1 | 14075 |
| 28088634622 | 2934 | 12 | 1 | 14046 |
| 28076849154 | 2934 | 12 | 1 | 14641 |
| 28076850500 | 2896 | 12 | 1 | 14046 |
| 28083293818 | 2859 | 12 | 1 | 14052 |

**Cluster summary:** `9` runs, avg duration `3039.6s`; this cluster accounts for **all `108` OR calls** in the sample.

### Semble / Serena / MCP summary

| System / target | Query calls | Logged bytes / response bytes | Avg observed latency | Fallbacks | Notes |
|---|---:|---:|---:|---:|---|
| Semble `reviewer-context` | 9 | 125680 bytes | 505.2 ms | 0 runtime observed | additive on long review runs |
| Semble `overflow` | 1 | 7982 bytes | 510.0 ms | 40 contract-test total repo-wide | sampled runtime query seen in `28085768807` |
| Semble `conflict-resolver-context` | 1 | 10739 bytes | 506.0 ms | 0 runtime observed | seen in `28085768807` |
| **Semble total** | **11** | **144401 bytes** | **505.7 ms** | **40 total / 40 contract-test / 0 runtime** | all fallbacks were test-only |
| Serena (all targets) | 0 | 0 | 0 | 0 | no actual query/probe/fallback telemetry |
| Other MCP servers observed | 0 | 0 | 0 | 0 | none observed |

### MCP availability rows

| Server | Target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---:|---:|---:|---|
| Serena | `<none observed>` | 0 | 0 | 0 | sampled logs only showed config echoes like `SERENA_ENABLED: false` |
| Other MCP | `<none observed>` | 0 | 0 | 0 | no `<NAME>_PROBE` telemetry observed |

### AI memory telemetry

| Metric | Value |
|---|---:|
| Total `AI_MEMORY_TELEMETRY` lines | 58 |
| `record-run-event` | 40 |
| `record-candidate` | 9 |
| `retrieve` | 9 |
| Retrieve hit rate | 55.6% |
| Avg retrieve `estimated_tokens` | 109.4 |
| `keyword_method=llm` | 9 |
| Zero-record retrieves | 4 |
| `fail_open: true` retrieves | 0 |
| `enabled: false` retrieves | 0 |
| Retrieve pushes with retries `>=3` | 0 |

### GH API summary (lower-bound / code-derived)

| Workflow / path | Current pattern | Evidence | Estimated reduction |
|---|---|---|---:|
| `review_autofix_sweep.yml` no-op ticks | still snapshots 2 workflows × 2 statuses after `candidates=0` | `18/25` sweeps no-op | 72 avoidable workflow-run list calls |
| `gh_helpers.sh::autofix_retrigger_has_inflight_peer()` | already bounded to one `/actions/runs` call | local code comment + implementation | keep as-is |
| `orchestrate_poll_process.sh::_subissue_closing_pr_number()` | tier-2 path can do one timeline fetch + per-PR `pulls/<n>` GETs | local code comment + implementation | only worth batching if this path becomes hot |

### Sampled idle poll overhead

| Run ID | Duration (s) | No-work observed? |
|---|---:|---|
| 28126727284 | 93 | yes |
| 28128971477 | 99 | yes |
| 28131051765 | 100 | yes |
| 28132538743 | 97 | yes |
| 28134186070 | 102 | yes |
| 28135425728 | 104 | yes |
| 28136664338 | 108 | yes |
| 28137850019 | 97 | yes |

**Sample summary:** `8` recent no-work polls averaged `100.0s`.
