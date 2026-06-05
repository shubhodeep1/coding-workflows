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

## Deep Audit — Workflows & Scripts (2026-06-05)

### Section 1: Bug & Correctness Sweep

- **ID:** `BUG-001`  
  **File:** `.github/workflows/review_autofix.yml:775-777, 5216-5228, 5397-5411, 6386-6400` (contrast: `.github/workflows/issue_pr_status.yml:272-287`, `.github/workflows/review_autofix.yml:2669-2682`)  
  **Severity:** High  
  **Category:** `bug`  
  **Description:** `review_autofix.yml` still treats bare `issues/<n>` and `issue #<n>` prose as linked issues in four fallback paths. `issue_pr_status.yml:272-287` explicitly forbids those forms because they caused incorrect issue transitions, and `review_autofix.yml:2669-2682` already uses the stricter closing-keyword pattern for smoke-test detection. In review_autofix, the looser regex feeds post-merge validation dispatch (`.github/workflows/review_autofix.yml:787-807`) and the linked-issue label mutations in the `ready-to-merge` / `review-blocked` steps, so incidental PR-body references can dispatch validation or relabel the wrong issue.  
  **Recommended fix:** Extract one shared helper, e.g. `resolve_linked_issue_numbers_from_pr_text <repo> <text>`, into `scripts/gh_helpers.sh` and reuse the stricter `close/fix/resolve + #N or repo-scoped issue URL` pattern already present in `issue_pr_status.yml:272-287` / `review_autofix.yml:2669-2682` at all four review_autofix call sites.

- **ID:** `CONSIST-001`  
  **File:** `.github/workflows/implement.yml:839-842, 2478-2480, 3254-3256`  
  **Severity:** Medium  
  **Category:** `consistency`  
  **Description:** These guards treat any repo matching `*/coding-workflows` as the canonical workflow-source repo. Earlier in the same workflow, exact-match guards at `.github/workflows/implement.yml:206-209` and `780-784` already use `wf_source="shubhodeep1/coding-workflows"`. The suffix-based checks therefore give forks or mirrors named `*/coding-workflows` self-repo behavior: local support assets are trusted as source-of-truth and consumer-repo commit exclusions are dropped.  
  **Recommended fix:** Replace all three wildcard checks with the same exact `wf_source` equality test already used earlier in `implement.yml` and in `.github/workflows/validate.yml:218-235` / `.github/workflows/review_autofix.yml:1238-1242`, so every self-repo decision shares one source-of-truth.

No additional high-confidence secret-leak, quoting, or `set -e` defects rose above report threshold beyond the items above and the failure-path issues already captured in the parent report.

### Section 2: GitHub API Call Redundancy Audit

_Excluding the fixed-interval `actions/runs/{id}` polling hotspot already covered in the parent report, the remaining static GH API candidates are below._

- **ID:** `BATCH-001`  
  **File:** `.github/workflows/review_autofix.yml:1916-1922; scripts/gh_helpers.sh:734-900`  
  **Severity:** Medium  
  **Category:** `api-batching`  
  **Description:** `Collect PR metadata` hydrates PR context with four separate fetches: PR payload, issue comments, reviews, and review comments. Two are paginated, so the underlying REST request count can exceed four. `scripts/gh_helpers.sh` already has a GraphQL-first consolidated batching pattern in `gh_pr_with_all_comments`. **Current call count:** 4 logical fetches per normal PR path. **Proposed call count:** 1 logical helper call on the fast path, with REST fallback only when GraphQL/pagination parity requires it.  
  **Recommended fix:** Extend `gh_pr_with_all_comments` to also emit top-level review state/body metadata, then use that helper here to materialize `PR_META_FILE`, `PR_ISSUE_COMMENTS_FILE`, `PR_REVIEWS_FILE`, and `PR_REVIEW_COMMENTS_FILE`. Existing batching pattern to extend: `scripts/gh_helpers.sh:734-900`.

- **ID:** `BATCH-002`  
  **File:** `.github/workflows/review_autofix.yml:1946-1951, 2017-2032`  
  **Severity:** Medium  
  **Category:** `api-batching`  
  **Description:** When `closingIssuesReferences` returns `[]`, review_autofix regex-parses fallback issue numbers and then fetches each fallback issue body with `gh api repos/{repo}/issues/{n}` inside a loop. The path is capped at 20 issues. **Current call count:** 1 initial GraphQL PR query + up to 20 REST issue fetches (21 total). **Proposed call count:** 2 total with the current cap (1 PR GraphQL query + 1 batched issue GraphQL query).  
  **Recommended fix:** Batch the fallback issue hydration with an aliased issue query, reusing the pattern from `_fetch_candidate_issue_details_graphql`. Existing batching pattern to extend: `scripts/orchestrate_poll_process.sh:10174-10297`.

- **ID:** `BATCH-003`  
  **File:** `.github/workflows/review_autofix.yml:767-784, 789-799`  
  **Severity:** Medium  
  **Category:** `api-batching`  
  **Description:** The post-merge validate-dispatch step already does one GraphQL `closingIssuesReferences` lookup, but on the regex-fallback path it stores `labels: null` and then resolves labels with `gh issue view` inside the per-issue loop. The inline comment at `.github/workflows/review_autofix.yml:2035` says the empty-GraphQL path is expected for non-default-base orchestrator PRs, so this loop is not rare. **Current call count:** 1 GraphQL query + N per-issue label lookups. **Proposed call count:** 2 total (the existing PR query + 1 batched issue-label query).  
  **Recommended fix:** After regex fallback, batch-fetch `{number, labels}` for all candidate issues and feed the loop from local JSON instead of `gh issue view` per issue. Existing batching pattern to extend: `scripts/orchestrate_poll_process.sh:10174-10297`.

### Section 3: Code Duplication & Modularization Opportunities

- **ID:** `DUP-001`  
  **File:** `scripts/gh_helpers.sh:26-126, 391-446; .github/workflows/review_autofix.yml:1834-1872; .github/workflows/cancel_on_pr_close.yml:26-50; .github/workflows/mark-stable.yml:383-410, 546-559; .github/workflows/orchestrate_poll.yml:95-113`  
  **Severity:** Medium  
  **Category:** `duplication`  
  **Description:** The repo already has a canonical GH retry layer in `scripts/gh_helpers.sh`, but a repository-wide search still finds 57 `gh_retry`/`_gh_retry` definitions or stubs. The inline copies above drift from the canonical helper by omitting `_is_gh_permanent_failure`, `_gh_actions_escape`, and the shared rate-limit wait/breaker behavior. That makes retry semantics inconsistent and forces every future GH-layer fix to be patched in many places.  
  **Recommended fix:** Keep `scripts/gh_helpers.sh` as the single owner of GH retry behavior (`gh_retry "$@"`, `gh_retry_to_file <outfile> "$@"`, `_safe_gh_jq ...`). Update callers such as `cancel_on_pr_close.yml`, `mark-stable.yml`, `orchestrate_poll.yml`, `review_autofix.yml`, `scripts/review_apply_fixes.sh`, `scripts/review_run_reviewers.sh`, `scripts/review_rb_judge.sh`, and `scripts/validate_process.sh` to source it instead of redefining wrappers.

- **ID:** `DUP-002`  
  **File:** `.github/workflows/validate.yml:212-636; .github/workflows/review_autofix.yml:1228-1554; .github/workflows/implement.yml:829-1058`  
  **Severity:** Medium  
  **Category:** `duplication`  
  **Description:** These three large blocks each re-implement support-source staging: canonical repo detection, primary-ref vs `main` fallback, curated script/prompt/schema copies, and commit-exclusion bookkeeping. Variants of the same resolve/checkout/stage pattern also exist in `clarify.yml`, `plan.yml`, `orchestrate.yml`, `orchestrate_poll.yml`, and `orchestrate_clarify_respond.yml`. This is the maintainability side of the startup-tax issue already captured in the parent report, and it is also what pushed validate/review_autofix near the expression-size limit.  
  **Recommended fix:** Move support staging into one shared owner, preferably `scripts/stage_workflow_support.sh` (or a composite action), with a profile-style interface such as `stage_workflow_support <profile> <dest_root> [fetched_manifest]`. First-wave callers: `validate.yml`, `review_autofix.yml`, `implement.yml`; then repoint `clarify.yml`, `plan.yml`, `orchestrate.yml`, `orchestrate_poll.yml`, and `orchestrate_clarify_respond.yml`.

### Section 4: Expression Size Limit Risk Assessment

- **ID:** `EXPR-001`  
  **File:** `.github/workflows/validate.yml:212-636`  
  **Severity:** High  
  **Category:** `expression-limit`  
  **Description:** The `Fetch workflow support files` `run:` block contains `${{ }}` interpolations and measures approximately **20,065 characters**, leaving only about **935 characters** of headroom under GitHub's 21,000-character expression cap. It already embeds helper functions, copy loops, fallback logic, and many inline comments, so a small edit can make the workflow unloadable.  
  **Recommended fix:** Extract the whole block to an external script (ideally the shared support-staging module from `DUP-002`) so the workflow step becomes a thin launcher.

- **ID:** `EXPR-002`  
  **File:** `.github/workflows/review_autofix.yml:1228-1554`  
  **Severity:** High  
  **Category:** `expression-limit`  
  **Description:** `Stage workflow support files` measures approximately **18,675 characters**, leaving about **2,325 characters** of headroom. The block contains long file lists, main-fallback logic, and many conditional copies; it is already inside the repo's stated danger zone for expression regressions.  
  **Recommended fix:** Extract this block to the same shared support-staging script/composite action rather than continuing to edit the inline YAML body.

- **ID:** `EXPR-003`  
  **File:** `.github/workflows/implement.yml:3164-3539`  
  **Severity:** Medium  
  **Category:** `expression-limit`  
  **Description:** `Commit changes` measures approximately **17,460 characters**, leaving about **3,540 characters** of headroom. It mixes failure traps, fetched-manifest cleanup, self-repo path exclusions, destructive-delete guarding, scope enforcement, and no-op detection in one interpolated `run:` block.  
  **Recommended fix:** Split the block into multiple smaller steps or extract the commit orchestration to `scripts/implement_commit_changes.sh`.

- **ID:** `EXPR-004`  
  **File:** `.github/workflows/review_autofix.yml:1828-2220`  
  **Severity:** Medium  
  **Category:** `expression-limit`  
  **Description:** `Collect PR metadata` measures approximately **17,408 characters**, leaving about **3,592 characters** of headroom. It combines a bespoke retry helper, no-PR synthesis, linked-issue lookups, fallback API loops, inline Python serializers, diff capture, and base-ref resolution in one interpolated step.  
  **Recommended fix:** Extract PR-context hydration to a script such as `scripts/review_collect_pr_context.sh` and keep the workflow step as a short wrapper.

I did not find any `if:` expression remotely close to the 21,000-character limit; the largest single-line `if:` found was 115 characters in `.github/workflows/internal-review.yml:54`. No workflow exceeds 800 KB; the largest file is `.github/workflows/review_autofix.yml` at **401,929** bytes.

### Section 5: Cross-Cutting Concerns

- **ID:** `DEAD-001`  
  **File:** `scripts/issue_attachment_bundle.py:1-354; scripts/ai_context_utils.py:38-612; .github/workflows/ci.yml:89-95`  
  **Severity:** Low  
  **Category:** `dead-code`  
  **Description:** Repository search only found `issue_attachment_bundle.py` and `ai_context_utils.py` referenced by CI `py_compile` and by `issue_attachment_bundle.py`'s own import of two helper functions. The rest of `ai_context_utils.py`'s public APIs (`build_issue_envelope`, `build_pull_request_envelope`, `ingest_attachments`, `build_prompt_parts`, etc.) have no callers in workflows or scripts. This looks like an unattached attachment-ingestion subsystem that is syntax-checked but not exercised at runtime. [NEEDS VERIFICATION]  
  **Recommended fix:** Either wire this subsystem into an owning workflow and add an integration test that exercises `build`/`extract`, or move/remove it from the production script surface so CI and maintainers do not treat it as a live path.

No literal `TODO` / `FIXME` / `HACK` markers were found under `.github/workflows/*.yml` or `scripts/*.{sh,py}`. No additional high-confidence shellcheck issues rose above threshold beyond the duplicated GH-helper layer called out in `DUP-001`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BUG-001, EXPR-001, EXPR-002 |
| Medium | 8 | CONSIST-001, BATCH-001, BATCH-002, BATCH-003, DUP-001, DUP-002, EXPR-003, EXPR-004 |
| Low | 1 | DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2-3 | Small |
| API call optimization | 2-3 | Medium |
| Code modularization | 8-10 | Large |
| Expression size reduction | 4 | Large |
| Medium/Low fixes | 3-4 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-06-05)

### Safety Tag Legend
`SAFE_TO_MERGE` means the consolidation can be implemented directly without changing endpoint scope, control flow, or fail-open behavior; `NEEDS_VERIFICATION` means the overlap is real but pagination/freshness/parity must be checked first; `RISKY_SKIP` means the duplicate-looking calls sit in race-defense, retry, or other guard paths that must not be auto-consolidated.

### Consolidation Candidates (MERGE-###)

- **ID:** `MERGE-001`  
  **Safety tag:** `NEEDS_VERIFICATION`  
  **Files:** `.github/workflows/clarify.yml:414-438`  
  **Current call count:** 2 calls when `SEMANTIC_CACHE_BACKEND != none`.  
  **Proposed call count:** 1 call in that branch, if the single paginated result is reused for both outputs.  
  **Endpoint(s):** `GET /repos/{repo}/issues/{issue_number}/comments` with the same `sort=created&direction=asc` filter, once as `per_page=50` and once as paginated `per_page=100`.  
  **Evidence:**
  ```sh
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=50" > "${ISSUE_COMMENTS_FILE}"

  if [ "${SEMANTIC_CACHE_BACKEND}" != "none" ]; then
    if ! gh_retry gh api --paginate --slurp "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=100" \
      | jq -r 'add // [] | .[] | "[" + (.created_at // "") + "] @" + (.user.login // "unknown") + ":\n" + (.body // "") + "\n"' > "${THREAD_HISTORY_FILE}"; then
  ```
  **Proposed fix:** In the `Fetch issue comments` step, when semantic cache is enabled, fetch the full paginated comment set once into a temp JSON blob, derive `THREAD_HISTORY_FILE` from that blob, and write `ISSUE_COMMENTS_FILE` from the first 50 ordered entries of the same blob; keep the current single-50-comment path only for the cache-disabled branch or as an explicit fail-open fallback.  
  **Safety rationale:** The calls overlap, but they currently have different pagination shapes and intentionally decoupled failure behavior, so a one-call path needs parity verification before it is safe.  
  **Downstream signal:** Verify with an issue having `>100` comments that a merged path preserves `ISSUE_COMMENTS_FILE` ordering/truncation and that a simulated full-fetch failure still degrades to the current semantic-cache bypass behavior without losing prompt context.

- **ID:** `MERGE-002`  
  **Safety tag:** `SAFE_TO_MERGE`  
  **Files:** `scripts/implement_diagnose_post_codex_failure.sh:166-172`, `scripts/implement_diagnose_post_codex_failure.sh:261-277`  
  **Current call count:** 2 calls on the `ISSUE_META_FILE` miss/parse-fail path.  
  **Proposed call count:** 1 shared `GET /issues/{n}` call on that path, with the existing split behavior retained only as fail-open fallback if the shared fetch fails.  
  **Endpoint(s):** `GET /repos/{repo}/issues/{issue_number}`  
  **Evidence:**
  ```sh
  if [ -z "${ISSUE_LABELS_JSON}" ]; then
    ISSUE_LABELS_JSON="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}" --jq '[.labels[].name]' || echo '[]')"
  fi
  ...
  if [ "${issue_body_loaded_from_meta}" != "true" ]; then
    gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}" --jq '.body // ""' > "${ISSUE_BODY_FILE}" || printf '' > "${ISSUE_BODY_FILE}"
  fi
  ```
  **Proposed fix:** In `scripts/implement_diagnose_post_codex_failure.sh`, add one shared fallback issue JSON fetch (for example via `gh_api_json_to_file` from `scripts/gh_helpers.sh` or a temp-file `gh_retry gh api "repos/.../issues/${ISSUE_NUMBER}"`) when `ISSUE_META_FILE` is unusable, then derive both `ISSUE_LABELS_JSON` and `ISSUE_BODY_FILE` from that payload.  
  **Safety rationale:** Both reads hit the same endpoint in one script path, no code mutates the issue between them, and a success-only shared fetch can preserve the existing fail-open semantics exactly.  
  **Downstream signal:** Implement a shared fallback issue JSON cache in `scripts/implement_diagnose_post_codex_failure.sh` and use it for both label extraction and body extraction on the `ISSUE_META_FILE` miss path.

- **ID:** `MERGE-003`  
  **Safety tag:** `RISKY_SKIP`  
  **Files:** `.github/workflows/test-and-mark-stable.yml:1040-1045`, `.github/workflows/test-and-mark-stable.yml:1070-1075`  
  **Current call count:** 3 `/pulls/{PR_NUMBER}` reads on the stable-first-attempt path (`HEAD_A`, `HEAD_B`, then `PR_META`), up to 11 if all 5 stability attempts run.  
  **Proposed call count:** A tempting floor is 2 on the stable-first-attempt path by widening the second read and reusing it, but this must not be auto-implemented.  
  **Endpoint(s):** `GET /repos/{repo}/pulls/{pull_number}`  
  **Evidence:**
  ```sh
  HEAD_A=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")
  sleep 3
  HEAD_B=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")
  ...
  PR_META=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" 2>/dev/null || echo "")
  ```
  **Proposed fix:** No auto-fix. If manually reviewed, only consider widening the second post-sleep pull fetch to include `state`, `merged`, `merged_at`, and `closed_at`, then reusing that payload instead of the separate `PR_META` read.  
  **Safety rationale:** This is an explicit upstream-race defense path; the duplicate reads are part of the “stable across two reads ≥3s apart” contract, so policy requires `RISKY_SKIP`.  
  **Downstream signal:** Do not auto-implement; manual review must prove that any widened second `/pulls/{PR_NUMBER}` read still preserves both the 3-second stability check and the separate closed/merged guard semantics.

### Redundant Re-Fetch (REUSE-###)

- **ID:** `REUSE-001`  
  **Safety tag:** `NEEDS_VERIFICATION`  
  **Files:** `.github/workflows/orchestrate_clarify_respond.yml:62-71`, `.github/workflows/orchestrate_clarify_respond.yml:152-158`, `scripts/resolve_integration_ref.sh:38-40`, `scripts/resolve_integration_ref.sh:59-62`, `.github/workflows/orchestrate_clarify_respond.yml:425-436`  
  **Current call count:** 3 child-issue `GET /issues/{ISSUE_NUMBER}` calls on the orchestrator-managed path before prompt assembly.  
  **Proposed call count:** 1 child-issue fetch reused across the step and the resolver helper.  
  **Endpoint(s):** `GET /repos/{repo}/issues/{issue_number}`  
  **Evidence:**
  ```sh
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ...
  child_body="$(get_issue_body "${ISSUE}")"
  # get_issue_body -> gh api "repos/${REPO}/issues/${issue_num}" --jq '.body // ""'
  ...
  ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ```
  **Proposed fix:** Persist the first `ISSUE_PAYLOAD` (or at least its body/title/tracking-number fields) from `Check orchestrator metadata` to a temp file, extend `scripts/resolve_integration_ref.sh` with an optional cached-body input so it only falls back to `gh api` when that input is absent/invalid, and make `Fetch issue and tracking context` read the same cached JSON first.  
  **Safety rationale:** The reuse crosses workflow-step and helper-script boundaries, so freshness expectations must be validated before replacing live reads with cached issue body data.  
  **Downstream signal:** Verify that no supported operator flow depends on issue-body edits made after `Check orchestrator metadata` starts, then add resolver coverage for a cached-body override path and confirm the fallback-to-live-API path still works when the cache is missing or malformed.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- `BATCH-001`: `NEEDS_VERIFICATION` — agreed; extending `gh_pr_with_all_comments` is directionally correct, but parity for reviews/review-comments pagination and fail-open fallback must be re-tested before swapping the step over.
- `BATCH-002`: `NEEDS_VERIFICATION` — agreed; batching fallback issue hydration is the right shape, but the replacement must preserve the current 20-issue cap and per-issue fail-open behavior.
- `BATCH-003`: `NEEDS_VERIFICATION` — agreed; batching regex-fallback issue-label lookups is sound, but it still needs parity checks for the non-default-base fallback path and the current “labels unknown” handling.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 1 | `MERGE-002` |
| NEEDS_VERIFICATION | 2 | `MERGE-001`, `REUSE-001` |
| RISKY_SKIP | 1 | `MERGE-003` |

### Implement-Stage Handoff

- `MERGE-002`
