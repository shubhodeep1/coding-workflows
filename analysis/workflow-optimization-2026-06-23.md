## Executive Summary

- **Coarse-shard the monolithic CI `lint` job.** `ci` ran 39 times with p50/p95 **1694s / 1802.1s**, and all 13 failures happened inside the single serial `lint` job; **8/13** were the same `Shared shell-block anti-regression checks` failure. **Estimated impact:** ~6-10 minutes off median CI and faster fail-fast behavior. **Confidence:** high.
- **Fix review_autofix control-plane failures before tuning the model path.** `review_autofix` had **6 hard failures**: **4** `Create Codex config` bootstrap failures and **2** dirty-worktree checkout failures; cancelled review runs also burned **4.01h**. **Estimated impact:** removes most hard review failures and several minutes of dead-end rerun time per affected PR. **Confidence:** high.
- **Review prompts are too large too early.** All **36** `CONTEXT_BUDGET_WARN` events came from **6** slow `review_autofix` runs with roughly **187k-199k prompt tokens**, **16 OR calls**, and default reviewer/editor reasoning set to `xhigh`. **Estimated impact:** ~5-15 minutes off the slowest 60+ minute review runs, plus token savings once OR telemetry is fixed. **Confidence:** medium.
- **Hosted-runner queueing is a recurring bottleneck.** Deep-dive logs showed runner-wait messages on **39 unique runs** (**20 review_autofix, 9 CI, 6 poller**); the poller also performs support checkout before its work/no-work gate. **Estimated impact:** ~30-90 seconds per no-work/low-work control-plane run and lower queue pressure overall. **Confidence:** medium-high.
- **Telemetry is sufficient to find hotspots, but not to fully price them.** Aggregate telemetry shows **120 OR calls**, but OR prompt/completion/cache totals are all `0`, `cache_hit_rate` is `null`, Serena traffic is `0`, and AI memory retrieves hit **0/9**. **Estimated impact:** better future cost tuning and faster incident triage rather than immediate runtime savings. **Confidence:** high.

## Speed Optimizations

### Critical-path wins

1. **Split CI into 3-4 coarse jobs, not one giant `lint` lane.**
   - **Evidence:** `ci` consumed **13.7h** across **39 runs** with p50/p95 **1694s / 1802.1s**. `.github/workflows/ci.yml` puts static lint, shell checks, and dozens of unit/contract steps into one `lint` job. Run **27969435384** was cancelled at **1819s** after `278 passed, 0 failed`.
   - **Root cause:** serializing unrelated checks maximizes critical-path wall time and means late failures waste almost the whole job.
   - **Exact change:** split `lint` into a few coarse jobs such as `static-lint`, `shell-contracts`, `review-stack-tests`, and `orchestrator/validation-tests`; keep the existing fast-fail orchestrate subset at the front of the most failure-prone lane.
   - **Estimated time savings (inference):** ~6-10 minutes off median CI, ~10-15 minutes off p95 on non-queued runs.
   - **Implementation risk:** low-medium; avoid over-sharding because CI already saw runner-wait signals on **9** deep-dive runs.

2. **Cut review_autofix rerun latency by fixing setup failures first.**
   - **Evidence:** `review_autofix` consumed **28.3h** across **86 runs**. Failures were concentrated: **4/6** at `Create Codex config` and **2/6** at `Checkout PR head branch`. Run **27948945349** failed because runtime `scripts/codex_helpers.sh` was missing; run **27949127803** failed because `.ai/.workspace_source_manifest.txt` blocked checkout.
   - **Root cause:** control-plane breakage happens before useful model work starts.
   - **Exact change:** validate staged support scripts before `Create Codex config`, source/guard `tg_send_phase_failure`, and move or clean `.workspace_source_manifest.txt` before branch checkout.
   - **Estimated time savings (inference):** removes 3-8 minutes of dead-end time per failed review run plus human rerun delay.
   - **Implementation risk:** low.

3. **Reduce first-pass review prompt size and reasoning effort.**
   - **Evidence:** the slowest review runs were **27930927798 (4009s)**, **27942405726 (3823s)**, and **27949129254 (3659s)**. All had **16 OR calls**; runs like **27930927798** and **27949129254** emitted `CONTEXT_BUDGET_WARN` with prompt sizes from **186,982** to **190,549** tokens. `review_autofix.yml` defaults both reviewer and editor reasoning to `xhigh`.
   - **Root cause:** oversized prompts plus max reasoning on the first pass.
   - **Exact change:** default reviewer/editor first pass to `high`; keep `xhigh` only for judge/conflict/retry paths. Add a phase cap such as `MAX_PROMPT_TOKENS_FOR_REVIEW` or lower `TARGETED_FILE_CONTEXT_MAX_BYTES` from `102400` to ~`65536`.
   - **Estimated time savings (inference):** ~5-15 minutes on the slowest outliers, ~1-3 minutes on typical review runs.
   - **Implementation risk:** medium; preserve escalation when reviewer confidence is low.

### Micro-optimizations

4. **Move poller work discovery ahead of support checkout/setup.**
   - **Evidence:** `.github/workflows/orchestrate_poll.yml` checks out workflow support source and stages helpers before `Find active tracking issues`. Run **27997889579** waited for a hosted runner, then `Find active tracking issues` reported only **1 active tracking issue**. Poller p50/p95 were **182.5s / 289.1s** across **24** runs.
   - **Root cause:** control-plane setup runs before the cheap “is there work?” test.
   - **Exact change:** keep only minimal auth + `gh issue list` ahead of the gate; defer support checkout, Semble install, and heavier bootstrap until `has_work=true`.
   - **Estimated time savings (inference):** ~30-90 seconds on no-work/low-work poll cycles.
   - **Implementation risk:** low.

5. **Reduce queue-exposed no-op workflow fan-out.**
   - **Evidence:** skipped wrapper runs were frequent: `clarify` **139 skipped**, `plan` **133 skipped**, `implement` **131 skipped**, `orchestrate_clarify_respond` **136 skipped**. Deep-dive logs showed runner-wait messages on **39 unique runs** overall.
   - **Root cause:** broad event fan-out increases queue exposure even when most runs do almost nothing.
   - **Exact change:** keep command parsing and skip decisions ahead of checkout/setup in command-driven workflows; if Codex review is the required reviewer, make Copilot review opt-in rather than always-on.
   - **Estimated time savings (inference):** roughly 1-2 hours of runtime in a window like this plus lower queue contention.
   - **Implementation risk:** medium.

## Cost Optimizations

**Bottom line:** in this window, **runner minutes matter more than measured AI tokens**. Aggregate telemetry shows only **4052 Codex tokens**, while `review_autofix` + `ci` alone consumed about **42.0h**.

1. **Eliminate failed/cancelled control-plane work and duplicate review surfaces first.**
   - **Evidence:** `review_autofix` spent **4.01h cancelled** and **0.38h failed**; `copilot_pull_request_reviewer` added **18 runs / 1.2h**; measured AI token usage was low (`codex_tokens_used=4052`) and OR token usage is invisible.
   - **Root cause:** the dominant observed cost is runner time spent on duplicate or broken workflows, not measured model spend.
   - **Exact change:** fix review_autofix bootstrap/checkout failures; make Copilot review label-gated or opt-in if Codex review is already authoritative.
   - **Estimated savings (inference):** largest dollar savings in this window come from runner-minute reduction, not model swaps.
   - **Quality-risk notes:** low for failure elimination; medium if removing duplicate review coverage.

2. **Lower default review reasoning on the first pass.**
   - **Evidence:** reviewer/editor default to `xhigh`; slow runs with **16 OR calls** and repeated context warnings were the longest review runs. OR prompt/completion totals remain `0` in telemetry, so exact token cost is hidden.
   - **Root cause:** expensive reasoning applied before prompt size is under control.
   - **Exact change:** use `high` for first-pass reviewer/editor; reserve `xhigh` for judge/conflict/retry paths.
   - **Estimated savings (inference):** medium token savings and meaningful latency savings once OR telemetry is working.
   - **Quality-risk notes:** medium; keep automatic escalation.

3. **Make Semble replace overlapping context, not append to it.**
   - **Evidence:** deep-dive logs captured **19 `SEMBLE_QUERY` events / 187,950 bytes** across **7** review runs. Run **27949129254** queried the same two `overflow` files twice and still emitted **6** context warnings. Average logged Semble payload was about **9.9 KB/query**.
   - **Root cause:** Semble is targeted, but it is not reducing enough downstream prompt expansion; some same-file queries are repeated within one run.
   - **Exact change:** memoize Semble results by `{run,target,file}` and suppress duplicate raw file embeds when a Semble chunk for the same file already exists.
   - **Estimated savings (inference):** saves 1-2 Semble calls and ~12-15 KB of retrieved context on affected runs, plus downstream prompt savings.
   - **Quality-risk notes:** low-medium; keep raw-context fail-open fallback.

4. **Fix OR token/cache instrumentation before making larger model-selection decisions.**
   - **Evidence:** aggregate telemetry shows **`or_calls=120`**, but `or_prompt_tokens`, `or_completion_tokens`, `or_total_tokens`, `or_cache_write_tokens`, and `or_cache_read_tokens` are all `0`; `cache_hit_rate=null`.
   - **Root cause:** instrumentation gap, not proven zero usage.
   - **Exact change:** emit token and cache counters from the OR response path into workflow telemetry and `cost_audit.py`.
   - **Estimated savings:** no immediate savings, but this is the prerequisite for reliable dollar tuning.
   - **Quality-risk notes:** none.

5. **Keep Serena disabled until it measurably replaces work.**
   - **Evidence:** aggregate telemetry shows **0 Serena queries**, **0 response bytes**, **0 tool calls**, **0 fallbacks**, **0 probes**; deep-dive logs had no `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines.
   - **Root cause:** Serena is currently absent from the hot path.
   - **Exact change:** keep `SERENA_ENABLED=false` by default and do not spend tuning time on it until one concrete callsite shows replacement value.
   - **Estimated savings:** avoids engineering effort now and prevents future low-value response-byte noise.
   - **Quality-risk notes:** none.

## Reliability Improvements

1. **Repair review_autofix support-script bootstrap.**
   - **Failure evidence:** `review_autofix` hard failures were dominated by `Create Codex config`: **27948945349**, **27947559678**, **27948923360**, and recent **27998711204**. The logs show missing runtime `scripts/codex_helpers.sh`; **27948945349** and **27998711204** then hit `tg_send_phase_failure: command not found`.
   - **Root cause category:** control-plane bootstrap drift.
   - **Exact fix:** stage `codex_helpers.sh` and `tg_helpers.sh` into `SUPPORT_SCRIPTS_DIR` before `Create Codex config`, then run `check_required_file` immediately before sourcing.
   - **Expected reliability impact:** removes about **66.7%** of observed `review_autofix` hard failures.
   - **Rollback/fail-open:** if staged helpers are missing, fall back to the checked-out repo copy and warn rather than failing before review starts.

2. **Stop dirty-worktree checkout failures.**
   - **Failure evidence:** runs **27949127803** and **27955720279** failed `Checkout PR head branch` because `.ai/.workspace_source_manifest.txt` would be overwritten.
   - **Root cause category:** generated file written inside the tracked worktree.
   - **Exact fix:** move the manifest into `$RUNNER_TEMP` or an ignored path, or hard-reset only that generated file before checkout.
   - **Expected reliability impact:** removes the remaining **33.3%** of observed `review_autofix` hard failures.
   - **Rollback/fail-open:** scope cleanup only to the generated manifest; avoid broad `git clean`.

3. **Make the live CI workflow image-independent.**
   - **Failure evidence:** **8/13 CI failures** hit `Shared shell-block anti-regression checks` with `ripgrep (rg) is required` (for example **27952399830** and **27948117417**). The current repo head now uses `grep` in that step, which suggests the source fix exists but the failing runs did not execute it on **2026-06-22**.
   - **Root cause category:** workflow-version skew plus runner dependency assumption.
   - **Exact fix:** promote/backport the grep-based shell-block step to the branch/ref actually used by CI, or explicitly install `ripgrep` if you intentionally keep `rg`.
   - **Expected reliability impact:** removes about **61.5%** of observed CI failures.
   - **Rollback/fail-open:** prefer grep-based logic so runner-image changes stay non-fatal.

4. **Turn inventory drift into generated state.**
   - **Failure evidence:** run **27942405237** failed `Inventory parity` because `docs/INVENTORY.md` was missing `scripts/dev/test_write_guard.sh` and `scripts/write_guard.sh`.
   - **Root cause category:** manual documentation drift.
   - **Exact fix:** generate `docs/INVENTORY.md` from the filesystem in CI/pre-commit and diff generated output, instead of hand-maintaining it.
   - **Expected reliability impact:** removes a recurring doc-drift failure class.
   - **Rollback/fail-open:** start as a warning or generated patch artifact if needed.

5. **Cap post-green sync/conflict retry burn.**
   - **Failure evidence:** prompt-supplied CI run **27969435384** was cancelled at **1819s** after `278 passed, 0 failed`; its summary notes `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` was reached.
   - **Root cause category:** tail retry loop outliving useful work.
   - **Exact fix:** once tests are green, move integration-sync conflict recovery into a separate non-blocking or narrowly retried tail job.
   - **Expected reliability impact:** lowers cancellation/rerun rate and can save ~30 minutes on affected runs.
   - **Rollback/fail-open:** keep the current blocking behavior behind an opt-in env while validating the split.

**Telemetry interpretation**
- `BREAK_GLASS`: **0**. I did not see evidence of policy/rubric pressure forcing overrides.
- `CONTEXT_BUDGET_WARN`: **36** events across **6** slow `review_autofix` runs. This is prompt-size risk, not policy pressure.
- Semble fallbacks: the broader aggregate reports **65** fallbacks, all contract-test. Deep-dive logs cover **50** fallback events across **10 CI runs**, all `target=overflow` and `context=contract-test`. That is healthy fail-open test coverage, not runtime Semble breakage.
- Serena: no query/fallback/probe traffic was observed. That indicates absence, not a broken rollout.

## AI Memory Health

Deep-dive `AI_MEMORY_TELEMETRY` was present and consistent enough to assess.

| Metric | Value |
|---|---:|
| Total memory telemetry ops | 62 |
| `record-run-event` | 46 |
| `record-candidate` | 7 |
| `retrieve` | 9 |
| Retrieve hit rate | 0% (0/9) |
| Avg `estimated_tokens` on retrieve | 0 |
| `keyword_method` distribution | `llm`: 9 |
| Retrieves with `records_selected=0` | 9 |
| Retrieves with `enabled=false` | 0 |
| Deep-dive retrieves with `fail_open=true` | 0 |
| Max `push_attempts` seen | 2 |

- **Evidence:** runs such as **27930927798**, **27942405726**, and **27949129254** logged `retrieve` operations with `enabled=true`, `estimated_tokens=0`, `keyword_method="llm"`, and `records_selected=0`.
- **Interpretation:** the write path is active, but the retrieval path is currently returning no useful memory. `estimated_tokens=0` is not “efficient”; it means nothing was selected.
- **Not observed in deep-dive:** `finalize-task`, `promote`, `compact`, or `processed-command-*` operations.
- **Fail-open note:** a prompt-supplied run summary for **27967239889** reported one `AI_MEMORY_TELEMETRY` fail-open event (`force-tick-put`); I could not verify that run in deep-dive logs, so I treat it as isolated evidence.

**Recommended fixes**
1. Add a **hybrid retrieval fallback**: when `keyword_method=llm` returns 0 records, immediately retry with a cheap plain-keyword lookup.
2. Enrich write-side indexes with **workflow, phase, PR/issue number, changed files, and failure signature** so retrieval has usable keys.
3. Alert when rolling retrieve hit rate stays below a floor (for example **20%**) or when `estimated_tokens` stays at `0` across multiple runs.

## GH API Call Audit

The collector did **not** expose per-run GH API call counters in this folder, so this audit is based on workflow source inspection and log hotspots, not numeric call totals.

This repo already codifies the rule in **`CLAUDE.md §15`**: reuse existing calls, prefer batched GraphQL over per-item REST, and treat per-iteration `gh api` inside loops as a review blocker.

| Workflow / job | Evidence | Redundant pattern | Concrete change | Estimated reduction |
|---|---|---|---|---|
| `implement.yml` active issue-processing path | Reads issue payload at multiple points (`ISSUE_PAYLOAD`, later issue meta reloads, later label reloads, paginated comments snapshot) | Same issue/body/labels are fetched more than once in one job | Fetch one canonical issue JSON + one comments snapshot near start; derive labels/body/title locally and pass file paths through `$GITHUB_ENV` | 2-4 read calls per active implement run (inference) |
| `plan.yml`, `clarify.yml`, `orchestrate_clarify_respond.yml` | Full issue-comment history is fetched with paginated REST in each workflow | Each workflow rebuilds similar comment snapshots independently | Centralize a comment-snapshot helper and reuse the on-disk file across later steps; batch multi-issue reads with GraphQL when needed | 1-2 paginated reads per active run (inference) |
| `review_autofix_sweep.yml` | Paginates open PRs, then snapshots active workflow runs per workflow/status | Sweep still performs repeated paginated status fetches | Combine queued + in-progress status reads via one GraphQL query per sweep, or at least one batched snapshot per workflow without status fan-out | ~50% of sweep status calls (inference) |
| `orchestrate_poll` / `scripts/orchestrate_poll_process.sh` | Repo already has GraphQL batching helpers, but the poll path still does issue listing plus later per-issue expansion | Risk of falling back to per-issue REST in loop bodies | Keep all issue/PR expansion on the existing cycle-local caches and GraphQL helpers; never add per-issue `gh api` in the inner loop | O(N) to O(1-2) calls on multi-issue polls (inference) |

**Priority judgment:** the biggest GH API hygiene win is **reuse within `implement` and comment-driven workflows**, because those are frequent and active. The poller and sweep are secondary.

## Prompt Cache & Memory System

- **Prompt-cache visibility is currently missing.** Aggregate telemetry reports **`or_calls=120`**, but OR prompt/completion/cache counters are all `0`, and `cache_hit_rate=null`. That means I cannot tell whether prompt caching is helping at all.
- **Cache fragmentation is likely high** (inference). The review path mixes large volatile inputs—issue comments, review text, dynamic file lists, Semble output, and per-run runtime paths—into already-large prompts. Repeated same-file Semble queries in run **27949129254** suggest reuse is not stable yet.
- **Prompt growth is already hurting cache value.** `CONTEXT_BUDGET_WARN` fired **36** times across **6** review runs; examples reached **186,982-198,908** prompt tokens before model execution.
- **Workflow file caching exists, but that is not model prompt caching.** `actions/cache/restore@v4` / `save@v4` are present in several workflows, but they do not substitute for missing OR prompt-cache telemetry.

**Concrete improvements**
1. Make the **prompt prefix stable**: keep invariant system/rubric text first; push volatile comments, Semble chunks, and per-run deltas to the end.
2. Emit a **cache-key fingerprint** and actual OR **cache read/write counters** per call.
3. **Memoize Semble results per run** and inject only one copy of each file’s contextual chunk.
4. Add **phase-specific prompt caps** so the cacheable shape stays consistent.
5. Fix memory retrieval first; today it contributes **0 selected records**, so it is not improving prompt reuse.

## Orchestrator Health

- **Healthy signals**
  - `orchestrate_poll` was **24/24 successful**.
  - `BREAK_GLASS` stayed at **0**.
  - No runtime Semble fallback pattern was observed; fallbacks were contract-test-only.
  - No Serena runtime failures were observed because Serena traffic was absent.

- **Operational pain points**
  - `review_autofix` is the least healthy major family: **62 success / 6 failure / 13 cancelled / 5 skipped** across **86** runs.
  - The command-driven wrappers are noisy: `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` produced **539 skipped runs** combined.
  - A sync/conflict retry path can outlive productive work: prompt evidence from CI run **27969435384** shows retries continuing after all tests passed.
  - Hosted-runner queueing is widespread: **39 unique deep-dive runs** showed wait-for-runner signals.

- **What I could not directly observe**
  - The collector did not expose explicit wave counters, deferral counts, or stuck-state markers, so wave-progression assessment is inferred from run churn, cancellations, and retry ceilings rather than direct orchestrator-state telemetry.

- **Smallest safe mitigations**
  1. Fix review_autofix bootstrap/checkout faults so orchestrator work reaches the model path reliably.
  2. Keep skip decisions ahead of repo checkout/setup in command workflows.
  3. Split post-green sync/conflict recovery from the main success path.

- **Track these indicators**
  - `review_autofix` non-success rate
  - unique runs with runner-wait signals
  - unique runs with `CONTEXT_BUDGET_WARN`
  - AI memory retrieve hit rate
  - no-work poll ratio

## Pipeline Flow Bottlenecks

| Stage | Bottleneck type | Evidence | Ordered fix |
|---|---|---|---|
| Review / autofix | compute + queue + control-plane failures | **28.3h total**, p95 **3719s**; top runs **27930927798**, **27942405726**, **27949129254**; **20** unique runner-wait runs; **6** hard failures | Fix support-script staging and dirty checkout first, then trim prompt size/reasoning |
| CI / validation | serial compute + late failure isolation | **13.7h total**, p50 **1694s**; single `lint` job; **8** repeated shell-block failures; one **1819s** cancellation after green tests | Coarse-shard CI and split tail retry/sync work from main success path |
| Clarify / plan / implement / respond wrappers | fan-out / no-op churn | **539 skipped runs** across four families | Keep command parsing and skip gating ahead of checkout/setup |
| Poller control plane | queue + early setup before work gate | **24** runs, p50 **182.5s**, runner-wait on **6** unique runs; `Find active tracking issues` comes after support checkout | Move work discovery to the front of the poller |
| Merge / conflict recovery | retry overhead | `conflict-resolver-context` Semble queries appeared on slow review runs; prompt summary flagged `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` on cancelled CI run **27969435384** | Narrow retry budgets and separate post-green recovery from the main critical path |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix`: **28.3h** total, p95 **3719s**
  - `ci`: **13.7h** total, p50 **1694s**
  - hosted-runner queueing across review, CI, and poller paths

- **Top failure modes**
  - missing staged `codex_helpers.sh` during `Create Codex config`
  - dirty `.ai/.workspace_source_manifest.txt` blocking checkout
  - `Shared shell-block anti-regression checks` failing on missing `rg`
  - inventory drift in `docs/INVENTORY.md`

- **Highest-cost drivers**
  - monolithic serial CI `lint` job
  - slow, prompt-heavy review_autofix outliers with repeated context-budget warnings
  - duplicate review surface from always-on Copilot review runs

- **Top 3 prioritized actions**
  1. **Coarse-shard CI** and ensure the live branch uses the grep-based shell-block check.
  2. **Fix review_autofix support-script staging** and move the workspace manifest out of the tracked tree.
  3. **Reduce review prompt size/reasoning** and memoize Semble results within a run.

## Metrics Appendix

### Overall window

| Metric | Value |
|---|---:|
| Total runs | 839 |
| Success | 258 |
| Failure | 19 |
| Cancelled | 16 |
| Other / skipped | 546 |
| Avg duration | 210.0s |
| p50 duration | 7.0s |
| p95 duration | 1747.0s |

### Workflow-family summary

| Workflow family | Runs | Success | Failure | Cancelled | Other/skipped | p50 (s) | p95 (s) | Total hours |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `review_autofix` | 86 | 62 | 6 | 13 | 5 | 364.0 | 3719.0 | 28.3 |
| `ci` | 39 | 25 | 13 | 1 | 0 | 1694.0 | 1802.1 | 13.7 |
| `orchestrate_poll` | 24 | 24 | 0 | 0 | 0 | 182.5 | 289.1 | 1.3 |
| `copilot_pull_request_reviewer` | 18 | 18 | 0 | 0 | 0 | 183.0 | 418.8 | 1.2 |
| `implement` | 137 | 4 | 0 | 2 | 131 | 2.0 | 11.0 | 0.7 |
| `validation_refresh` | 2 | 2 | 0 | 0 | 0 | 1054.5 | 1336.7 | 0.6 |
| `plan` | 137 | 4 | 0 | 0 | 133 | 1.0 | 11.0 | 0.4 |
| `clarify` | 144 | 5 | 0 | 0 | 139 | 1.0 | 11.0 | 0.3 |
| `validate` | 2 | 2 | 0 | 0 | 0 | 268.0 | 313.9 | 0.1 |
| `orchestrate_clarify_respond` | 137 | 1 | 0 | 0 | 136 | 2.0 | 10.0 | 0.1 |

### Window-wide AI / cache / MCP telemetry

| Metric | Value | Notes |
|---|---:|---|
| Runs with log telemetry | 125 | prompt-supplied aggregate |
| `codex_tokens_used` | 4052 | measured Codex usage is small |
| `codex_calls` | 2 |  |
| `or_calls` | 120 | OR active, but token/cache accounting missing |
| `or_prompt_tokens / completion / total` | 0 / 0 / 0 | instrumentation gap |
| `or_cache_write_tokens / read_tokens` | 0 / 0 | instrumentation gap |
| `cache_hit_rate` | null | no prompt-cache visibility |
| `break_glass_count` | 0 | no override events observed |
| `context_budget_warn_count` | 36 | all deep-dive events were review-path prompt pressure |
| `wall_clock_p50_ms / p99_ms` | 9000 / 3964360 | additive review telemetry subset |
| `semble_query_calls / bytes` | 20 / 199587 | ~9.98 KB/query |
| `semble_fallbacks` | 65 | all broader-window fallbacks were contract-test |
| `semble_contract_test_fallbacks / runtime_fallbacks` | 65 / 0 | healthy fail-open test noise, not runtime breakage |
| `serena_query_calls / response_bytes / tool_calls` | 0 / 0 / 0 | no measurable Serena replacement |
| `serena_fallbacks` | 0 |  |
| `serena_probe_ok / failed / skipped` | 0 / 0 / 0 | no probe traffic observed |

**Coverage note:** the checked local `summary.json` inside the log folder attributed only **39** telemetry runs, **108** OR calls, and **19** Semble queries. The prompt-supplied aggregate above is broader, so I used it for window totals and used deep-dive logs for concrete examples.

### Deep-dive extracted signals

| Signal | Value |
|---|---:|
| Unique runs with runner-wait messages | 39 |
| Runner-wait runs by family | review_autofix 20, ci 9, orchestrate_poll 6, other 4 |
| `CONTEXT_BUDGET_WARN` events | 36 |
| Unique runs with `CONTEXT_BUDGET_WARN` | 6 |
| Deep-dive `SEMBLE_QUERY` events | 19 |
| Deep-dive Semble query bytes | 187950 |
| Semble query targets | `overflow`: 10, `reviewer-context`: 7, `conflict-resolver-context`: 2 |
| Deep-dive `SEMBLE_FALLBACK` events | 50 |
| Unique runs with Semble fallback | 10 |
| Semble fallback context | 100% `contract-test` |
| AI memory ops | 62 |
| AI memory retrieve hit rate | 0% (0/9) |
| AI memory retrieve avg `estimated_tokens` | 0 |

### GH API hotspot summary

| Workflow | Hotspot | Practical fix |
|---|---|---|
| `implement.yml` | repeated issue/meta/label reads in one job | cache one issue JSON + one comments snapshot and reuse locally |
| `plan.yml` / `clarify.yml` / `orchestrate_clarify_respond.yml` | repeated paginated comment-history fetches | centralize comment snapshot helper and reuse on-disk file |
| `review_autofix_sweep.yml` | paginated PR list plus workflow-run status snapshots | combine status queries via batched GraphQL or single cached snapshot per sweep |
| `orchestrate_poll_process.sh` / poller | risk of per-issue expansion in loop bodies | keep all expansion on existing GraphQL helpers and cycle-local caches |

### MCP availability

| Server / target | Probe OK | Probe failed | Probe skipped | Notes |
|---|---:|---:|---:|---|
| Serena (all observed targets) | 0 | 0 | 0 | no `SERENA_PROBE` lines and no Serena query traffic |
| Other MCP servers | — | — | — | none observed beyond Semble query/fallback telemetry; Semble did not emit probe lines in this window |

## Deep Audit — Workflows & Scripts (2026-06-23)

### Section 1: Bug & Correctness Sweep

- **ID:** SEC-001  
  **File path:** `.github/workflows/update_workflows.yml:61-77,462-490`  
  **Severity:** High  
  **Category tag:** `security`  
  **Description:** The workflow first clones the canonical upstream from `shubhodeep1/coding-workflows@stable`, but the later Telegram step rebuilds `wf_source` as `${{ github.repository_owner }}/coding-workflows`, downloads `scripts/tg_helpers.sh` from that repo via `gh api`, and `source`s it while `GH_TOKEN`, `TG_BOT_SECRET`, and `TG_ADMIN_CHAT_ID` are in scope. In `workflow_call` runs outside this repo, that can execute helper code from an owner-controlled repository instead of the already-fetched canonical source.  
  **Recommended fix:** Reuse the checked-out canonical helper from the fetch step output, or hard-code the helper fetch to `shubhodeep1/coding-workflows@stable` and verify the file before sourcing it.

### Section 2: GitHub API Call Redundancy Audit

Cross-reference only: **SEC-001** also removes one avoidable raw-content `gh api` fetch in `update_workflows.yml` because the canonical upstream is already present on disk.

- **ID:** API-001  
  **File path:** `.github/workflows/review_autofix_sweep.yml:104-148`  
  **Severity:** Low  
  **Category tag:** `api-redundancy`  
  **Description:** Each sweep tick snapshots active review runs by looping `queued` and `in_progress` for each of two workflows (`internal-review.yml`, `review_autofix.yml`), so the same logical “active review runs” snapshot costs four REST calls every run.  
  **Current / proposed call count:** `4 → 2` per sweep tick.  
  **Batching pattern to extend:** `scripts/gh_helpers.sh:1160-1224` (`autofix_retrigger_has_inflight_peer()`), which already filters statuses and workflow paths from a single repo-level runs snapshot.  
  **Recommended fix:** Replace the per-workflow/per-status loop with one repo-level `actions/runs` snapshot per active status, then filter `.path` and `.head_branch` locally for both review workflows.

- **ID:** BATCH-001  
  **File path:** `.github/workflows/review_autofix.yml:824-887`  
  **Severity:** Medium  
  **Category tag:** `api-batching`  
  **Description:** When linked issues fall back to PR body/title parsing, `issue_nodes_json` is synthesized with `labels: null`, and the loop at lines 857-887 calls `gh issue view ... --json labels` once per linked issue. That is an API call inside a per-issue loop on a hot review path.  
  **Current / proposed call count:** `N → 1` label lookups for `N` fallback-linked issues.  
  **Batching pattern to extend:** `scripts/orchestrate_poll_process.sh:2633-2668` (`_fetch_issue_labels_batch_graphql()`).  
  **Recommended fix:** Batch the fallback issue numbers through `_fetch_issue_labels_batch_graphql()` and read label presence from the returned map instead of calling `gh issue view` inside the loop.

- **ID:** API-002  
  **File path:** `.github/workflows/test-and-mark-stable.yml:2933-2942,2996-3003`  
  **Severity:** Medium  
  **Category tag:** `api-redundancy`  
  **Description:** The cancel-on-close wait loop fetches the same run twice every poll iteration: once for `.status` and again for `.conclusion`. The same step already uses the better “fetch once, parse twice” pattern later at lines 2996-3003. Besides doubling API traffic, the split fetch can observe mismatched status/conclusion pairs if the run changes state between calls.  
  **Current / proposed call count:** `2 → 1` per poll iteration.  
  **Batching pattern to extend:** None exact from `gh_helpers.sh` or `orchestrate_poll_process.sh`; this is a local single-fetch reuse fix.  
  **Recommended fix:** Fetch the run JSON once per poll iteration, store it in a temp variable, and extract both fields locally with `jq`.

- **ID:** BATCH-002  
  **File path:** `scripts/gh_helpers.sh:902-932`  
  **Severity:** Low  
  **Category tag:** `api-batching`  
  **Description:** [NEEDS VERIFICATION] The REST fallback for `gh_issue_timeline_with_cross_refs()` fetches the issue timeline once, extracts unique PR URLs, then loops each URL and calls `gh api` once per PR to enrich state/merge data. That makes fallback cost scale with the number of cross-referenced PRs.  
  **Current / proposed call count:** `1 + U → 2`, where `U` is the number of unique PR URLs.  
  **Batching pattern to extend:** `scripts/orchestrate_poll_process.sh:2633-2668`’s GraphQL alias batching style, or the GraphQL-first timeline path in the same helper family.  
  **Recommended fix:** Keep the initial timeline fetch, then batch all PR-number enrichments into one GraphQL alias query and merge that result back into the timeline JSON.

### Section 3: Code Duplication & Modularization Opportunities

- **ID:** DUP-001  
  **File path:** `.github/workflows/review_autofix.yml:4318-4360,4502-4550,5372-5396`  
  **Severity:** Medium  
  **Category tag:** `duplication`  
  **Description:** Three late-stage `review_autofix` steps reimplement fallback `ensure_label_exists()` / `set_issue_phase_label_resilient()` logic inline even though `scripts/label_helpers.sh:120-207` already owns those behaviors. The copies have already drifted: the ready-to-merge path special-cases `ai:ready-to-merge` and `ai:closed`, the exhaustion path special-cases `ai:review-blocked`, and the workflow-failure path hard-codes the review-blocked description for any fallback label.  
  **Proposed owner / signature:** Reuse `scripts/label_helpers.sh` with existing `ensure_label_exists <label_name> [repo]` and `set_issue_phase_label_resilient <issue_number> <label> <repo>`.  
  **Callers to update:** The three `review_autofix.yml` late-stage labeling steps above.  
  **Recommended fix:** Stage/source `label_helpers.sh` once for the job and fall back to the checked-out repo copy if staged support files were cleaned up, instead of inlining step-local label helpers.

- **ID:** DUP-002  
  **File path:** `scripts/review_apply_fixes.sh:72-110`; `scripts/review_rb_judge.sh:188-226`; `scripts/review_run_reviewers.sh:35-73`  
  **Severity:** Low  
  **Category tag:** `duplication`  
  **Description:** `emit_context_budget_warn_for_prompt()` is repeated nearly line-for-line in three review scripts. Any future change to prompt-budget telemetry, import path handling, or failure behavior has to be kept in sync manually.  
  **Proposed owner / signature:** New `scripts/review_common.sh` (or equivalent shared helper) with `emit_context_budget_warn_for_prompt <phase> <prompt_path> <model>`.  
  **Callers to update:** `review_apply_fixes.sh`, `review_rb_judge.sh`, `review_run_reviewers.sh`.  
  **Recommended fix:** Move the helper into one shared module and source it from each caller before prompt execution.

- **ID:** DUP-003  
  **File path:** `scripts/review_apply_fixes.sh:372-387`; `scripts/review_conflict_resolve.sh:68-83`; `scripts/validate_process.sh:2665-2680`  
  **Severity:** Low  
  **Category tag:** `duplication`  
  **Description:** Three scripts implement the same “search `repo_path`, `.codex-workflow-src/`, then `.codex-workflow-src-main/`” resolver for thread-reuse assets, differing only in function name.  
  **Proposed owner / signature:** `scripts/codex_thread_reuse.sh` with `resolve_thread_reuse_asset <repo_path>`.  
  **Callers to update:** `review_apply_fixes.sh`, `review_conflict_resolve.sh`, `validate_process.sh`.  
  **Recommended fix:** Add one shared resolver to the existing thread-reuse helper module and delete the per-script copies.

- **ID:** DUP-004  
  **File path:** `.github/workflows/workflow-log-analysis.yml:405-628,1003-1184,1472-1650`  
  **Severity:** Medium  
  **Category tag:** `duplication`  
  **Description:** The `Run workflow log analysis`, `Run deep audit pass`, and `Run API redundancy pass` steps repeat the same control-plane shape: tracking-issue parsing, failure comment+label emission, Semble prefetch, prompt assembly, Codex retry/backoff, heading validation, and append-to-report handling. These blocks are each large and already diverge in small ways only because they are copy-edited inline.  
  **Proposed owner / signature:** New `scripts/run_workflow_log_analysis_phase.sh` (or a composite action) with a CLI like `run_workflow_log_analysis_phase --mode <analysis|deep_audit|api_redundancy> --report-file <path> --tracking-issue <n> --prompt <path> [--run-logs-dir <dir>]`.  
  **Callers to update:** The three `workflow-log-analysis.yml` phase steps above.  
  **Recommended fix:** Extract the shared control plane into one script/composite action and keep only phase-specific prompt inputs and heading expectations in the workflow YAML.

### Section 4: Expression Size Limit Risk Assessment

- **ID:** EXPR-001  
  **File path:** `.github/workflows/memory_maintenance.yml:45-391`  
  **Severity:** Medium  
  **Category tag:** `expression-limit`  
  **Description:** The `Extract repository learnings (fail-open)` step spans 346 lines and embeds five `${{ }}` interpolations. Using the raw interpolated `run:` block source as the conservative measure, the step is about **15,168** characters, leaving roughly **5,832** characters of headroom before GitHub’s 21,000-character expression ceiling. Given this repo’s prior expression-limit incidents, this is the closest current block to the warning threshold. [NEEDS VERIFICATION]  
  **Recommended fix:** Extract the inline Python/model-call logic into `scripts/` helpers, or split this step into smaller “discover source data”, “render prompt”, and “call model” steps so each interpolated block stays comfortably below 15k.

No other interpolated `run:` block crossed the 15,000-character threshold in this pass, and no workflow file in `.github/workflows` exceeds 800 KB.

### Section 5: Cross-Cutting Concerns

- **ID:** CONSIST-001  
  **File path:** `scripts/comprehensive_test_and_release_gh_api.sh:3-68`; `.github/workflows/comprehensive-test-and-release.yml:42-57,284-287`; `.github/workflows/test-and-mark-stable.yml:495,616,822,1298,2533`; `scripts/dispatch_and_watch_workflow_run.sh:5-7`  
  **Severity:** Low  
  **Category tag:** `consistency`  
  **Description:** The comprehensive release/test control plane uses a custom `gh_api_safe*` wrapper instead of the repo-standard GH helpers. The custom wrapper only special-cases stderr containing “rate limit”, while `scripts/gh_helpers.sh:391-562` already centralizes retry policy, JSON validation, and error classification. This makes GH API behavior inconsistent across workflows that should share the same control-plane semantics.  
  **Recommended fix:** Replace `gh_api_safe*` callsites with `gh_retry`, `gh_retry_to_file`, and `gh_api_json_to_file`, or refactor `gh_api_safe*` into a thin compatibility layer over those helpers so there is one retry/error-policy source of truth.

No `TODO`, `FIXME`, `HACK`, or `XXX` markers surfaced in `.github/workflows` or `scripts/` during this pass.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | SEC-001 |
| Medium | 5 | BATCH-001, API-002, DUP-001, DUP-004, EXPR-001 |
| Low | 5 | API-001, BATCH-002, DUP-002, DUP-003, CONSIST-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 1 | Small |
| API call optimization | 4 | Medium |
| Code modularization | 9 | Large |
| Expression size reduction | 2 | Medium |
| Medium/Low fixes | 4 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-06-23)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is local and explicit enough that implement can collapse it directly without changing behavior. `NEEDS_VERIFICATION` means the redundancy is plausible, but runtime parity still has to be proven first. `RISKY_SKIP` means the duplication is visible, but the call sits in paginated, poller, retry-sensitive, or race-defense code that this pass must not auto-change.

### Consolidation Candidates (MERGE-###)

- **ID:** `MERGE-001`  
  **Safety tag:** `NEEDS_VERIFICATION`  
  **Files:** `.github/workflows/test-and-mark-stable.yml:1022-1024`, `.github/workflows/test-and-mark-stable.yml:1051-1056`  
  **Current / proposed call count:** common stable-head success path `3 → 2` calls to the same PR endpoint.  
  **Endpoint(s):** `GET /repos/{owner}/{repo}/pulls/{pull_number}`  
  **Evidence:** the step reads the same PR twice for `.head.sha`, then immediately reads the full PR again for state/merge guard data.
  ```sh
  HEAD_A=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")
  sleep 3
  HEAD_B=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")
  ...
  PR_META=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" 2>/dev/null || echo "")
  PR_STATE=$(printf '%s' "${PR_META}" | jq -r '.state // ""' 2>/dev/null || echo "")
  PR_MERGED=$(printf '%s' "${PR_META}" | jq -r '.merged // false' 2>/dev/null || echo "false")
  ```
  **Proposed fix:** promote the second stability read to a full PR JSON fetch, compare `.head.sha` locally, and reuse that same payload as `PR_META`; keep a final fallback fetch only if the loop exits without a reusable full payload.  
  **Safety rationale:** same step and same endpoint, but the guard was added to catch a PR-close/auto-merge race, so parity must be proven before removing the extra read.  
  **Downstream signal:** Verify two cases before merging: `(1)` PR stays open after the stability loop, `(2)` PR closes/merges between the second stability read and the current guard read; confirm identical `status=pr_already_closed` behavior.

- **ID:** `MERGE-002`  
  **Safety tag:** `RISKY_SKIP`  
  **Files:** `scripts/orchestrate_poll_process.sh:7199-7205`  
  **Current / proposed call count:** `2 → 1` per `final_pr` check.  
  **Endpoint(s):** `GET /repos/{owner}/{repo}/pulls/{pull_number}`  
  **Evidence:** the poller reads `.state` and `.merged_at` from the same PR in back-to-back API calls.
  ```sh
  existing_pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  existing_pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
  ```
  **Proposed fix:** fetch the PR JSON once and derive both `existing_pr_state` and `existing_pr_merged` locally.  
  **Safety rationale:** this sits inside `orchestrate_poll_process.sh` final-merge recovery logic, which the prompt explicitly treats as `RISKY_SKIP` even when the duplication is obvious.  
  **Downstream signal:** Do not auto-implement; manually confirm the final-merge recovery path preserves the same race/error behavior and log output when both fields come from one payload.

- **ID:** `MERGE-003`  
  **Safety tag:** `RISKY_SKIP`  
  **Files:** `scripts/orchestrate_poll_process.sh:9929-9931`, `scripts/orchestrate_poll_process.sh:12000-12008`, `scripts/orchestrate_poll_process.sh:16179-16181`  
  **Current / proposed call count:** `2 → 1` at each cited branch (`6 → 3` if all three branches run in one poll cycle).  
  **Endpoint(s):** `GET /repos/{owner}/{repo}/issues/{issue_number}`  
  **Evidence:** three poller reissue paths split title/body reads into separate calls against the same issue endpoint.
  ```sh
  orig_title="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.title // ""' || echo "")"
  orig_body="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.body // ""' || echo "")"
  ```
  The same pattern repeats at `12007-12008` and `16180-16181`.  
  **Proposed fix:** at each site, fetch one issue JSON blob (or one `--jq '{title:(.title // ""), body:(.body // "")}'` object) and extract both fields locally.  
  **Safety rationale:** all three sites are inside `orchestrate_poll_process.sh` stall-recovery / reissue paths that explicitly defend against upstream races, so this pass must not auto-change them.  
  **Downstream signal:** Do not auto-implement; manually exercise each cited reissue branch and confirm the single-fetch replacement preserves current fail-open behavior under transient API faults.

### Redundant Re-Fetch (REUSE-###)

- **ID:** `REUSE-001`  
  **Safety tag:** `SAFE_TO_MERGE`  
  **Files:** `scripts/orchestrate_force_tick.sh:57-67`, `scripts/orchestrate_force_tick.sh:69-100`, `scripts/orchestrate_force_tick.sh:103-115`, `scripts/orchestrate_force_tick.sh:274-292`  
  **Current / proposed call count:** on the “`ISSUE_NUMBER` is a PR number, but tracking issue is still unresolved after PR parsing” path, `2 → 1`.  
  **Endpoint(s):** `GET /repos/{owner}/{repo}/pulls/{pull_number}`, `GET /repos/{owner}/{repo}/issues/{issue_number}`  
  **Evidence:** the first fetch already gives the PR body, and the extractor already searches that body for the tracking marker before the script falls back to a second issue-body fetch.
  ```sh
  if ! pr_json="$(_force_tick_fetch_pull_request_json "${REPOSITORY}" "${ISSUE_NUMBER}")"; then
    pr_lookup_failed="true"
    pr_json=""
  fi
  TRACKING_ISSUE="$(_force_tick_extract_tracking_issue_from_pull_request_json "${pr_json}")"
  ...
  if [ -z "${TRACKING_ISSUE}" ] && [ -n "${ISSUE_NUMBER}" ]; then
    issue_body=""
    if ! issue_body="$(_force_tick_fetch_issue_body "${REPOSITORY}" "${ISSUE_NUMBER}")"; then
  ```
  ```py
  body = payload.get("body") or ""
  match = body_re.search(body)
  if match:
      print(match.group(1))
  ```
  **Proposed fix:** in the second fallback block, skip `_force_tick_fetch_issue_body` when `pr_json` was successfully fetched; keep the issue-body fetch only for true non-PR inputs or PR-lookup failures.  
  **Safety rationale:** same function, no intervening mutation, no pagination/retry-loop/auth probe, and the non-PR fallback path remains intact.  
  **Downstream signal:** Implement directly: gate `_force_tick_fetch_issue_body` behind `pr_lookup_failed=true` (or empty `pr_json`) and do not re-fetch the body after a successful PR lookup.

### Dead Calls (DEAD-API-###)

- **ID:** `DEAD-API-001`  
  **Safety tag:** `RISKY_SKIP`  
  **Files:** `scripts/orchestrate_poll_process.sh:17641-17654`  
  **Current / proposed call count:** `1 → 0` per standalone conflict-sweep invocation.  
  **Endpoint(s):** `GET /repos/{owner}/{repo}`  
  **Evidence:** the standalone conflict sweep assigns `DEFAULT_BRANCH`, then immediately iterates on `S_PR`, `S_HEAD`, and `S_BASE`; the block never reads `DEFAULT_BRANCH` before it ends.
  ```sh
  STANDALONE_COUNT="$(echo "${STANDALONE_PRS}" | jq 'length')"
  echo "Found ${STANDALONE_COUNT} open PR(s) to scan."

  CONFLICT_SWEEP_FIXED=0
  DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"

  for (( sidx=0; sidx<STANDALONE_COUNT; sidx++ )); do
    S_PR="$(echo "${STANDALONE_PRS}" | jq -r ".[${sidx}].number")"
    S_HEAD="$(echo "${STANDALONE_PRS}" | jq -r ".[${sidx}].headRefName")"
    S_BASE="$(echo "${STANDALONE_PRS}" | jq -r ".[${sidx}].baseRefName")"
  ```
  **Proposed fix:** remove the late `DEFAULT_BRANCH` fetch from the standalone conflict-sweep block.  
  **Safety rationale:** the read is dead by static inspection, but it is inside `orchestrate_poll_process.sh`, so the prompt requires manual review instead of auto-removal.  
  **Downstream signal:** Do not auto-remove; manually confirm no later standalone-sweep branch or callee relies on the global `DEFAULT_BRANCH` value, then delete the fetch.

### Cross-References to Deep Audit Section

- API-001: `RISKY_SKIP` — correct hotspot, but it is a `--paginate`d control-plane sweep, so page-boundary and active-run parity need manual review before consolidation.
- BATCH-001: `NEEDS_VERIFICATION` — batching fallback-linked issue label lookups through the existing GraphQL helper is directionally right, but the fallback issue-number set still needs parity testing.
- API-002: `SAFE_TO_MERGE` — same run endpoint, same loop iteration, and the later code in the same step already demonstrates the one-fetch/two-field pattern.
- BATCH-002: `RISKY_SKIP` — the fallback starts from a paginated timeline read, so replacing per-PR enrichments with a batch helper changes page/ordering semantics and needs manual validation.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 1 | REUSE-001 |
| NEEDS_VERIFICATION | 1 | MERGE-001 |
| RISKY_SKIP | 3 | MERGE-002, MERGE-003, DEAD-API-001 |

### Implement-Stage Handoff

- REUSE-001
