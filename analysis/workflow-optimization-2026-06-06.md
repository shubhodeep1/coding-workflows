## Executive Summary

- **`review_autofix` is the dominant latency bottleneck.** It consumed **66,289s / 67.9%** of all observed runtime; **32 runs over 300s accounted for 98.4%** of that family’s wall time, and slow runs like **27043424250 (3,848s)** and **27040617821 (2,856s)** spent **99.5%** of runtime inside `review_codex-agent`. **Estimated impact:** ~**3.6h saved/window** from a 20% cut in long-tail agent time. **Confidence:** medium.
- **Known CI contract regressions are the clearest reliability fix.** **10/38 failures** came from CI: **9** at `lint / Orchestrate lib unit tests` and **1** at `lint / Review autofix review-pipeline plumbing contract test` (runs **27038112742**, **27042555561**, **27046800332**). **Estimated impact:** remove **26% of all failures** and lift CI family success well above the current **8/18 (44.4%)**. **Confidence:** high.
- **Validate is failing hard and is under-instrumented.** The `validate` family failed **26/26 times**, all at **0s**, with no job/step data; **10** of those also produced `partial_data:missing_log_archive` 404s, and the corresponding error folders contain **`metadata.json` only**. **Estimated impact:** highest reliability payoff once root cause is exposed; current exact fix is unknown. **Confidence:** high on the problem, low on the underlying cause.
- **Review check-run polling adds avoidable delay and GitHub API churn.** In **6/10** slow `review_autofix` deep dives, the workflow slept **20–298s** waiting for sibling check-runs (for example **27035370755 = 298s**, **27017928986 = 220s**). **Estimated impact:** **1–3 min** saved on affected runs, plus fewer `check-runs` API polls. **Confidence:** high.
- **AI memory and prompt-cache systems are not yet showing measurable value.** Sampled `AI_MEMORY_TELEMETRY` had **9/9 retrieves with 0 records selected**; prompt-cache metrics were effectively absent (`cache_hit_rate=null`, `or_cache_* = 0`). **Estimated impact:** medium once instrumented/fixed; today this is mostly an observability gap. **Confidence:** high.
- **Semble looks net-positive, not a problem to cut.** Enriched telemetry shows **12 Semble queries / 140,332 bytes**; raw deep-dive queries averaged only **~499ms** each. The only **5 `SEMBLE_FALLBACK`** events were all in CI run **27046800332** against an intentionally missing binary path, not in production review runs. **Estimated impact:** keep as-is; focus elsewhere. **Confidence:** high.

## Speed Optimizations

### 1) **Critical-path:** cut long-tail `review_autofix` AI time with adaptive reasoning/pass count
- **Evidence**
  - `review_autofix` averaged **920.7s**, p95 **3,253s**, and consumed **67.9%** of total window runtime.
  - Slow runs **27043424250**, **27040617821**, and **27024622274** spent **3,830/3,848s (99.5%)**, **2,841/2,856s (99.5%)**, and **3,182/3,200s (99.4%)** inside `step-001-review_codex-agent.log`.
  - Long runs log `Reviewer reasoning effort: xhigh`, `Updated reasoning effort to xhigh for editor phase`, and `ENABLE_REVIEWER_TWO_PASS: true` (for example **27043424250**, **27040617821**, **27035370755**).
  - Defaults in `.github/workflows/review_autofix.yml` are `REVIEWER_REASONING_EFFORT=xhigh` at **line 107**, `EDITOR_REASONING_EFFORT=xhigh` at **line 141**, and `ENABLE_REVIEWER_TWO_PASS=true` at **line 156**.
- **Root cause**
  - The long tail is overwhelmingly inside the Codex reviewer/editor step; existing defaults keep both reviewer and editor at `xhigh`, with two-pass review enabled.
- **Exact change**
  - Reuse the workflow’s existing adaptive knobs:
    - extend the existing smoke-test downgrade path at `.github/workflows/review_autofix.yml:2724-2725,2759`
    - for low-risk real PRs (small diff, no failing check-runs, no materiality suppressor), set reviewer to `high` or `low`, editor to `medium`, and `ENABLE_REVIEWER_TWO_PASS=false`.
- **Estimated time savings**
  - A conservative **20% cut** on the **32 runs >300s** saves about **13,051s (~3.6h)** per 1,000-run window.
- **Implementation risk**
  - **Medium.** Keep current `xhigh`/two-pass path for material diffs and PRs with failing checks.

### 2) **Critical-path:** shorten or early-exit `CHECK_RUNS_WAIT_TIMEOUT_SECS`
- **Evidence**
  - Actual wait-loop lines appeared in **6/10** slow `review_autofix` runs:
    - **27035370755:** **5** wait messages, **298s** scheduled sleep
    - **27017928986:** **4** wait messages, **220s**
    - **27024622274 / 27024715871 / 27040617821:** **140s** each
    - **27026987013:** **20s**
  - `.github/workflows/review_autofix.yml:179-180` defaults `CHECK_RUNS_WAIT_TIMEOUT_SECS` to **300**.
  - `README.md:66-67` says each poll iteration can cost **≥1 GitHub API request** and may exceed the nominal wait budget because of retry/backoff.
- **Root cause**
  - The workflow waits for sibling check-runs even when the in-flight signature is unchanged and no failing checks are eventually attached.
- **Exact change**
  - Lower the default timeout to **120–180s**, or exit after **two unchanged self-excluded snapshots** when only one sibling run remains in flight.
- **Estimated time savings**
  - **1–3 min** on the worst affected runs; **~158–338s** across the sampled slow runs depending on the cap.
- **Implementation risk**
  - **Medium.** Could miss a late-finishing failure, but the current design already fail-opens after timeout.

### 3) **Secondary:** move deterministic skip earlier
- **Evidence**
  - Run **27050661606** finished in **30s** and logged a deterministic skip decision with `doc_only=true`, `skip=true`, `reason=docs_only`.
  - **36** successful `review_autofix` runs finished in **≤40s**, consuming only **469s** total.
- **Root cause**
  - Low-risk PRs still start the workflow and pay runner/gate overhead before the skip exits.
- **Exact change**
  - Reuse the existing deterministic skip logic to prevent dispatching `internal-review.yml`, or at least prevent starting `review_codex-agent`, for clearly doc-only/small-diff PRs.
- **Estimated time savings**
  - Small: about **8 min/window** on current data.
- **Implementation risk**
  - **Low.**

### 4) **Micro:** treat `orchestrate_poll` outliers as queueing, not compute
- **Evidence**
  - `orchestrate_poll` p50 was **189s**, but p95 reached **577s**.
  - Run **27048519888** took **902s** and its `log_summary` says `poll/system waited for a runner`; run **27049668465** with the same setup-uv cache hit and `SEMBLE_AVAILABLE=false` finished in **189s**.
- **Root cause**
  - Runner availability / queueing, not poll logic.
- **Exact change**
  - Track queue wait separately and only tune schedule/concurrency if outliers recur.
- **Estimated time savings**
  - Up to **~713s** on rare queue-bound outliers.
- **Implementation risk**
  - **Low**, but recurrence confidence is only **medium**.

**Speed gap to leave unoptimized for now:** CI is the second-largest time bucket (**12.6%** of total duration), but this deep-dive set did not include slow CI step logs, so I’m not making step-level CI speed recommendations yet.

## Cost Optimizations

### 1) Adaptive review/autofix reasoning is the biggest cost lever
- **Evidence**
  - The same evidence that dominates latency also dominates likely AI spend: long `review_autofix` runs are almost entirely `review_codex-agent`, while runtime logs show `xhigh` reviewer/editor settings and two-pass review.
  - Telemetry coverage is incomplete: raw folder telemetry shows only **2 Codex calls / 4,052 tokens**, while enriched run-row telemetry lifts that to **3 calls / 6,078 tokens**. That is clearly a **lower bound**, because the slowest AI-heavy runs were only partially instrumented.
- **Root cause**
  - High reasoning effort is being used on the hottest and slowest path by default.
- **Exact change**
  - Same as Speed #1: extend the existing adaptive downgrade path to low-risk real PRs.
- **Estimated savings**
  - **Largest available token/$ reduction**, but exact dollars are undercounted by telemetry gaps.
- **Quality-risk notes**
  - **Medium.** Keep `xhigh` and two-pass for material or failing-check PRs.

### 2) Prompt-cache instrumentation is missing; fix that before trying to tune it
- **Evidence**
  - `cache_hit_rate=null`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0` in both raw summary and enriched analysis context.
  - No `CONTEXT_BUDGET_WARN` lines or counts were observed.
- **Root cause**
  - Either prompt cache is not enabled/emitted, or telemetry isn’t surfacing it.
- **Exact change**
  - Emit `cache_hit_rate`, read/write tokens, and stable prompt IDs for every Codex/OpenRouter call.
  - Keep stable policy/system preambles at the front of prompts; move volatile repo/event/diff blobs to the tail or referenced files.
- **Estimated savings**
  - **Unknown until instrumentation exists**, but likely meaningful on repeated `review_autofix` and orchestrator prompts.
- **Quality-risk notes**
  - **Low.**

### 3) Keep Semble; it is not the cost problem
- **Evidence**
  - Enriched telemetry: **12 queries / 140,332 bytes**.
  - Raw deep dives: **11 queries / 135,212 bytes / 5,489ms** total, averaging **~12.3KB** and **~499ms** each.
  - Query targets were mostly `reviewer-context` (7) and `overflow` (4), with targeted files such as `.github/workflows/test-and-mark-stable.yml`.
  - All **5 fallbacks** came from CI run **27046800332** against `missing_semble`.
- **Root cause**
  - Semble is already acting as a focused retrieval layer; its overhead is tiny compared with multi-thousand-second review runs.
- **Exact change**
  - Do **not** remove Semble.
  - Add alerting only if `SEMBLE_FALLBACK` appears outside CI/test workflows.
- **Estimated savings**
  - Removing it would save almost nothing and likely increase prompt expansion.
- **Quality-risk notes**
  - **High risk if removed.**
  - **Inference:** 6–12 chunk, 6.5–18.0KB Semble payloads are likely cheaper than expanding whole files into prompts.

### 4) Cut wasted reruns / cancellations after fixing known failures
- **Evidence**
  - Cancelled `review_autofix` runs consumed **9,107s** total.
  - CI failures consumed **461s** total; plan lint failures another **42s**.
- **Root cause**
  - Contract drift and review supersession/cancellation.
- **Exact change**
  - Fix the CI regressions first, then inspect why `review_autofix` cancellations reached **6 runs** in this window.
- **Estimated savings**
  - At least **~2.7h** of wasted runner time.
- **Quality-risk notes**
  - **Low**, but cancellation causes should be verified before changing concurrency behavior.

### 5) Serena is currently neither saving money nor adding noise
- **Evidence**
  - No runtime `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines were found.
  - All Serena counters were **0**.
- **Root cause**
  - No observable rollout/use in this window.
- **Exact change**
  - Either keep it disabled, or instrument rollout so savings/overhead can be measured.
- **Estimated savings**
  - None today.
- **Quality-risk notes**
  - **Low.**

## Reliability Improvements

_No `BREAK_GLASS` or `CONTEXT_BUDGET_WARN` lines were observed in deep dives, and enriched top-line counts were also zero. Current reliability pressure is coming from contract drift and observability gaps, not policy/rubric overrides or prompt-size exhaustion._

### 1) Fix the CI contract regressions immediately
- **Failure evidence**
  - **9** CI failures hit `lint / Orchestrate lib unit tests`.
    - Runs **27038112742** and **27042555561** show `REVIEW_RUN_MAX_RUNTIME_MINUTES: unbound variable` in `extracted/poller_stall.sh` and a missing `timeout --signal=TERM --kill-after=5s` contract in `.github/workflows/implement.yml`.
  - **1** CI failure (**27046800332**) hit `lint / Review autofix review-pipeline plumbing contract test`, asserting Codex stdin must come from `${attempt_prompt_file}`, not `${EDITOR_PROMPT_FILE}`.
- **Root cause category**
  - Workflow/script contract drift.
- **Exact fix**
  - Restore `REVIEW_RUN_MAX_RUNTIME_MINUTES` plumb-through in the shared poller stall path.
  - Restore `timeout --signal=TERM --kill-after=5s` in `.github/workflows/implement.yml`.
  - Feed editor stdin from the per-attempt prompt file.
- **Expected reliability impact**
  - Removes **10/38 failures (26.3%)** in this window.
- **Rollback / fail-open**
  - Keep the contract tests; they are already catching real regressions.

### 2) Restore validate observability before trying to “fix” validate behavior
- **Failure evidence**
  - `validate` failed **26/26** times, all at **0s**, with no `job_name` or `step_name`.
  - `summary.json` recorded **10** `partial_data:missing_log_archive` errors for validate runs like **27046798333**, **27045589137**, and **27043422852**.
  - The corresponding error folders contain **only `metadata.json`**.
- **Root cause category**
  - Unknown pre-job workflow failure + missing archive observability gap.
- **Exact fix**
  - Add a validate bootstrap/dispatch contract test in CI so bad validate wiring fails before merge.
  - In the collector, persist/archive-terminal 404 knowledge so metadata-only failures stop re-triggering archive fetches on subsequent windows.
  - If feasible, add a minimal bootstrap job/step in validate that always emits something loggable before any dynamic staging.
- **Expected reliability impact**
  - Highest potential payoff: validate accounts for **68.4% of all failures**.
- **Rollback / fail-open**
  - Ship collector/logging improvements first if workflow bootstrap changes are risky.

### 3) Treat current Semble fallbacks as healthy fail-open, not a production rollout incident
- **Failure evidence**
  - All **5** observed `SEMBLE_FALLBACK` lines occurred in CI run **27046800332**, all on `target=overflow`, all with `reason=[Errno 2] ... missing_semble`.
  - No production `review_autofix` or `orchestrate_poll` deep dive logged a Semble fallback.
- **Root cause category**
  - Test-driven fail-open path validation.
- **Exact fix**
  - Keep the fallback behavior.
  - Split reporting severity: CI/test fallbacks = informational; non-CI fallbacks = actionable.
- **Expected reliability impact**
  - Reduces false-alarm noise while preserving resilience.
- **Rollback / fail-open**
  - Current behavior is already fail-open and safe.

### 4) Prevent plan archival lint failures earlier
- **Failure evidence**
  - Runs **27042555542** and **27046800333** both failed `Run plan-archival completeness lint` in **~21s**.
  - The error was the same: tracking issue **#3042** still had unchecked sub-issue checkboxes while the PR archived a plan into `docs/completed/` without a non-empty `## De-scoped phases` section; **27046800333** explicitly cited `fix-s2-stall-guard-contract-test: #3146`.
- **Root cause category**
  - Process/template gap.
- **Exact fix**
  - Mirror the lint in a PR template or pre-submit script when files move under `docs/completed/`.
- **Expected reliability impact**
  - Small-to-medium; removes **2/38 failures** with low effort.
- **Rollback / fail-open**
  - No behavior change at runtime; only earlier feedback.

## AI Memory Health

- I found **30 `AI_MEMORY_TELEMETRY` lines** across **9** slow `review_autofix` runs: **27013113475**, **27017928986**, **27019350211**, **27024622274**, **27024715871**, **27026987013**, **27035370755**, **27040617821**, **27043424250**.
- Operation mix:
  - **18** `record-run-event`
  - **9** `retrieve`
  - **3** `record-candidate`
- **Retrieval hit rate was 0%.** All **9/9** retrieves had `records_selected=0`.
- **Average `estimated_tokens` on retrieve was 0.** The sampled payloads did **not** emit a memory-budget field, so estimated-tokens-vs-budget cannot be computed from this window.
- `keyword_method` was **`llm` in 9/9 retrieves**.
- No sampled retrieve had `enabled=false` or `fail_open=true`.
- Push health was mostly good:
  - all sampled record operations succeeded
  - max `push_attempts` observed was **2** (run **27024622274**, `phase_started` event); otherwise pushes were 1-attempt.

**What this means**
- Memory is **enabled**, but it is not currently retrieving usable records in the sampled heavy path.
- The smallest safe next step is **not more memory writes**; it is to inspect retrieval query construction / filters and add a budget field to telemetry.
- I also only saw memory telemetry in `review_autofix` deep dives, so if memory is supposed to help `plan`/`implement`/`clarify`, verify that those workflows emit telemetry consistently.

## GH API Call Audit

_No actual rate-limit events or retry storms were visible in the deep-dive logs. The main GitHub-call risks are repeated polling, repeated 404s, and a few code-path redundancies._

### 1) `review_autofix` check-run polling is the hottest GitHub API pattern
- **Evidence**
  - Six slow runs logged repeated `Waiting for 1 in-progress/queued check-run(s)...` lines.
  - Conservative observed volume: **19 wait iterations** across those six runs.
  - `README.md:66-67` explicitly notes the check-run snapshot path may consume multiple underlying GitHub API requests and retries.
- **High-redundancy pattern**
  - Polling `GET /repos/{repo}/commits/{sha}/check-runs?per_page=100` while the snapshot stays unchanged.
- **Concrete change**
  - Early-exit on repeated identical snapshots; lower timeout.
- **Expected reduction**
  - At least **several `check-runs` requests per affected run**, plus lower rate-limit exposure.

### 2) Missing validate log archives are creating repeat 404 traffic with no diagnostic payoff
- **Evidence**
  - `summary.json` recorded **10** `repos/.../actions/runs/{id}/logs` 404s.
  - The stored validate error folders for those runs contain no step logs, only metadata.
  - The collector tests in `tests/test_collect_workflow_logs.py:647-688` and `:844-875` show the current behavior is already a **1-retry soft-fail with per-run cache**, so these 404s are unique-run misses, not retry loops.
- **High-redundancy pattern**
  - Terminal archive fetches against runs that provide no archive.
- **Concrete change**
  - Persist “archive unavailable” across the analysis window, or short-circuit archive fetch for **0s validate failures with no job/step**.
- **Expected reduction**
  - **10 failed archive calls/window** immediately.

### 3) `review_autofix` still has a duplicate paginated PR-files fetch path
- **Evidence**
  - `.github/workflows/review_autofix.yml:460` fetches `repos/${REPOSITORY}/pulls/${PR_NUMBER}/files`.
  - `.github/workflows/review_autofix.yml:517` fetches the same endpoint again if `pr_files_json` is empty while the materiality suppressor runs.
- **High-redundancy pattern**
  - Same paginated `/files` snapshot may be re-fetched inside one gate evaluation.
- **Concrete change**
  - Memoize the first `/files` snapshot and reuse it across doc-only and materiality checks.
- **Expected reduction**
  - Up to **1 paginated GET** per qualifying gate evaluation.
- **Rate-limit risk reduction**
  - Low, but essentially free.

### 4) Reuse the repo’s existing batching patterns more broadly
- **Evidence**
  - `.github/workflows/review_autofix_sweep.yml:104-133` already snapshots active review runs once per workflow to avoid **N×2** fanout.
  - `.github/workflows/issue_pr_status.yml:363-406` already batches issue classification in a single GraphQL alias query and only falls back to per-issue REST on failure.
- **Concrete change**
  - Treat those as the house style for other workflows that still do repeated GitHub lookups or repeated support-source staging.

### 5) GitHub control-plane fetches (checkouts/clones) are also duplicated
- **Evidence**
  - The same support-source fetch/fallback block appears in `.github/workflows/orchestrate.yml:156-188`, `.github/workflows/clarify.yml:164-203`, `.github/workflows/plan.yml:215-254`, and similar jobs elsewhere.
  - `.github/workflows/issue_pr_status.yml:41-120`, `:542-568`, and `:642-668` embed very similar staging logic multiple times.
  - `.github/workflows/validate.yml:238-330` already shows the cleaner “stage once, copy locally” pattern.
- **Concrete change**
  - Lift validate’s pattern into one shared helper/composite action and stage support files once per job.
- **Expected reduction**
  - Fewer GitHub-side clone/fetch operations and less copy/paste drift.
- **Rate-limit risk reduction**
  - Low-to-medium; mostly a hygiene and reliability win.

## Prompt Cache & Memory System

- **Prompt cache is effectively invisible right now.**
  - Raw folder telemetry: `cache_hit_rate=null`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`.
  - Enriched window telemetry says the same.
- **There is no current prompt-size emergency.**
  - `CONTEXT_BUDGET_WARN` count was **0** in enriched telemetry, and no runtime lines were observed.
  - `break_glass_count` was also **0**.
- **Do not confuse infra cache with prompt cache.**
  - Runs **27048519888** and **27049668465** showed `setup-uv` cache hits; that is environment caching, not AI prompt caching.
- **Semble looks like a helpful prompt-shaping tool.**
  - Raw queries were targeted and cheap:
    - `reviewer-context`: **7** queries, **109,240 bytes**, **3,399ms**
    - `overflow`: **4** queries, **25,972 bytes**, **2,090ms**
- **Memory retrieval is not yet paying off.**
  - `AI_MEMORY_TELEMETRY.retrieve` hit rate was **0/9**.

**Concrete improvements**
1. Emit prompt-cache metrics on every AI call.
2. Keep stable system/policy blocks fixed and early; move dynamic repo/event blobs later.
3. Verify whether dynamic orchestrator state is leaking into model prompts.  
   - **Inference:** the large `ORCHESTRATOR_STATE_V2` blob visible in plan run **27049717151**’s `plan.if` evaluation would fragment cache prefixes if similar data is injected near the front of prompts.
4. Make memory retrieval observable enough to debug (`budget`, candidate count, rejection reason), then tune retrieval quality before increasing memory volume.

## Orchestrator Health

- The orchestrator control plane is **functionally active but very skip-heavy**:
  - `clarify`: **194/199** skipped
  - `plan`: **188/196** skipped
  - `implement`: **188/193** skipped
  - `orchestrate_clarify_respond`: **193/193** skipped
- Those **763 skipped runs** consumed only **1,172s** total, so they are **not** the critical-path latency problem; they are operational noise and runner churn.
- Example: plan run **27049717151** evaluated `plan.if` to `false` immediately and ended in **1s**.
- The actual heavy orchestrator path is:
  - active `clarify` successes averaging **93s**
  - active `plan` successes averaging **454s**
  - active `implement` successes averaging **691s**
  - then `review_autofix`, which dominates everything after that
  - `validate`, which currently fails before any useful work is observable
- `orchestrate_poll` is generally healthy but has queue outliers; **27048519888** is the clearest example.
- No runtime Serena usage was observed, so there is no evidence of a broken Serena rollout—just no rollout signal.
- No `BREAK_GLASS` pressure is visible.

**Smallest safe mitigations**
1. Tighten upstream dispatch so only the needed child workflow starts.
2. Track `review_autofix` p95 and `validate` 0s failures as first-class orchestrator health KPIs.
3. Track skipped-run ratio across clarify/plan/implement/respond; if it stays high, move filtering earlier.

## Pipeline Flow Bottlenecks

| Stage | Evidence | Bottleneck type | Recommended fix |
|---|---|---|---|
| Clarify | 199 runs, 194 skipped, 5 successes avg 93s | Control-plane fan-out | Tighten dispatch; low urgency |
| Plan | 193 AI-plan runs with 188 skipped; 5 active successes avg 454s; 2 lint failures at 21s | Moderate compute + process interruption | Keep active path as-is; prevent archival lint failures earlier |
| Implement | 193 runs, 188 skipped, 5 active successes avg 691s | Compute | No current failure signal; hold until step logs show hotspots |
| Review/Autofix | 72 runs; 67.9% of total duration; p95 3253s; step-001 dominates slow runs | **Primary compute bottleneck** | Adaptive reasoning/two-pass control; shorten check-run waits |
| Validate | 26/26 failed at 0s; no logs | **Terminal blocker / observability failure** | Add bootstrap contract + metadata-first collection |
| Orchestrate poll | p50 189s, p95 577s, one 902s runner-wait outlier | Queueing | Measure queue wait and concurrency impact |
| Merge/conflict handling | No recurring conflict-heal or merge-retry pattern visible in sampled logs | Not a current hotspot | Monitor only |

**Ordered by end-to-end impact**
1. Trim long-tail `review_autofix` AI work.
2. Restore validate diagnosability and bootstrap health.
3. Fix CI contract regressions that block merges.
4. Reduce no-op orchestration fan-out.
5. Then micro-optimize GitHub polling/fetch paths.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` (**67.9%** of total runtime)
- CI success path (**12.6%** of total runtime), though step-level hotspots were not captured
- `orchestrate_poll` queue outliers

**Top failure modes**
- `validate`: **26** zero-second failures
- CI `Orchestrate lib unit tests`: **9** failures
- CI review/autofix plumbing contract: **1** failure
- plan archival lint: **2** failures

**Highest-cost drivers**
- `review_autofix`: **66,289s**
- `ci`: **12,346s**
- `orchestrate_poll`: **5,359s**
- `copilot_pull_request_reviewer`: **4,512s**

**Top 3 prioritized actions**
1. **Reduce long-tail `review_autofix` AI time** by adapting reasoning effort and disabling reviewer two-pass on low-risk PRs.
2. **Fix the known CI regressions and add a validate bootstrap contract test** so broken workflow contracts stop landing.
3. **Tighten orchestration fan-out and shorten review check-run polling**, using the repo’s existing batching patterns as the template.

## Metrics Appendix

**Note:** `summary.json` only contains raw deep-dive telemetry for **30** runs. The prompt’s enriched `analysis_context` widens telemetry coverage to **115** runs via run-row `log_summary`. I use the wider row for top-line telemetry totals and the raw logs for step-level evidence.

### Window summary

| Metric | Value |
|---|---:|
| Total runs | 1000 |
| Success | 190 |
| Failure | 38 |
| Cancelled | 6 |
| Skipped/other | 766 |
| Avg duration | 97.6s |
| P50 duration | 1s |
| P95 duration | 451s |
| Non-skipped success rate | 81.2% |

### Telemetry coverage and cache metrics

| Telemetry source | Runs with telemetry | Wall-clock samples | codex_calls | codex_tokens | semble_queries | semble_bytes | semble_fallbacks | cache_hit_rate | wall_clock_p50_ms | wall_clock_p99_ms | break_glass | context_budget_warn |
|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|
| summary.json raw deep-dive subset | 30 | 30 | 2 | 4052 | 11 | 135212 | 5 | null | 30000 | 4358490 | 0 | 0 |
| analysis_context enriched window | 115 | 106 | 3 | 6078 | 12 | 140332 | 5 | null | 1000 | 3835900 | 0 | 0 |

### Workflow-family metrics

| Workflow family | Runs | Success | Failure | Cancelled | Skipped | Avg s | P50 s | P95 s | Total s | Share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 72 | 64 | 0 | 6 | 2 | 920.7 | 65 | 3253 | 66289 | 67.9% |
| ci | 18 | 8 | 10 | 0 | 0 | 685.9 | 227 | 1552 | 12346 | 12.6% |
| orchestrate_poll | 20 | 20 | 0 | 0 | 0 | 267.9 | 189 | 577 | 5359 | 5.5% |
| copilot_pull_request_reviewer | 17 | 17 | 0 | 0 | 0 | 265.4 | 253 | 479 | 4512 | 4.6% |
| implement | 193 | 5 | 0 | 0 | 188 | 19.6 | 1 | 9 | 3782 | 3.9% |
| plan | 196 | 6 | 2 | 0 | 188 | 15.6 | 1 | 11 | 3057 | 3.1% |
| clarify | 199 | 5 | 0 | 0 | 194 | 3.6 | 1 | 2 | 717 | 0.7% |
| issue_pr_status | 11 | 11 | 0 | 0 | 0 | 61.9 | 67 | 71 | 681 | 0.7% |
| orchestrate_clarify_respond | 193 | 0 | 0 | 0 | 193 | 1.5 | 1 | 6 | 299 | 0.3% |
| integration_pr_readiness | 21 | 21 | 0 | 0 | 0 | 8.9 | 8 | 11 | 187 | 0.2% |
| lint_pr_body_auto_close | 18 | 18 | 0 | 0 | 0 | 8.3 | 8 | 13 | 150 | 0.2% |
| cancel_on_pr_close | 11 | 11 | 0 | 0 | 0 | 7.9 | 8 | 10 | 87 | 0.1% |
| forward_merge_stable_to_main | 2 | 2 | 0 | 0 | 0 | 27.0 | 28 | 28 | 54 | 0.1% |
| memory_maintenance | 1 | 1 | 0 | 0 | 0 | 51.0 | 51 | 51 | 51 | 0.1% |
| promote_main_to_stable | 1 | 1 | 0 | 0 | 0 | 30.0 | 30 | 30 | 30 | 0.0% |
| update_workflows | 1 | 0 | 0 | 0 | 1 | 3.0 | 3 | 3 | 3 | 0.0% |
| validate | 26 | 0 | 26 | 0 | 0 | 0.0 | 0 | 0 | 0 | 0.0% |

### Review/autofix sub-breakdown

| Review/autofix workflow | Runs | Success | Cancelled | Skipped | Avg s | P50 s | P95 s |
|---|---:|---:|---:|---:|---:|---:|---:|
| Internal: AI Review & Autofix | 44 | 36 | 6 | 2 | 1094.6 | 336 | 3606 |
| Codex PR Self-Healing Semantic Agent | 11 | 11 | 0 | 0 | 1636.1 | 1659 | 3025 |
| Internal: AI Review Autofix Sweep | 17 | 17 | 0 | 0 | 7.6 | 7 | 11 |

### No-op orchestrator fan-out

| Workflow family | Skipped runs | Total skipped seconds | Avg skipped seconds | Max skipped seconds |
|---|---:|---:|---:|---:|
| clarify | 194 | 251 | 1.29 | 11 |
| plan | 188 | 293 | 1.56 | 29 |
| implement | 188 | 329 | 1.75 | 10 |
| orchestrate_clarify_respond | 193 | 299 | 1.55 | 11 |

### Failure modes

| Failure mode | Runs | Evidence |
|---|---:|---|
| validate 0s failures with no job/step | 26 | validate family; e.g. 27046798333, 27045589137, 27043422852 |
| CI lint / Orchestrate lib unit tests | 9 | 27038112742, 27042555561 and 7 peers |
| CI lint / Review autofix plumbing contract | 1 | 27046800332 |
| plan archival completeness lint | 2 | 27042555542, 27046800333 |

### Review/autofix check-run wait hotspot

| Run ID | Wait messages | Observed scheduled sleep |
|---|---:|---:|
| 27035370755 | 5 | 298s |
| 27017928986 | 4 | 220s |
| 27024622274 | 3 | 140s |
| 27024715871 | 3 | 140s |
| 27040617821 | 3 | 140s |
| 27026987013 | 1 | 20s |

### GH API summary

| Pattern | Evidence | Conservative volume in sampled window | Recommendation |
|---|---|---:|---|
| `check-runs` polling in review_autofix | 6 slow runs logged repeated wait lines; README says each iteration costs ≥1 request | ≥19 poll iterations | Early-exit unchanged snapshots; reduce timeout |
| Missing `actions/runs/{id}/logs` archives | 10 summary.json 404 errors on validate runs | 10 failed archive calls | Persist “archive unavailable” and skip repeat fetches |
| Duplicate `/pulls/{PR_NUMBER}/files` fetch in gate | `review_autofix.yml:460` and `:517` | up to 1 extra paginated GET per qualifying run | Memoize first files snapshot |
| Good batching pattern already present | `review_autofix_sweep.yml:104-133`, `issue_pr_status.yml:363-406` | n/a | Reuse these patterns elsewhere |

### Semble / Serena / MCP telemetry

| Semble target | Raw query count | Raw bytes | Avg bytes/query | Total query ms | Avg ms/query |
|---|---:|---:|---:|---:|---:|
| reviewer-context | 7 | 109240 | 15606 | 3399 | 486 |
| overflow | 4 | 25972 | 6493 | 2090 | 522 |

| Metric | Value |
|---|---:|
| Enriched Semble query calls | 12 |
| Enriched Semble query bytes | 140332 |
| Enriched Semble fallbacks | 5 |
| Raw deep-dive Semble fallbacks | 5, all in CI run 27046800332 |
| Serena query calls | 0 |
| Serena fallbacks | 0 |
| Serena probes ok/failed/skipped | 0 / 0 / 0 |
| Other MCP servers observed | none |

| MCP server | Target | probe_ok | probe_failed | probe_skipped | query_calls | fallbacks | response/query bytes |
|---|---|---:|---:|---:|---:|---:|---:|
| Serena | all | 0 | 0 | 0 | 0 | 0 | 0 |
| Other MCP servers observed | none | 0 | 0 | 0 | 0 | 0 | 0 |

### AI memory telemetry

| AI memory metric | Value |
|---|---:|
| Runs with AI_MEMORY_TELEMETRY | 9 |
| Telemetry lines | 30 |
| retrieve ops | 9 |
| retrieve hit rate | 0/9 (0%) |
| avg estimated_tokens on retrieve | 0 |
| keyword_method=llm | 9/9 |
| enabled=false retrieves | 0 |
| fail_open=true retrieves | 0 |
| record-run-event ops | 18 |
| record-candidate ops | 3 |
| max push_attempts | 2 |

## Deep Audit — Workflows & Scripts (2026-06-06)

### Section 1: Bug & Correctness Sweep

- **BUG-001**  
  **File path:** `scripts/review_run_reviewers.sh:3099-3108`  
  **Severity:** High  
  **Category tag:** `bug`  
  **Description:** In two-pass mode, pass 1 is still hardcoded to `xhigh` via `run_reviewer_pass "pass1" ... "xhigh"`. That bypasses the normal reasoning-resolution path used by single-pass mode at `scripts/review_run_reviewers.sh:3213-3215`, and it also bypasses the explicit override that pass 2 honors at `scripts/review_run_reviewers.sh:3189-3197`. The workflow advertises `REVIEWER_REASONING_EFFORT` as the override surface (`.github/workflows/review_autofix.yml:107,2724-2725,2759-2760`), and `agents.md:62-64` documents smoke reviewer downgrades, but two-pass pass 1 ignores them.  
  **Recommended fix:** Make pass 1 use the same reasoning-resolution path as single-pass—either pass `""` to `run_reviewer_pass` or compute `PASS1_REASONING="${REVIEWER_REASONING_EFFORT:-}"` first—so smoke and repo-level overrides apply consistently to both passes.

- **SEC-001**  
  **File path:** `scripts/review_conflict_resolve.sh:2314-2316; scripts/review_commit_changes.sh:482-490`  
  **Severity:** Medium  
  **Category tag:** `security`  
  **Description:** Both scripts set the remote URL with an unquoted PAT-bearing string: `git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}`. Shellcheck reports SC2086 on both lines, so word-splitting/globbing is possible, and the token is also embedded directly in the git command line. The repo already uses a safer header-based auth pattern in `.github/workflows/clarify.yml:82-85` and `.github/workflows/validate.yml:108-110`.  
  **Recommended fix:** At minimum, quote the full URL in both scripts. Prefer the existing `git -c "http.extraHeader=Authorization: Basic ..."` helper pattern so the PAT is not embedded in the remote URL/argv at all.

### Section 2: GitHub API Call Redundancy Audit

_Not repeated here: the existing report already covers the `review_autofix` check-run polling hotspot and the duplicate `/pulls/{PR}/files` fetch path._

- **API-001**  
  **File path:** `scripts/orchestrate_poll_process.sh:2843-2884`  
  **Severity:** Medium  
  **Category tag:** `api-batching`  
  **Description:** `_subissue_closing_pr_number()` does one `gh pr list --head` lookup, then on a tier-1 miss calls `_issue_cross_ref_pr_numbers_unique`, then loops over each candidate PR and fetches `repos/${GITHUB_REPOSITORY}/pulls/${pr}` one at a time. The tier-2 path only needs merged state and PR body text, so the per-PR REST loop is batchable.  
  **Current call count:** Tier-2 path = `2 + N` calls (`1` head-branch list, `1` cross-reference/timeline fetch, up to `N` individual PR fetches).  
  **Proposed call count:** Tier-2 path = `2 + ceil(N/25)` calls, typically `3` when `N <= 25`.  
  **Pattern to extend:** `scripts/orchestrate_poll_process.sh:10174-10409` (`_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql`).  
  **Recommended fix:** Add a batched helper such as `_fetch_pr_details_graphql <pr_numbers_json>` using the same GraphQL-alias batching shape, then replace the `while read pr; gh api pulls/${pr}` loop with one batched fetch.

- **API-002**  
  **File path:** `.github/workflows/review_autofix.yml:767-819`  
  **Severity:** Low  
  **Category tag:** `api-batching`  
  **Description:** The post-merge validate dispatch first queries `closingIssuesReferences`, then falls back to `GET /pulls/${PR_NUMBER}` body/title parsing, and finally loops over each recovered issue number. When `labels_known != true`, every issue triggers a separate `gh issue view ... --json labels` call. Those label fetches are independent and batchable.  
  **Current call count:** Fallback path = `1 + 1 + N` calls (`1` GraphQL PR lookup, `1` PR REST body fetch, up to `N` per-issue label fetches).  
  **Proposed call count:** Fallback path = `1 + 1 + 1` calls (`1` GraphQL PR lookup, `1` PR REST body fetch, `1` batched issue-label lookup).  
  **Pattern to extend:** `scripts/orchestrate_poll_process.sh:10174-10297` (`_fetch_candidate_issue_details_graphql`).  
  **Recommended fix:** After the regex fallback builds `issue_numbers`, batch-fetch labels in one aliased GraphQL query (or lift that into `scripts/gh_helpers.sh`) and drive the loop from the returned map instead of calling `gh issue view` once per issue.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001**  
  **File path:** `.github/workflows/validate.yml:209-637; .github/workflows/review_autofix.yml:1227-1555; .github/workflows/implement.yml:831-1059; .github/workflows/clarify.yml:215-308; .github/workflows/plan.yml:266-358; .github/workflows/orchestrate_clarify_respond.yml:259-370`  
  **Severity:** Medium  
  **Category tag:** `duplication`  
  **Description:** Six workflows inline the same support-staging concerns: resolve `SCRIPT_REF` vs `main`, clone/copy support files, distinguish required vs optional assets, stage prompts and AI-memory schemas, and emit `.gitignore` entries for fetched files. `validate.yml:238-345` already contains the most reusable version (`checkout_support_ref`, `copy_from_ref_or_local`), but the other workflows re-implement similar logic with slightly different behavior. This duplication is already causing drift and it overlaps the two highest expression-risk blocks (EXPR-001 and EXPR-002).  
  **Proposed module:** `scripts/stage_workflow_support.sh`  
  **Function signature:** `stage_workflow_support <wf_source> <script_ref> <primary_root> <fallback_root> <dest_root> <profile>`  
  **Callers to update:** `validate.yml`, `review_autofix.yml`, `implement.yml`, `clarify.yml`, `plan.yml`, `orchestrate_clarify_respond.yml`  
  **Recommended fix:** Lift validate’s `checkout_support_ref` / `copy_from_ref_or_local` logic into the shared module and let each workflow supply only a manifest/profile describing which scripts, prompts, and schemas are required or optional.

- **DUP-002**  
  **File path:** `.github/workflows/comprehensive-test-and-release.yml:72-98,318-344; .github/workflows/test-and-mark-stable.yml:477-489,610-622,829-841,1284-1302,2465-2476`  
  **Severity:** Low  
  **Category tag:** `duplication`  
  **Description:** Near-identical `gh_api_safe()` wrappers are redefined seven times across these two workflows. All copies call `gh api`, detect rate-limit text, back off 30→60→120 seconds, and otherwise fail open. Keeping seven copies invites drift in backoff, error logging, and permanent-failure handling away from the repo’s canonical GitHub API helper behavior.  
  **Proposed module:** `scripts/gh_helpers.sh`  
  **Function signature:** `gh_api_safe <gh-api-args...>`  
  **Callers to update:** dispatch/polling steps in `comprehensive-test-and-release.yml` and `test-and-mark-stable.yml`  
  **Recommended fix:** Add a shared `gh_api_safe` (or `gh_retry_or_empty`) wrapper to `scripts/gh_helpers.sh` on top of the existing `gh_retry` logic, then source that helper instead of redefining the function inline in each workflow step.

### Section 4: Expression Size Limit Risk Assessment

_I found four interpolated `run:` blocks above the 15,000-character warning threshold. I did not find any `if:` expression close to the 21,000-character cap, and no workflow file exceeded the 800 KB warning threshold._

- **EXPR-001**  
  **File path:** `.github/workflows/validate.yml:209-637`  
  **Severity:** High  
  **Category tag:** `expression-limit`  
  **Estimated current expression size:** ~20,065 chars  
  **Headroom remaining:** ~935 chars  
  **Description:** `Fetch workflow support files` is already within ~4.5% of GitHub’s 21,000-character hard failure limit. It combines ref selection, support checkout, fallback logic, file copy helpers, schema staging, prompt staging, and support-file staging in one interpolated `run:` block, so small future edits can make `validate.yml` fail at workflow-load time.  
  **Recommended fix:** Extract the entire step to an external script (preferred, and shared with DUP-001) so the YAML step becomes a thin env/argument wrapper.

- **EXPR-002**  
  **File path:** `.github/workflows/review_autofix.yml:1227-1555`  
  **Severity:** High  
  **Category tag:** `expression-limit`  
  **Estimated current expression size:** ~18,675 chars  
  **Headroom remaining:** ~2,325 chars  
  **Description:** `Stage workflow support files` is already above the 85% warning band. It is another large inline support-staging block in the repo’s largest workflow file, and it will keep growing as review/autofix support assets change.  
  **Recommended fix:** Move this block to the shared external staging script/composite proposed in DUP-001.

- **EXPR-003**  
  **File path:** `.github/workflows/review_autofix.yml:1828-2221`  
  **Severity:** Medium  
  **Category tag:** `expression-limit`  
  **Estimated current expression size:** ~17,408 chars  
  **Headroom remaining:** ~3,592 chars  
  **Description:** `Collect PR metadata` is already well into the warning band. The step mixes retry helpers, multiple GitHub API fetches, diff generation, hashing, and environment export in a single interpolated block, so future edits have little room before hitting the hard cap.  
  **Recommended fix:** Extract PR metadata/diff collection into a dedicated script such as `scripts/review_collect_pr_metadata.sh <repo> <pr_number> <out_dir>`, leaving only env setup in YAML.

- **EXPR-004**  
  **File path:** `.github/workflows/implement.yml:3161-3540`  
  **Severity:** Medium  
  **Category tag:** `expression-limit`  
  **Estimated current expression size:** ~17,460 chars  
  **Headroom remaining:** ~3,540 chars  
  **Description:** `Commit changes` bundles stderr capture, commit/no-op branching, remaining-change reporting, and destructive-commit plumbing into one interpolated `run:` block. It is not failing yet, but it is already in the zone where a modest refactor or extra guard can push it over the cap.  
  **Recommended fix:** Move the body into `scripts/implement_commit_changes.sh`, or split the step into smaller commit, no-op, and failure-capture steps.

### Section 5: Cross-Cutting Concerns

- **CONSIST-001**  
  **File path:** `scripts/review_run_reviewers.sh:3134-3161`  
  **Severity:** Medium  
  **Category tag:** `consistency`  
  **Description:** The script comments and fallback assignments say both pass-2 branches default to `xhigh`, and the code sets `PASS2_REASONING_SMALL="${REVIEWER_PASS2_REASONING_SMALL:-xhigh}"`. That diverges from `.github/workflows/review_autofix.yml:116-118`, `README.md:91`, and `agents.md:63-64`, which all define small diffs as `high` and large diffs as `xhigh`. The workflow currently masks this by exporting the env vars, but any script-only caller or future workflow that omits them will silently run small diffs at `xhigh`.  
  **Recommended fix:** Align the script fallback and nearby comments with the documented contract (`small=high`, `large=xhigh`), or explicitly document a separate standalone-script default if the divergence is intentional.

- **DEBT-001**  
  **File path:** `.github/workflows/orchestrate_poll.yml:7-20; .github/workflows/workflow-log-analysis.yml:16-20; .github/workflows/comprehensive-test-and-release.yml:151-159; scripts/validation_refresh_runner.py:779-787; scripts/analyze_workflow_logs.py:112-119`  
  **Severity:** Low  
  **Category tag:** `tech-debt`  
  **Description:** Several backward-compatibility surfaces are now effectively no-op inside this repo: `caller_workflow` is declared deprecated in `orchestrate_poll.yml` and has no in-repo caller; `codex_mode` is declared deprecated in both `workflow-log-analysis.yml` and `scripts/analyze_workflow_logs.py`, but `comprehensive-test-and-release.yml` still passes `codex_mode=true`; `validation_refresh_runner.py` keeps deprecated `--commit-message` and `--pr-title` flags with no in-repo callers. These dead surfaces add interface noise and make future cleanup harder.  
  **Recommended fix:** Remove the remaining internal `codex_mode=true` call first, then prune the unused deprecated inputs/flags after a documented grace window. If external compatibility must remain, keep a single shim layer instead of threading deprecated options through the core workflows/scripts.

_No `TODO` / `FIXME` / `HACK` markers were present under `.github/workflows` or `scripts/`, and no additional high-signal shellcheck defects stood out beyond SEC-001._

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BUG-001, EXPR-001, EXPR-002 |
| Medium | 6 | SEC-001, API-001, DUP-001, EXPR-003, EXPR-004, CONSIST-001 |
| Low | 3 | API-002, DUP-002, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 1 script | Small |
| API call optimization | 2 files + 1 helper | Medium |
| Code modularization | 8 workflows + 1 shared helper | Large |
| Expression size reduction | 3 workflows + 2 extracted scripts/helpers | Large |
| Medium/Low fixes | 7 files + docs cleanup | Medium |
