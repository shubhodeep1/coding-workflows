## Executive Summary

- **`review_autofix` is the dominant speed/cost lever.** It used **81,253s / 54.4%** of all observed run time across **111 runs**. Slow runs **26013098223**, **26014929366**, **26027445588**, and **26029342776** spent **1,845-2,500s** in `codex-agent`, while runtime config still showed **6 reviewer models**, **two-pass review**, **`xhigh` reviewer/editor reasoning**, and **`TARGETED_FILE_CONTEXT_MAX_BYTES=102400`**. **Estimated impact:** 20-40% faster on qualifying runs. **Confidence:** high.

- **CI failures were concentrated and surfaced late.** The `ci` family failed **8/44 runs (18.2%)**, wasting **4,982s**. Run **26031068317** failed **656.8s** into `lint` on `implement.yml missing resolved-ref log output`; runs **26037045079** and **26039161815** failed **574.2s** and **586.1s** into `Orchestrate poll process unit tests` on fingerprint-regression checks. **Estimated impact:** 8-11 minutes faster failure feedback and near-elimination of this failure cluster. **Confidence:** high.

- **Release-tail latency is dominated by audit and poll loops, not runner queue.** `workflow_log_analysis` averaged **3,756.5s**; run **26036110500** spent **1,904.0s** in `analyze-commit-notify`, **919.1s** in `deep-audit`, **825.9s** in `api-redundancy`, and **233.5s** in `Summarize unselected runs`. `test_and_mark_stable` run **26036073220** spent **3,795.7s** in `workflow-log-analysis-test` and **3,414.2s** in `e2e-smoke-test`. **Estimated impact:** 6-13 minutes per audit run, more on releases if audit tests become conditional. **Confidence:** high for reasoning reduction, medium for conditional gating.

- **GH API pressure is mostly redundancy, not outage.** `internal-review.yml` still does an open-PR lookup plus repo `default_branch` lookup before skip paths; `review_autofix.yml` has **4** `closingIssuesReferences` call sites and `issue_pr_status.yml` has **1**; `test-and-mark-stable.yml` uses fixed **5/10/15s** polling loops while explicitly budgeting around the **5,000/hr** GH_PAT limit. No sampled deep-dive run showed a real `429` or secondary-rate-limit incident. **Estimated impact:** materially lower call volume and lower rate-limit risk. **Confidence:** medium-high.

- **Semble looks useful; Serena is not in rollout.** I found **7 strict operational `SEMBLE_QUERY` lines** totaling **76,811 bytes** at about **480ms** average across slow `review_autofix` runs—about **3.4s total overhead**, which is tiny relative to 30-40 minute runs. I found **no operational `SERENA_QUERY` / `SERENA_FALLBACK` / `SERENA_PROBE` lines**, and sampled deep-dive `review_autofix` runs logged `SERENA_ENABLED: false`. **Estimated impact:** medium on cost control and rollout focus. **Confidence:** high.

- **AI memory retrieval is currently not helping.** All **4/4** strict `retrieve` events (runs **26013098223**, **26014929366**, **26027445588**, **26029342776**) returned **`records_selected=0`**, **`estimated_tokens=0`**, **`keyword_method=none`**. No `fail_open:true`, `enabled:false`, or push retries >1 were observed. **Estimated impact:** medium if retrieval quality improves; low until observability is expanded. **Confidence:** high.

## Speed Optimizations

1. **[Critical-path] Right-size `review_autofix` for medium-complexity PRs**
   - **Evidence:** `review_autofix` consumed **81,253s / 54.4%** of all observed run time. Slow runs **26013098223**, **26014929366**, **26027445588**, **26029342776** each spent **1,845-2,500s** in `codex-agent`. Run **26029342776** logged `REVIEWER_REASONING_EFFORT: xhigh`, `ENABLE_REVIEWER_TWO_PASS: true`, `TARGETED_FILE_CONTEXT_MAX_BYTES: 102400`, `SEMBLE_ENABLED: true`, `SERENA_ENABLED: false`, and `AUTOFIX_GATE_DET_SKIP_EVAL pr=2739 files=6 additions=61 ... small_diff=false skip=false`. The current workflow defaults in `.github/workflows/review_autofix.yml:96-160` still wire **6 reviewer models**, `xhigh` reasoning for reviewer/editor, and two-pass review.
   - **Root cause:** model fan-out + two-pass review + `xhigh` reasoning + large per-file context on PRs that are not obviously conflict-heavy.
   - **Exact change:** add a middle tier in `review_autofix` for modest PRs (for example: low file count, low total churn, no merge conflicts, no retry state) that keeps Semble on, but drops to **2-3 reviewer models**, **single pass**, `high` reviewer reasoning, `medium/high` editor reasoning, and **32-64 KB** targeted file context. Keep the current full path for large diffs, conflict-heal flows, and retried runs.
   - **Estimated time savings (inference):** **5-15 minutes** on qualifying `review_autofix` runs; roughly **20-40%** on the observed **1,845-2,500s** codex-agent spans.
   - **Implementation risk:** **medium**. Safe if the downgrade is strictly gated and fail-open to current defaults.

2. **[Critical-path] Fail fast on workflow-contract and fingerprint regressions in CI**
   - **Evidence:** Run **26031068317** failed **656.8s** after `lint` started. Runs **26037045079** and **26039161815** failed **574.2s** and **586.1s** into `Orchestrate poll process unit tests`. The failing checks are currently embedded inside the monolithic `lint` job in `.github/workflows/ci.yml:133-161`. In the checked-out repo, `.github/workflows/implement.yml:719-729` already contains the expected fallback-ref/base-ref logging, and `tests/test_workflow_checkout_integration_ref_audit.py:86-105` now expects those strings, which suggests the sampled contract drift was transient and the bigger gap is **late detection**.
   - **Root cause:** cheap structural regression checks are buried inside expensive suites.
   - **Exact change:** create a first CI preflight job/step that runs only:
     - `tests/test_workflow_checkout_integration_ref_audit.py`
     - the narrow fingerprint-regression subset of `tests/test_orchestrate_poll_process.py`
     before the broader Python/unit suites.
   - **Estimated time savings (inference):** **8-11 minutes** on recurrence runs; this would have cut most of the **4,982s** lost across the 8 CI failures in this window.
   - **Implementation risk:** **low**. Coverage does not change; only ordering changes.

3. **[Critical release tail] Lower `workflow_log_analysis` reasoning and summarization breadth**
   - **Evidence:** Run **26009107875** took **3,754s**: `analyze-commit-notify` **1,605.6s**, `deep-audit` **1,361.1s**, `api-redundancy` **676.2s**, `collect-logs` **87.4s**. Run **26036110500** took **3,759s** with the same pattern. `.github/workflows/workflow-log-analysis.yml:911-925` hardcodes `--reasoning xhigh` and explicitly says to lower it to `high` first if timeouts recur. The same workflow sets `WORKFLOW_LOG_SUMMARY_MAX_RUNS=100` and `WORKFLOW_LOG_SUMMARY_TOKEN_BUDGET=1500000` in `.github/workflows/workflow-log-analysis.yml:360-389`. Telemetry showed:
     - **26009107875:** `summarize_unselected_runs` used **150,488 tokens** for **87/100** runs
     - **26036110500:** **233,604 tokens** for **99/100** runs, and the summary step itself took **233.5s**
   - **Root cause:** `xhigh` reasoning across a very wide audit surface + aggressive unselected-run summarization cap.
   - **Exact change:** set deep-audit/analyze reasoning to **`high`**; reduce `WORKFLOW_LOG_SUMMARY_MAX_RUNS` from **100** to **30-50** and lower the summary token budget; keep the current settings only behind a manual override.
   - **Estimated time savings (inference):** **6-13 minutes** per audit run overall, including **2-4 minutes** from the summarizer step.
   - **Implementation risk:** **low-medium**. The workflow file already documents the safer fallback.

4. **[Micro / conditional] Path-gate `workflow-log-analysis-test` inside `test_and_mark_stable`**
   - **Evidence:** `test_and_mark_stable` run **26036073220** spent **3,795.7s** in `workflow-log-analysis-test`; run **26009091997** spent **3,795.9s** in the same step.
   - **Root cause:** a very long audit test remains on the release critical path even when release contents may not touch workflow/analyzer logic.
   - **Exact change:** only run `workflow-log-analysis-test` when the release changes workflow/analyzer paths such as `.github/workflows/**`, `scripts/analyze_workflow_logs.py`, `scripts/collect_workflow_logs.py`, or `scripts/summarize_unselected_runs.py`; otherwise keep it nightly or non-blocking.
   - **Estimated time savings (inference):** up to **~63 minutes** on unaffected release-test runs.
   - **Implementation risk:** **medium** because it narrows release-time validation coverage; use a manual override and nightly backstop.

## Cost Optimizations

> **Cost note:** operational prompt/completion/cache token counters were not emitted for normal model calls, so I am using **explicit token telemetry where available** plus **runtime/model-fan-out proxies** elsewhere.

1. **Reduce `review_autofix` model fan-out on the common path**
   - **Evidence:** `.github/workflows/review_autofix.yml:96-129` defaults to **6 reviewer models**, `xhigh` reviewer reasoning, `xhigh` pass-2 reasoning for both small and large diffs, and `xhigh` editor reasoning. Runtime logs for **26029342776** confirm those settings. `review_autofix` also consumed **81,253s** total, with **22 cancelled runs** adding **21,986s** (**27.1%** of family run time).
   - **Root cause:** expensive reviewer/editor path is the default, not the exception.
   - **Exact change:** keep the current full path only for large/conflicted/high-risk PRs; for medium PRs, drop to fewer reviewer models, single-pass review, and lower reasoning effort.
   - **Estimated savings (inference):** the largest single cost win in the pipeline; likely **30-60%** model-spend reduction on qualifying `review_autofix` runs.
   - **Quality-risk notes:** medium. Keep current defaults behind an override for conflict-heal, release, or rerun contexts.

2. **Cut unselected-run summarization spend in `workflow_log_analysis`**
   - **Evidence:** `summarize_unselected_runs` used **150,488** tokens in run **26009107875** and **233,604** tokens in run **26036110500**—**384,092 tokens** across just those two unique audit runs.
   - **Root cause:** default cap of **100** summaries and large summary token budget.
   - **Exact change:** reduce `WORKFLOW_LOG_SUMMARY_MAX_RUNS`; prioritize only runs with warnings, retries, MCP telemetry, GH API anomalies, or AI-memory signals.
   - **Estimated savings (inference):** **90k-160k tokens** per audit run, plus lower audit latency.
   - **Quality-risk notes:** low. Quiet successful runs are the least valuable summaries.

3. **Prevent avoidable canceled `review_autofix` spend**
   - **Evidence:** `review_autofix` had **22 cancelled runs** totaling **21,986s** (**14.7%** of all observed run time). Example pairs from the sampled window:
     - **26028633138** cancelled at **2162s**; **26028643542** succeeded **14s later** at **2157s**
     - **26007850963** cancelled at **1969s**; **26007857846** succeeded **15s later** at **1964s**
   - **Root cause (inference):** overlapping dispatches or stale-head reruns. This is **not** primarily explained by the main PR-backed concurrency block, because `.github/workflows/review_autofix.yml:718-742` explicitly keeps `cancel-in-progress: false` for PR-backed branches.
   - **Exact change:** add an upstream dedupe guard on **PR number + head SHA + workflow family** before any codex-agent launch; if an identical newer run already exists, skip before model work starts.
   - **Estimated savings (inference):** up to **~15%** of total observed runtime and associated model spend in this window.
   - **Quality-risk notes:** low if dedupe only applies to exact-head duplicates.

4. **Keep Semble; do not spend effort on Serena until it emits operational value**
   - **Evidence:** I found **7** strict operational `SEMBLE_QUERY` lines in slow `review_autofix` runs:
     - `reviewer-context`: **4 queries**, **54,472 bytes**, **~477.5ms** average
     - `overflow`: **3 queries**, **22,339 bytes**, **~483.3ms** average  
     Total overhead was about **3.4s** across those runs. Against `TARGETED_FILE_CONTEXT_MAX_BYTES=102400`, this is targeted, not noisy. By contrast, there were **no operational Serena query/fallback/probe lines**, and sampled deep-dive `review_autofix` runs logged `SERENA_ENABLED: false`.
   - **Root cause:** Semble is being used as a selective context fetcher; Serena is not yet active.
   - **Exact change:** keep Semble enabled on reviewer-context and overflow paths; only optimize overflow if it begins spilling to many files per run. Do **not** expand Serena rollout until it emits operational telemetry.
   - **Estimated savings:** prevents wasted rollout work and avoids replacing a targeted context reducer with an unmeasured path.
   - **Quality-risk notes:** low. Current evidence says Semble is helping, not hurting.

5. **Instrument prompt-cache counters before trying to tune prompt caching**
   - **Evidence:** slow `review_autofix` runs logged `OPENROUTER_PROMPT_CACHE_DISABLED: false`, but I found **no trustworthy operational** `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens` counters in deep-dive logs.
   - **Root cause:** cache instrumentation is configured but not observable.
   - **Exact change:** emit per-call token/cache counters by workflow/job/phase and model.
   - **Estimated savings:** unquantifiable today; this is a prerequisite optimization.
   - **Quality-risk notes:** none.

## Reliability Improvements

1. **Move workflow-contract and fingerprint regressions into a preflight gate**
   - **Failure evidence:** CI failures **26029332723**, **26031068317**, **26032757226**, **26034365768**, **26035468279**, **26035946559** all failed on the `implement.yml` checkout-ref contract; **26037045079** and **26039161815** failed on fingerprint-regression checks in `Orchestrate poll process unit tests`.
   - **Root cause category:** workflow/test contract drift.
   - **Exact fix:** keep the current broad `lint` coverage, but add a first preflight job for the two regression classes so they fail before the rest of CI runs.
   - **Expected reliability impact:** highest. This directly targets the full **8/44 CI failure cluster** in the window.
   - **Rollback / fail-open:** low-risk; removing the preflight only restores the current ordering.

2. **Deduplicate upstream `review_autofix` launches**
   - **Failure evidence:** **22** canceled `review_autofix` runs consumed **21,986s**; several were followed almost immediately by successful siblings.
   - **Root cause category:** duplicate dispatch / stale-head churn (**inference**).
   - **Exact fix:** add exact-head dedupe before launch and before any post-commit retrigger.
   - **Expected reliability impact:** lower rerun noise, fewer stale run states, fewer operator-facing cancellations.
   - **Rollback / fail-open:** skip only on exact `PR + head SHA + workflow` matches; otherwise run normally.

3. **Treat Semble fallbacks in this window as healthy fail-open test coverage, not a production incident**
   - **Failure evidence:** I found **15 raw** `SEMBLE_FALLBACK` lines, but only **10 unique operational lines** after deduping duplicate step copies. They all came from `test_and_mark_stable` validate-scripts runs **26009091997** and **26036073220**, all `target=overflow`, all `missing_semble`, all `ms=0`.
   - **Root cause category:** test fixture / deliberate fail-open path.
   - **Exact fix:** keep the fail-open behavior; optionally mark test-only fallbacks distinctly or dedupe duplicate logging so they do not look like production incidents.
   - **Expected reliability impact:** improves alert quality and avoids false outage narratives.
   - **Rollback / fail-open:** preserve the existing fail-open path.

4. **Reserve telemetry prefixes for runtime telemetry only**
   - **Failure evidence:** strict parsing found **249** lines containing `AI_MEMORY_TELEMETRY:` but only **16** valid JSON events; **233** were echoed/generated-report text. I also found analyzer-generated zero lines like `SERENA_QUERY 0`, `SERENA_FALLBACK 0`, `SERENA_PROBE 0`, `SEMBLE_PROBE 0` inside `workflow_log_analysis` logs.
   - **Root cause category:** telemetry/logging contract mismatch.
   - **Exact fix:** never emit bare runtime prefixes inside generated markdown/report text. Use a different prefix such as `MCP_SUMMARY:` / `MEMORY_SUMMARY:` or wrap them so downstream parsers cannot confuse them with live telemetry.
   - **Expected reliability impact:** materially better analytics accuracy; avoids false counts and rollout misreads.
   - **Rollback / fail-open:** none; runtime telemetry format stays unchanged.

**Serena rollout read:** I found **no operational** `SERENA_FALLBACK` or `SERENA_PROBE` failures. In this window, Serena looks **disabled**, not broken.

## AI Memory Health

- **Strict telemetry result:** **16 valid JSON events** and **233 invalid/echoed prefix lines**.
- **Observed op mix:** `record-run-event=8`, `retrieve=4`, `record-candidate=1`, `summarize_unselected_runs=3 raw` (**2 unique payloads**).
- **Retrieve hit rate:** **0/4 = 0%**.
  - Runs: **26013098223**, **26014929366**, **26027445588**, **26029342776**
  - All 4 had `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`, `enabled=true`
- **Average `estimated_tokens`:** **0**.  
  **Budget comparison:** not possible; no retrieve-budget field was emitted.
- **`keyword_method` distribution:** `none=4`, `plain=0`, `llm=0`
- **Fail-open / disabled:** no observed `fail_open:true`; no observed `enabled:false`
- **Push health:** max `push_attempts=1`
- **Write-side health:** one `record-candidate` was observed in run **26014929366**; no sampled `promote`, `compact`, `finalize-task`, or `processed-command-*` events appeared in deep-dive logs.
- **Only concrete token telemetry tied to this subsystem:** `summarize_unselected_runs`
  - **26009107875:** **87/100** runs summarized, **150,488** tokens
  - **26036110500:** **99/100** runs summarized, **233,604** tokens

**Recommendation:** memory retrieval quality should be treated as a product issue, not yet a cost win. First improve record selection/keyword extraction so `retrieve` returns anything useful; second, keep the telemetry prefix clean so hit-rate reporting remains trustworthy.

## GH API Call Audit

1. **`internal-review` skip path still does two calls before deciding not to proceed**
   - **Evidence:** `.github/workflows/internal-review.yml:99-101` calls:
     - open-PR lookup: `pulls?state=open&head=...`
     - repo lookup: `.default_branch`
   - The deep-audit in run **26036110500** reported this pattern in sampled skip runs **26029329678**, **26031066834**, **26032754907**, **26035465941**—**8 calls across 4 runs**.
   - **Concrete change:** use `github.event.repository.default_branch` directly, or defer the repo lookup until the path that actually needs it.
   - **Estimated call-count reduction:** **1 call per skip run** on this path; **50%** reduction for that block.
   - **Rate-limit impact:** small per run, but very low-risk.

2. **Linked-issue lookups are duplicated across workflows and then re-expanded per issue**
   - **Evidence:** repo search shows `closingIssuesReferences` in:
     - `.github/workflows/review_autofix.yml:518-519`
     - `.github/workflows/review_autofix.yml:649-650`
     - `.github/workflows/review_autofix.yml:1595-1596`
     - `.github/workflows/review_autofix.yml:4430-4431`
     - `.github/workflows/issue_pr_status.yml:192-193`
   - `issue_pr_status.yml:507-512` then loops `repos/${REPOSITORY}/issues/${issue_number}` per linked issue to rediscover orchestrator-managed state.
   - **Concrete change:** fetch linked issues once per PR event, persist the JSON, and reuse it across downstream steps/jobs.
   - **Estimated call-count reduction:** **3-4 GraphQL calls** per merged-PR flow plus **up to N REST issue GETs** on merged-alert paths.
   - **Rate-limit impact:** medium; this is the cleanest batching win in the repo.

3. **`test-and-mark-stable` uses fixed polling on long deadlines**
   - **Evidence:** `.github/workflows/test-and-mark-stable.yml:3336-3345` explicitly states the workflow is shaping API usage to stay under the **5,000/hr** GH_PAT budget. The same file repeatedly polls `actions/workflows/.../runs` and `actions/runs/...` with **5s**, **10s**, and **15s** sleeps (`:3407-3437`, `:1181-1315`, and multiple later wait loops). In sampled runs:
     - **26036073220**: `workflow-log-analysis-test` **3795.7s**, Phase 4 wait-review **1295.0s**
     - **26009091997**: `e2e-smoke-test` **3667.6s**
   - **Concrete change:** use adaptive backoff after no state change (`5/10/15 -> 30 -> 60`), and stop job/log fetches until a run has actually entered `in_progress`.
   - **Estimated call-count reduction (inference):** **40-70%** fewer run-status polls on the longest waits.
   - **Rate-limit impact:** medium-high, even though no actual rate-limit outage was observed.

4. **Rate-limit hygiene is present and should be kept**
   - **Evidence:** sampled workflows include explicit GH API wrappers and backoff logic; no sampled deep-dive run showed a real `429` or secondary-rate-limit incident.
   - **Concrete change:** keep the wrappers; focus optimization on redundancy, not emergency throttling.
   - **Estimated call-count reduction:** none directly.
   - **Rate-limit impact:** preserves current resilience.

## Prompt Cache & Memory System

- **Prompt cache is configured but not observable.**
  - **Evidence:** sampled slow `review_autofix` runs logged `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
  - **Gap:** no trustworthy operational `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens` counters were emitted.
  - **Recommendation:** emit those counters per workflow/job/phase/model before any cache tuning work.
  - **Estimated impact:** unknown until instrumentation exists; reliability impact is high because current cache hit/miss claims cannot be verified.

- **Cache fragmentation risk in `review_autofix` is high (inference).**
  - **Evidence:** six reviewer models, two-pass review, large `TARGETED_FILE_CONTEXT_MAX_BYTES`, dynamic PR/check-run context, and repeated reviewer/editor phases.
  - **Recommendation:** stabilize prompt prefixes:
    1. keep stable policy/system instructions first,
    2. reuse one serialized PR-context artifact across reviewer passes,
    3. append volatile run-specific state after the stable prefix,
    4. avoid repeating large unchanged context in pass-2 when pass-1 already serialized it.
  - **Estimated impact (inference):** medium token and latency reduction if prompt caching is active.

- **Semble is likely reducing prompt expansion, not adding noisy bytes.**
  - **Evidence:** only **7** strict operational queries, **76,811** bytes total, **~480ms** average, all in slow `review_autofix` runs; no flood pattern.
  - **Recommendation:** keep Semble enabled on current reviewer-context/overflow paths; only tighten overflow if it starts expanding to many files/run.
  - **Estimated impact:** keeps context targeted with negligible latency overhead.

- **Serena is not yet replacing downstream model/tool work.**
  - **Evidence:** no operational Serena telemetry; sampled deep-dive `review_autofix` runs logged `SERENA_ENABLED: false`.
  - **Recommendation:** do not spend optimization effort on Serena behavior until it is actually emitting operational telemetry.

- **Actions cache is healthy, but it is not a prompt cache.**
  - **Evidence:** `workflow_log_analysis` run **26036110500** hit and restored `codex-v0.114.0-v2`.
  - **Recommendation:** keep the Actions cache; do not treat it as evidence of prompt-cache effectiveness.

## Orchestrator Health

- **The clarify/plan/implement wrapper layer looks healthy and cheap.**
  - Recent runs **26039289042** (`plan`), **26039243089** (`clarify`), **26039290034** (`implement`), and **26039289907** (`orchestrate_clarify_respond`) all exited quickly on `Result: false`.
  - Combined wrapper skips across `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` were **704 runs / 1004s** total, so they are mostly **UI/event noise**, not compute waste.

- **The real orchestrator pain is downstream wait-and-heal behavior, not clarify loops.**
  - `test_and_mark_stable` run **26036073220** spent **1295.0s** in Phase 4 wait-review and **1237.3s** in Phase 5 orchestrator script integration testing.
  - The long waits are consistent with downstream `review_autofix` compute and poll behavior, not with a stuck clarify/answer loop.

- **Cancellation/successor-run handling is already sophisticated, but it needs better metrics.**
  - `test-and-mark-stable.yml:1181-1360` already documents pin-advance logic to avoid polling dead canceled review runs forever.
  - **Smallest safe mitigation:** emit explicit counters for `poll_iterations`, `successor_run_switches`, `head_sha_pin_advances`, `rate_limit_backoff_seconds`, and `final_wait_reason`.

- **Rollout signals are healthy, not masked.**
  - Semble fail-open signals in this window were test-only.
  - Serena was disabled in sampled deep dives, so there is no evidence of a broken production rollout hiding behind fallbacks.

- **Track these indicators weekly:**
  - non-skipped p50 / p95
  - `review_autofix` cancel rate
  - CI contract-preflight duration
  - `workflow_log_analysis` tokens per audit
  - AI-memory retrieve hit rate
  - GH API polls per long wait step

## Pipeline Flow Bottlenecks

1. **Review/autofix compute bottleneck**
   - **Flow stage:** implement → review/autofix
   - **Evidence:** `review_autofix` used **81,253s** total; slow runs spent **1,845-2,500s** in `codex-agent`
   - **Type:** compute
   - **Fix:** right-size reviewer count / reasoning / two-pass behavior

2. **Release-tail audit bottleneck**
   - **Flow stage:** validate/release tail
   - **Evidence:** `workflow_log_analysis` average **3756.5s**; `test_and_mark_stable` workflow-log-analysis test **~3796s**
   - **Type:** compute
   - **Fix:** lower audit reasoning and reduce summary breadth; optionally path-gate the audit test

3. **Late CI regression detection**
   - **Flow stage:** validate
   - **Evidence:** CI failures surfaced **574-657s** into the failing steps
   - **Type:** compute + fail-late
   - **Fix:** preflight contract/fingerprint regression checks before the heavy suites

4. **Duplicate `review_autofix` churn**
   - **Flow stage:** review/autofix loop
   - **Evidence:** **22** canceled runs, **21,986s** lost
   - **Type:** retry / rerun overhead
   - **Fix:** exact-head dedupe before codex-agent launch

5. **Fixed-interval poll overhead**
   - **Flow stage:** review/autofix wait, workflow-log-analysis wait, orchestrator verification
   - **Evidence:** long 5/10/15s polling loops in `test-and-mark-stable.yml`
   - **Type:** retry / API overhead
   - **Fix:** adaptive backoff and state-change-driven polling

6. **Runner queueing is visible but not dominant**
   - **Flow stage:** multiple
   - **Evidence:** system logs often show “waiting for a hosted runner,” but sampled waits were typically only seconds
   - **Type:** queueing
   - **Fix:** none right now; do not optimize here before compute/poll bottlenecks

7. **Skipped wrapper runs skew top-level latency metrics**
   - **Flow stage:** clarify / plan / implement wrappers
   - **Evidence:** overall p50 is **1s**, but non-skipped p50 is **174s**
   - **Type:** reporting distortion
   - **Fix:** separate skipped-wrapper dashboards from active-path dashboards

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix`: **81,253s**, **54.4%** of all observed runtime
  - `ci`: **32,916s**, success p50 **770s**, failure rate **18.2%**
  - Release tail: `workflow_log_analysis` **7,513s** + `test_and_mark_stable` **7,684s**

- **Top failure modes**
  - late `implement.yml` checkout-ref contract drift in CI
  - late fingerprint-regression failures in `test_orchestrate_poll_process.py`
  - high canceled-run churn in `review_autofix`

- **Highest-cost drivers**
  - six-reviewer, two-pass, `xhigh` `review_autofix`
  - audit summarization cap of **100** unselected runs
  - long poll-based wait phases in `test-and-mark-stable`

- **Top 3 prioritized actions**
  1. **Introduce a medium-cost `review_autofix` tier** for modest PRs while keeping the current full path for large/conflicted cases.
  2. **Add a CI preflight regression gate** for checkout-ref and integration-fingerprint contracts.
  3. **Lower `workflow_log_analysis` reasoning/summarization defaults** and path-gate `workflow-log-analysis-test` when release contents do not touch workflow/analyzer code.

## Metrics Appendix

### Run summary

| Scope | Runs | Success | Failure | Cancelled | Other/Skipped | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| All observed runs | 1000 | 257 | 8 | 27 | 708 | 149.3 | 1.0 | 793.0 |
| Non-skipped runs | 292 | 257 | 8 | 27 | 0 | 506.8 | 174.0 | 2159.3 |
| Skipped wrapper runs (`clarify+plan+implement+orchestrate_clarify_respond`) | 704 | 0 | 0 | 0 | 704 | 1.4 | 1.0 | 3.0 |

### Key workflow families

| Workflow family | Runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg s | p50 s | p95 s | Run-seconds | Share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 111 | 87 | 0 | 22 | 2 | 0.0% | 732.0 | 454.0 | 2282.5 | 81,253 | 54.4% |
| ci | 44 | 36 | 8 | 0 | 0 | 18.2% | 748.1 | 770.0 | 810.1 | 32,916 | 22.0% |
| test_and_mark_stable | 2 | 2 | 0 | 0 | 0 | 0.0% | 3842.0 | 3842.0 | 3848.3 | 7,684 | 5.1% |
| workflow_log_analysis | 2 | 2 | 0 | 0 | 0 | 0.0% | 3756.5 | 3756.5 | 3758.8 | 7,513 | 5.0% |
| implement | 182 | 11 | 0 | 5 | 166 | 0.0% | 26.9 | 1.0 | 253.6 | 4,893 | 3.3% |
| plan | 182 | 11 | 0 | 0 | 171 | 0.0% | 13.4 | 1.0 | 15.9 | 2,441 | 1.6% |
| clarify | 200 | 13 | 0 | 0 | 187 | 0.0% | 8.8 | 1.0 | 84.1 | 1,756 | 1.2% |
| orchestrate_clarify_respond | 182 | 2 | 0 | 0 | 180 | 0.0% | 1.5 | 1.0 | 3.0 | 278 | 0.2% |

### Observed token telemetry

| Source | Run ID | Model | Targeted runs | Summarized runs | Tokens used | Approx summary duration |
|---|---:|---|---:|---:|---:|---:|
| `summarize_unselected_runs` | 26009107875 | openai/gpt-5.4-mini | 100 | 87 | 150,488 | 136.6s |
| `summarize_unselected_runs` | 26036110500 | openai/gpt-5.4-mini | 100 | 99 | 233,604 | 233.5s |
| **Unique total observed** | 2 runs | — | 200 | 186 | **384,092** | — |

### Cache metrics

| Signal | Observation | Evidence |
|---|---|---|
| Prompt cache config | Enabled in sampled deep dives | `OPENROUTER_PROMPT_CACHE_DISABLED: false` in slow `review_autofix` runs |
| Prompt/cache token counters | **Not emitted operationally** | No trustworthy `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` found |
| Actions cache | Healthy | `workflow_log_analysis` run **26036110500** hit/restored `codex-v0.114.0-v2` |
| Cache tuning readiness | Blocked | No hit/miss counters for actual model prompts |

### GH API hotspot summary

| Workflow / step | Pattern | Sample evidence | Observed volume | Estimated avoidable reduction |
|---|---|---|---:|---:|
| `internal-review` / resolve-claude-branch-pr | open-PR lookup + repo default-branch lookup before skip | `.github/workflows/internal-review.yml:99-101`; sampled by audit in runs `26029329678`, `26031066834`, `26032754907`, `26035465941` | 8 calls / 4 runs | 4 calls |
| `review_autofix` + `issue_pr_status` | duplicate `closingIssuesReferences` queries | 4 call sites in `review_autofix.yml`; 1 in `issue_pr_status.yml` | 5 code call sites | 3-4 GraphQL calls / merged-PR flow |
| `issue_pr_status` merged alert | per-linked-issue REST issue GET loop | `.github/workflows/issue_pr_status.yml:507-512` | up to N calls / merged PR | up to N calls |
| `test-and-mark-stable` waits | fixed polling of `actions/workflows/.../runs` and `actions/runs/...` | `.github/workflows/test-and-mark-stable.yml:3336-3345`, `:3407-3437`, `:1181-1315`; long waits in runs `26009091997`, `26036073220` | inference: hundreds of polls on longest waits | 40-70% fewer polls |

### Semble / Serena / MCP telemetry (strict operational lines only)

| Server | Target | Query count | Query bytes | Avg query ms | Raw fallback count | Unique fallback count | Probe count | Response bytes | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| Semble | reviewer-context | 4 | 54,472 | 477.5 | 0 | 0 | 0 | 0 | All queries from slow `review_autofix` runs |
| Semble | overflow | 3 | 22,339 | 483.3 | 15 | 10 | 0 | 0 | Fallbacks were test-only in `test_and_mark_stable` validate-scripts runs `26009091997` and `26036073220` |
| Serena | n/a | 0 | 0 | n/a | 0 | 0 | 0 | 0 | No operational telemetry; sampled deep-dive `review_autofix` runs logged `SERENA_ENABLED: false` |
| Other MCP servers observed | — | 0 | 0 | n/a | 0 | 0 | 0 | 0 | None |

### MCP availability rows

| Server | Target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---:|---:|---:|---|
| Semble | reviewer-context | 0 | 0 | 0 | No operational probe lines emitted |
| Semble | overflow | 0 | 0 | 0 | No operational probe lines emitted |
| Serena | n/a | 0 | 0 | 0 | No operational probe lines emitted; rollout disabled in sampled deep dives |
| Other MCP servers | — | 0 | 0 | 0 | None observed |

### AI memory metrics

| Metric | Value |
|---|---:|
| Valid strict `AI_MEMORY_TELEMETRY` JSON events | 16 |
| Invalid / echoed prefix lines | 233 |
| `retrieve` events | 4 |
| Retrieve hit rate | 0/4 = 0% |
| Avg `estimated_tokens` on retrieve | 0 |
| `keyword_method=none` | 4/4 |
| `fail_open:true` observed | 0 |
| `enabled:false` observed | 0 |
| Max `push_attempts` observed | 1 |
| `record-run-event` events | 8 |
| `record-candidate` events | 1 |
| `promote` / `compact` / `finalize-task` / `processed-command-*` events in deep dives | 0 observed |


## Deep Audit — Workflows & Scripts (2026-05-18)

### Section 1: Bug & Correctness Sweep

- **ID:** `BUG-001`  
  **File path:** `scripts/tg_helpers.sh:296-356,365-428`  
  **Severity:** Medium  
  **Category tag:** `bug`  
  **Description:** Both `tg_cleanup_phase_msgs()` and `tg_cleanup_msgs()` paginate issue comments with `?page=${page}`, delete matching comments inside the same page loop, then increment `page`. On issues with more than 100 comments, deleting page-1 items shifts later comments left, so the next `page=2` fetch can skip tracked comments that moved onto page 1. The result is partial Telegram cleanup: orphaned tracking comments and undeleted TG message IDs.  
  **Recommended fix:** Collect all matching comment IDs across all pages first, then delete in a second pass; alternatively, keep re-reading page 1 until no tracked comments remain. Prefer extracting a shared internal helper in `scripts/tg_helpers.sh` so both cleanup paths use one stable implementation, and route DELETEs through `curl_gh_api` for the same retry/backoff behavior as GETs.

- **ID:** `BUG-002`  
  **File path:** `.github/workflows/orchestrate_clarify_respond.yml:67-83`  
  **Severity:** Medium  
  **Category tag:** `bug`  
  **Description:** The `Check orchestrator metadata` step runs under `set -euo pipefail` but performs its primary control-path lookup with raw `gh api` at line 68. A transient GitHub API/auth/rate-limit failure aborts the workflow before any retry helper is sourced. The tracking-title lookup at line 83 is also raw `gh api` (with a local `|| echo ""` fallback), so this step bypasses the repo’s normal `gh_retry` contract.  
  **Recommended fix:** Source `scripts/gh_helpers.sh` at the top of the step and use `gh_retry`/`_safe_gh_jq` for both issue lookups. Keep the tracking-title path fail-open only after retries are exhausted.

### Section 2: GitHub API Call Redundancy Audit

- **ID:** `API-001`  
  **File path:** `scripts/orchestrate_poll_process.sh:6462-6555,9216-9238`  
  **Severity:** Medium  
  **Category tag:** `api-redundancy`  
  **Description:** `_fetch_candidate_issue_details_graphql()` already fetches `linked_pr` data into `_current_wave_details_json` (`number`, `state`, `merged`, `head_ref`, `head_sha`, `mergeable`, `mergeStateStatus`, `headPushedAt`), but the current-wave reconcile loop ignores that cache and still calls `_issue_cross_ref_pr_number_last` plus `_fetch_pr_json` per issue. **Current call count:** after the existing batch call, the happy path still adds `N` timeline lookups + up to `N` PR GETs per cycle (`2N` logical calls). **Proposed call count:** `0` extra calls on cache hits, with per-issue fallback only when `.linked_pr` is absent. **Existing batching pattern to extend:** `_fetch_candidate_issue_details_graphql`.  
  **Recommended fix:** Read `.linked_pr.number`, `.linked_pr.state`, and `.linked_pr.merged` from `_current_wave_details_json` inside the reconcile loop, and invoke `_issue_cross_ref_pr_number_last` / `_fetch_pr_json` only for cache misses.

- **ID:** `API-002`  
  **File path:** `.github/workflows/implement.yml:3550-3560; scripts/orchestrate_poll_process.sh:5198-5225`  
  **Severity:** Low  
  **Category tag:** `api-batching`  
  **Description:** The “ancestor no-op” scan fetches parent issue body and parent comments separately on every hop in both implementations. **Current call count:** `2 × threshold` logical calls per implement invocation (default `4` at `IMPL_NOOP_ANCESTRY_THRESHOLD=2`) and `2 × max_depth` per poller invocation (default `6` at `max_depth=3`). **Proposed call count:** `threshold` / `max_depth` calls by fetching body+comments together once per hop, or fewer if the chain is batched. **Existing batching pattern to extend:** `_fetch_candidate_issue_details_graphql` (or a slimmer shared helper derived from it).  
  **Recommended fix:** Add a shared helper that returns issue body plus recent comments in one query, and reuse it from both call sites.

### Section 3: Code Duplication & Modularization Opportunities

- **ID:** `DUP-001`  
  **File path:** `.github/workflows/implement.yml:3550-3560; scripts/orchestrate_poll_process.sh:5198-5225`  
  **Severity:** Low  
  **Category tag:** `duplication`  
  **Description:** The no-op ancestor-chain walk is duplicated across implement and orchestrator polling, with the same marker text, parent-number extraction regex, hop loop, and comment-count logic. This is the same hot path noted in `API-002`; any future fix has to land twice.  
  **Recommended fix:** Move the logic into a shared module such as `scripts/noop_ancestry_helpers.sh` with a function like `count_noop_reissue_ancestors <repo> <issue_number> <max_depth> [marker_regex]`. Update callers in the implement no-op guard and `scripts/orchestrate_poll_process.sh`.

- **ID:** `DUP-002`  
  **File path:** `.github/workflows/review_autofix.yml:602-616; scripts/label_helpers.sh:110-143; scripts/review_rb_judge.sh:120-134; scripts/orchestrate_poll_process.sh:1368-1423`  
  **Severity:** Low  
  **Category tag:** `duplication`  
  **Description:** The repo already has a canonical `ensure_label_exists` in `scripts/label_helpers.sh`, but review/autofix, the review-blocked judge, and the orchestrator each carry their own label-creation logic with hardcoded colors/descriptions. That duplicates label metadata from `.github/ai/label_contract.v1.json` and increases drift risk whenever a label is added or edited.  
  **Recommended fix:** Standardize on `scripts/label_helpers.sh::ensure_label_exists <label_name> [repo]`. For lightweight jobs that avoid a full checkout, stage just `label_helpers.sh` (and `gh_helpers.sh`) the way `validate.yml` stages support files.

- **ID:** `DUP-003`  
  **File path:** `.github/workflows/cancel_on_pr_close.yml:40-53; .github/workflows/mark-stable.yml:344-357; .github/workflows/orchestrate_poll.yml:96-113; .github/workflows/test-and-mark-stable.yml:1728-1750`  
  **Severity:** Low  
  **Category tag:** `duplication`  
  **Description:** Four workflows reimplement slightly different `_gh_retry` / `gh_api_with_retry` wrappers instead of reusing `scripts/gh_helpers.sh`. Backoff rules, stderr capture, and error wording now vary by workflow.  
  **Recommended fix:** Extend `scripts/gh_helpers.sh` with one shared wrapper, e.g. `gh_retry_capture <stderr_file> -- gh api ...`, then replace the inline copies in these workflows.

- **ID:** `DUP-004`  
  **File path:** `scripts/tg_helpers.sh:296-356,365-428`  
  **Severity:** Low  
  **Category tag:** `duplication`  
  **Description:** `tg_cleanup_phase_msgs()` and `tg_cleanup_msgs()` duplicate the same paginated fetch/delete loop, including the page-shift defect in `BUG-001`.  
  **Recommended fix:** Add an internal helper such as `_tg_collect_tracking_comments <issue_num> [phase]` plus `_tg_delete_tracking_comments <comment_id...>`, and have both public cleanup functions call it.

### Section 4: Expression Size Limit Risk Assessment

- **ID:** `EXPR-001`  
  **File path:** `.github/workflows/test-and-mark-stable.yml:1203-1587`  
  **Severity:** High  
  **Category tag:** `expression-limit`  
  **Description:** The `Phase 4: Wait for review & autofix to complete` `run:` block contains `${{ }}` interpolations and is about **19,899 chars** dedented, leaving only **1,101 chars** of headroom under GitHub’s **21,000-char** limit. It already embeds a local GH retry wrapper, polling loop, job-log probe, and multiple shortcut paths.  
  **Recommended fix:** Extract the whole step to an external script (preferred), e.g. `scripts/test_and_mark_stable_wait_review.sh`, and pass the small set of env vars in from YAML.

- **ID:** `EXPR-002`  
  **File path:** `.github/workflows/validate.yml:210-583`  
  **Severity:** Medium  
  **Category tag:** `expression-limit`  
  **Description:** `Fetch workflow support files` is about **17,416 chars** dedented, leaving **3,584 chars** of headroom. The inline clone/copy/bootstrap logic and long template list make this block likely to keep growing.  
  **Recommended fix:** Extract the support-file bootstrap into `scripts/fetch_validate_support_files.sh`, or move the template/file lists into manifest files that the script reads at runtime.

- **ID:** `EXPR-003`  
  **File path:** `.github/workflows/test-and-mark-stable.yml:1673-2078`  
  **Severity:** Medium  
  **Category tag:** `expression-limit`  
  **Description:** `Phase 4b: Verify editor restored canary (pytest + retry)` is about **17,408 chars** dedented, leaving **3,592 chars** of headroom. It inlines GH retry helpers, canary fetch helpers, pytest classification, retry-dispatch polling, and result handling in one YAML block.  
  **Recommended fix:** Extract to an external script such as `scripts/verify_e2e_smoke_canary.sh`, keeping YAML responsible only for env/setup and final outputs.

- **Workflow file size note:** No workflow exceeds **800 KB**. Largest files are `review_autofix.yml` (**345,188 bytes**) and `test-and-mark-stable.yml` (**281,597 bytes**).

### Section 5: Cross-Cutting Concerns

- **ID:** `DEAD-001`  
  **File path:** `scripts/orchestrate_poll_process.sh:5299-5306,5416-5425`  
  **Severity:** Low  
  **Category tag:** `dead-code`  
  **Description:** `read_standalone_state_json()` and `stall_recovery_action_is_terminal()` are defined but have no call sites in workflows or scripts; repository-wide search only finds their definitions.  
  **Recommended fix:** Remove them, or mark them explicitly as reserved APIs and add tests/documentation showing the intended future caller.

- **ID:** `DEAD-002`  
  **File path:** `scripts/memory_helpers.sh:172-192,226-234`  
  **Severity:** Low  
  **Category tag:** `dead-code`  
  **Description:** `memory_processed_command_list()` and `memory_promote()` are defined in the shared memory wrapper but have no workflow/script call sites.  
  **Recommended fix:** Remove the unused wrappers, or annotate them as reserved and add a self-test that proves they are intentionally kept.

- **ID:** `CONSIST-001`  
  **File path:** `scripts/label_helpers.sh:110-143; scripts/orchestrate_poll_process.sh:1399-1422; scripts/review_rb_judge.sh:120-134; .github/workflows/review_autofix.yml:613-616`  
  **Severity:** Medium  
  **Category tag:** `consistency`  
  **Description:** The same logical operation—“ensure this repo label exists”—has different return contracts in different places. `scripts/label_helpers.sh` returns failure on non-`already exists` errors, `scripts/orchestrate_poll_process.sh` logs a warning but still returns success, and the review/judge helpers swallow creation failures with `|| true`. That makes label-creation regressions visible in some paths and silent in others.  
  **Recommended fix:** Standardize on `scripts/label_helpers.sh` as the canonical behavior. When a caller truly wants fail-open semantics, wrap the canonical helper at the call site (`ensure_label_exists ... || true`) so the policy choice is explicit.

- **Shellcheck note:** A `shellcheck` pass over `scripts/*.sh` did not surface additional high-confidence warnings beyond the structural issues above.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | EXPR-001 |
| Medium | 6 | BUG-001, BUG-002, API-001, EXPR-002, EXPR-003, CONSIST-001 |
| Low | 7 | API-002, DUP-001, DUP-002, DUP-003, DUP-004, DEAD-001, DEAD-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 2 | Medium |
| Code modularization | 10 | Large |
| Expression size reduction | 2 | Medium |
| Medium/Low fixes | 7 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-05-18)

### Safety Tag Legend
`SAFE_TO_MERGE` means the consolidation is statically provable in the current code path without changing retry/error semantics; `NEEDS_VERIFICATION` means the overlap is real but equivalence is not fully provable from static reading alone; `RISKY_SKIP` means the duplication sits in a retry/race-sensitive path and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

- **ID:** `MERGE-001`  
  **Safety tag:** `SAFE_TO_MERGE`  
  **File path and line ranges:** `.github/workflows/orchestrate_clarify_respond.yml:68-83`, `.github/workflows/orchestrate_clarify_respond.yml:409-420`  
  **Current call count:** 2 child-issue GETs plus up to 2 tracking-issue GETs per orchestrator-managed run with a tracking issue.  
  **Proposed call count:** 1 child-issue GET plus up to 1 tracking-issue GET, with the later step reading cached payloads.  
  **Endpoint(s):** `GET /repos/{owner}/{repo}/issues/{issue_number}`  
  **Evidence:**
  ```bash
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ...
  TRACKING_TITLE="$(gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.title // ""' ...)"

  ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ...
  TRACKING_BODY="$(gh_retry gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.body // ""')"
  ```
  The first step already fetches the child issue, and conditionally fetches the tracking issue; the later `Fetch issue and tracking context` step re-fetches both objects from the same REST endpoint.  
  **Proposed fix:** In `Check orchestrator metadata`, persist full child/tracking issue JSON to temp files (or `$GITHUB_ENV` file paths), not just derived strings; in `Fetch issue and tracking context`, parse those cached payloads first and call the current `gh_retry gh api` lines only on cache miss/unparseable cache.  
  **Safety rationale:** Both pairs hit the same `GET /issues/{n}` endpoint in the same job, nothing between these steps mutates issue title/body, and preserving the later `gh_retry gh api` as cache-miss fallback keeps failure behavior aligned.  
  **Downstream signal:** Persist full child/tracking issue payloads in `Check orchestrator metadata` and make `Fetch issue and tracking context` consume them before falling back to its current `gh_retry gh api` calls.

- **ID:** `MERGE-002`  
  **Safety tag:** `NEEDS_VERIFICATION`  
  **File path and line ranges:** `scripts/review_rb_judge.sh:246-251`, `scripts/review_rb_judge.sh:267-283`  
  **Current call count:** 1 GraphQL call plus up to `N` REST issue GETs (`N = linked issues`, stopping once the first non-empty body is found).  
  **Proposed call count:** 1 GraphQL call on the common path; keep the REST loop only as fallback when GraphQL data is missing/insufficient.  
  **Endpoint(s):** GraphQL `repository.pullRequest.closingIssuesReferences`; REST `GET /repos/{owner}/{repo}/issues/{issue_number}`  
  **Evidence:**
  ```bash
  ISSUE_NUMBERS="$(gh_retry gh api graphql \
    ... closingIssuesReferences(first: 50) { nodes { number } } ...)"

  while IFS= read -r issue_number; do
    ISSUE_META_JSON="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" || echo '{}')"
    BODY="$(printf '%s' "${ISSUE_META_JSON}" | jq -r '.body // ""' ...)"
    ...
  done <<< "${ISSUE_NUMBERS}"
  ```
  The GraphQL call only asks for `number`, then the script rehydrates `body` and `labels` issue-by-issue via REST.  
  **Proposed fix:** Extend the existing GraphQL query to request `nodes { number body labels(first: 100) { nodes { name } } }`, populate `FIRST_ISSUE_BODY` and `FIRST_ISSUE_LABELS_JSON` directly from that response, and retain the current REST loop only when the GraphQL node set is absent or insufficient. Use the repo’s existing batching style from `_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql` as the reference pattern.  
  **Safety rationale:** The overlap is real, but GraphQL node ordering/body nullability must be proven equivalent to the current “first linked issue with a non-empty body” loop before the REST hydration can be removed safely.  
  **Downstream signal:** Verify on representative PRs that the extended GraphQL query yields the same `FIRST_ISSUE`, `FIRST_ISSUE_BODY`, and `FIRST_ISSUE_LABELS_JSON` as the current REST loop before dropping the per-issue GETs.

- **ID:** `MERGE-003`  
  **Safety tag:** `RISKY_SKIP`  
  **File path and line ranges:** `scripts/orchestrate_poll_process.sh:3905-3945`, `scripts/orchestrate_poll_process.sh:3995-4000`, `scripts/orchestrate_poll_process.sh:4049-4051`  
  **Current call count:** 8 REST PR GETs inside `finalize_integration_merge_if_needed()`.  
  **Proposed call count:** 3 REST PR GETs if each same-block field bundle is collapsed to one JSON fetch while preserving fresh reads across mutation boundaries.  
  **Endpoint(s):** `GET /repos/{owner}/{repo}/pulls/{pull_number}`  
  **Evidence:**
  ```bash
  existing_pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' ...)"
  existing_pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' ...)"

  pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' ...)"
  pr_mergeable="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' ...)"
  pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' ...)"

  pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' ...)"
  pr_mergeable="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' ...)"
  pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' ...)"
  ```
  Each block reads multiple fields from the same PR payload without mutation between those reads.  
  **Proposed fix:** Add a local `_final_pr_json` fetch inside each state-check block in `finalize_integration_merge_if_needed()`, parse `.state/.mergeable/.merged_at` from that one object, and keep a separate fresh fetch after each state-changing boundary (`gh pr create`, merge attempt, state-file writes as needed).  
  **Safety rationale:** This sits inside `orchestrate_poll_process.sh`’s final-merge/race-handling path, so even a correct-looking collapse can accidentally remove intentionally fresh reads across mutable boundaries.  
  **Downstream signal:** Do not auto-implement; manual review must prove one fresh PR read is still preserved on each side of every merge/create boundary and that no log/diagnostic contract depends on today’s multi-read cadence.

### Redundant Re-Fetch (REUSE-###)

- **ID:** `REUSE-001`  
  **Safety tag:** `SAFE_TO_MERGE`  
  **File path and line ranges:** `.github/workflows/review_autofix.yml:1238-1244`, `.github/workflows/review_autofix.yml:1561-1575`, `scripts/review_rb_judge.sh:246-256`  
  **Current call count:** 1 extra `GET /pulls/{PR_NUMBER}` on the GraphQL-empty fallback path in `review_rb_judge.sh`.  
  **Proposed call count:** 0 extra calls on cache hit; keep the current API fallback only if `PR_META_FILE` is missing/blank.  
  **Endpoint(s):** `GET /repos/{owner}/{repo}/pulls/{pull_number}`  
  **Evidence:**
  ```bash
  echo "PR_META_FILE=${RUNTIME_DIR}/pr_meta.json"
  ...
  gh_retry "${PR_PAYLOAD_FILE}" api repos/${{ github.repository }}/pulls/"${PR_NUMBER}"
  jq '{
    title: (.title // ""),
    body: (.body // ""),
    ...
  }' "${PR_PAYLOAD_FILE}" > "${PR_META_FILE}"
  ```
  ```bash
  if [ -z "${ISSUE_NUMBERS}" ]; then
    PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' || echo "")"
    ...
  fi
  ```
  The workflow already materializes PR title/body into `PR_META_FILE` before invoking `review_rb_judge.sh`; the main workflow also already uses a `PR_META_FILE`-first pattern elsewhere:
  ```bash
  PR_DATA="$(jq -r '[.title // "", .body // ""] | join(" ")' "${PR_META_FILE}" ...)"
  if [ -z "$(printf '%s' "${PR_DATA}" | tr -d '[:space:]')" ]; then
    PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" ...)"
  fi
  ```  
  **Proposed fix:** Mirror the existing `PR_META_FILE`-first fallback pattern from `review_autofix.yml:4528-4532` (or `5478-5482`) inside `scripts/review_rb_judge.sh` before calling `GET /pulls/{PR_NUMBER}`.  
  **Safety rationale:** The cached metadata file is populated earlier in the same job from the same PR endpoint, and retaining the current API fallback on blank/missing cache preserves existing failure semantics.  
  **Downstream signal:** Make `review_rb_judge.sh` read title/body from `PR_META_FILE` first and fall back to the current pull GET only when that file is absent or empty.

- **ID:** `REUSE-002`  
  **Safety tag:** `NEEDS_VERIFICATION`  
  **File path and line ranges:** `.github/workflows/issue_pr_status.yml:291-330`, `.github/workflows/issue_pr_status.yml:503-512`  
  **Current call count:** after the earlier orchestrator-classification step has already fetched issue bodies, the merged-alert step still performs up to `N` extra `GET /issues/{issue_number}` body reads (`N = linked issues`).  
  **Proposed call count:** 0 extra alert-time reads when the earlier step successfully computed the needed body-marker signal; retain the current loop only when that earlier signal is unavailable.  
  **Endpoint(s):** GraphQL aliased `issue(number: ...) { body ... }`; REST `GET /repos/{owner}/{repo}/issues/{issue_number}`  
  **Evidence:**
  ```bash
  ORCH_ALIAS_FRAGMENT+=" i${ORCH_IDX}: issue(number: ${_orch_num}) { number labels(first: 50) { nodes { name } } body }"
  ...
  ORCH_RESP="$(gh_retry gh api graphql -f query="${ORCH_QUERY}" ...)"
  ...
  _orch_meta="$(gh_retry gh api "repos/${REPOSITORY}/issues/${_orch_num}" --jq '{labels:[.labels[].name], body:(.body // "")}' ...)"
  ```
  ```bash
  while IFS= read -r issue_number; do
    BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
    if printf '%s' "${BODY}" | grep -qF 'Managed by: AI Orchestrator'; then
      IS_ORCHESTRATED="true"
      break
    fi
  done
  ```
  The earlier step already has body text for the same linked issues, but the later alert step re-fetches it.  
  **Proposed fix:** In the earlier classification step, compute and export a body-marker-only boolean (matching the later `grep -qF 'Managed by: AI Orchestrator'` semantics exactly) from `ORCH_RESP` / `_orch_meta`; in the alert step, consume that exported boolean first and fall back to the current `_safe_gh_jq` loop only if the boolean is unset.  
  **Safety rationale:** The reused data comes from an earlier GraphQL/REST mix, so the body text and null-handling should be verified against the current alert-time REST reads before removing the later loop.  
  **Downstream signal:** Verify on standalone, orchestrator-managed, and tracking-linked merged PRs that a precomputed body-marker boolean matches the current alert-time `_safe_gh_jq` loop exactly; only then replace the late re-fetch with that exported signal.

### Dead Calls (DEAD-API-###)
No findings.

### Cross-References to Deep Audit Section
- `API-001`: `RISKY_SKIP` — correct target, but it lives in `scripts/orchestrate_poll_process.sh`’s cycle-local reconcile path where stale-cache mistakes can change race recovery behavior.
- `API-002`: `RISKY_SKIP` — worthwhile batching opportunity, but one call site is inside `scripts/orchestrate_poll_process.sh`, so ancestor-walk semantics need manual review before any auto-change.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 2 | `MERGE-001`, `REUSE-001` |
| NEEDS_VERIFICATION | 2 | `MERGE-002`, `REUSE-002` |
| RISKY_SKIP | 1 | `MERGE-003` |

### Implement-Stage Handoff
- `MERGE-001`
- `REUSE-001`
