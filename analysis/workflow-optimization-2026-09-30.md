## Executive Summary

- **Fix measurement before changing model spend.** In `shubhodeep1/coding-workflows`, six slow review runs contain the same OpenRouter and Semble events in aggregate job logs and individual step logs. The collector reports **124.06M tokens and 173 calls** for the sampled runs; counting canonical step events gives **72.90M tokens and 109 calls**. The 51.16M-token difference is *reporting inflation, not realized cost savings*. Impact: reliable prioritization and budgets; **confidence: high**.
- **Review latency has two observable non-model costs.** Five review runs, including `36648823889` and `36647263924`, exhausted a **300-second check-run wait**; six segmented runs spent **96–498 seconds** in “Free disk space.” Logging check identities and disk headroom should precede changes to either safeguard. Potential saving: up to the overlapping wait or unnecessary cleanup time per eligible run; **confidence: high for durations, medium for achievable savings**.
- **One concrete CI failure needs a small fix.** CI run `36649492354` failed “Inventory parity” because `docs/INVENTORY.md` did not document `scripts/guard_differential.py`; it finished after 548 seconds. Update the inventory on the affected branch and run parity before submission. Impact: prevent recurrence of this failure; **confidence: high**.
- **MCP availability is not the failure pattern shown here.** The failed CI log’s four distinct `SEMBLE_FALLBACK target=overflow` events are labelled `context=contract-test`; duplicate log copies make that run’s row report eight. No runtime fallback or Serena probe was observed. Impact: avoid treating a contract test as a broken rollout; **confidence: high for the inspected run**.
- **Coverage limits broader conclusions.** Only **16 of 1,000 runs** have downloaded, parsed logs in the folder summary; ten slow review runs supply all measured OpenRouter usage. The assembled analysis calls 115 rows “with log telemetry,” but 99 of those rows have no downloaded logs. Do not extrapolate sampled costs or infer orchestrator stalls from skips alone. Impact: more trustworthy trend detection; **confidence: high**.

## Speed Optimizations

1. **Critical path — diagnose, then overlap check-run waiting.** The “Collect PR check-run failures CI lint autofix context” step reached its 300-second timeout in `36648823889`, `36646798608`, `36647239481`, and `36647263924`; the aggregate log shows the same timeout in `36641109403`. `scripts/collect_pr_check_runs_context.py` logs the number pending but not their names or origin, so the cause is unknown. Emit a per-poll summary of check name, app, status, elapsed time, self-run exclusion count, and exit reason. If those checks can safely be polled during independent disk cleanup, start polling earlier and join before review **without shortening the timeout or weakening the merge gate**. Estimated saving where overlap is possible: **96–211 seconds** in the four runs with segmented cleanup timings; implementation risk **medium**.

2. **Critical path — make cleanup conditional only after measuring headroom.** “Free disk space” took 498 seconds in `36648413566`, 214 in `36648758366`, and 166 in `36648823889`; it was also the step at cancellation in `36648716510` and `36641494664`, though the cancellation cause is unknown. Log bytes available before and after cleanup and its action duration. Then test skipping it only above a verified safe free-space threshold, retaining the existing cleanup otherwise. Estimated saving: **0–498 seconds per eligible sampled run**; risk **medium** because an incorrect threshold could cause disk failures.

3. **Critical path — profile reviewer slots before changing models.** “Run reviewer models” took **337–1,074 seconds** across six segmented slow reviews; `36648823889` took 765 seconds in that step. Emit per-slot elapsed time, pass, retry class, cache-read tokens, and verdict. Pilot any pass or reasoning-level reduction against review-quality outcomes before rollout; the latency saving is **not estimable from current evidence** and a model change carries **high quality risk**.

4. **Failure-feedback win, not a routine-run shortcut.** In CI `36649492354`, inventory parity failed about 32 seconds after run creation, but parallel CI work continued to the 548-second conclusion. Add a pre-submission parity check on affected changes; consider an early CI gate only if its startup cost does not slow successful runs. Potential failure-feedback improvement is **up to roughly eight minutes on a similar failure**; implementation risk **low for local preflight, medium for CI dependency changes**. Semble’s observed reviewer queries took roughly 0.4–0.6 seconds each, so optimizing them first would be a micro-optimization.

## Cost Optimizations

1. **Correct attribution first.** `scripts/collect_workflow_logs.py` has a structured-line deduper, but its full-line key includes timestamps: the two copies of `SEMBLE_QUERY` in `36648823889` have the same fields and timestamps differing by microseconds. Normalize the timestamp and pair matching parent-job and child-step events **one-to-one**, with a regression test that preserves genuinely repeated calls. This removes **51.16M falsely attributed tokens, 64 calls, and 84,972 falsely attributed Semble bytes** from this sample; it saves **no actual tokens or dollars**. Risk: **low with paired-event tests**.

2. **Target fresh prompt expansion, not cached reads indiscriminately.** The eight usage-complete slow reviews have **17.62M canonical prompt tokens** and a **73.09% weighted cache-hit rate**. The review warnings in `36641109403` and `36641456604` show prompts of **153,066** and **173,855** tokens against a 140,000-token warning threshold. Preserve the stable prefix, put run-specific material later, and remove demonstrably duplicated context; log static-prefix fingerprint and dynamic-byte counts to validate the cause. A *hypothetical* 10% cut in fresh prompt tokens across those eight runs is **1.76M tokens**; quality risk is **low only for verified duplication**. No price data supports a dollar estimate.

3. **Measure first- versus second-pass value before model changes.** Canonical logs contain **109 OpenRouter usage events** in ten slow reviews, including `pass1` and `review` calls; three inspected runs log reviewer reasoning effort `xhigh`. There are seven “reviewer … failed” warnings across five sampled runs, so automatically dropping a pass or downgrading reasoning could damage coverage. Log pass-specific finding contribution and cost, then run a guarded quality comparison. Savings: **unknown until that comparison**; quality risk **high**.

4. **Assess retrieval value rather than assuming it.** Ten distinct `SEMBLE_QUERY target=reviewer-context` events returned **12 chunks each and 140,861 logged bytes total**. There is no before/after prompt-size or finding-quality measure, so these lines do **not** establish that Semble reduces prompt expansion rather than adding context. Add selected-byte, displaced-byte, and finding-usefulness summaries; cap low-value chunks only after measurement. Serena has **zero queries and zero response bytes** in this window, so it cannot be credited with replacing downstream tool or model work. Savings: **unmeasured**.

5. **Prevent avoidable CI reruns.** Inventory drift caused the sole failed CI run, `36649492354`; the inspected run shows no OpenRouter calls and no retry. Fixing parity avoids approximately **548 seconds of CI run time per similar failure**, not measured token spend. Risk: **low**.

## Reliability Improvements

1. **Observability integrity — highest priority.** The collector’s exact timestamped deduplication misses job/step copies in six reviews and the failed CI run. For `36649492354`, eight reported contract-test fallbacks represent **four distinct events** in its step log. Fix the deduper as above, retain source-step provenance and a `duplicates_suppressed` count, and compare old/new totals during rollout. Expected impact: eliminate this demonstrated counting error; rollback to the old counter if distinct-event tests reveal undercounting, without changing workflow behavior.

2. **Documentation drift — actual CI failure.** `36649492354`, job `tests-release-and-log-analysis`, step “Inventory parity,” identifies the missing inventory entry for `scripts/guard_differential.py`. Add the entry on the affected branch and enforce parity before CI. Expected impact: prevent this failure class; **do not fail open** the parity gate.

3. **Unknown pending checks — latency and merge-safety risk.** Five sampled reviews timed out after 300 seconds and proceeded with a snapshot. The log gives a count, not the pending check identities; **inference:** an unrelated, stale, or legitimately slow check could explain the waits. Emit identities and final collection status, and keep an unknown/incomplete result distinct from passing in downstream decisions. Expected impact: identify whether waits can safely be reduced; retain the current timeout and required-check protections until proven safe.

4. **Partial reviewer and validation signals.** Seven reviewer-failure warnings occurred across sampled runs, including two in `36648413566`; their aggregate status does not provide a consistent actionable cause. Emit failure class, attempt, fallback, and quorum result while retaining successful reviewers. Separately, the `log_summary` for successful review run `36651391802` warns that no standalone validation workflow could be dispatched for merged PR **#5321**. Log dispatch decision and subsequent validation evidence; do not equate workflow success with validated merge. Expected failure-rate reduction is **not measurable yet**; preserve existing fail-open behavior for advisory logging, not for required validation.

`BREAK_GLASS` was **0** in collected telemetry. The two `CONTEXT_BUDGET_WARN` events, both in review runs, indicate **prompt-size risk**, not demonstrated rubric-pressure failure. The Semble fallbacks are contract-test events, not probe failures or evidence of a masked runtime rollout.

## AI Memory Health

In canonical deep-dive logs, **8/8 `retrieve` operations hit** (`records_selected` 25–26). They averaged **1,395 estimated tokens against a 1,400-token budget**; `keyword_method` was `llm` in all eight, with **0 `plain` and 0 `none`**. No observed retrieve selected zero records, set `enabled: false`, or reported `fail_open: true`. This is a positive sampled signal, but retrieval is operating close to its budget; log rejected-record counts and truncation reason before enlarging it.

The same logs contain **20 `record-run-event`** and **10 `record-candidate`** operations. **7/30 writes needed more than one push attempt**; the maximum was four on review run `36647263924`, and observed operations reported success. Emit a bounded retry-reason/status field to distinguish contention from API failure; the present attempt count does not prove rate limiting. A non-deep-dive `log_summary` for issue-PR status run `36651391781` reports a successful `ingest_implement_plan_lessons` with 39 parsed and one written. No deep-dive evidence establishes `finalize-task`, `promote`, `compact`, or processed-command coverage; verify those emissions in an appropriate active run.

## GH API Call Audit

| Observed or code path | Evidence and call assessment | Smallest safe action |
|---|---|---|
| Review check polling | Five timed-out reviews log **28 “Waiting” iterations** combined. The loop in `scripts/collect_pr_check_runs_context.py` performs a paginated check-runs GET before each wait and one final GET: **at least 33 GET attempts**, excluding extra pages or retries. Actual request counts and rate-limit headers are not emitted. | Log endpoint *template*, pages, attempts, remaining-limit signal, cache status, pending check identities, and timeout reason. Reuse a completed snapshot within a cycle; do not suppress required-check polling. |
| Orchestrator issue lookups | `scripts/orchestrate_poll_process.sh` contains per-row issue and optional comment GET paths, but the eight poller runs have **no deep-dive logs or measured call counts**. Executed volume is unknown. | Instrument calls per cycle and batch size. If the path is active for multiple rows, reuse the existing candidate-details/linked-PR GraphQL helpers and cycle-local caches instead of adding per-item lookups. Savings are conditional on measured row count. |
| Memory pushes | Seven of 30 sampled memory writes required repeated push attempts; HTTP cause and request count are absent. | Log sanitized retry class and attempt count, without payloads. Do not classify these as rate-limit events without response evidence. |

Repository guidance in `unattended_system_instructions.md` §14 and `agents.md` already requires batching, reuse of `ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`, and `_candidate_details_json`, and fail-open cache misses. Follow those rules rather than add another cache or service. **No rate-limit event or repository-wide GH API total is measurable** from this log set; emit request counts at `gh_retry` and report per workflow/job/step before claiming a call-count reduction.

## Prompt Cache & Memory System

The folder summary’s `cache_hit_rate` is **null** because five OpenRouter usage events lack complete usage data, despite **8/10** slow review runs having individual rates (**55.55%–84.13%**). Recomputed from canonical, usage-complete events, the weighted rate is **73.09%**. Canonical logs record **51.17M cache-read tokens** across the ten reviews and **0 reported cache-write tokens**; zero reported writes does not establish that no cache creation occurred.

Add a per-call coverage line containing phase, model, usage availability, cache-enabled/breakpoint status, read/write counts, and a fingerprint of the *non-sensitive static prefix*. Compare that fingerprint across runs before attributing misses to unstable prefixes or dynamic noise. Keep review memory’s 1,400-token retrieval budget, but log selection/truncation so its near-full utilization does not silently grow prompts. The two context-budget warnings merit targeted context trimming; estimate latency and reliability improvement **only after** recording prompt size and cache behavior before and after. No observed cache fail-open event justifies changing its current behavior.

## Orchestrator Health

There are **645 skipped runs**, including 154 each in plan, implement, and orchestrate-clarify-respond, and 145 in clarify. Recent examples `36652090406` (plan) and `36652090345` (implement) have empty archives. A skipped workflow with no job log is **not evidence of a stuck wave**. Emit a sanitized eligibility/skip decision at the dispatching gate or collect its job-condition outcome, with issue/PR correlation, phase, prior state, and decision reason.

All eight `orchestrate_poll` runs succeeded but have a **447.5-second p50** and no deep-dive step evidence. Add one cycle summary covering wave transition, clarification count, deferral reason, conflict-heal attempt, terminal outcome, API calls, and time spent waiting versus computing. Track repeated identical state transitions and age since last productive transition. The validation-dispatch warning for PR #5321 warrants a separate observable `dispatched | already_covered | unavailable` outcome; whether that PR lacked validation cannot be determined here.

## Pipeline Flow Bottlenecks

- **Clarify → plan → implement:** Clarify had 18 successes among 163 runs; plan had two among 156; implement had zero among 154, all skipped. This window cannot establish conversion or clarification-loop rates without correlated issue/PR and skip-reason events. Add phase-transition timestamps and reason codes.
- **Review/autofix:** The dominant measured compute is reviewer models (**337–1,074 seconds** in six segmented slow runs). Separate from it the **300-second check wait** in five runs and **96–498-second cleanup** in six; log these as model compute, external wait, and runner preparation respectively.
- **Validate/CI:** CI p50 is **537.5 seconds** over 14 runs; `36649492354` failed a fast parity check but concluded after 548 seconds of parallel work. Capture first-failure time and remaining-job time for early-feedback decisions.
- **Queue, retry, merge/conflict:** Queue time and merge/conflict overhead are **not measured**. The inspected failed CI run has zero retries, while sampled memory pushes do retry internally; these are different measures. Record job queued/start timestamps, request attempts, and conflict-heal outcomes before ranking those overheads against observed review waits.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** long reviewer-model steps, check-run waits, disk cleanup, then CI’s roughly nine-minute runs. **Top failure modes:** one inventory-parity CI failure; incomplete check-run snapshots; duplicated telemetry that obscures costs. **Highest-cost driver:** sampled slow review OpenRouter usage—**72.90M canonical total tokens across ten runs**, not the collector’s 124.06M.

**Top three actions:** (1) fix paired-event deduplication and log provenance; (2) add pending-check identity/API-attempt and disk-headroom diagnostics before altering review timing; (3) repair affected-branch inventory parity and require pre-submission checking. These preserve review and merge safeguards while making subsequent optimization decisions testable.

## Metrics Appendix

All-run durations include skipped runs. Cost figures below cover **ten selected slow review runs**, not all 1,000 runs; “canonical” means aggregate-job copies were excluded when individual step logs were present.

| Family | Runs | Success | Failure | Cancelled | Skipped | Success/run | Failure/run | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| All | 1,000 | 349 | 1 | 5 | 645 | 34.9% | 0.1% | 2 / 423 s |
| Review/autofix | 217 | 213 | 0 | 4 | 0 | 98.2% | 0% | 13 / 1,282.4 s |
| CI | 14 | 13 | 1 | 0 | 0 | 92.9% | 7.1% | 537.5 / 549.4 s |
| Orchestrate poll | 8 | 8 | 0 | 0 | 0 | 100% | 0% | 447.5 / 484.6 s |
| Clarify | 163 | 18 | 0 | 0 | 145 | 11.0% | 0% | 1 / 162 s |
| Plan | 156 | 2 | 0 | 0 | 154 | 1.3% | 0% | 1 / 10 s |
| Implement | 154 | 0 | 0 | 0 | 154 | 0% | 0% | 1 / 9.3 s |
| Issue-PR status | 18 | 18 | 0 | 0 | 0 | 100% | 0% | 75.5 / 171.2 s |

Among the **355 non-skipped runs**, 349 succeeded (**98.3%**); this denominator includes five cancellations. Success is not equivalent to an AI model call.

| Sampled cost/review measure | Collector-reported | Canonical-log baseline or coverage |
|---|---:|---:|
| OpenRouter calls; total tokens | 173; 124,060,643 | **109; 72,902,998** |
| Prompt; completion tokens | 33,528,549; 1,748,116 | **20,664,460; 1,065,412** |
| Cache-read; cache-write tokens | 88,786,316; 0 | **51,174,807; 0 reported** |
| Usage available; unavailable | 168; 5 calls | **104; 5 calls** |
| `cache_hit_rate` | null aggregate | **73.09%**, eight complete runs only |
| `wall_clock_p50_ms`; `wall_clock_p99_ms` | 1,456,000; 2,005,300 | 16 downloaded-log run samples; not model-call latency |
| `break_glass_count`; `context_budget_warn_count` | 0; 2 | 0; **2 distinct review warnings** |
| Codex calls; tokens | 0; 0 recorded | No Codex usage line in this sample |

| MCP / API measure | Recorded result | Interpretation and gap |
|---|---:|---|
| Semble reviewer-context queries / logged bytes | Collector **16 / 225,833**; canonical **10 / 140,861** | One distinct query per inspected slow review; mean **14,086 bytes**. Prompt displacement unmeasured. |
| Semble fallbacks | Folder summary **8**; assembled run rows **16**; failed CI `36649492354` **4 distinct / 8 reported** | All classified **contract-test**; **0 runtime** reported. The other CI run’s distinct count lacks a deep-dive log. A fallback/query rate across these different workloads would be misleading. |
| Serena queries / response bytes / tool calls / query time | **0 / 0 / 0 / 0 ms** | No per-tool breakdown exists; `SERENA_ENABLED: false` is reported in review run `36651713955`. |
| Serena fallbacks; probes ok / failed / skipped | **0; 0 / 0 / 0** | No availability conclusion without probes. |
| GH API calls; rate-limit events | **At least 33 check-run GET attempts** in five timed-out reviews; no total or rate-limit counter | Pagination, retries, other endpoints, and rate-limit incidence are unmeasured. |
| Other MCP servers observed | **None validated** | No other-server query, fallback, or probe totals to report. |

| MCP target | Distinct query / bytes confirmed | Fallback evidence | `probe_ok` | `probe_failed` | `probe_skipped` |
|---|---:|---|---:|---:|---:|
| Semble `reviewer-context` | 10 / 140,861 | 0 runtime observed | 0 | 0 | 0 |
| Semble `overflow` | 0 / 0 | 4 distinct contract-test events in failed CI; second CI not independently verified | 0 | 0 | 0 |
| Serena — no target observed | 0 / 0 | 0 observed | 0 | 0 | 0 |

**Collection gap:** folder `summary.json` counts **16 downloaded parsed logs**; the assembled analysis counts **115 `log_parsed` rows**, including **85 `not_selected`** and **14 `empty_archive`** rows. Its 113-sample wall-clock percentile therefore answers a different question from the folder’s 16-sample percentile. Report downloaded-log coverage, summarized-only coverage, and zero-filled rows separately before publishing another aggregate.

## Deep Audit — Workflows & Scripts (2026-09-30)

### Section 1: Bug & Correctness Sweep

Read-only checks covered all 52 `.github/workflows/*.yml` files and 159 `scripts/*.sh` and `scripts/*.py` files. YAML parsing, Python AST parsing, and Bash syntax checks found no parse errors. The findings below concern behavior, not syntax. Previously reported inventory drift, check-run polling latency, and log-collector double counting are not repeated.

- **ID:** SEC-001 · **File:** `.github/workflows/check_failure_triage.yml:219-225`; `scripts/check_failure_triage.sh:281-331` · **Severity:** Critical · **Category:** `security`  
  **Description:** Triage checks out the PR head using `GH_PAT` without `persist-credentials: false`, then passes PR text and check-run logs to Codex with `--sandbox danger-full-access`. Removing `GH_TOKEN` from the child environment does not address credentials persisted by checkout. **Inference:** prompt-injected diagnosis could read or use checkout credentials; confirm the effective checkout credential storage in this job. [NEEDS VERIFICATION]  
  **Recommended fix:** Set `persist-credentials: false` on both triage checkouts, remove any persisted Git authentication before model execution, and run diagnosis read-only and isolated from GitHub credentials. Keep authenticated issue creation in a separate post-model step; `.github/workflows/workflow-failure-heal-intake.yml:82-87` and `scripts/workflow_failure_heal_intake.sh:670-680` provide existing credential and sandbox patterns.

- **ID:** BUG-001 · **File:** `scripts/orchestrate_poll_process.sh:20664-20685` (also `scripts/orchestrate_poll_process.sh:19853-19862`, `scripts/orchestrate_poll_process.sh:23748-23749`) · **Severity:** High · **Category:** `bug`  
  **Description:** The poller checks a fetched PR head and its check-runs, but subsequently invokes `gh pr merge` without `--match-head-commit`. **Inference:** a push between those operations can make the merge request apply to a head that was not checked; auto-merge enrollment also needs later-head reauthorization.  
  **Recommended fix:** Require a validated, nonempty head SHA and pass `--match-head-commit` on every auto and direct merge branch, as `scripts/review_enable_auto_merge.sh:282-286` does. On a changed head, defer to the next review/synchronize run; separately clear or reauthorize an outstanding auto-merge setting after a push.

- **ID:** SEC-002 · **File:** `scripts/gh_helpers.sh:444-490`; `scripts/gh_helpers.sh:511-558` · **Severity:** High · **Category:** `security`  
  **Description:** Retry failures print unredacted `$*` and finally dump raw stderr. Unlike the separately escaped diagnostic at lines 460-461, arguments can contain an entire `-f body=...` comment—for example the editor-summary post at `.github/workflows/review_autofix.yml:5113`. **Inference:** a failed request can place untrusted multiline content in Actions logs, including lines resembling workflow commands; whether a particular payload is interpreted depends on the runner and failure path. [NEEDS VERIFICATION]  
  **Recommended fix:** Log only a sanitized command name, endpoint template, attempt, and failure class. Do not print request bodies, authorization arguments, or raw stderr; apply the existing `_gh_actions_escape` approach to bounded, redacted diagnostics in both retry helpers.

- **ID:** BUG-002 · **File:** `scripts/check_failure_triage.sh:151-177`; `scripts/check_failure_triage.sh:358-395` · **Severity:** Medium · **Category:** `bug`  
  **Description:** If the triage step’s PR metadata fetch fails, it substitutes `{}`. Empty state and head-repository fields pass both subsequent skip checks, so the script can proceed to create a triage issue without reconfirming that the PR is open and same-repository. The earlier workflow job checks origin, but that result can become stale before this fetch. **Inference:** this matters when the PR changes state or the later fetch is unavailable. [NEEDS VERIFICATION]  
  **Recommended fix:** Require a successful, parseable PR response with `state=open` and an exact head-repository match immediately before triage issue creation. On an unknown response, alert and stop rather than treating missing fields as approval.

### Section 2: GitHub API Call Redundancy Audit

Counts below are **calls in the stated code path**, not measured production totals; pagination and retries can increase request counts.

- **ID:** API-001 · **File:** `scripts/orchestrate_poll_process.sh:13933-13936` · **Severity:** Medium · **Category:** `api-redundancy`  
  **Description:** One reissue reads the same issue endpoint twice, once for `.title` and once for `.body`. **Current → proposed:** 2 GETs → 1 GET per reissue. This pinpoints a case within the earlier report’s broader orchestrator-lookup observation.  
  **Recommended fix:** Fetch one issue JSON object and extract both fields locally; where several issues need hydration, extend the existing `_fetch_candidate_issue_details_graphql` pattern in `scripts/orchestrate_poll_process.sh:14686-14756`, retaining its cache-miss fallback.

- **ID:** API-002 · **File:** `scripts/orchestrate_poll_process.sh:10431-10444` · **Severity:** Low · **Category:** `api-redundancy`  
  **Description:** On a snapshot miss, final-PR reconciliation reads the identical PR endpoint separately for state and `merged_at`. **Current → proposed:** 2 GETs → 1 GET per miss; a single response also avoids mixing states from two instants.  
  **Recommended fix:** Use one `_fetch_pr_json` response and the same `_jq_field` extraction already used for `final_pr_json_snapshot` at lines 10436-10439. No new batching helper is needed.

- **ID:** BATCH-001 · **File:** `scripts/orchestrate_poll_process.sh:15707-15714` · **Severity:** Medium · **Category:** `api-batching`  
  **Description:** Standalone recovery executes seven `gh issue list` requests, one per phase label, before its candidate-details GraphQL prefetch. **Current → proposed:** 7 list calls → potentially 1 aliased GraphQL call when each result fits its page, plus required pagination or REST fallback. Query cost and parity with the present `--limit 1000` need testing. [NEEDS VERIFICATION]  
  **Recommended fix:** Extend `_fetch_standalone_marker_issues_graphql`’s aliased-search pattern (`scripts/orchestrate_poll_process.sh:14578-14642`) to return phase-label sets, detect every `hasNextPage`, and preserve the seven-list path when results cannot be proved complete.

- **ID:** BATCH-002 · **File:** `scripts/orchestrate_poll_process.sh:15681-15695` · **Severity:** Medium · **Category:** `api-batching`  
  **Description:** The tracking-issue loop makes one paginated comments GET per row before extracting state. **Current → proposed:** `T` logical GETs → `ceil(T/25)` GraphQL batches for rows whose required state comments fit the batched response, **plus** per-row fallback for incomplete histories. Old or chunked state comments may require full REST pagination. This is a more specific candidate under the earlier report’s orchestrator per-row lookup observation. [NEEDS VERIFICATION]  
  **Recommended fix:** Extend `_fetch_candidate_issue_details_graphql` (`scripts/orchestrate_poll_process.sh:14686-14815`) with completeness information for these tracking issues; call `extract_latest_valid_orchestrator_state` on prefetched comments only when complete, otherwise retain the existing paginated GET.

- **ID:** API-003 · **File:** `scripts/gh_helpers.sh:74-94`; `scripts/gh_helpers.sh:439-486` · **Severity:** Medium · **Category:** `api-redundancy`  
  **Description:** `_is_gh_permanent_failure` recognizes several permanent errors but not an HTTP 401. With the default retry limit, a stable bad-credentials response takes up to five attempts with exponential sleeps. **Current → proposed:** up to 5 requests → 1 request per 401.  
  **Recommended fix:** Extend the existing permanent-failure classifier for verified authentication failures, before backoff in both `gh_retry` and `gh_retry_to_file`; keep rate-limit handling separate.

- **ID:** API-004 · **File:** `scripts/check_failure_triage.sh:391-395`; `scripts/gh_helpers.sh:439-486` · **Severity:** Medium · **Category:** `api-redundancy`  
  **Description:** Issue creation uses the generic retry helper after an open-issue dedup read at `scripts/check_failure_triage.sh:219-231`. **Inference:** if GitHub accepts a create but its response is lost, the helper can submit the non-idempotent create again before dedup is rerun. **Current → proposed:** up to 5 POST attempts → 1 POST, with 1 reconciliation read only after an ambiguous outcome. [NEEDS VERIFICATION]  
  **Recommended fix:** Add a single-attempt mutation path to `gh_helpers.sh`; on ambiguous failure, search for the existing fingerprint marker before another create. Follow the marker-reconciliation and process-cache pattern in `scripts/orchestrate_poll_process.sh:5928-5962`.

- **ID:** API-005 · **File:** `scripts/review_merge_train.sh:255-290` · **Severity:** Low · **Category:** `api-redundancy`  
  **Description:** When upserting an existing marker comment, `_mt_find_marker_comment_id` lists comments but discards their bodies; `_mt_upsert_comment` then GETs the selected comment to compare its body. **Current → proposed:** 2 GETs → 1 paginated comments lookup per upsert, before any conditional write.  
  **Recommended fix:** Have the marker lookup return both `id` and `body` from its existing response and pass both to the upsert. For a multi-PR release sweep, the cycle-local keyed-prefetch approach of `_fetch_candidate_issue_details_graphql` is the batching pattern to assess separately, with pagination fallback.

### Section 3: Code Duplication & Modularization Opportunities

- **ID:** DUP-001 · **File:** `scripts/label_helpers.sh:20-62` (also `scripts/orchestrate_poll_process.sh:2843-2898`; `scripts/validate_process.sh:1158-1190`) · **Severity:** Medium · **Category:** `duplication`  
  **Description:** Label colors/descriptions and check-and-create behavior have three implementations. The poller and validator read `.github/ai/label_contract.v1.json`; `label_helpers.sh` embeds a second catalog. Contract changes can therefore leave callers with different metadata or failure behavior.  
  **Recommended fix:** Make `scripts/label_helpers.sh` own `ensure_label_exists <label> [repo]`, reading the contract when available and retaining its embedded catalog as a documented fallback. Keep compatibility wrappers for the poller’s process cache and fail-open behavior; update the poller and validator callers without removing their existing identifiers in place.

- **ID:** DUP-002 · **File:** `scripts/review_run_reviewers.sh:1849-1859`; `scripts/review_run_reviewers.sh:1905-1915` · **Severity:** Low · **Category:** `duplication`  
  **Description:** Full and scoped reviewer prompt builders repeat the same untrusted comments, check-run, and slop-scan sections, including byte caps and trust-boundary text. A change to one block can silently alter reviewer treatment in only one mode.  
  **Recommended fix:** Add `emit_reviewer_common_context_sections()` in `scripts/review_run_reviewers.sh`; call it from both `emit_full_reviewer_prompt_context_sections` and `emit_scoped_reviewer_prompt_context_sections` at their current positions, preserving section order.

- **ID:** DUP-003 · **File:** `scripts/check_failure_triage.sh:58-79` (also `scripts/implement_diagnose_post_codex_failure.sh:49-64`; `.github/workflows/implement.yml:5170-5184`) · **Severity:** Low · **Category:** `duplication`  
  **Description:** Three callers source `gh_helpers.sh` and then carry near-identical fallback definitions of `_safe_gh_jq`; the canonical implementation is `scripts/gh_helpers.sh:580-594`.  
  **Recommended fix:** Keep `_safe_gh_jq <endpoint> [gh-api-options...]` in `gh_helpers.sh` as the shared implementation. Where support staging guarantees that helper, fail clearly if sourcing fails and remove the inline copies; retain a compatibility path only where an older support ref is explicitly supported.

No workflow pair in the similarly sized, non-comment line comparison exceeded the requested 70% near-duplicate threshold; the closest checked pair was `.github/workflows/internal-plan.yml:1-34` and `.github/workflows/internal-implement.yml:1-32` at 70.00%.

### Section 4: Expression Size Limit Risk Assessment

All 52 workflows parsed. Of 774 `run:` scalars measured, 225 contain `${{ }}` and were assessed; 549 without interpolation were excluded. Lengths below measure the YAML-decoded **template body**, not unknowable runtime substitution lengths.

- **ID:** EXPR-001 · **File:** `.github/workflows/implement.yml:986-1342` · **Severity:** Medium · **Category:** `expression-limit`  
  **Description:** “Stage workflow support files” is the only interpolated `run:` body above 15,000 characters: **16,985 characters**, with three interpolations and approximately **4,015 characters of headroom** to 21,000 before substitution growth. It is below the 18,000-character High threshold, but continued inline staging changes could cross the limit. [NEEDS VERIFICATION]  
  **Recommended fix:** Extract the body to a staged `scripts/` helper, passing `github.repository`, `PROMPT_PRELUDE_REFACTOR_ENABLED`, and `UNATTENDED_IDENTITY_REINJECT_ENABLED` through step `env:`. Preserve the step’s conditions and its support-ref checks.

The next-largest interpolated block is `.github/workflows/implement.yml:3223-3529` at **14,392 characters** (about **6,608** headroom), below the flag threshold. The longest measured `if:` scalar was **859 characters** at `.github/workflows/clarify.yml:19`, not near 21,000. No workflow exceeds the requested **800 KB** file-size warning threshold. The repository’s *stricter* workflow-size guard is addressed in Section 5.

### Section 5: Cross-Cutting Concerns

- **ID:** CONSIST-001 · **File:** `scripts/review_collect_pr_metadata.sh:25-29`; `scripts/review_collect_pr_metadata.sh:63-68` · **Severity:** Low · **Category:** `consistency`  
  **Description:** After sourcing `gh_helpers.sh`, this script redefines its public `gh_retry` name with a different, output-file-first signature. Its current local calls use that signature, but any later shared helper called in the same shell would see the incompatible replacement.  
  **Recommended fix:** Call canonical `gh_retry_to_file <outfile> gh ...` directly at this script’s call sites, leaving `gh_retry`’s existing signature intact.

- **ID:** DEAD-001 · **File:** `scripts/orchestrate_poll_process.sh:20925-20932`; `scripts/orchestrate_poll_process.sh:20948-20962` · **Severity:** Low · **Category:** `dead-code`  
  **Description:** Both follow-up refusal paths assign `RB_FOLLOWUP_REFUSED="true"`, but the script never reads that variable; the adjacent warnings and `REVIEW_BLOCKED_STATE_CHANGED` assignments perform the observable work.  
  **Recommended fix:** Remove the unused assignments, or explicitly consume the flag in the subsequent state/retry decision if refusal was intended to change it; test both refusal paths.

- **ID:** SHELL-001 · **File:** `scripts/stage_workflow_support.sh:138-145`; `scripts/stage_workflow_support.sh:198-208` · **Severity:** Low · **Category:** `shellcheck`  
  **Description:** ShellCheck reports SC2043 for two `for` loops over single literal filenames. They are not splitting bugs, but present one-off operations as iteration and leave warning noise in an otherwise useful lint sweep.  
  **Recommended fix:** Replace each with a direct assignment and its existing conditional/install body; preserve required-versus-optional failure behavior.

- **ID:** DEBT-001 · **File:** `.github/workflows/review_autofix.yml:1-7508` · **Severity:** Medium · **Category:** `tech-debt`  
  **Description:** This workflow measures **454,700 bytes**. Although below the prompt’s 800 KB warning, it has only **25,300 bytes** before the repository’s **480,000-byte CI guard** documented in `CLAUDE.md` §27. This is a separate, stricter size constraint, not an expression-limit finding.  
  **Recommended fix:** Extract a large remaining inline step into a `scripts/review_autofix_step_<slug>.sh` helper using the existing staged-support and step-wrapper pattern; verify the file remains below 480,000 bytes.

No `TODO`, `FIXME`, or `HACK` markers were found in the scoped workflow and script files. ShellCheck also reported warnings associated with intentional pattern matching and cross-function assignments; those are not presented as defects without a demonstrated failure path.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 1 | SEC-001 |
| High | 2 | BUG-001, SEC-002 |
| Medium | 9 | BUG-002, API-001, BATCH-001, BATCH-002, API-003, API-004, DUP-001, EXPR-001, DEBT-001 |
| Low | 7 | API-002, API-005, DUP-002, DUP-003, CONSIST-001, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `check_failure_triage.yml`, `check_failure_triage.sh`, `orchestrate_poll_process.sh`, `gh_helpers.sh`, and focused tests | Large |
| API call optimization | `orchestrate_poll_process.sh`, `review_merge_train.sh`, `check_failure_triage.sh`, `gh_helpers.sh`, and parity tests | Large |
| Code modularization | `label_helpers.sh`, `validate_process.sh`, `orchestrate_poll_process.sh`, `review_run_reviewers.sh`, two diagnosis/triage scripts, `implement.yml`, and tests | Medium |
| Expression size reduction | `implement.yml`, one staged `scripts/` helper, and contract tests | Medium |
| Medium/Low fixes | `review_autofix.yml`, `review_collect_pr_metadata.sh`, `stage_workflow_support.sh`, applicable shared helpers, and tests | Medium |
