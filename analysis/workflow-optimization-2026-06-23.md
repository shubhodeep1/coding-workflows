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
