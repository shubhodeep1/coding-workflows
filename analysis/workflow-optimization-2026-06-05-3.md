## Executive Summary

- **Implement is the dominant cost lever.** `implement` used **6,637,037 / 6,653,251 observed Codex tokens (99.8%)** and produced the longest AI critical paths: run `27000656054` (**1,882s**, **1.324M** tokens), `27011512893` (**1,864s**, **2.665M** tokens), and `27011358732` (**823s**, **2.648M** tokens). **Estimated impact:** 15-30% lower AI spend and 2-6 minutes off long implement runs if first-pass reasoning is tiered. **Confidence:** high.
- **One review/autofix failure mode is slow and very fixable.** Run `27008848028` failed after **1,752s** with **96** repeated AI-memory worktree collisions, `parse_failed=1`, two merge failures, “No clean resolver entry-point available,” and **3** futile resolver retries. **Estimated impact:** 5-10 minutes less failure-tail latency and lower rerun risk. **Confidence:** high.
- **CI has a repeated contract-drift failure, not random flakiness.** Runs `26995064186`, `26999966118`, and `27009821039` all failed in `lint / Orchestrate lib unit tests` on `REVIEW_RUN_MAX_RUNTIME_MINUTES: unbound variable`; run `27009821039` also flagged a missing timeout wrapper in `.github/workflows/implement.yml`. **Estimated impact:** eliminate at least 3 recurring CI failures (8.1 points of the CI family). **Confidence:** high.
- **Support-source checkout is a real startup tax.** Successful `clarify` runs `27010850906` (**85s**) and `27010850951` (**94s**) were dominated by `Checkout workflow support source`; `orchestrate_clarify_respond` run `27011470894` (**159s**) also spent most of its runtime in checkout. **Estimated impact:** ~60-90s off successful clarify/respond runs. **Confidence:** high.
- **A GH API polling hotspot is easy to trim.** In `test_and_mark_stable` run `26994091117`, step `Dispatch & watch — validation-refresh` polled `actions/runs/{id}` **111 times** before child run `26994110328` failed. **Estimated impact:** ~60%+ fewer GH API calls for long watch loops with no behavior change. **Confidence:** high.
- **The window is sufficient overall, but validate evidence is incomplete.** `validate` failed **15/16** times, and at least **11** runs reported `partial_data:missing_log_archive ... HTTP 404`; sampled validate deep-dive folders contained only `metadata.json`. **Estimated impact:** better diagnosis quality, not proven runtime reliability change, if fallback collection is improved. **Confidence:** high.

## Speed Optimizations

1. **Critical-path: tier implement reasoning/model usage**
   - **Evidence:** `implement` accounts for the longest AI runs and nearly all observed token spend. Long successful outliers: `27000656054` (**1,882s**), `27011512893` (**1,864s**), `27011358732` (**823s**). In deep-dive run `27000656054`, `implement/implement` exported `MODEL_EDITOR: openai/gpt-5.4` and `MODEL_REASONING_EFFORT: xhigh`.
   - **Root cause:** first-pass implement work is using a high-latency/high-cost model profile even though successful implement runtimes vary widely (**274s** to **1,882s**).
   - **Exact change:** start `implement` with a cheaper first pass for small/medium diffs (for example `high` instead of `xhigh`, or keep `gpt-5.4` for repair only and use a smaller/cheaper diagnose/scoping pass), then auto-escalate only after validation failure, large diff size, or merge conflict.
   - **Estimated time savings:** **2-6 minutes** on long implement runs.  
   - **Implementation risk:** **medium**; keep auto-escalation to preserve quality.

2. **Critical-path: fail fast when review/autofix conflict resolution is impossible**
   - **Evidence:** failing `review_autofix` run `27008848028` logged `parse_failed=1`, then two `Automatic merge failed` events, then `Resolver entry-point check ... No clean resolver entry-point available`, then **3** resolver retries, then final exit 1.
   - **Root cause:** the failure path retries conflict resolution after preconditions are already known to be broken.
   - **Exact change:** if parser output shows `parse_failed=1` or resolver-entry-point validation fails, skip resolver retries and immediately fall back to the manual-resolution/comment path.
   - **Estimated time savings:** **5-10 minutes** on conflict-failure tails.
   - **Implementation risk:** **low**; this only changes already-failing paths.

3. **Critical-path: remove duplicate workflow-support repo checkouts**
   - **Evidence:** `clarify` successes `27010850906` (**85s**) and `27010850951` (**94s**) were dominated by support checkout. `orchestrate_clarify_respond` success `27011470894` (**159s**) also spent most of its time on checkout. Workflow YAML shows primary checkout, fallback checkout, and a `main` snapshot checkout in:
     - `.github/workflows/plan.yml:215-263`
     - `.github/workflows/clarify.yml:164-212`
     - `.github/workflows/orchestrate.yml:156-197`
   - **Root cause:** cold-start jobs do up to two remote repo checkouts plus a snapshot checkout just to stage support scripts.
   - **Exact change:** when `github.repository == shubhodeep1/coding-workflows`, use the current workspace instead of remote checkout; otherwise use sparse checkout for only required support files, and fetch `.codex-workflow-src-main` only if a required file is missing.
   - **Estimated time savings:** **~60-90s** on successful clarify/respond runs; smaller but repeatable savings elsewhere.
   - **Implementation risk:** **low-medium**; preserve current fallback logic.

4. **Micro-optimization / queue relief: stop dispatching child workflows that immediately skip**
   - **Evidence:** high skip/other counts:
     - `clarify`: **178/186**
     - `plan`: **166/176**
     - `implement`: **165/176**
     - `orchestrate_clarify_respond`: **174/176**
     Most of these complete in **1-2s**, but heavy runs like `27010886418`, `27010884752`, `27012380547`, and `27011470894` explicitly waited for hosted runners.
   - **Root cause:** many conditions are evaluated inside child reusable workflows instead of before dispatch.
   - **Exact change:** move branch/comment/materiality gating into the parent orchestrator so only value-bearing child workflows are called.
   - **Estimated time savings:** direct runner-time savings are small (these skipped runs total only ~**873s** across the four families), but it should reduce queue contention and Checks noise.
   - **Implementation risk:** **low**.

## Cost Optimizations

1. **Tier implement reasoning first; that is the only cost change with clearly large upside**
   - **Evidence:** `implement` consumed **6,637,037 / 6,653,251** observed Codex tokens (**99.8%**). Long token-heavy runs were `27000656054` (**1.324M** tokens), `27011512893` (**2.665M**), and `27011358732` (**2.648M**).
   - **Root cause:** the most expensive family is using `gpt-5.4` + `xhigh` on paths that are not always equally complex.
   - **Exact change:** complexity-gate the first implement pass; reserve `xhigh` for retries, large diffs, or failed validation.
   - **Estimated savings:** **~1.0M-2.0M tokens per 1,000-run window** (15-30% of observed implement tokens; inference).
   - **Quality-risk notes:** **medium**; mitigate with escalation-on-failure.

2. **Cut waste from cancelled review/autofix runs**
   - **Evidence:** `review_autofix` had **19 cancelled** runs consuming **40,043s** total (~**11.1h**), with median cancelled duration **770s** and max `26989826766` at **24,665s**.
   - **Root cause:** stale/superseded review runs are living too long before cancellation.
   - **Exact change:** re-check PR head SHA before reviewer/editor/conflict-resolution phases and terminate stale runs earlier via existing concurrency controls.
   - **Estimated savings:** up to **11.1h** of runner time per similar window; token savings are unquantified because review telemetry under-reports model usage.
   - **Quality-risk notes:** **low**.

3. **Keep high-signal Semble queries; cap low-value overflow queries**
   - **Evidence:** repo aggregate telemetry shows **36** Semble queries and **320,030** query bytes; `review_autofix` alone accounts for **26** queries and **237,908** bytes (**74.3%**). Deep-dive actual telemetry shows:
     - `reviewer-context`: **5** queries, **73,815** bytes
     - `overflow`: **6** queries, **39,488** bytes
     - `conflict-resolver-context`: **1** query, **9,375** bytes  
     In failure run `27008848028`, overflow queries were **5/7** calls and **33,032/57,307** bytes, all near a parser/conflict tail.
   - **Root cause:** reviewer-context appears useful, but overflow lookup continues even after the run is already failing structurally.
   - **Exact change:** preserve `reviewer-context`, but stop `overflow` queries after `parse_failed=1`, after resolver-entry failure, or after a small per-run overflow budget.
   - **Estimated savings:** the example failure would save **33KB** and ~**2.5s** of Semble latency; repo-wide savings are smaller but low-risk.
   - **Quality-risk notes:** **low** if reviewer-context is retained.

4. **Fix telemetry blind spots before doing prompt-cache or reviewer-model tuning**
   - **Evidence:** repo `cache_hit_rate` is **null**, all `or_*` prompt-cache fields are **0**, Serena telemetry is all **0**, yet `review_autofix` run `27011075501` still lasted **1,877s** with only **2** Codex calls / **4,052** tokens recorded.
   - **Root cause:** prompt-cache and some reviewer-path model spend are not currently measurable.
   - **Exact change:** emit reviewer-panel token usage and real prompt-cache stats alongside existing Codex telemetry.
   - **Estimated savings:** not directly quantifiable yet; this is an enabling fix.
   - **Quality-risk notes:** **none**.

**Semble / Serena readout**
- **Semble is helping in some places.** Deep-dive `reviewer-context` queries are likely replacing larger prompt expansion.
- **Semble is noisy in conflict tails.** The overflow pattern in `27008848028` added bytes and latency after the run was already unrecoverable.
- **Serena is not currently reducing work.** No `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines were observed; aggregate Serena counts are all **0**, and `SERENA_ENABLED: false` was observed in `27009821188` and `27000656054`.

## Reliability Improvements

1. **Fix CI contract drift around `poller_stall.sh` and implement timeout expectations**
   - **Failure evidence:** CI runs `26995064186`, `26999966118`, and `27009821039` failed in `lint / Orchestrate lib unit tests`. All three logged `REVIEW_RUN_MAX_RUNTIME_MINUTES: unbound variable`; `27009821039` also logged `missing 'timeout --signal=TERM --kill-after=5s' in .github/workflows/implement.yml`.
   - **Root cause category:** workflow-contract drift / strict-shell env dependency.
   - **Exact fix:** give `poller_stall.sh` a default for `REVIEW_RUN_MAX_RUNTIME_MINUTES` (or export it everywhere it is sourced), and keep the timeout wrapper contract in `implement.yml`.
   - **Expected reliability impact:** removes at least **3** repeated CI failures (**3/37**, 8.1 points of the CI family).
   - **Rollback / fail-open:** keep an explicit warning when the default is used so silent misconfiguration is still visible.

2. **Repair AI-memory workspace collisions on review/autofix**
   - **Failure evidence:** `review_autofix` run `27008848028` produced **94** `AI_MEMORY_ERROR` lines and **96** repeated `working tree ... already exists` collisions. Its memory retrieve was disabled (`enabled: false`, `warning: git_error`), with **2** `record-run-event` fail-open warnings and **1** `record-candidate` fail-open warning.
   - **Root cause category:** stale workspace/worktree collision in the memory clone path.
   - **Exact fix:** make the memory worktree path unique per phase/attempt or prune the existing worktree before clone; after the first collision in a phase, stop retrying memory writes for that phase.
   - **Expected reliability impact:** reduces one observed `review_autofix` failure amplifier and restores reviewer-path memory availability.
   - **Rollback / fail-open:** retain current fail-open behavior if cleanup itself fails.

3. **Fail fast when the conflict resolver has no clean entry point**
   - **Failure evidence:** in run `27008848028`, the resolver logged `No clean resolver entry-point available`, then burned **3** immediate retries and failed.
   - **Root cause category:** retry policy ignores impossible preconditions.
   - **Exact fix:** turn “no clean resolver entry point” into an immediate terminal/manual-resolution branch instead of retrying.
   - **Expected reliability impact:** lowers failure-tail time and reduces duplicate retry churn on merge-conflict cases.
   - **Rollback / fail-open:** keep current retry mode behind a temporary flag if comparison is needed.

4. **Close the validate observability gap before attributing validate failures to workflow logic**
   - **Failure evidence:** `validate` failed **15/16** times. At least **11** runs explicitly logged `partial_data:missing_log_archive ... HTTP 404`, and sampled deep-dive validate folders contained only `metadata.json` with **0s** durations and no step logs.
   - **Root cause category:** collector/archive availability gap.
   - **Exact fix:** capture validate job/step metadata earlier and persist a tiny step-summary artifact, or fall back to jobs/steps API data whenever log archive fetches 404.
   - **Expected reliability impact:** improves diagnosis and reduces blind reruns; runtime failure impact is currently unknown.
   - **Rollback / fail-open:** current `partial_data` soft-fail behavior is appropriate and should stay.

**Pressure / fallback signals**
- **`BREAK_GLASS`**: **0** repo-wide.
- **`CONTEXT_BUDGET_WARN`**: **0** repo-wide. `CONTEXT_BUDGET_WARN_RATIO: 0.7` appeared in runs `27011075501` and `27012380547`, but that is configuration, not an emitted warning.
- **Semble fail-open behavior looks healthy, not broken.** All **5** observed `SEMBLE_FALLBACK` events came from `test_and_mark_stable` run `26994091117`, step `validate-scripts`, target `overflow`, `ms=0`, with missing temp paths; this is test-harness fail-open behavior.
- **Serena rollout looks disabled, not masked-broken.** Observed Serena query/fallback/probe counts are all **0**.

## AI Memory Health

- **Telemetry presence:** **31** `AI_MEMORY_TELEMETRY` entries were observed, only in `implement` and `review_autofix`.
- **Retrieve hit rate:** **1/7 = 14.3%**.
- **Average `estimated_tokens`:** **4.7**. No explicit memory-budget field was emitted, so budget comparison is unavailable.
- **`keyword_method` distribution:** `llm=5`, `plain=1`, `none/missing=1`.
- **Zero-record retrieves:** **6/7**.
- **Disabled retrieves:** **1/7** (`27008848028`, `enabled=false`, `warning=git_error`).
- **Retrieve `fail_open: true`:** **0**.
- **Other fail-open memory events:** **3** total (`record-run-event` x2, `record-candidate` x1), all in failing run `27008848028`.
- **High push retry counts:** none observed; max `push_retry_count` was **0**.

**Role split**
- **Reviewer path:** **0/5** retrieves returned records (`27007965773`, `26989826766`, `27007113338`, `27004013381`, `27001860839`).
- **Implementation path:** **1/1** retrieve returned records (`27000656054`, `records_selected=1`, `estimated_tokens=28`, `keyword_method=plain`).

**Assessment**
- AI memory is **working better on implementation** than on reviewer paths.
- Reviewer retrieval is currently low-yield and fragile: every enabled reviewer retrieve returned **0** records, and the one failing reviewer run disabled retrieval entirely due to git/worktree issues.
- The smallest safe improvement is:
  1. fix the worktree collision first,
  2. skip reviewer retrieval when git health is bad,
  3. keep implementation retrieval enabled.

## GH API Call Audit

1. **Good existing hygiene in `review_gate`; extend it downstream**
   - **Evidence:** `review_autofix` run `27012380547`, step `review_gate`, explicitly reuses `/pulls/{n}` for state, merged status, head ref, labels, additions, and deletions; it only falls back to paginated `/files` when needed. The same step propagates `head_ref` specifically to avoid repeating `/pulls/{n}` later. Actual output confirmed a material PR evaluation: `AUTOFIX_GATE_DET_SKIP_EVAL pr=3095 files=10 additions=26 ...`.
   - **Audit finding:** this is good API hygiene and aligns with the repo’s own `CLAUDE.md §15` comment in the step.
   - **Recommendation:** also pass `file_count`, `additions`, `deletions`, and a compact file-summary output to downstream editor/merge steps.
   - **Estimated reduction:** **1-2 GH API calls per non-skipped review_autofix run** (~**75-150** calls per similar window; inference).
   - **Rate-limit reduction:** low-moderate.

2. **The biggest observed GH API hotspot is fixed-interval run watching**
   - **Evidence:** in `test_and_mark_stable` run `26994091117`, step `Dispatch & watch — validation-refresh`, the watcher printed **111** `status=... conclusion=...` lines before child run `26994110328` failed. The workflow source at `.github/workflows/test-and-mark-stable.yml:3673-3723` shows a tight `gh api "repos/.../actions/runs/${NEW_ID}"` polling loop, and the file contains this same watcher pattern **6** times.
   - **Audit finding:** this is an unbatched per-item polling loop and the clearest GH API hotspot in the current window.
   - **Recommendation:** extract a shared watcher with adaptive backoff (for example 15s for the first 2 minutes, then 30s, then 60s) or switch to `gh run watch` behavior while keeping explicit terminal-state checks.
   - **Estimated reduction:** about **69 fewer status calls** in the observed long watch (`111` down to roughly `42`; inference), and potentially hundreds across the whole release-test workflow.
   - **Rate-limit reduction:** high.

3. **Collector-side archive retries are already sane; the remaining issue is coverage**
   - **Evidence:** current-window operations saw **11** explicit `partial_data:missing_log_archive` failures on `/actions/runs/{id}/logs` for `validate`. The test suite (`tests/test_collect_workflow_logs.py`) already verifies missing-archive 404s are classified as soft-fail and cached after a single retry path.
   - **Audit finding:** this is **not** a retry storm problem; it is an archive-availability/collection-timing problem.
   - **Recommendation:** do not add more retries. Instead, prefetch jobs/steps metadata before log fetch, or collect validate artifacts earlier.
   - **Estimated reduction:** little direct call-count reduction, but materially better coverage with lower rate-limit exposure than repeated archive retries.

**Confirmed GH API rate-limit events**
- None were confirmed in the current deep-dive logs.

## Prompt Cache & Memory System

- **Prompt-cache telemetry is effectively absent.**
  - Repo `cache_hit_rate` = **null**.
  - All `or_*` fields (`or_prompt_tokens`, `or_total_tokens`, `or_cache_write_tokens`, `or_cache_read_tokens`, `or_calls`) = **0** across the full 1,000-run window.
  - I cannot verify prompt-cache hit/miss behavior from this window.

- **Do not confuse this with Actions cache.**
  - Actions cache hits were visible in run summaries such as `27010886418`, `27010884752`, `27011358732`, `27011512893`, `27011470894`, and `27010755120` (`setup-uv ... not saving cache`).
  - So infrastructure caching is working; prompt-cache observability is the blind spot.

- **Likely cache-fragmentation causes (inference, not directly measured):**
  - dynamic branch/commit fields in plan logs (`HEAD branch`, `HEAD commit` in `27010884752`),
  - multi-part orchestrator state blobs in skipped respond runs (`ORCHESTRATOR_STATE_V2 part=...` in `27010851935` / `27010852936`),
  - per-run hashes, paths, and PR-specific file lists.
  - These are exactly the kinds of unstable prefixes that erode prompt-cache reuse.

- **Concrete improvement:**
  - keep stable instructions, repo policy, and tool contract text at the prompt prefix;
  - append run IDs, commit SHAs, `ORCHESTRATOR_STATE_V2`, and volatile PR/file metadata at the end;
  - then expose real cache-hit telemetry so the effect is measurable.

- **Memory retrieval effectiveness is asymmetric.**
  - Reviewer path: poor hit rate (**0/5**).
  - Implement path: useful in the one observed retrieve (**1/1**, run `27000656054`).
  - Recommendation: keep implementation retrieval on; make reviewer retrieval conditional on clean git state and likely-memory candidates.

- **Prompt-size pressure does not currently show as emitted warnings.**
  - `CONTEXT_BUDGET_WARN` count is **0** repo-wide.
  - `CONTEXT_BUDGET_WARN_RATIO: 0.7` is present as configuration in some logs, but no actual warning events were emitted.

## Orchestrator Health

- **Most child workflow fan-out is conditionally skipped, not stuck.**
  - `clarify`: **178/186** other/skipped
  - `plan`: **166/176**
  - `implement`: **165/176**
  - `orchestrate_clarify_respond`: **174/176**
  - This looks more like control-plane noise than a broken loop.

- **The real orchestrator pain point is late cancellation, not clarify-loop churn.**
  - `review_autofix` had **19 cancelled** runs totaling **40,043s**.
  - Median cancelled duration was **770s**; max was `26989826766` at **24,665s**.
  - That is where early supersession checks will matter most.

- **No evidence of policy/rubric pressure.**
  - `break_glass_count = 0`
  - `context_budget_warn_count = 0`

- **No evidence of a live Serena rollout problem.**
  - Serena runtime telemetry was absent; this looks disabled, not half-broken.

- **Smallest safe mitigations**
  1. pre-dispatch more conditions in the parent orchestrator,
  2. re-check head SHA before long review/editor phases,
  3. alert on long-lived cancelled `review_autofix` runs,
  4. track validate archive-miss rate separately from workflow failure rate.

- **Indicators to track**
  - skipped child-workflow ratio by family,
  - cancelled `review_autofix` median/max duration,
  - conflict-resolver retry count per run,
  - `AI_MEMORY_ERROR` count,
  - validate missing-archive rate,
  - hosted-runner wait mentions.

## Pipeline Flow Bottlenecks

1. **Clarify → startup bottleneck**
   - Successful `clarify` runs ranged from **85s** to **213s**.
   - `27010850906` (**85s**) and `27010850951` (**94s**) were dominated by support-source checkout.
   - Bottleneck type: **startup / checkout overhead**.

2. **Plan → bifurcated compute + queue bottleneck**
   - Successful plan runs ranged from **14s** to **986s**.
   - Outliers `27010884752` (**689s**) and `27010886418` (**902s**) used only **6 Codex calls / 4,054 tokens** each, yet still took a long time and explicitly waited for hosted runners.
   - Bottleneck type: **queueing + high-latency reasoning**.

3. **Implement → main compute bottleneck**
   - Successful implement runs ranged from **274s** to **1,882s**.
   - The long outliers are also the token outliers and dominate spend.
   - Bottleneck type: **AI compute / prompt expansion / repair loops**.

4. **Review/autofix → merge/conflict + cancellation bottleneck**
   - Successful `review_autofix` runs have **p50 477s** and **success-only p95 ~2,132s**.
   - One failure (`27008848028`) shows conflict/parser tail waste.
   - Cancelled runs consumed **11.1h** total.
   - Bottleneck type: **merge/conflict overhead + stale run churn**.

5. **Validate / validation_refresh → visibility bottleneck with long downstream waits**
   - `validation_refresh` failed in both observed runs (`26993599946`, `26994110328`) after roughly **25-27 minutes**.
   - `test_and_mark_stable` run `26994091117` spent ~**27 minutes** watching child run `26994110328` before failing.
   - `validate` itself is mostly blind in this window because log archives are missing.
   - Bottleneck type: **watch-loop overhead + missing diagnostics**.

6. **CI / external review tail**
   - Successful `ci` runs are consistently long: **p50 1,470s**, **p95 1,537s**.
   - `copilot_pull_request_reviewer` adds another **175s p50 / 416s p95**.
   - Current CI deep dive does not include a representative successful full-log breakdown, so I would profile before recommending job sharding.
   - Bottleneck type: **downstream verification / external review latency**.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `implement` critical-path AI runtime and token spend (`27000656054`, `27011512893`, `27011358732`)
- `review_autofix` cancellation churn and conflict tails (`27008848028`, `26989826766`)
- support-source checkout startup tax in `clarify` / `orchestrate_clarify_respond`
- long stable-path verification (`ci`, `validation_refresh`, `copilot_pull_request_reviewer`)

**Top failure modes**
- repeated CI contract drift around `REVIEW_RUN_MAX_RUNTIME_MINUTES`
- review/autofix memory worktree collision + futile resolver retries
- validate archive 404s causing observability loss

**Highest-cost drivers**
- `implement`: **99.8%** of observed Codex tokens
- `review_autofix`: **74.3%** of observed Semble bytes
- cancelled `review_autofix` runs: **40,043s** of wasted runner time

**Top 3 prioritized actions**
1. **Tier implement reasoning/model effort** and auto-escalate only on failed validation or large diffs.
2. **Fix review/autofix failure path**: unique/pruned AI-memory worktree + immediate exit when resolver entry point is dirty.
3. **Reduce startup/control-plane waste**: dedupe support-source checkout and replace fixed-interval watch polling with adaptive backoff.

## Metrics Appendix

### Window summary

| Repository | Runs | Success | Failure | Cancelled | Other | Success rate | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 266 | 25 | 21 | 688 | 26.6% | 2.5% | 172.4 | 1 | 1,379 |

### Key workflow-family metrics

| Workflow family | Total | Success | Failure | Cancelled | Other | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| clarify | 186 | 8 | 0 | 0 | 178 | 1.0 | 9.8 |
| plan | 176 | 10 | 0 | 0 | 166 | 1.0 | 14.3 |
| implement | 176 | 9 | 0 | 2 | 165 | 1.0 | 79.0 |
| review_autofix | 99 | 75 | 1 | 19 | 4 | 521 | 2,662.2 |
| ci | 37 | 32 | 5 | 0 | 0 | 1,456 | 1,537.2 |
| validate | 16 | 1 | 15 | 0 | 0 | 0.0 | 39.2 |
| orchestrate_clarify_respond | 176 | 2 | 0 | 0 | 174 | 1.0 | 2.0 |
| orchestrate_poll | 8 | 8 | 0 | 0 | 0 | 187.0 | 1,115.4 |
| copilot_pull_request_reviewer | 22 | 22 | 0 | 0 | 0 | 175.0 | 415.8 |
| validation_refresh | 2 | 0 | 2 | 0 | 0 | 1,575 | 1,611.9 |
| test_and_mark_stable | 1 | 0 | 1 | 0 | 0 | 1,995 | 1,995 |

### Repo-level cost, cache, and wall-clock telemetry

| Scope | Runs with log telemetry | Codex calls | Codex tokens | cache_hit_rate | OR calls | OR cache read/write tokens | wall_clock_p50_ms | wall_clock_p99_ms | break_glass_count | context_budget_warn_count |
|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|
| Repo total | 111 | 92 | 6,653,251 | n/a (null) | 0 | 0 / 0 | 2,000 | 4,141,380 | 0 | 0 |

### Family cost / Semble telemetry

| Workflow family | Codex calls | Codex tokens | Semble query calls | Semble query bytes | Semble fallbacks |
|---|---:|---:|---:|---:|---:|
| implement | 72 | 6,637,037 | 10 | 82,122 | 0 |
| review_autofix | 2 | 4,052 | 26 | 237,908 | 0 |
| plan | 12 | 8,108 | 0 | 0 | 0 |
| orchestrate_clarify_respond | 6 | 4,054 | 0 | 0 | 0 |
| test_and_mark_stable | 0 | 0 | 0 | 0 | 5 |

### AI memory telemetry

| Metric | Value |
|---|---:|
| Total AI memory telemetry entries | 31 |
| `retrieve` entries | 7 |
| Retrieve hit rate | 14.3% (1/7) |
| Avg `estimated_tokens` on retrieve | 4.7 |
| `keyword_method` distribution | `llm=5`, `plain=1`, `none=1` |
| Zero-record retrieves | 6 |
| Disabled retrieves | 1 |
| `fail_open=true` retrieves | 0 |
| `fail_open=true` total entries | 3 |
| Max `push_retry_count` | 0 |
| Reviewer retrieve hits | 0/5 |
| Implementation retrieve hits | 1/1 |

### GH API call summaries

| Workflow / job / step | Observed pattern | Approx current calls seen | Recommendation |
|---|---|---:|---|
| `review_autofix` / `review_gate` / run `27012380547` | Reuses `/pulls/{n}` for state, labels, additions, deletions; `/files` only when needed | Efficient; likely ~2 core calls on this material PR | Pass more gate outputs downstream to avoid later refetches |
| `test_and_mark_stable` / `orphan-workflows-test` / `Dispatch & watch — validation-refresh` / run `26994091117` | Fixed 15s polling of `/actions/runs/{id}` until completion | 111 status polls + list lookups | Replace with shared adaptive-backoff watcher |
| Collector archive fetch / validate runs | `/actions/runs/{id}/logs` returned 404 | 11 explicit failed archive fetches | Do earlier jobs/steps fallback collection instead of more retries |

**Confirmed GH API rate-limit events:** none observed.

### Semble / Serena / MCP telemetry

| Server | Query calls | Logged bytes | Fallbacks | Response bytes | Tool calls | Probe OK | Probe failed | Probe skipped |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Semble | 36 | 320,030 | 5 | n/a | n/a | n/a | n/a | n/a |
| Serena | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

### Semble target breakdown from actual deep-dive log lines

| Semble target | Query count | Logged bytes | Avg ms/query | Fallback count |
|---|---:|---:|---:|---:|
| reviewer-context | 5 | 73,815 | 506.6 | 0 |
| overflow | 6 | 39,488 | 490.5 | 5 |
| conflict-resolver-context | 1 | 9,375 | 499.0 | 0 |

### Per-target MCP availability

| MCP target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---:|---:|---:|---|
| Serena | 0 | 0 | 0 | No runtime probe/query telemetry observed; rollout appears disabled |
| Other MCP servers observed | 0 | 0 | 0 | None confirmed in first-class telemetry |
