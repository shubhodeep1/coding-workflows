## Executive Summary

- **Move the prompt-budget check earlier.** CI run `36411570320` failed after 1,326 seconds because `prompts/mode-judge-review-blocked.txt` had 264 lines against a 250-line limit. Running the existing check immediately after checkout could save **up to ~21 minutes on a similar failure**, with no expected green-run speedup. **Confidence: high.**
- **Review execution dominates active review latency.** In review/autofix run `36416865588`, `Run reviewer models` took 2,345 of 2,743 seconds; the `minimax/minimax-m3` slot encountered repeated stall guards across two passes. Trial a shorter second-attempt stall cap while retaining the existing fallback and reviewer-quorum rules. **Potential impact: ~6–12 minutes on a similarly stalled run; confidence: medium.**
- **Published cost totals are inflated by duplicate log views.** Run `36416865588` appears in both combined and split step logs with slightly different subsecond timestamps. Counting its primary log once changes the window’s reported OpenRouter total from **33.63 million to 26.51 million tokens** and calls from **61 to 45**. This is an **accuracy fix, not a usage saving**. **Confidence: high.**
- **Semble alarms are test noise in this sample.** The published 44 `SEMBLE_FALLBACK` events reduce to 32 distinct events; all are CI contract tests deliberately targeting a missing executable, not observed runtime outages. Three distinct `reviewer-context` queries had no observed runtime fallback. **Impact: fewer false availability alarms; confidence: high for inspected logs.**
- **GitHub API and orchestration diagnoses lack call-site evidence.** No per-endpoint call counts, cache-hit counts, or rate-limit headers were available for the 12 poller runs or the 15-candidate all-skipped sweep in run `36423773471`. Add bounded, structured counters before changing batching or poll frequency. **Impact: prevents speculative changes; confidence: high.**

## Speed Optimizations

1. **Critical path — bound repeated reviewer stalls.** Run `36416865588` spent **2,345 seconds** in `review_codex-agent / Run reviewer models` (85.5% of its run). Its second `minimax/minimax-m3` attempts followed earlier failures and ended in `stall_guard` after roughly 11 minutes in each pass. **Root cause:** long retries of an already troubled slot. **Change:** trial a configurable five-minute cap *only on a second attempt after a stall guard*, then use the existing failback; log slot attempt duration, termination reason, and whether quorum was met. **Estimated saving:** roughly six minutes per affected second attempt, up to ~12 minutes in a run with this pattern; not a prediction for ordinary reviews. **Risk: medium**—compare review findings and quorum before retaining the cap.
2. **Critical path — measure and rebalance CI test shards.** In successful CI run `36417072376`, `lint / Orchestrate poll process unit tests` took **1,293 seconds**, 48% of its 2,706-second run, despite the existing four-shard configuration. **Root cause:** expensive poller subprocess tests; shard imbalance is *unverified*. **Change:** emit shard test count, elapsed time, slowest test durations, and completion reason, then rebalance only if a shard is consistently last. **Estimated saving:** unknown until shard timings are collected; a **0–200-second trial target**, not a measured gain. **Risk: low** for logging, medium for redistributing tests.
3. **Critical path — reduce serial memory-write time.** In review run `36416865588`, memory start-event, candidate, and completion steps took **56.9, 57.6, and 57.1 seconds**, respectively, with one push attempt each. **Root cause inference:** separate memory operations incur substantial step-level overhead; the logs do not isolate Git push time. **Change:** log operation versus push duration; if semantics permit, commit the candidate and completion event in one idempotent, fail-open write without delaying the start event. **Estimated saving:** up to ~57 seconds per qualifying active review. **Risk: medium** until independent failure behavior is verified.
4. **Failure-path win — run the cheap prompt gate first.** The `Prompt tier budget` step failed run `36411570320` near its end; the local gate uses the standard library and currently appears after other CI work in `.github/workflows/ci.yml`. **Change:** place the unchanged gate after checkout/Python setup, without relaxing its limit. **Estimated saving:** up to ~21 minutes per repeat; approximately zero on passing runs. **Risk: low.** The currently inspected prompt is at 250 lines, so this is prevention, not evidence of an ongoing failure.
5. **Micro-optimization — avoid work for ineligible sweep candidates.** Review sweep `36423773471` spent about **29 seconds** processing 15 candidates and skipping all 15. **Change:** log time spent fetching, eligibility-checking, and dispatching; reuse already-fetched eligibility state to short-circuit where possible. **Estimated saving:** at most the observed 29 seconds for an equivalent all-skipped sweep, likely less. **Risk: low** if dispatch eligibility remains unchanged.

## Cost Optimizations

1. **First correct the accounting baseline.** Across the three token-bearing review runs (`36405519570`, `36407481306`, `36416865588`), the collector reports **61 calls and 33,634,671 tokens**; counting combined logs once gives **45 calls and 26,513,170 tokens**. The collector’s structured-event dedupe compares full timestamped lines, while combined and split copies in run `36416865588` differ by microseconds. **Change:** match structured events within their job/step hierarchy using normalized event content and bounded timestamp matching; retain genuinely repeated events and add regression tests. **Estimated real token/dollar saving: zero**; measured reporting correction: **7,121,501 tokens**. **Quality risk: none** to review behavior.
2. **Target costly reviewer retries, not blanket model downgrades.** The three active reviews used **14, 15, and 16 distinct model calls**. Run `36416865588` records `minimax/minimax-m3` stall, server-error, failback, and model-provider rate-limit classifications; its final slot failed in both passes. The workflow already has review tiers and cheaper-reasoning retry settings. **Change:** measure tokens and elapsed time by slot/attempt and pilot the bounded retry change above; use existing tier routing for smaller diffs only after logging eligibility and comparing findings. **Estimated saving:** up to the calls and elapsed time of avoided stalled attempts; their token and dollar cost cannot be established because usage is unavailable for **6 of 45 distinct calls**, and prices are absent. **Quality risk: medium**; preserve reviewer quorum and compare outcomes. Do not lower reasoning globally on this evidence.
3. **Measure prompt components before trimming context.** Distinct `CONTEXT_BUDGET_WARN` events occurred once in `36405519570` and twice in `36416865588`, at **155,293; 161,511; and 161,934 prompt tokens** against a 200,000-token window. **Root cause of excess is unmeasured.** Log byte/token estimates separately for instructions, diff, Semble output, memory, and prior-round material; remove verified duplication while retaining relevant evidence. **Estimated saving:** unquantifiable now; a 5% reduction in the **8.50 million recorded uncached prompt tokens** would represent ~425,000 tokens *if* attainable. **Quality risk: medium** if evidence is pruned without a review-quality comparison.
4. **Keep MCP and skipped-run costs in perspective.** Three distinct `SEMBLE_QUERY target=reviewer-context` events supplied **40,912 logged bytes**, 12 chunks each, without observed runtime fallback. There is no before/after prompt-size baseline, so reduced prompt expansion is **not established**; log source bytes and bytes actually inserted into prompts. Serena had **zero calls or response bytes**, and recent review summaries report it disabled, so replacement of downstream work cannot be assessed. Awaiting-session gate runs such as `36424279556` and `36424276654` used no recorded model tokens; optimizing them chiefly saves Actions time, not measured model spend.

## Reliability Improvements

1. **Preflight prompt invariants without weakening them.** The sole recorded failed run, CI `36411570320`, ended at `lint / Prompt tier budget`: **264 lines exceeded the DEFAULT limit of 250**. **Category:** deterministic validation, surfaced late. **Fix:** move the same gate earlier and emit prompt name, tier, measured lines, limit, and elapsed-before-failure. **Expected impact:** similar violations fail promptly rather than after long CI work; the one observed failure does not establish a recurring failure rate. **Rollback:** restore step order; retain the limit.
2. **Make reviewer fallback outcomes explicit.** In `36416865588`, `minimax/minimax-m3` exhausted a slot after classified failures including `stall_guard`, `server_error`, and a **model-provider** `rate_limit`; `x-ai/grok-4.20` also had a non-retryable failure. **Category:** model execution/retry, not proven GitHub API throttling. **Fix:** emit per-pass slot attempts, cumulative wait, fallback model, terminal reason, and quorum result; trial the bounded second-attempt cap. **Expected impact:** less exposure to repeated stalls, with failure-rate impact unmeasured. **Rollback/fail-open:** restore the prior cap and retain existing fallback/quorum behavior.
3. **Classify cancellations separately from failed steps.** Review/autofix had **6 cancellations**; runs `36424344199`, `36423550826`, and `36423386421` were cancelled while `Free disk space` was the last observed step. That does **not** show disk cleanup failed. **Category:** cancellation provenance gap. **Fix:** log cancellation/concurrency reason, last started step, and whether review execution began; never auto-retry solely from the last-step label. **Expected impact:** fewer false failure diagnoses and avoidable reruns; direct failure-rate improvement unknown. **Rollback:** logging-only.
4. **Separate synthetic MCP fallback from availability.** The collector reports **44 Semble fallbacks**, all tagged `context=contract-test`; inspection yields **32 distinct** events at `target=overflow`, with **0 observed runtime fallbacks**. **Category:** telemetry classification/deduplication, not evidence of a broken rollout. **Fix:** deduplicate split logs and exclude contract-test events from production availability alerts, while retaining their test counts. No `SERENA_PROBE` was observed; do not interpret zero failures as a successful probe. **Expected impact:** removes these 32 synthetic events from runtime-alert consideration. **Rollback:** retain raw events alongside classified counts.
5. **Expose release retry causes.** Auto-release run `36424440238` succeeded in 20 seconds, but its `release-check` summary says `test-and-mark-stable.yml` already had **two failed attempts against a budget of three**. **Category:** downstream gate retry, cause unavailable. **Fix:** log target run conclusion, retry reason class, attempt, remaining budget, and terminal decision. **Expected impact:** earlier identification of repeat failures; no justified rerun-rate estimate. **Rollback:** logging-only.

`BREAK_GLASS` was **0** in collected telemetry. The **3 distinct** context warnings indicate prompt-size pressure, not demonstrated policy or rubric pressure.

## AI Memory Health

In the three inspected active review logs (`36405519570`, `36407481306`, `36416865588`), `AI_MEMORY_TELEMETRY` contains **3 `retrieve` operations: 3/3 hits (100%)**, selecting **25, 25, and 23 records**. Estimated context averaged **1,390 tokens against a 1,400-token budget** (99.3%); `keyword_method` was **`llm`: 3, `plain`: 0, `none`: 0**. No inspected retrieval returned zero records, reported `enabled: false`, or reported `fail_open: true`. Each run also logged a start event, candidate, and completion event with **one push attempt** apiece; no high push-retry count was observed.

**Recommendation:** retain retrieval—it is small beside the warned review prompts—but log retrieval duration and candidates considered, plus explicit `fail_open` and push-duration fields. The ~57-second memory steps in `36416865588` warrant separating operation time from push time. `finalize-task`, `promote`, `compact`, and processed-command operations were not present in these selected logs; sample their owning workflows before making system-wide memory-health claims.

## GH API Call Audit

**No defensible GitHub REST/GraphQL call-count or rate-limit total is available in this window.** Model-provider `rate_limit` in review run `36416865588` must not be counted as a GitHub event. CI dry-run label messages are likewise not production API calls.

| Candidate hotspot | Evidence | Smallest safe audit/change | Estimated calls saved |
|---|---|---|---|
| Review sweep | `36423773471`: 15 candidates, all skipped, ~29-second sweep; `36424235790`: 17 candidates, 11 dispatched, 4 active-skipped, 2 `skip_ai`-skipped | Emit fetch, eligibility, and dispatch call counts and elapsed times; reuse fetched candidate state before proposing batching | **Unknown**; zero if already cached |
| Orchestrator poll | 12 successful runs; p50 **390 seconds**; no poller step/API logs in the deep-dive set | Emit endpoint-template counts, retry counts, and hit/miss counts for `ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`, and `_candidate_details_json` | **Unknown** |
| Issue–PR status sync | 12 successful runs; p50 **96 seconds**, p95 **139.3 seconds**; no call-site breakdown | Count PR/issue lookups by endpoint template and cache outcome before changing the loop | **Unknown** |

Repository §14 in `unattended_system_instructions.md` already requires checking existing calls, preferring the batched GraphQL helpers `_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql`, reusing cycle-local caches, and failing open to the smallest legacy call on cache miss. Preserve those contracts. **Conditional illustration only:** replacing 15 verified per-candidate reads with one batch could avoid up to 14 requests; the sweep logs do *not* establish that 15 reads occur.

Add one redacted aggregate per job/step: endpoint **template**, calls, cached hits, retries, status classes, elapsed milliseconds, and rate-limit remaining/reset. Exclude response bodies, credentials, and raw query parameters. This would make call-count and rate-limit-risk reductions measurable without new infrastructure.

## Prompt Cache & Memory System

The corrected distinct-call sample records **17,583,921 cache-read tokens**, **8,499,611 prompt tokens**, and **0 reported cache-creation tokens**. Cache reads are **67.4% of those recorded input-token categories**, **not** a measured `cache_hit_rate`: that field is **null** because six distinct calls have unavailable usage. Zero recorded creation tokens also does not prove cache creation never occurs. In `36416865588`, reviewer-slot telemetry marks Gemini Flash Lite cache status `unsupported` while other inspected slots report `supported`; compare cache behavior by model rather than aggregating all slots as misses.

**Recommendation:** emit per-call usage availability, model, pass, eligible prefix hash/length, cache-read and creation tokens, and a reason when unsupported. Keep stable reviewer instructions ahead of variable PR/diff, Semble, and memory material **if a prefix comparison confirms fragmentation**; dynamic-prefix fragmentation is presently an inference, not a measured cause. Log component sizes to test whether prompt growth behind the three distinct context warnings is eroding reuse. Keep memory’s observed ~1,390-token retrieval intact unless component evidence shows low value. Token, latency, and reliability gains from prefix changes remain unquantified until cache-hit and prompt-component coverage improves.

## Orchestrator Health

The source-repo window contains **12 successful orchestrator-poller runs** (p50 **390 seconds**, p95 **460.9 seconds**) but no selected poller step logs. `plan` and `implement` each have **161 skipped runs and no successes in this window**; `workflow_failure_heal` has **36 skipped runs**. Skips are outcomes, **not evidence of stuck projects**. Review gate summaries for PRs `4546` and `4590`, among others, explicitly say `claude_fixer_awaiting_session`; they show waiting decisions but not eventual session completion.

**Recommendation:** emit one state-transition record per project tick with prior/new phase, wave, deferral reason, oldest-wait age, judge attempt/result, conflict-heal attempt, and terminal reason. Track *waiting-to-resumed time*, repeated same-state ticks, wave advancement, judge cycles, and conflict retries. Do not shorten poll intervals or force retries until those indicators distinguish useful waiting from a stall. No clarification-loop, merge-conflict, or terminal-state rate can be calculated from the supplied rows.

## Pipeline Flow Bottlenecks

| Flow component | Observed bottleneck | Queue / compute / retry / merge distinction | First action |
|---|---|---|---|
| Clarify → plan → implement | Clarify: **22 successes**, **151 skips**; plan and implement: **161 skips each** | Active clarify run `36423754260` took 201 seconds; these rows cannot reconstruct cross-run handoffs | Log correlated task and phase-transition timestamps, including skip reasons |
| Review/autofix | Family p50 **13 seconds** is dominated by quick gates; p95 **1,899 seconds**; active run `36416865588` spent **2,345 seconds** in reviewer models | Model compute/retry is observed; three ~57-second memory steps are additional step time | Bound measured stalls; time memory writes |
| Validate/CI | CI p50 **2,698 seconds**; run `36417072376` spent **1,293 seconds** in poller tests | Test compute is observed; run `36411570320` demonstrates late deterministic failure | Log shard timings; move prompt preflight early |
| Orchestrate/merge | Poller p50 **390 seconds**; release gate in `36424440238` reports two prior failed attempts | Poll compute versus API wait and merge/conflict overhead cannot be separated | Log tick-stage, API, retry, and conflict timings |

The supplied `created_at` and `run_started_at` rows yield zero-second differences for these families, but do **not** provide a reliable runner-queue breakdown. Log job queued/start and first-step timestamps before attributing delay to queueing. Run rows also lack a common task identifier for calculating end-to-end clarify-to-merge latency.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** CI p50 **2,698 seconds** (`36417072376` poller-test step: **1,293 seconds**); active review model execution (`36416865588`: **2,345 seconds**); poller p50 **390 seconds**. **Top failure modes:** one late prompt-budget CI failure (`36411570320`), classified reviewer stalls/fallbacks (`36416865588`), and ambiguous cancellation last-step labels—not evidence of disk failures. **Highest recorded cost driver:** three active review runs account for all **26.51 million distinct-log OpenRouter tokens** in the inspected cost-bearing sample.

**Top three actions, in order:** (1) move the unchanged prompt-budget check to CI preflight; (2) fix combined/split telemetry deduplication and add reviewer-slot/shard timings; (3) trial a bounded second-attempt reviewer stall cap while preserving fallback and quorum.

## Metrics Appendix

**Window:** 1,000 Actions runs, one repository. Percentages below use all runs unless noted; skips and cancellations are not failures.

| Scope | Runs | Success | Failure | Cancelled | Skipped | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| All workflows | 1,000 | 310 (31.0%) | 1 (0.1%) | 12 (1.2%) | 677 (67.7%) | 2 / 297 s |
| CI | 18 | 16 | 1 | 1 | 0 | 2,698 / 2,790.9 s |
| Review/autofix | 172 | 160 | 0 | 6 | 6 | 13 / 1,899.1 s |
| Orchestrator poll | 12 | 12 | 0 | 0 | 0 | 390 / 460.9 s |
| Clarify | 173 | 22 | 0 | 0 | 151 | 1 / 159 s |
| Issue–PR status | 12 | 12 | 0 | 0 | 0 | 96 / 139.3 s |

The failure rate among **success-or-failure conclusions only** is **1/311 (0.32%)**; treating 677 skips as failures would misstate health.

| Cost/cache metric | Collector-reported | Distinct primary-log count where applicable |
|---|---:|---:|
| OpenRouter calls; usage available / unavailable | 61; 52 / 9 | **45; 39 / 6** |
| Prompt / completion / total tokens | 11,299,134 / 535,332 / **33,634,671** | 8,499,611 / 429,638 / **26,513,170** |
| Cache read / reported creation tokens | 21,800,205 / 0 | 17,583,921 / 0 |
| `cache_hit_rate` | **null** | **not measurable** with unavailable usage |
| `break_glass_count` / `context_budget_warn_count` | 0 / 5 | 0 / **3** |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | **7,000 / 3,125,560** in assembled context, 113 samples | Folder `summary.json`: **2,721,500 / 3,478,400**, 16 samples; different coverage, not a deduped comparison |

| MCP metric or target | Collector-reported | Distinct inspected events | `probe_ok` / `probe_failed` / `probe_skipped` |
|---|---:|---:|---:|
| Semble `reviewer-context` queries; logged bytes | 4; 54,851 B | **3; 40,912 B**, 12 chunks each | 0 / 0 / 0 observed |
| Semble `overflow` contract-test fallbacks | 44 | **32** across 8 CI runs; runtime fallbacks **0** | 0 / 0 / 0 observed |
| Serena queries / response bytes / tool calls / fallbacks | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0; **no per-tool breakdown available** | 0 / 0 / 0 observed; no target emitted |

**Other MCP servers observed:** none in inspected structured lines. **GitHub API calls, endpoint hotspots, GitHub rate-limit events, cache hits, and API retry counts:** not collected at usable call-site granularity. The assembled context reports **115** runs with log telemetry, while the folder’s `summary.json` reports **16**; reconcile that coverage distinction before comparing wall-clock percentiles or treating zero-valued rows as full observations.

## Deep Audit — Workflows & Scripts (2026-09-28)

### Section 1: Bug & Correctness Sweep

The audit covered 52 workflows and 159 scripts. All workflows parsed as YAML, and all 62 Python scripts parsed as Python. No confirmed issue-title or comment-body interpolation directly into a `run:` shell body was found. The findings below are source-level risks; they are not claims of observed incidents unless stated.

- **BUG-001** — **File:** `.github/workflows/review_autofix_sweep.yml:163-214,254-301`. **Severity:** High. **Category:** `bug`. **Description:** Each active-run API failure is swallowed with `|| true`; an empty snapshot then means “no active run” to the dispatch loop. *Inference:* an API outage can queue redundant reviews for PRs already under review. Whether this occurred in the inspected runs is unknown. [NEEDS VERIFICATION] **Recommended fix:** Preserve a snapshot-success flag per workflow. If either read fails, skip dispatch for affected PRs or perform a bounded per-PR active-run fallback before dispatching.

- **BUG-002** — **Files:** `scripts/claude_issue_route.py:1107-1127`; `scripts/claude_issue_intake.sh:260-278`; `scripts/claude_pr_sweep.py:146-156`; `scripts/claude_issue_queue_watchdog.sh:59-70`. **Severity:** High. **Category:** `bug`. **Description:** Four open-queue readers request only `per_page=100`, without pagination. Above 100 matching issues, intake and sweep deduplication can miss an existing item, while the watchdog omits older stale items. The observed backlog size is unknown. [NEEDS VERIFICATION] **Recommended fix:** Give the shared queue reader paginated, validated output; make the shell callers use `gh api --paginate` and merge page arrays as `claude_issue_intake.sh:223-226` already does. Add a greater-than-100-item contract test.

- **BUG-003** — **File:** `scripts/claude_pr_sweep.py:159-183,247-263`. **Severity:** Medium. **Category:** `bug`. **Description:** A successful queue POST with malformed JSON or no numeric `number` returns `None`, but `sweep` still increments `queued`, records the PR as queued, and posts a claim. `bind_pr_fix` explicitly refuses the resulting unbound item. This requires an abnormal POST response; incidence is unknown. [NEEDS VERIFICATION] **Recommended fix:** Require a validated issue number before counting, binding, or claiming. On an indeterminate POST response, reconcile the open queue by the durable item identity rather than blindly posting again.

- **BUG-004** — **File:** `scripts/tg_helpers.sh:330-374,399-445`. **Severity:** Medium. **Category:** `bug`. **Description:** Both cleanup loops delete tracking comments from page 1 *before* requesting page 2. When page 1 contains deletions, offset pagination can skip comments shifted from the next page. This requires a multi-page issue; occurrence is unknown. [NEEDS VERIFICATION] **Recommended fix:** Fetch and validate all comment pages before deleting any comment, then process the collected IDs.

- **BUG-005** — **File:** `scripts/tg_helpers.sh:135-145,349-369,418-440`. **Severity:** Medium. **Category:** `bug`. **Description:** `tg_delete_msg` discards the response from a `curl -s` request, and cleanup then deletes the GitHub tracking comment regardless of whether Telegram accepted the deletion. *Inference:* a transient Telegram rejection can permanently remove the IDs needed for retry. [NEEDS VERIFICATION] **Recommended fix:** Check the Telegram response’s `ok` field and retain the tracking comment when deletion is transiently unsuccessful; distinguish permanent “already absent” responses.

### Section 2: GitHub API Call Redundancy Audit

Counts below are **per source-code execution path**, not measured production totals. The existing report already calls for endpoint telemetry; these are distinct call-site candidates.

- **API-001** — **File:** `scripts/orchestrate_poll_process.sh:19435-19459` (returned fields at `14686-14720,14765-14777`). **Severity:** Medium. **Category:** `api-redundancy`. **Description:** Current-wave issues are passed to both `_fetch_candidate_issue_details_graphql` and `_fetch_issue_labels_batch_graphql`, although the details result already contains labels. **Current → proposed:** `2 × ceil(N/25)` → `ceil(N/25)` GraphQL calls when both batches succeed, plus miss fallbacks. **Recommended fix:** Derive `LABELS_JSON` from `_current_wave_details_json`; invoke the existing labels batch only for missing keys and retain per-issue REST fallback. This preserves the independently fail-open behavior documented at the call site.

- **API-002** — **File:** `scripts/gh_helpers.sh:693-725` (contrast `439-465`). **Severity:** Medium. **Category:** `api-redundancy`. **Description:** `curl_gh_api` retries non-rate-limit HTTP 404 and 422 responses through its default five attempts, unlike `gh_retry`, which recognizes permanent failures. **Current → proposed:** five → one request for a permanent failure; default backoff also falls from 31 seconds to none. **Recommended fix:** Classify permanent HTTP statuses before the exponential-backoff branch, retaining the existing rate-limit handling. Extend the permanent-failure policy used by `gh_retry`, rather than adding another retry wrapper.

- **BATCH-001** — **File:** `scripts/orchestrate_poll_process.sh:5385-5402,21436-21456`. **Severity:** Medium. **Category:** `api-batching`. **Description:** Two blocker-status loops make one issue GET per blocker. **Current → proposed:** `B` reads per loop → `ceil(B/25)` batched reads, with single-issue fallback for missing results. **Recommended fix:** Add a state-only alias batch modeled on `_fetch_candidate_issue_details_graphql`, cache blocker states for the tick, and retain the loops’ existing “unknown means defer” rule.

- **BATCH-002** — **File:** `scripts/orchestrate_poll_process.sh:15707-15714`. **Severity:** Medium. **Category:** `api-batching`. **Description:** Standalone recovery runs seven `gh issue list --json number` queries, one for each phase label. **Current → proposed:** seven listing requests → one aliased GraphQL request for the first page of each label, plus necessary pagination. Query/filter parity with the current 1,000-item-per-label behavior needs confirmation. [NEEDS VERIFICATION] **Recommended fix:** Extend the poller’s alias-batching pattern with seven independently paginated label connections; fall back to the existing listing for a failed alias.

- **BATCH-003** — **File:** `scripts/review_merge_train.sh:110-136`. **Severity:** Medium. **Category:** `api-batching`. **Description:** `_mt_pr_files_into` caches files per PR, but a cold scan still makes a paginated REST read for each distinct older PR. **Current → proposed:** one listing plus `K` file reads → one listing plus `ceil(K/10)` aliased queries for PRs whose files fit one page; at the documented maximum of 20 older PRs, approximately 21 → three requests. GraphQL file/rename and pagination parity is unverified. [NEEDS VERIFICATION] **Recommended fix:** Trial aliases using the poller’s batch-query pattern, populate `_MT_FILES_CACHE`, and retain REST for missing, renamed, or additional-page cases.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — **Files:** `.github/workflows/clarify.yml:61-130`; `.github/workflows/orchestrate_clarify_respond.yml:115-184`; `.github/workflows/implement.yml:445-514`; `.github/workflows/validate.yml:106-175`; `.github/workflows/plan.yml:124-196`. **Severity:** Medium. **Category:** `duplication`. **Description:** Four integration-ref resolver `run:` bodies match across roughly 3,594 characters; Plan’s variant is near-identical. **Recommended fix:** Put the shared staging/resolution logic in a trusted `scripts/integration_ref_bootstrap.sh` entrypoint, `resolve_integration_ref_bootstrap <issue_number> <repository> <workflow_ref>`. Update all five callers, retaining their existing trusted-checkout bootstrap and passing expressions through `env:`.

- **DUP-002** — **Files:** `.github/workflows/mark-stable.yml:656-805`; `.github/workflows/test-and-mark-stable.yml:5641-5790`. **Severity:** Medium. **Category:** `duplication`. **Description:** The release tag-publication steps each contain approximately 7,656 characters of matching logic, including `publish_tag_with_remote_verification`. **Recommended fix:** Move that function and its tag-update sequence to a trusted `scripts/release_tag_helpers.sh` with `publish_tag_with_remote_verification <tag_ref> <immutable|moving>`; source it from both workflows without changing step gates or outputs.

- **DUP-003** — **Files:** `scripts/validation_refresh_runner.py:114-163,701-723`; `scripts/audit_consumer_drift.py:64-113,142-164`. **Severity:** Low. **Category:** `duplication`. **Description:** `CommandExecutor.run` and `load_target_repositories` have matching implementations in both modules. **Recommended fix:** Move their shared behavior and `CommandFailure` contract to a new `scripts/validation_runner_helpers.py`; keep `run(command, *, cwd, check, env_overrides, input_text, timeout)` and `load_target_repositories(repos_file)` signatures, then update both callers.

- **DUP-004** — **Files:** `scripts/cost_audit.py:283-295`; `scripts/collect_workflow_logs.py:96-108`; `scripts/analyze_workflow_logs.py:40-52`; `scripts/workflow_retro.py:50-62`. **Severity:** Low. **Category:** `duplication`. **Description:** Four copies of `_parse_iso8601` implement the same UTC normalization. **Recommended fix:** Provide `parse_iso8601(value: str | None) -> datetime | None` in a shared `scripts/workflow_time_helpers.py`, and migrate the four callers with parity tests.

- **DUP-005** — **File:** `scripts/tg_helpers.sh:156-207,227-278`. **Severity:** Low. **Category:** `duplication`. **Description:** General and phase-specific Telegram ID storage duplicate the read/select/PATCH-or-POST sequence. This is separate from the cleanup defect in **BUG-004**. **Recommended fix:** Make `tg_helpers.sh` own `_tg_store_tracking_id <issue_number> <marker_kind> <message_id>` and update `tg_store_msg_id` and `tg_store_phase_msg_id` to call it while preserving their marker formats; incorporate checked mutation responses when addressing **BUG-005**.

### Section 4: Expression Size Limit Risk Assessment

The counts are static `run:` body character counts **only where `${{ }}` occurs**. Runtime substitution can change the final length; reported headroom is therefore an estimate. No measured workflow exceeds 800 KB, and no large `if:` approaches 21,000 characters. This repository also documents a *stricter* 512,000-byte workflow load limit and a 480,000-byte CI guard (`CLAUDE.md:2102-2126`; `tests/test_workflow_file_size_limit.py:1-34`).

- **EXPR-001** — **File:** `.github/workflows/implement.yml:986-1342`. **Severity:** High. **Category:** `expression-limit`. **Description:** “Stage workflow support files” has three interpolations in approximately **20,326** source characters—only **674** below 21,000 before runtime substitution. The exact expanded count is unknown. [NEEDS VERIFICATION] **Recommended fix:** Move the body to a staged, trusted script under `scripts/`; pass the three expression values through step `env:` and leave the workflow step’s gates intact.

- **EXPR-002** — **File:** `.github/workflows/implement.yml:3223-3529`. **Severity:** Medium. **Category:** `expression-limit`. **Description:** “Preflight destructive-commit guard” has one interpolation in approximately **17,313** source characters, leaving **3,687** estimated characters. [NEEDS VERIFICATION] **Recommended fix:** Extract the preflight body to a trusted script under `scripts/` and pass `github.repository` through `env:`.

The next-largest measured expression-bearing run is `.github/workflows/implement.yml:4181-4409` at 14,846 source characters, below the requested 15,000-character finding threshold. Runs without `${{ }}` were excluded.

### Section 5: Cross-Cutting Concerns

- **DEAD-001** — **File:** `scripts/orchestrate_poll_process.sh:19484-19512`. **Severity:** Low. **Category:** `dead-code`. **Description:** `LINKED_PR_NUM` is initialized and assigned during linked-PR selection but is not subsequently read in the script. **Recommended fix:** Remove those assignments if no output contract needs the value, or consume it in the selection telemetry if it was intended to identify the chosen PR.

- **CONSIST-001** — **Files:** `scripts/review_rb_judge.sh:735-765,2366-2384`; `.github/ai/label_contract.v1.json:4-7,152-155`. **Severity:** Medium. **Category:** `consistency`. **Description:** If the canonical label helper cannot be loaded, the judge’s fallback creates labels other than `ai:ready-to-merge` and `ai:closed` with generic blue metadata. The judge can request `ai:clarification` and `ai:orchestrator-managed`, whose contract metadata differs. **Recommended fix:** Keep the verified-helper loading path, and make its last-resort fallback use the exact contract metadata for every label it can create; refuse unknown labels rather than silently assigning generic metadata.

- **SHELL-001** — **File:** `scripts/stage_workflow_support.sh:138-145,198-208`. **Severity:** Low. **Category:** `shellcheck`. **Description:** Shellcheck reports `SC2043` for two literal single-item `for` loops. These are warning-level compliance findings, not demonstrated runtime failures. **Recommended fix:** Replace each singleton loop with a direct guarded install, preserving the existing optional-versus-required behavior.

- **DEBT-001** — **File:** `.github/workflows/review_autofix.yml:1-7464`. **Severity:** Low. **Category:** `tech-debt`. **Description:** At **451,634 bytes**, the workflow is below both repository limits but has only **28,366 bytes** before the 480,000-byte CI guard—less than the documented 50,000-byte headroom aim. **Recommended fix:** Before the next substantial expansion, extract a large inline step using the existing `review_autofix_step_*.sh` staging and registry pattern; do not raise the guard or split the workflow.

No `TODO`, `FIXME`, or `HACK` marker was found in the audited workflow and script files. The existing report already covers the prompt-budget gate, reviewer stalls, memory-step timing, and telemetry deduplication; they are not repeated as new findings here.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BUG-001, BUG-002, EXPR-001 |
| Medium | 12 | BUG-003, BUG-004, BUG-005, API-001, API-002, BATCH-001, BATCH-002, BATCH-003, DUP-001, DUP-002, EXPR-002, CONSIST-001 |
| Low | 6 | DUP-003, DUP-004, DUP-005, DEAD-001, SHELL-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 5–7 | Medium |
| API call optimization | 2–4 | Medium |
| Code modularization | 10–15 | Large |
| Expression size reduction | 2–3 | Medium |
| Medium/Low fixes | 6–10 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-09-28)

### Safety Tag Legend

`SAFE_TO_MERGE` is authorized for implementation without further review. `NEEDS_VERIFICATION` requires the stated checks first. `RISKY_SKIP` must not be auto-implemented; the identified call protects pagination, retry, or race-sensitive behavior.

### Consolidation Candidates (MERGE-###)

- **MERGE-001 — `RISKY_SKIP`.** **Calls:** `scripts/orchestrate_poll_process.sh:10441` and `scripts/orchestrate_poll_process.sh:10442`, in `finalize_integration_merge_if_needed` (`:10352`). **Current → proposed:** two successful-path REST requests → one, only when the existing final-PR snapshot does not match. **Endpoint:** `GET /repos/{owner}/{repo}/pulls/{final_pr}`. **Evidence:** adjacent reads fetch `.state` and `.merged_at` from the same PR, with no mutation between them:
  ```sh
  existing_pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  existing_pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
  ```
  **Proposed fix:** Have the snapshot-miss branch fetch one PR JSON object and derive both fields locally; retain the current matching-snapshot branch and its fields. **Safety rationale:** This is a race-sensitive poller finalization path, and two independently failing retries do not have the same failure semantics as one shared read. **Downstream signal:** Do not auto-implement. Manually review PR-state changes between reads and test either read failing independently before changing the final-merge decision.

### Redundant Re-Fetch (REUSE-###)

- **REUSE-001 — `RISKY_SKIP`.** **Calls:** `scripts/review_merge_train.sh:260-261` (`_mt_find_marker_comment_id`) and `scripts/review_merge_train.sh:275-287` (`_mt_upsert_comment`); the no-ID caller is at `:483-486`. **Current → proposed:** `P + 1` successful-path requests → `P` when a marker exists and no comment ID was supplied, where `P` is the number of comment-list pages. **Endpoints:** `GET /repos/{owner}/{repo}/issues/{pr}/comments?per_page=100` and `GET /repos/{owner}/{repo}/issues/comments/{id}`. **Evidence:** the paginated listing receives comment bodies but retains only the selected ID; the upsert then fetches that comment’s body.
  ```sh
  --jq ".[] | select(.body | startswith(\"${marker}\")) | .id"
  existing_body="$(gh_retry gh api "repos/${MT_REPO}/issues/comments/${existing_id}" --jq '.body' 2>/dev/null || true)"
  ```
  **Proposed fix:** Extend `_mt_find_marker_comment_id` with a separately named result that carries the selected `{id, body}` while preserving its ID-only callers. Let `_mt_upsert_comment` use that body only on its no-ID path; retain the individual GET when an ID is supplied or the listed body is unusable. **Safety rationale:** The listing is paginated, and replacing the later GET could discard a comment edit made after the list response. **Downstream signal:** Do not auto-implement. Manually verify latest-marker selection across pages, concurrent comment edits, and the upsert’s behavior when the list succeeds but the individual GET would fail.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: `RISKY_SKIP` — Poller cache consolidation needs manual review of independent GraphQL failure and per-issue fallback behavior.
- API-002: `RISKY_SKIP` — Changing `curl_gh_api` request counts changes a retry loop’s permanent-error handling; review it separately.
- BATCH-001: `RISKY_SKIP` — Blocker reads are in the race-sensitive poller; preserve its unknown-state deferral.
- BATCH-002: `RISKY_SKIP` — Standalone stall recovery and per-label pagination both require manual filter and page-boundary review.
- BATCH-003: `RISKY_SKIP` — The existing PR-file reads are paginated; GraphQL file and rename parity remains unverified.

### Summary Counts

Net-new findings only; Deep Audit cross-references are excluded.

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 0 | — |
| RISKY_SKIP | 2 | MERGE-001, REUSE-001 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
