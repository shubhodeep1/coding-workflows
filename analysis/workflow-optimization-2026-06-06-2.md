## Executive Summary

- **`review_autofix` is the dominant cost and latency hotspot.** In `shubhodeep1/coding-workflows`, `review_autofix` accounts for **401,148 / 407,226 codex tokens (98.5%)** and has a **p95 duration of 4,068s**. The worst run, **27052802146** (`Internal: AI Review & Autofix`), took **5,404s**, used **243,120 tokens across 120 codex calls**, and emitted **14 `CONTEXT_BUDGET_WARN` events**. **Estimated impact:** save **10–35 min** and **100k–200k tokens** on outlier runs. **Confidence:** high.
- **The main CI failures are deterministic contract regressions, not flaky infra.** `ci` failed **5/24 runs (20.8%)**. Concrete failures were: stall-guard/env drift in **27042555561**, per-attempt prompt-file contract failure in **27046800332**, behavioural-smoke restore contract failure in **27052802097**, and orchestrate reissue state regression in **27053190246**. **Estimated impact:** materially reduce reruns and unblock downstream automation. **Confidence:** high.
- **`validate` is a reliability blind spot.** `validate` failed **13/14 runs** with **p50 duration 0s**; **9 runs** also produced collector `missing_log_archive` 404s, and the deep-dive `validate` folders contain only `metadata.json` with **no step logs**. **Estimated impact:** large MTTR reduction and fewer blind redispatches once pre-job failures are observable. **Confidence:** high.
- **Review check-run polling is adding avoidable critical-path delay and GitHub API churn.** In `review_autofix` run **27052802146**, `review / codex-agent` waited **20s + 40s + 80s + 80s + 78s = ~300s** on **1 queued/in_progress check-run** before timing out; **27035370755** shows the same ~298s pattern. **Estimated impact:** save **3–5 min/run** on affected runs and cut polling calls by **60–80%**. **Confidence:** high.
- **AI memory retrieval and prompt-cache telemetry are not delivering value yet.** Across parsed deep-dive `AI_MEMORY_TELEMETRY`, `retrieve` hit rate was **0/10 = 0%** with **estimated_tokens=0** every time; repo-wide prompt-cache telemetry shows **`cache_hit_rate=null`** and all `or_*` cache fields at **0**. **Estimated impact:** medium; fixing this could save **tens of thousands of prompt tokens per reused pass** (inference). **Confidence:** medium.
- **Semble is helping as an overflow valve, but the fallback metrics are partially noisy.** In **27052802146**, Semble retrieved targeted overflow context at **~7.5 KB/query** and a `reviewer-context` payload of **16,173 bytes**, yet prompts still hit **216k–245k tokens**. Separately, run **27051284971** reports **90 `semble_fallbacks`**, but the log contains only **5 unique fallback lines repeated 18×**, so aggregate fallback counts are an **upper bound** until parsing is tightened. **Estimated impact:** medium, mainly by improving decision quality around rollout/tuning. **Confidence:** high.

## Speed Optimizations

### 1) [Critical path] Reduce `review_autofix` check-run waiting

- **Evidence:** In `shubhodeep1/coding-workflows` run **27052802146** (`review / codex-agent`), the workflow waited on **1 queued/in_progress check-run** for **~300s** before `CHECK_RUNS_WAIT_TIMEOUT`; the sleep pattern was **20s, 40s, 80s, 80s, 78s**. Run **27035370755** shows the same pattern at **~298s**.
- **Root cause:** the review job blocks on sibling check-runs before snapshotting failures.
- **Exact change:** for small-diff PRs and single-file PRs, lower the effective wait budget from **300s** to **60–120s**, and continue after **2 unchanged snapshots** instead of riding the full timeout. Preserve the current fail-open snapshot behavior.
- **Estimated time savings:** **3–5 min** on affected `review_autofix` runs.
- **Implementation risk:** **medium** — you may snapshot slightly earlier than an about-to-fail sibling run, but the existing fail-open/retrigger model makes this safe enough.

### 2) [Critical path] Shrink oversized reviewer/editor prompts before model invocation

- **Evidence:** `review_autofix` run **27052802146** hit review prompt sizes of **244,370** and **244,939** tokens and an editor prompt of **216,811** tokens; run **27051284971** hit **246,506** and **249,669** review tokens. These two runs account for **all 20 repo-wide `CONTEXT_BUDGET_WARN` events**.
- **Root cause:** too much raw context survives into review/editor prompts even after Semble overflow retrieval.
- **Exact change:** for small diffs, feed reviewer pass 2 and editor only:
  1. touched-file hunks,
  2. failed check-run summaries/log tails,
  3. reviewer consensus,
  4. Semble overflow snippets for directly referenced files only.  
  Drop repeated archival/postmortem text and prior-run narrative blocks from the hot path.
- **Estimated time savings:** **10–30 min** on worst-case `review_autofix` runs.
- **Implementation risk:** **medium** — reduce context too aggressively and recall may drop; mitigate by falling back to full context only when pass 1 reports low confidence or cross-file coupling.

### 3) [Critical path] Fail deterministic contract suites earlier in CI

- **Evidence:** `ci` has **p50 1,462s**. Deterministic failures included **27053190246** failing after **1,424s** in `lint / Orchestrate poll process unit tests`, and **27046800332** / **27052802097** failing in `lint` contract suites.
- **Root cause:** high-signal workflow/script contract tests are discovered late in a long CI run.
- **Exact change:** split the contract-style workflow tests into an early parallel shard ahead of broad lint/test coverage.
- **Estimated time savings:** on broken commits like **27053190246**, failure would surface **20+ min earlier**.
- **Implementation risk:** **low** — this is scheduling, not behavior change.

### 4) [Micro] Resolve support checkout/helper fallback once per `issue_pr_status` job

- **Evidence:** recent `issue_pr_status` runs **27054939865**, **27054941101**, and **27054934390** all logged support-checkout fallback warnings; family **p50 = 36s**.
- **Root cause:** helper/script resolution is repeated across multiple steps.
- **Exact change:** fetch/resolve helper scripts once at job start, publish the resolved path as a job output, and emit one warning instead of per-step warnings.
- **Estimated time savings:** **seconds per run**, plus cleaner logs.
- **Implementation risk:** **low**.

## Cost Optimizations

### 1) Collapse `review_autofix` long-tail prompt growth before changing models

- **Evidence:** `review_autofix` consumed **401,148 tokens**; the top two runs, **27052802146** and **27051284971**, used **243,120 + 145,872 = 388,992 tokens (97.0%)**. Ancillary tasks are already on cheaper models in recent runs (**27053983984**, **27054941115** used `gpt-5.4-mini` for `AGENTS_MD_MATERIALITY_MODEL` and `XPOLL_SUMMARISER_MODEL`).
- **Root cause:** the expensive part is not the mini-model side tasks; it is repeated large-context review/editor work.
- **Exact change:** keep current model choices for side tasks, but make review pass 2 conditional and editor context smaller on small diffs.
- **Estimated savings:** **100k–200k tokens** on outlier `review_autofix` runs.
- **Quality-risk notes:** lower than swapping core models or globally reducing reasoning effort; this trims context, not capability.

### 2) Stop paying for structurally unfixable review loops and cancellations

- **Evidence:** `review_autofix` had **12 cancelled runs / 70 total (17.1%)**, totaling **16,180s** of cancelled runtime. The biggest were **27053194561 (4,257s)** and **27053190281 (4,248s)**. In run **27054941115**, `review / gate` concluded the failing guard around `test_review_apply_fixes_has_per_attempt_cache_busting_nonce` had become a **false positive** after a helper refactor, causing CI to stay red and the orchestrator to keep spawning fix-issues.
- **Root cause:** the system is re-running expensive review flows against failures the editor cannot safely resolve.
- **Exact change:** classify known structural/literal-grep contract failures as **human-needed** after the first repeat, or update the test to assert behavior instead of the exact shell literal.
- **Estimated savings:** **hours of runner time** and multiple full review passes per affected PR.
- **Quality-risk notes:** **low** if limited to known non-autofixable cases.

### 3) Keep Semble, but tighten what it is allowed to pull

- **Evidence:** `review_autofix` logged **23 Semble queries / 198,868 bytes**. In **27052802146**, Semble pulled one `reviewer-context` payload (**16,173 bytes**) and four `overflow` files at **7,519 bytes each**. Despite that, prompts still reached **216k–245k tokens**.
- **Root cause:** Semble is reducing file-overflow expansion, but too much non-file context still remains.
- **Exact change:** retain Semble for `overflow` and `reviewer-context`, but gate overflow retrieval to files named by:
  - failing tests/check-runs,
  - touched files,
  - directly referenced imports/functions.
  Also dedupe identical overflow pulls across review/editor steps.
- **Estimated savings:** **15–30 KB** of extra context per heavy run, i.e. **several thousand tokens** (inference).
- **Quality-risk notes:** **low** if fallback remains available for explicit misses.

### 4) Repair prompt-cache telemetry before trying to optimize cache behavior

- **Evidence:** repo summary shows **`cache_hit_rate=null`**, `or_prompt_tokens=0`, `or_completion_tokens=0`, `or_cache_write_tokens=0`, `or_cache_read_tokens=0`, `or_calls=0`. Recent runs **27053983984** and **27054572029** mention only GitHub Actions/UV cache behavior, not model prompt-cache reads.
- **Root cause:** prompt cache is either disabled, not instrumented, or too fragmented to register.
- **Exact change:** emit prompt-cache reads/writes/hit rate on every model call, and keep dynamic noise (timestamps, nonce, replayed logs) after a stable prefix.
- **Estimated savings:** **unquantified** in this window; **inference:** caching even **25%** of the **244,370-token** review prompt in **27052802146** would avoid about **61k prompt tokens** on a reused pass.
- **Quality-risk notes:** **low**.

### 5) Serena is not currently replacing meaningful downstream work

- **Evidence:** the analysis aggregate shows **1 Serena query**, **1 Serena fallback**, **1 Serena probe skipped**, and **0 response bytes / 0 tool calls / 0 ms**; no runtime `SERENA_*` lines were found in the deep-dive logs.
- **Root cause:** Serena is effectively inactive in this window.
- **Exact change:** do not spend optimization effort here until real Serena telemetry appears; treat it as disabled.
- **Estimated savings:** **none immediately**.
- **Quality-risk notes:** **none**.

## Reliability Improvements

_Repo-wide `BREAK_GLASS` usage was **0**. Repo-wide `CONTEXT_BUDGET_WARN` count was **20**, and all 20 came from `review_autofix` prompt-size pressure (runs **27052802146 = 14**, **27051284971 = 6**), so the current reliability signal is prompt-size risk, not policy-bypass pressure._

### 1) Restore the shared stall-guard contract in CI

- **Failure evidence:** `ci` run **27042555561**, `lint / Orchestrate lib unit tests`, failed **5/6** tests, including `REVIEW_RUN_MAX_RUNTIME_MINUTES: unbound variable` and missing `timeout --signal=TERM --kill-after=5s` in `.github/workflows/implement.yml`.
- **Root cause category:** workflow/script contract drift.
- **Exact fix:** restore the missing runtime variable defaulting and put the shared timeout wrapper back into `implement.yml`.
- **Expected reliability impact:** removes a fully deterministic CI failure cluster and likely cuts `ci` family failures materially from the current **20.8%**.
- **Rollback/fail-open:** low risk; restoring the existing shared contract is backward-compatible.

### 2) Fix the per-attempt prompt-file guard so it survives helper refactors

- **Failure evidence:** `ci` run **27046800332**, `lint / Review autofix review-pipeline plumbing contract test`, failed with: per-attempt prompt data was no longer being asserted via the old literal shell redirection. `review / gate` in **27054941115** later described this as a **false positive** after the helper refactor.
- **Root cause category:** brittle literal-grep contract test.
- **Exact fix:** assert the behavioral contract (the helper receives the per-attempt prompt file) instead of the exact shell literal, or keep a thin wrapper that preserves the literal while still calling the helper.
- **Expected reliability impact:** prevents structurally unfixable red CI and reduces orchestrator backpressure/fix-issue churn.
- **Rollback/fail-open:** keep the old assertion behind a temporary compatibility path if maintainers want a staged change.

### 3) Restore validate/workflow behavioural-smoke artifact handoff

- **Failure evidence:** `ci` run **27052802097**, `lint / Review synthesize-smoke contract test`, failed `test_validate_workflows_restore_cached_behavioural_smoke_artifacts` with **15 passed, 1 failed**.
- **Root cause category:** validate workflow / cached artifact contract drift.
- **Exact fix:** restore cached behavioural-smoke artifacts in validate workflows and preserve the dispatch parameters the test expects.
- **Expected reliability impact:** eliminates a deterministic contract failure and reduces downstream validate ambiguity.
- **Rollback/fail-open:** low risk; this is contract restoration, not behavior expansion.

### 4) Recover observability for `validate` pre-job failures

- **Failure evidence:** `validate` failed **13/14** times; **9** failed runs also had collector `missing_log_archive` 404s, and the deep-dive validate folders have **no `.log` files**, only `metadata.json`.
- **Root cause category:** workflow bootstrap / collector observability gap.
- **Exact fix:** capture pre-job failure reason into run summary/artifact, and negative-cache missing archives so follow-up analyses do not keep blind-fetching them.
- **Expected reliability impact:** large MTTR improvement; root-cause identification becomes possible instead of speculative.
- **Rollback/fail-open:** preserve the current soft-fail collector behavior if archive fetch still fails.

### 5) Treat Semble fail-open as healthy, but fix the rollout gap and the counting noise

- **Failure evidence:** actual runtime `SEMBLE_FALLBACK` lines were observed in:
  - `ci` runs **27046800332**, **27052802097**, **27053190246** — **5 fallbacks each**
  - successful `test_and_mark_stable` run **27050798259** — **5 fallbacks**
  all due to missing `missing_semble` binaries.  
  Separately, `review_autofix` run **27051284971** reported **90** fallbacks because the same **5 unique** fallback lines were replayed **18×** inside prompt/context logs.
- **Root cause category:** dependency rollout gap plus telemetry parser sensitivity.
- **Exact fix:** ensure jobs that are not supposed to use Semble run in explicit no-Semble mode, and anchor fallback parsing to true runtime telemetry lines instead of quoted/excerpted text.
- **Expected reliability impact:** fewer masked rollout problems and far more trustworthy telemetry.
- **Rollback/fail-open:** keep current fail-open behavior when Semble is unavailable.

### 6) Prevent archival-lint failures before the expensive path starts

- **Failure evidence:** `plan` runs **27042555542** and **27046800333** failed because tracking issue **#3042** still had **1 unchecked sub-issue** and the PR body lacked a non-empty `## De-scoped phases` section.
- **Root cause category:** policy/template mismatch.
- **Exact fix:** add a PR-body/template stub for `## De-scoped phases` when archiving plans, or pre-check the issue state before opening the archival PR.
- **Expected reliability impact:** removes two deterministic `plan` family failures.
- **Rollback/fail-open:** low risk.

## AI Memory Health

- Parsed **38** JSON `AI_MEMORY_TELEMETRY` records from the deep-dive logs.
- Observed operations:
  - `record-run-event`: **20**
  - `retrieve`: **10**
  - `record-candidate`: **7**
  - `summarize_unselected_runs`: **1**
- **Retrieve hit rate:** **0/10 = 0%** (`records_selected > 0` never occurred).
- **Average `estimated_tokens`:** **0**.
- **`keyword_method` distribution:** **`llm`: 10**, `plain`: 0, `none`: 0.
- **No degraded-mode signals observed:** `enabled:false = 0`, `fail_open:true = 0`, `push_attempts > 1 = 0`.
- **Interpretation:** memory telemetry is emitting correctly, but retrieval is not contributing usable context in the sampled slow runs; it is currently observability-positive but execution-negative.
- **Recommendation:** tune retrieval recall before expanding memory usage. A simple SLO to track is: **retrieve hit rate**, **median selected-record count**, and **median estimated_tokens** by workflow family.

## GH API Call Audit

_No rate-limit or secondary-limit events were visible in the deep-dive logs. The main GitHub-call risks are repeated polling, repeated blind archive fetches, and a few low-volume redundancies._

### 1) `review_autofix` check-run polling is the hottest GitHub pattern

- **Evidence:** run **27052802146** (`review / codex-agent`) made at least **5 polling iterations** before timing out after **300s**; **27035370755** shows the same pattern.
- **Redundancy pattern:** repeated `check-runs` polls against an unchanged in-flight set.
- **Concrete change:** shorten/conditionalize the wait budget for small diffs and exit earlier after unchanged snapshots.
- **Estimated call-count reduction:** **3–4 fewer poll iterations** on affected runs.
- **Rate-limit reduction:** meaningful, because each iteration is at least one logical API poll and can expand with pagination/retries.

### 2) `issue_pr_status` is already partially batched, but the close path still stages extra lookups

- **Evidence:** recent run **27054939865**, step `sync-status / Update linked issue labels when PR closes`, stages:
  - one GraphQL query for `closingIssuesReferences`,
  - one GraphQL query for orchestrator classification,
  - a PR REST fallback lookup,
  - label POST,
  - per-issue REST fallback lookup,
  - `gh issue close`.
- **Redundancy pattern:** mixed GraphQL + REST fallback logic in one close path.
- **Concrete change:** keep the existing GraphQL batching, but cache results as job outputs and only hit the PR body/title REST fallback when the first GraphQL path returns no closing issues.
- **Estimated call-count reduction:** **2–4 requests per PR-close event**.
- **Rate-limit reduction:** low to moderate; this family is low-volume.

### 3) Validate archive 404s are wasted analysis calls

- **Evidence:** **9** `missing_log_archive` 404s for `validate` runs in the current window.
- **Redundancy pattern:** repeated blind fetches for runs that appear not to have retrievable archives.
- **Concrete change:** persist negative archive-fetch results across the analysis pass and surface the missing-archive status directly in the report input.
- **Estimated call-count reduction:** **9 avoided archive fetches** per comparable audit window.
- **Rate-limit reduction:** small, but it removes noise and improves determinism.

### 4) `review_autofix` standalone validate dispatch is not a current hotspot

- **Evidence:** run **27054941115** logged a single `gh api graphql` call in `review / post-merge-validate-dispatch`.
- **Recommendation:** leave as-is for now; focus first on `review_autofix` polling and `validate` observability.

## Prompt Cache & Memory System

- **Prompt-cache telemetry is effectively absent.** Repo aggregate: `cache_hit_rate=null`, `or_* = 0`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`.
- **Do not confuse workflow caches with prompt cache.** Recent runs show GitHub Actions cache behavior (`setup-uv` hit in **27054572029**, `not saving cache` in **27053983984** / **27054414179**), but that is not model prompt caching.
- **Context growth is currently eroding any possible cache value.** All `CONTEXT_BUDGET_WARN` events came from:
  - **27052802146**: review **244,370 / 244,939** tokens; editor **216,811**
  - **27051284971**: review **246,506 / 249,669** tokens
- **Likely cache-fragmentation causes (inference):**
  - dynamic run-state noise early in prompts,
  - replayed log excerpts,
  - repeated multi-pass reviewer context,
  - oversized check-run/log-tail embeds.
- **Concrete improvements:**
  1. keep the stable repo/workflow instructions first,
  2. append volatile data late,
  3. hash/dedupe repeated log excerpts,
  4. emit real prompt-cache read/write/hit telemetry per step.
- **Estimated impact:**  
  - **tokens:** if only **25%** of the **244,370-token** review prompt in **27052802146** were reusable, that is roughly **61k prompt tokens saved per reused pass** (inference);  
  - **latency:** lower prompt assembly + transfer time;  
  - **reliability:** fewer `CONTEXT_BUDGET_WARN` threshold crossings.
- **Memory retrieval verdict:** telemetry exists, but current retrieval effectiveness is poor enough that it is not materially reducing prompt size.

## Orchestrator Health

- **Healthy signals**
  - `clarify`, `plan`, and `implement` are usually skipped quickly rather than doing wasted work.
  - `orchestrate_poll` succeeded **16/16** times with **p50 174s** and **p95 658.2s**.
  - `BREAK_GLASS` usage was **0**.
- **Operational pain point #1: structurally unfixable review loops**
  - **Evidence:** `review / gate` in **27054941115** identified a literal-grep contract guard that the editor cannot safely fix after refactor; the summary explicitly ties this to orchestrator backpressure.
  - **Smallest safe mitigation:** add a “cannot-autofix / escalate-to-human” state after the first repeat of known structural guard failures.
- **Operational pain point #2: state-transition regression risk in poller logic**
  - **Evidence:** `ci` run **27053190246** failed `test_implementation_failed_reissue_preserves_dependency_gates_and_pending_defs`.
  - **Smallest safe mitigation:** land the dependency-gate/pending-def preservation fix before widening orchestrator behavior.
- **Operational pain point #3: contradictory merge-state messaging**
  - **Evidence:** `forward_merge_stable_to_main` run **27054934367** logged both `git push failed 3 times ... falling back to PR` and `Push reported success but origin/main ... does not contain origin/stable`, then later verified propagation complete.
  - **Smallest safe mitigation:** only emit terminal error state if final verification fails.
- **Track these indicators**
  - count of `CHECK_RUNS_WAIT_TIMEOUT`,
  - `review_autofix` cancellations longer than **15 min**,
  - `validate` failures before first job starts,
  - AI memory `retrieve` hit rate,
  - `context_budget_warn_count`.

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | End-to-end fix |
|---|---|---|---|
| Clarify → Plan | Mostly healthy gating, not compute | `clarify` and `plan` are mostly skipped/other; p50 is **1s** for both families | Keep as-is |
| Implement | Moderate compute when active | successful `implement` runs ranged roughly **272–1,390s**; not the main spend driver | No urgent change before review/autofix fixes |
| Review / Autofix | **Primary bottleneck**: huge prompts, long codex-agent runtime, and check-run waits | `review_autofix` p95 **4,068s**; **27052802146 = 5,404s**, **27053282075 = 2,154s** with `codex-agent` dominating ~35 min; **27054414179** had `review / gate` dominate **1,540s** | Cut prompt size first; then shorten check-run wait budget; then dedupe impossible loops |
| Validate | Fast failure, poor visibility | `validate` failed **13/14** runs, p50 **0s**, with **9** missing archives | Add pre-job observability and archive negative-cache |
| Orchestrate poll / merge | Moderate runtime, mostly healthy; some state confusion | `orchestrate_poll` p50 **174s**; forward-merge **27054934367** had contradictory warning/error/success chain | Fix merge-state reporting and keep poller state transitions conservative |
| Queueing overhead | Secondary bottleneck | hosted-runner wait appears in **27054414121**, **27053983984**, **27054939865**, **27054572029** | Reduce duplicate/cancelled work first; queue time is not the primary issue |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` long tail: **p95 4,068s**, worst run **5,404s**
  - `ci` runtime: **p50 1,462s**
  - `validate` blind failures: **13/14 failed**, often at **0s**
- **Top failure modes**
  - deterministic workflow/script contract drift in CI
  - validate pre-job failure with missing log archives
  - archival policy/template mismatches in `plan`
- **Highest-cost drivers**
  - `review_autofix`: **401,148 tokens**, **198 codex calls**, **20 context warnings**
  - Semble usage concentrated in `review_autofix`: **23 queries / 198,868 bytes**
  - `review_autofix` cancellations: **12 runs**, **16,180s** cancelled wall-clock
- **Top 3 prioritized actions**
  1. **Reduce `review_autofix` prompt size and check-run wait budget**.
  2. **Fix the four deterministic CI contract regressions** (`27042555561`, `27046800332`, `27052802097`, `27053190246`).
  3. **Make `validate` pre-job failures observable and stop blind archive re-fetches**.

## Metrics Appendix

_Notes:_
- Repo-wide summary metrics below use the richer analysis context aggregate supplied with the task.
- Workflow-family rows are from directly parsed run rows in `workflow_log_report.json`, so telemetry subtotals may not sum exactly to repo-wide totals.
- Semble fallback counts should be treated as **upper bounds** until quoted telemetry lines are excluded from parsing.

### Repo summary

| Repo | Runs | Success | Failure | Cancelled | Other | Failure rate | p50 s | p95 s | Codex tokens | Codex calls | `cache_hit_rate` | OR cache read/write | `BREAK_GLASS` | `CONTEXT_BUDGET_WARN` | Semble queries / bytes / fallbacks | Serena queries / fallbacks | Serena probes ok/failed/skipped | Wall p50 / p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---|---|---|---|
| `shubhodeep1/coding-workflows` | 1000 | 228 | 20 | 14 | 738 | 2.0% | 1.0 | 874.0 | 407,226 | 201 | null | 0 / 0 | 0 | 20 | 55 / 503,798 / 150 | 1 / 1 | 0 / 0 / 1 | 6,000 ms / 5,302,820 ms |

### Workflow-family hotspots

| Workflow family | Runs | Success | Failure | Cancelled | Failure rate | p50 s | p95 s | Codex tokens | Codex calls | Context warns | Semble q / bytes / fallbacks |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `review_autofix` | 70 | 56 | 0 | 12 | 0.0% | 252.0 | 4068.0 | 401,148 | 198 | 20 | 23 / 198,868 / 90 |
| `ci` | 24 | 19 | 5 | 0 | 20.8% | 1462.0 | 1572.0 | 0 | 0 | 0 | 0 / 0 / 40 |
| `validate` | 14 | 1 | 13 | 0 | 92.9% | 0.0 | 48.6 | 0 | 0 | 0 | 0 / 0 / 0 |
| `orchestrate_poll` | 16 | 16 | 0 | 0 | 0.0% | 174.0 | 658.2 | 0 | 0 | 0 | 0 / 0 / 0 |
| `issue_pr_status` | 10 | 10 | 0 | 0 | 0.0% | 36.0 | 67.0 | 0 | 0 | 0 | 0 / 0 / 0 |
| `workflow_log_analysis` | 1 | 1 | 0 | 0 | 0.0% | 5228.0 | 5228.0 | 6,078 | 3 | 0 | 32 / 304,930 / 15 |

### GH API summary

| Workflow / step | Evidence | Conservative request floor | Main reduction opportunity |
|---|---|---:|---|
| `review_autofix / review / codex-agent` check-run wait | **27052802146** waited through **5** poll sleeps before timeout; **27035370755** did the same | ≥5 logical polls on affected runs | shrink wait budget and stop after unchanged snapshots |
| `issue_pr_status / Update linked issue labels when PR closes` | **27054939865** stages two GraphQL queries plus REST fallback/label/close path | ~4–6 scripted request opportunities | cache GraphQL results; skip REST fallback when GraphQL already resolved links |
| `validate` archive collection | **9** `missing_log_archive` 404s in current window | 9 failed archive GETs | negative-cache missing archives across audit passes |
| `review_autofix / post-merge-validate-dispatch` | **27054941115** logged one `gh api graphql` hot spot | 1 | low priority |

### Prompt/cache, Semble, Serena, and memory telemetry

| Metric | Value | Evidence / note |
|---|---:|---|
| Prompt cache hit rate | null | no usable prompt-cache telemetry in window |
| `or_prompt_tokens` / `or_total_tokens` | 0 / 0 | no OpenRouter prompt-cache accounting captured |
| AI memory telemetry records | 38 | deep-dive logs only |
| AI memory retrieve hit rate | 0 / 10 (0%) | all retrieves returned `records_selected=0` |
| AI memory retrieve avg `estimated_tokens` | 0 | all sampled retrieves |
| AI memory retrieve `keyword_method` | `llm`: 10 | no `plain` / `none` seen |
| Repo `BREAK_GLASS` count | 0 | none observed |
| Repo `CONTEXT_BUDGET_WARN` count | 20 | all from `review_autofix` |
| Semble aggregate (analysis context) | 55 queries / 503,798 bytes / 150 fallbacks | fallback total is likely inflated by quoted lines |
| Semble runtime lines observed in deep-dive logs | 17 query lines / 20 fallback lines | no Semble probe telemetry observed |
| Serena aggregate (analysis context) | 1 query / 1 fallback / 1 probe skipped | 0 response bytes / 0 tool calls / 0 ms |
| Serena runtime lines observed in deep-dive logs | 0 | effectively inactive in sampled logs |
| Other MCP servers observed | none | none in runtime deep-dive lines |

### Per-target MCP availability

| Server | Target | Probe ok | Probe failed | Probe skipped | Query calls | Fallbacks | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| `serena` | unknown / not emitted in runtime logs | 0 | 0 | 1 | 1 | 1 | aggregate only; no runtime target line available |
| `semble` | n/a | n/a | n/a | n/a | present | present | no Semble probe telemetry emitted in this window |
