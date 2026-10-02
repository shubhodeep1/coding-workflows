## Executive Summary

- **Review failure reporting needs attention first.** In `shubhodeep1/coding-workflows`, run **36920824276** failed because its sole reviewer produced malformed structured output in both passes. The job reported “All reviewers failed,” but `REVIEW_AUTOFIX_RUN_SUMMARY_V1` recorded `finalize_reason=clean_review_no_commit`, and `AUTOFIX_FAILURE_HEADLINE` recorded `first_error_present=0`. Add a sanitized parse-failure class and a terminal outcome field that cannot say “clean” when the reviewer job fails. **Impact:** makes this failure diagnosable and prevents a misleading clean signal; **confidence: high**.
- **The poller has a measurable, low-risk API and latency opportunity.** Run **36947018303** spent **59.4 seconds** in its standalone conflict sweep. Its code fetched each eligible PR before filtering drafts; the log identifies **63 draft `claude/*` PRs** skipped *after* that read. Prefilter drafts using fields in the existing PR-list response, retaining REST for uncertain cases. **Estimated impact:** 63 fewer reads and roughly **30–50 seconds** in a similarly shaped poll; **confidence: medium** for time, high for the avoidable reads.
- **Long reviews, not typical runs, dominate the observed model spend.** Ten deep-dived full-panel reviews lasted **1,879–3,657 seconds**. Across parsed telemetry, **139 OpenRouter calls** logged **317.3 million total tokens**; `google/gemini-3.8-flash` accounted for **200.6 million**, including **178.2 million cache-read tokens**. Instrument per-slot context and elapsed time before changing protected-path review coverage. **Impact:** identifies the largest savings target without weakening review; **confidence: high** for counts, low for prospective dollar savings.
- **A conflict dispatch can complete without resolving the conflict.** Poller run **36947018303** received HTTP **422** updating PR **#5360**, dispatched review run **36947421427**, and that run skipped with `reason=claude_fixer_awaiting_session`. Record dispatch-to-gate-to-resolution outcomes; do not count dispatch alone as a fix. **Impact:** exposes ineffective recovery cycles; **confidence: high** for this instance, low for its window-wide frequency.
- **Coverage limits all population-wide cost and API conclusions.** The 1,000-run window has **643 successes, one failure, one cancellation, 353 skips, and two `action_required` runs**. The assembled context reports parsed telemetry for **115 runs**, while the collector’s `summary.json` reports **30 downloaded-log samples**; **14/139** OpenRouter calls lack usable usage data. **Impact:** better collection would permit reliable rates and savings estimates; **confidence: high**.

## Speed Optimizations

1. **Critical-path reviewer stalls — potentially minutes in affected runs; medium implementation risk.** Full-panel review runs **36912230201** and **36915996909** each logged repeated `minimax/minimax-m3` stall-guard kills and failback; runs **36917073052** and **36927185877** also logged stall recovery. Inference: stalled attempts and rework contribute substantially to the long tail, but the logs do not isolate their full wall-clock cost. Add per-slot `start`, `last_progress`, `kill`, retry, and completion elapsed times, with failure class and model. Then investigate whether a narrower stall condition or earlier existing failback is safe. **Potential saving:** up to approximately one configured **600-second** stall window per *avoidable* attempt; **no saving is established yet**. Preserve the current fail-closed reviewer result and rollback any timing change that reduces successful coverage.

2. **Poller per-PR reads — approximately 30–50 seconds per similarly shaped poll; low risk.** `scripts/orchestrate_poll_process.sh` lists up to **100** open PRs, performs a REST PR read, then tests whether a `claude/*` PR is a draft. Poller **36947018303** logged **63** such draft skips during a **59.4-second** sweep. Include draft status in the existing list response where supported and skip those candidates *before* REST; retain the existing read on missing or uncertain status. Log `listed`, `prefiltered_draft`, `rest_fetched`, and whether the 100-item limit may have truncated the list. This is a critical-path poller change, not a model micro-optimization.

3. **Repeated no-op review dispatches — about 9–14 seconds per avoided gate-only run; medium risk.** Recent summaries explicitly show **41** `claude_fixer_awaiting_session` skips across **28 PRs**; PR **#5360** appears four times in those summaries and skipped again in run **36947421427**. Correlate sweep dispatches with gate outcomes by PR and head, then suppress only *confirmed* duplicate dispatches for an unchanged head and unchanged handoff state. On missing state, keep dispatching. This saves runner work; whether it shortens PR completion requires measurement.

**Do not treat Semble as the speed bottleneck:** the 11 logged `reviewer-context` queries took roughly **0.4–0.6 seconds each**, versus multi-minute reviewer attempts.

## Cost Optimizations

1. **Measure and remove demonstrably redundant Gemini context; quality risk high if content is removed indiscriminately.** The **21** logged `google/gemini-3.8-flash` calls used **200,595,820 total tokens**, including **21,768,628 prompt** and **178,169,193 cache-read** tokens, in the deep-dived reviews. Add per-model/per-pass input-component byte or token counts—diff, comments, check-runs, memory, Semble, and repeated ledger—without logging their contents. Remove only components proven duplicated or irrelevant by output-parity tests. **Scenario, not forecast:** eliminating 10% of that model’s logged prompt tokens would save about **2.18 million prompt tokens** in this sample; cached-token pricing and actual redundancy are unknown.

2. **Bound prompt growth on large reviews; quality risk medium.** `CONTEXT_BUDGET_WARN` appeared **four times**: run **36912230201** at **160,849/200,000** tokens; **36915996909** twice at **186,069** and **186,516/200,000**; and **36927252541** at **166,772/200,000**. Measure repeated context before trimming it, keep required findings and protected-file context, and compare review outcomes. Across instrumented runs, a hypothetical 10% reduction in the **40.96 million** logged prompt tokens would be **4.10 million** tokens; this overlaps the model opportunity above and is **not additive**.

3. **Avoid full reruns for format-only failures; quality risk low if bounded and fail-closed.** Failed run **36920824276** consumed **826,384 logged tokens** over two model calls before both reviewer passes failed to parse. Record a specific `malformed_structured_output` class and test a single bounded format-repair attempt against a fresh full rerun. Keep “all reviewers failed” terminal on unsuccessful repair. **Potential saving:** up to one **826,384-token** run per prevented recurrence, offset by repair tokens; recurrence frequency is unknown.

**Model policy:** the ten long reviews were logged as full panels on protected paths; do **not** broadly downgrade their six reviewers or two-pass reasoning to save cost. Test any lower-cost routing only on eligible, non-protected changes with quality comparison. Semble logged **11 queries, 156,584 bytes, and zero fallbacks**; whether its context replaces more prompt expansion than it adds is unmeasured. Serena logged **zero calls** and was disabled in recent review runs, so no replacement benefit or noisy-response cost can be attributed to it.

## Reliability Improvements

1. **Fix failure classification and terminal-summary consistency — highest priority; rollback-safe as an additive logging change.** In review run **36920824276**, `review / codex-agent` → **Run reviewer models** failed after both passes returned malformed OpenCode output; both had zero successful reviewers. Yet the final summary called the run `clean_review_no_commit`, and the failure headline lacked a first error. Emit a sanitized parser reason, pass, slot, output byte count, and job exit status; add an explicit terminal outcome to `REVIEW_AUTOFIX_RUN_SUMMARY_V1`. Assert in tests that a failed reviewer job cannot be classified clean. Keep existing fingerprint fields while validating downstream consumers; do not expose raw model output. **Expected impact:** correct diagnosis and alert routing for this observed failure class; the overall failure-rate reduction from a later repair retry is unmeasured.

2. **Distinguish conflict recovery *attempted* from *resolved* — low-risk logging, conservative behavior.** In poller **36947018303**, PR **#5360** returned non-retryable HTTP **422** on `update-branch`; the dispatched review **36947421427** skipped at its gate. Emit one linked outcome with poller run, PR/head identifier, update result, dispatch run, gate reason, and subsequent resolution status. Do not bypass `claude_fixer_awaiting_session`; leave unresolved conflicts for their owning session or a visible human-gated outcome. **Expected impact:** prevents ineffective dispatches from masquerading as fixes; actual failure-rate effect awaits counts.

3. **Keep fail-safe waits and preservation visible — low risk.** Reviews **36917073052** and **36911906436** each reached `CHECK_RUNS_WAIT_TIMEOUT` after **300 seconds**, with **one** and **two** check-runs still in progress, then proceeded with snapshots. Cancel-on-close run **36945007826** preserved **three** active runs lacking PR linkage. Log snapshot freshness, poll/request/retry totals, unresolved check categories, and preservation reasons; retain unknown-as-unknown and do **not** auto-cancel unlinked runs or declare pending checks passed. **Expected impact:** attributes avoidable waits and prevents unsafe “fixes,” with no immediate claimed failure-rate reduction.

Across parsed runs, `BREAK_GLASS` was **0**. The **four** context warnings indicate prompt-size risk, not demonstrated policy or rubric pressure. Semble had **0 contract-test and 0 runtime fallbacks**. Poller **36947018303** showed Semble unavailable *before* installation/index build and available afterwards; that sequence is not evidence of a masked runtime outage. Serena had **0 probes and 0 fallbacks**, alongside `SERENA_ENABLED: false`; absence of probes cannot establish server availability. Collect full diagnostics for the two zero-duration `claude_twin_sync` `action_required` runs before assigning a root cause.

## AI Memory Health

In unique deep-dive logs, **11 reviewer `retrieve` events all selected records** (**100% observed hit rate**), averaging **1,388 estimated tokens** against a **1,400-token** budget (**99.1%**). All **11** used `keyword_method=llm`; `plain` and `none` were **0**. Run **36920824276**, for example, selected **24 records / 1,395 tokens**. The same logs contained **10 `record-candidate`** and **24 `record-run-event`** events; no observed event had `enabled=false`, `fail_open=true`, or more than one push attempt. No `finalize-task`, `promote`, `compact`, or processed-command event was found **in this deep-dive set**, not necessarily across the pipeline.

Poller **36947018303** spent approximately **59 seconds** in **Record poll run start** and **56 seconds** in **Record poll run end**; each logged a successful one-attempt memory push. Add `duration_ms` split into read/commit/push and `records_considered` to existing memory telemetry, without recording content. **Expected impact:** identifies whether roughly **115 seconds** of this poll is reducible while retaining its audit events. The nearly full retrieval budget warrants a relevance check, but trimming memory blindly offers little demonstrated savings.

## GH API Call Audit

| Workflow / step | Observed request signal | Exact change and expected effect |
|---|---|---|
| `orchestrate_poll`, **Process each tracking issue**, run **36947018303** | **100** PR candidates; **63** draft `claude/*` skips occur after the code’s per-candidate REST read; one `update-branch` **422** and one review dispatch. Actual physical request total is not emitted. | Prefilter drafts from the existing list, retaining REST on uncertainty: **at least 63 logical PR reads** removable in this run. Log endpoint-template counts, attempts, elapsed time, and list truncation; this also reduces rate-limit exposure. |
| `review_autofix`, **Collect PR check-run failures**, runs **36917073052**, **36911906436** | Two **300-second** wait timeouts; polling uses paginated, retried check-run snapshots, but request/page counts are absent. | Log logical polls, pagination pages, physical attempts, retry sleep, and snapshot reuse. Reuse a valid same-head snapshot within the job; **call reduction cannot be quantified yet**. |
| `cancel_on_pr_close`, run **36945007826** | **Three** unlinked active runs preserved; lookup counts and linkage failure categories absent. | Log batched-lookup coverage and unknown-association counts; preserve unknown runs. No safe call or cancellation saving is established. |

This follows `CLAUDE.md` **§15**: extend existing reads, prefer batched GraphQL for per-item data, and reuse cycle-local caches such as the poller’s documented helpers rather than adding new loop calls. No rate-limit event was confirmed in the inspected poller log; its HTTP **422** is a merge conflict, **not** a rate-limit retry. Add a per-job `GH_API_SUMMARY` with endpoint *templates*, logical calls, physical attempts, `403/429/422` counts, retry sleep, and cache/batch hits—never tokens or response bodies.

## Prompt Cache & Memory System

The assembled aggregate `cache_hit_rate` is **null**, correctly: `scripts/cost_audit.py` suppresses that rate when any usage call is unavailable, and **14/139** calls were unavailable. The five calculable run rates were **57.9%** (failed **36920824276**) and **78.6%, 83.2%, 83.7%, 86.9%** on successful slow reviews. Logged totals include **269.35 million cache-read** and **5.23 million cache-write** tokens; cache-read share of all reported tokens is **84.9%**, **not** the missing aggregate hit-rate metric.

First log cache eligibility, stable-prefix fingerprint, dynamic-section placement, and per-call read/write availability by model and pass. Inference to test: moving changing PR/check-run/ledger material after a stable instruction prefix may reduce fragmentation; current logs do **not** establish prefix instability as the cause of run **36920824276**’s lower rate. Keep prompt content unchanged during an ordering-only trial and compare hit rate, latency, and review quality. **Conditional impact:** a 5% reduction in the observed **40.96 million** prompt tokens would be about **2.05 million tokens** in this instrumented sample; no latency or reliability improvement is established. The three warning-bearing reviews above should be the first prompt-size checks. Memory retrieval is effective in its 11 sampled events, but its near-full budget needs relevance telemetry rather than an arbitrary lower cap.

## Orchestrator Health

All **25** `orchestrate_poll` runs succeeded, but their **p50/p95 durations were 484/547.8 seconds**. In poller **36947018303**, tracking issue **#4139** remained active, security-pass fix issue **#4664** remained in progress, and `STALL_SKIP issue=4664 reason=human_gated_latch label=ai:scope-blocked` preserved a human gate. That is a deliberate stop, not evidence that a wave or judge failed. Clarify, plan, implement, and clarify-respond runs in this window were overwhelmingly **skipped**, so their short durations do not measure productive phase cadence.

The observable pain point is recovery attribution: the poller counted PR **#5360** as one conflict-sweep fix after dispatch, while review **36947421427** skipped. Add correlated `wave_before/after`, `deferral_reason`, `judge_cycle_started/completed`, `conflict_dispatch_outcome`, and age-in-state counters using existing state and logs. **Expected impact:** distinguish progress, safe deferral, and a same-head no-op loop without relaxing human or review gates. Judge-cycle frequency and merge/conflict-heal retry rates remain unmeasured.

## Pipeline Flow Bottlenecks

| Stage / overhead | Evidence | Next diagnostic or safe fix |
|---|---|---|
| Clarify → plan → implement | **87 / 87 / 88** runs respectively, all skipped in this window. | Log dispatch-to-active-phase transitions; do not infer fast implementation from 1-second skipped runs. |
| Review/autofix compute and retry | Ten full-panel reviews took **1,879–3,657 seconds**; repeated reviewer stall kills occurred in **36912230201** and **36915996909**. | Record per-pass and per-slot compute, idle, retry, and failback time; investigate long-tail model slots before changing coverage. |
| Validation waiting | **36917073052** and **36911906436** waited to the **300-second** check-run limit. CI p50 was **560 seconds** across five runs. | Log peer-check identities/status categories and snapshot request counts; retain pending-check safeguards. |
| Polling and merge/conflict overhead | Poller **36947018303**: **184.5-second** processing step, including a **59.4-second** conflict sweep; PR **#5360** update returned **422** then its dispatched review skipped. | Prefilter draft reads and track resolution after dispatch, not merely dispatch success. |
| Queueing | Run-level `created_at` and `run_started_at` do not expose a usable job-queue distribution; the failed review’s system log shows a runner wait but not a population estimate. | Collect job queued/start timestamps before recommending runner or concurrency changes. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Bottlenecks:** long protected-path two-pass reviews (**36912230201**, **36915996909**); repeated roughly eight-minute pollers; check-run waits and the 100-PR conflict sweep. **Failure modes:** malformed reviewer output with contradictory clean summary (**36920824276**); conflict dispatch followed by an awaiting-session gate skip (**36947018303 → 36947421427**); incomplete attribution for two `claude_twin_sync` `action_required` runs. **Highest-cost driver:** the Gemini reviewer slot’s **200.6 million** logged total tokens in the deep-dive sample.

**Top three actions, in order:** (1) add truthful, sanitized reviewer failure and terminal-outcome logging, then test the all-reviewers-failed path; (2) prefilter draft PRs before conflict-sweep REST reads and log request/truncation counts; (3) instrument per-model prompt components, cache eligibility, and reviewer stall elapsed time before trimming context or altering models. These preserve review and human gates while targeting the strongest observed gaps.

## Metrics Appendix

**Window:** runs created **October 1, 2026, 18:40:58 UTC–October 2, 2026, 00:43:29 UTC**; one repository. Rates below include skipped runs unless stated otherwise.

| Workflow family | Runs | Success | Failure | Other outcome | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|
| **All families** | **1,000** | **643 (64.3%)** | **1 (0.1%)** | 353 skipped; 2 action-required; 1 cancelled | **10 / 279 s** |
| `review_autofix` | 552 | 550 | 1 | 1 cancelled | 12 / 94.9 s |
| `orchestrate_poll` | 25 | 25 | 0 | 0 | 484 / 547.8 s |
| `ci` | 5 | 5 | 0 | 0 | 560 / 797.4 s |
| `copilot_pull_request_reviewer` | 17 | 17 | 0 | 0 | 231 / 426 s |
| `cancel_on_pr_close` | 29 | 29 | 0 | 0 | 16 / 69.6 s |
| `clarify` / `plan` / `implement` / `orchestrate_clarify_respond` | 349 | 0 | 0 | 349 skipped | Each family p50 **1 s**; p95 **9–10.7 s** |
| `claude_twin_sync` | 2 | 0 | 0 | 2 action-required | 0 / 0 s |
| Remaining families | 21 | — | — | — | Inspect by family before comparing percentiles |

| Parsed cost/review metric | Assembled-context value | Coverage or interpretation |
|---|---:|---|
| Runs with parsed log telemetry | **115 / 1,000** | Collector `summary.json` separately reports **30** downloaded-log telemetry samples. |
| OpenRouter calls; usable / unavailable usage | **139; 125 / 14** | Logged calls, not all pipeline model usage. |
| Prompt / completion / total tokens | **40,958,680 / 1,732,962 / 317,265,712** | Total includes cached input. |
| Cache creation / read tokens; aggregate `cache_hit_rate` | **5,225,590 / 269,352,513; null** | Five run-level rates available: **57.9–86.9%**. |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | **11,000 / 3,333,800** | Assembled **115-sample** values; collector’s **30-sample** values are **13,500 / 3,564,490 ms**. These are run-duration samples, not model-call latency. |
| `break_glass_count` / `context_budget_warn_count` | **0 / 4** | Warnings belong to **three** review runs. |
| AI memory deep-dive retrieves | **11/11 hits; mean 1,388/1,400 tokens** | `llm=11`, `plain=0`, `none=0`; sampled reviews only. |

| GH API signal | Measured count | Rate-limit interpretation |
|---|---:|---|
| Poller **36947018303**, conflict-sweep candidates / draft skips after REST | **100 / 63** | At least **63** avoidable logical reads; physical attempts not logged. |
| Same poller, failed `update-branch` / review dispatch | **1 HTTP 422 / 1 dispatch** | Merge conflict, not a confirmed rate-limit event. |
| Review check-run wait timeouts | **2 runs**, **300 s each** | Poll/page/retry request totals unavailable. |
| Poller rate-limit warnings in inspected process step | **0 observed** | No window-wide rate-limit count exists. |

| MCP target / tool | Queries | Logged bytes | Fallbacks | `probe_ok` / `probe_failed` / `probe_skipped` |
|---|---:|---:|---:|---:|
| Semble `reviewer-context` | **11**, **12 chunks each** | **156,584 query bytes** (~14,235/query) | **0** runtime; **0** contract-test | **0 / 0 / 0** — probe availability not measured |
| Serena — no target or tool observed | **0**; tool calls **0** | **0 response bytes** | **0** | **0 / 0 / 0** — disabled in cited recent reviews |
| **Other MCP servers observed** | **None** | — | — | — |

**Collection gaps:** no population-wide GH API endpoint/attempt counters, job queue distribution, judge-cycle or merged-PR counts, or outcomes linked from conflict dispatch to resolution. Add those fields to the existing collector and structured job summaries before assigning population-wide savings or stall rates.

## Deep Audit — Workflows & Scripts (2026-10-02)

### Section 1: Bug & Correctness Sweep

The local sweep covered 52 `.github/workflows/*.yml` files and 160 top-level `scripts/*.sh` and `scripts/*.py` files. YAML parsing, Python AST parsing, and `bash -n` found no syntax failures. The findings below concern behavior, not syntax.

- **SEC-001** — **High** · `security` · `.github/workflows/workflow-log-analysis.yml:1162-1169`; also `.github/workflows/mark-stable.yml:44-52` and `.github/workflows/test-and-mark-stable.yml:163-181`. **Description:** These `run:` blocks insert `github.ref_name` directly into shell source before assigning or validating it. Inference: a dispatchable ref containing shell syntax could execute that syntax before the ref check; the report-push workflow also checks out with a credentialed token. Whether repository ref and dispatch permissions permit an exploitable name needs confirmation. **[NEEDS VERIFICATION]** **Recommended fix:** Pass the ref through step `env:` and read `$GITHUB_REF_NAME` or the env-bound value in Bash, as the later deep-audit sync step does at `.github/workflows/workflow-log-analysis.yml:1459-1469`. Keep the `stable` checks.

- **BUG-001** — **Medium** · `bug` · `scripts/claude_issue_queue_watchdog.sh:60-74`. **Description:** The hourly watchdog makes one non-paginated `issues?labels=ai:claude-issue-queue&state=open&per_page=100` read, then treats that response as the full queue. If more than 100 matching issues are open, items beyond the first page are never assessed for staleness. The parser only examines issues it receives (`scripts/claude_issue_route.py:1010-1036`). **Recommended fix:** Fetch all pages with `gh_retry gh api --paginate --slurp`, flatten and validate the arrays before `queue-stale`, and log the page and issue counts.

- **SEC-002** — **Medium** · `security` · `scripts/gh_helpers.sh:648-659`. **Description:** When a successful API command returns invalid JSON, `gh_api_json_to_file` prints the first 50 lines of its *raw* response to the Actions log. Its callers include issue and PR reads (`scripts/workflow_failure_heal_report.sh:109-116`; `scripts/check_failure_triage.sh:149-154`). Inference: a malformed or partial response could expose private response content in logs. **[NEEDS VERIFICATION]** **Recommended fix:** Log endpoint class, byte count and a digest or sanitized error class—not response content—and test the malformed-response path with sensitive fixture text.

- **BUG-002** — **Low** · `bug` · `scripts/review_merge_train.sh:275-291`. **Description:** `_mt_upsert_comment` ends both its PATCH and POST branches with `|| true`; it therefore reports success even when the comment write failed. The release caller’s warning-on-error path at lines 484-486 cannot observe that failure. **Recommended fix:** Return the write’s real status; let callers retain their existing fail-open handling and emit the warning.

The existing report already covers the poller’s draft-PR prefilter and the observed conflict-dispatch/skip outcome. They are not repeated as new findings. The current tree also sets `AUTOFIX_REVIEWERS_FAILED` before its summary step (`.github/workflows/review_autofix.yml:5066-5084`; `scripts/review_autofix_step_iteration_summary.sh:709-718`), so the report’s historical clean-summary incident is not asserted as an unchanged code defect.

### Section 2: GitHub API Call Redundancy Audit

Counts below are **logical calls on the stated path**; pagination and retries can increase physical requests. Candidates with state or pagination trade-offs require parity tests before replacement.

- **API-001** — **Medium** · `api-redundancy` · `scripts/orchestrate_poll_process.sh:10432-10444`. **Description:** When `final_pr_json_snapshot` is not for the recorded final PR, the same `pulls/${final_pr}` endpoint is fetched separately for `.state` and `.merged_at != null`: **2 reads → 1**. **Recommended fix:** Fetch one PR object and derive both fields locally, retaining the existing snapshot-first branch and unknown-value behavior. This extends the file’s existing snapshot reuse pattern.

- **API-002** — **Medium** · `api-redundancy` · `scripts/orchestrate_poll_process.sh:17704-17723,18212-18218,23219-23227`. **Description:** The tracking-issue loop resets `DEFAULT_BRANCH_TRACKING` and can fetch repository `.default_branch` for each eligible issue; the standalone conflict sweep fetches that same repository field again. Current count is **at least one per eligible tracking issue plus one sweep read** when those paths execute; a successful poll-cycle cache would make it **1 shared read**. **Recommended fix:** Cache a confirmed default branch once per poll process, distinguish an API failure from a confirmed value, and retain the legacy read on a cache miss. Follow the cycle-local cache contract used by `_candidate_details_json` and `STALL_MANAGED_LINKED_PR_CACHE`.

- **BATCH-001** — **Medium** · `api-batching` · `scripts/review_merge_train.sh:119-140,199-233,429-457`. **Description:** The merge-train gate/release loop obtains files through one paginated REST `pulls/{n}/files` read per distinct examined PR. The gate can examine up to `MT_MAX_OLDER_PRS` older PRs, default 20; the release loop reuses its per-process PR-file cache. For **F distinct PRs**, the first-page path is **F logical reads → approximately `ceil(F/25)` aliased GraphQL reads**. Extra file pages would still need pagination. **Recommended fix:** Add a batch prefetch using the aliased-query pattern of `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh:14686-14790`; preserve `_mt_pr_files_into` as the per-PR fallback for missing results, GraphQL failure, or additional pages. **[NEEDS VERIFICATION]** for GraphQL file-page parity with the current REST output.

- **API-003** — **Low** · `api-redundancy` · `scripts/review_merge_train.sh:257-291,480-486`. **Description:** An `_mt_upsert_comment` call without an already-known ID lists comments to find the marker and then fetches the matched comment again for its body: **2 reads → 1** for an existing marker. **Recommended fix:** Have the marker-list lookup retain both ID and body and pass them to the upsert; retain the single-comment fallback when only an ID is supplied. This extends the existing marker-list call, not the poller’s issue-detail batch.

- **API-004** — **Medium** · `api-redundancy` · `scripts/gh_helpers.sh:636-684,704-748`. **Description:** `gh_api_json_to_file` retries command failures without consulting `_is_gh_permanent_failure`; `curl_gh_api` likewise backs off on non-rate-limit HTTP failures. With the default maximum, a deterministic 404 or 422 can cost **5 attempts → 1** in either wrapper; the JSON helper also sleeps after its final failed attempt. **Recommended fix:** Reuse the permanent-failure classification already applied by `gh_retry` and `gh_retry_to_file` (`scripts/gh_helpers.sh:91-94,452-515,531-576`), while retaining retries for invalid successful JSON and transient or rate-limit failures. Test permanent and transient statuses separately.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — **Medium** · `duplication` · `.github/workflows/clarify.yml:218-255`, `.github/workflows/plan.yml:281-318`, `.github/workflows/orchestrate.yml:358-395`, `.github/workflows/orchestrate_clarify_respond.yml:281-318`; related staging in `.github/workflows/validate.yml:360-388`. **Description:** These workflows repeat the trusted support-checkout, per-file fallback, required-file validation and installation loop, with phase-specific asset lists. **Recommended fix:** Extend `scripts/stage_workflow_support.sh` with a phase-scoped entry point such as `stage_phase_support <phase> <verified-support-ref> <destination>`. Fetch and verify that helper from the immutable support checkout *before* invoking it; keep each phase’s required/optional asset contract and the existing review-specific staging behavior. Update the listed callers and their contract tests.

- **DUP-002** — **Low** · `duplication` · `.github/workflows/review_autofix.yml:1618-1643,1848-1877`. **Description:** Two jobs carry near-identical inline `gh_retry` implementations, including temporary stdout buffering, although `scripts/gh_helpers.sh:452-515` owns the full helper. **Recommended fix:** Load the helper from the verified support checkout in both early jobs and call its existing `gh_retry <command> [args...]` interface. Preserve their pre-bootstrap behavior when verified support is unavailable; test that neither path executes a PR-head helper.

No pair of complete workflows was established to be more than 70% identical; similar internal wrappers have distinct event and job contracts.

### Section 4: Expression Size Limit Risk Assessment

The YAML-node sweep measured **779 `run:` blocks**, of which **225** contain `${{ }}`. The counts below are static rendered-script-size proxies: runtime substitutions can change them. Individual `${{ }}` expressions were also measured separately; the largest source expression was **185 characters**. Five `run:` bodies over 18,000 characters contained **no** `${{ }}` and were excluded as requested.

- **EXPR-001** — **Medium** · `expression-limit` · `.github/workflows/implement.yml:986-1343`. **Description:** “Stage workflow support files” is **16,985 characters** of YAML-decoded `run:` source with **3** interpolations: approximately **4,015 characters** of static-body headroom to the requested 21,000-character assessment threshold. Its largest individual interpolation is **59 characters**; the body measurement does **not** establish that an individual GitHub expression is near rejection. **Recommended fix:** Move the staging body into a verified external script under `scripts/`, pass expression values through step `env:`, and register the script in the phase’s support staging and contract tests. Do not source it from the PR worktree.

No other interpolated body reached 15,000 characters; the largest measured `if:` was **859 characters** (`.github/workflows/internal-clarify.yml:15-17`). No workflow reached the requested **800 KB** file-size flag. A separate, stricter repository contract matters here: `agents.md:620-645` and `tests/test_workflow_file_size_limit.py:1-45` document a measured **512,000-byte** execution limit and a **480,000-byte** CI guard. `.github/workflows/review_autofix.yml` is **458,436 bytes**, leaving **21,564 bytes** to that guard.

### Section 5: Cross-Cutting Concerns

- **DEAD-001** — **Low** · `dead-code` · `scripts/review_issue_ledger.sh:866-917`. **Description:** `CURRENT_FLOOR` is declared and populated, but the repository-script search found no read of that array. ShellCheck also reports it as apparently unused. **Recommended fix:** Remove the declaration and assignment if no sourced caller consumes them; pin unchanged ledger output in a test before removal. **[NEEDS VERIFICATION]** for external sourcing.

- **DEBT-001** — **Low** · `tech-debt` · `.github/workflows/ci.yml:259-277,365-369`; mirrored release check at `.github/workflows/test-and-mark-stable.yml:4225-4229`. **Description:** CI disables actionlint’s inline-shell ShellCheck integration and checks script files only at `--severity=error`. The full warning-level script sweep reported warnings, including SC2034 in `scripts/review_issue_ledger.sh:866-917` and SC2043 in `scripts/stage_workflow_support.sh:138,198`; neither warning-level script regressions nor inline-block warnings fail these gates. Some warnings are intentional or analyzer artifacts and should not be promoted indiscriminately. **Recommended fix:** Add a changed-lines warning report with a reviewed baseline, then resolve or explicitly justify warnings by code; keep the current error-level release gate during rollout.

No `TODO`, `FIXME`, or `HACK` marker was found in the scoped workflow and script files. The shellcheck sweep did **not** establish an actionable unquoted-variable defect in the sampled call paths.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | SEC-001 |
| Medium | 8 | BUG-001, SEC-002, API-001, API-002, API-004, BATCH-001, DUP-001, EXPR-001 |
| Low | 5 | BUG-002, API-003, DUP-002, DEAD-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

Estimates are prospective and overlap; no files were changed during this audit.

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 3 workflows, plus tests | Medium |
| API call optimization | 3 scripts, plus tests | Large |
| Code modularization | 6 workflows and 2 scripts, plus tests | Large |
| Expression size reduction | 1 workflow and at least 1 support script, plus tests | Medium |
| Medium/Low fixes | 5 scripts and 2 workflow gates, plus tests | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-10-02)

### Safety Tag Legend

`SAFE_TO_MERGE` is authorized for implementation without further review. `NEEDS_VERIFICATION` requires the stated checks first. `RISKY_SKIP` must not be auto-implemented because a specified safety trigger applies.

### Consolidation Candidates (MERGE-###)

- **MERGE-001 — RISKY_SKIP** · `.github/workflows/clarify.yml:586-587` and `.github/workflows/clarify.yml:589-606` (`Fetch issue comments` step). **Current call count:** 2 logical reads when semantic caching is enabled; **proposed:** 1 on successful full-history retrieval, with a separate fallback read on failure. **Endpoint:** `GET /repos/{owner}/{repo}/issues/{issue_number}/comments`, first with `sort=created&direction=asc&per_page=50`, then with the same filters, `per_page=100` and `--paginate --slurp`. **Evidence:** The first response supplies bounded prompt context; the second fetches those comments again as part of full thread history. **Proposed fix:** In `Fetch issue comments`, derive the first 50 comments for `ISSUE_COMMENTS_FILE` from a successful full-history response, while retaining the bounded read when caching is disabled or full-history retrieval fails. **Safety rationale:** The second call implements pagination, and the current bounded read is mandatory while the full-history read fails open; changing their order or failure behavior needs manual review. **Downstream signal:** Do not auto-implement. Manually test 0, 50, 51, and more than 100 comments, including later-page failure and comments arriving between reads; preserve prompt bounds and the existing cache-bypass behavior.

### Redundant Re-Fetch (REUSE-###)

No findings.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: RISKY_SKIP — The calls are in `orchestrate_poll_process.sh`, which explicitly defends against upstream races; preserve snapshot-first and unknown-value behavior.
- API-002: RISKY_SKIP — A poll-cycle cache in `orchestrate_poll_process.sh` needs manual validation of cache misses and changed default-branch state.
- BATCH-001: RISKY_SKIP — The existing PR-files calls are paginated; GraphQL batching needs file-page parity and fallback review.
- API-003: RISKY_SKIP — The marker lookup is paginated; retaining its body must preserve page selection and lookup-failure behavior.
- API-004: RISKY_SKIP — This changes retry-loop behavior, including rate-limit handling, and is not an authorized automatic consolidation.

### Summary Counts

Counts cover net-new findings; Deep Audit cross-references are excluded.

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 0 | — |
| RISKY_SKIP | 1 | MERGE-001 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
