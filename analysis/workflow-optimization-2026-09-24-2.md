## Executive Summary

- **Stop work after an unusable review fanout.** In `shubhodeep1/coding-workflows`, runs 35953733499, 35959343311, and 35960499945 each recorded **zero successful reviewers**, then made ten failed summariser attempts. Skipping summarisation in that specific state would avoid **2,555 seconds (42m35s) of backoff per affected run** while retaining a failed review outcome. **Confidence: high.**
- **Repair the repeated CI contract failure.** Eighteen of 19 failed CI runs stopped at `Review/judge/conflict strict-render contract test`; runs 35960907525 and 35961511685 show an assertion in `tests/test_assemble_prompt.py`. Fix the template-versus-expected-bytes mismatch without weakening the assertion. This could avoid repeated **roughly 16–18-minute failed CI cycles** seen in those two runs. **Confidence: high.**
- **Make failures diagnosable before adding retries.** Thirty-six review failures had zero-second duration and no failed step; five additional review runs were classified as one-second startup failures. Two selected runs returned HTTP 404 when logs were collected. Capture job state and validation annotations for these cases; the failure-rate benefit is **not yet estimable**. **Confidence: high for the visibility gap, low for its cause.**
- **Treat the cost and MCP aggregates as provisional.** The assembled context reports **112,714,643 tokens** across 120 runs with log telemetry, but 45 of 149 reported OpenRouter calls lack usage. Parsing both aggregate-job and individual-step logs duplicated at least two Semble queries and eight CI test fallbacks. Deduplicate before using these totals for savings targets. **Confidence: high.**
- **Separate unavailable tooling from failed tooling.** Three successful poller summaries report `SEMBLE_AVAILABLE: false`; the 44 reported Semble fallbacks are contract-test events, not production query failures. Add an availability reason and distinguish probes from runtime fallbacks. Serena has no observed query or probe events, so its benefit cannot be assessed. **Confidence: medium**, given sampled logs.

## Speed Optimizations

1. **Critical path — skip the summariser when no reviewer succeeded.** `review / codex-agent`, `Run reviewer models`, recorded zero successful reviewers before ten summariser attempts in runs 35953733499, 35959343311, and 35960499945. `scripts/summarize_reviewer_consensus.sh` applies nine uncapped exponential sleeps totaling **2,555 seconds**; the last is 1,280 seconds. Add a zero-success guard before invoking the summariser, emit `reason=no_successful_reviewers`, preserve failure artifacts, and fail the review closed. **Estimated saving:** at least the backoff per affected run, plus failed call time; **risk: low** if partial-success reviews retain their current path.

2. **Critical path through reruns — restore the strict-render contract.** CI runs 35960907525 and 35961511685 failed at `Review/judge/conflict strict-render contract test`; 18 of 19 CI failures share that step. Identify the first differing byte, lengths, and hashes of actual and expected prompts in test diagnostics, then correct the intended template or expectation—not the strictness of the test. **Estimated saving:** the observed failed cycles of 948 and 1,049 seconds when the mismatch is fixed; **risk: low** after confirming intended prompt semantics.

3. **Micro-optimization — investigate repeated PR lookup latency, not sweep batching.** The `Resolve PR for head branch` step took approximately **6.6 seconds** in review run 35963280342. Reuse an existing PR payload for *read-only, same-job* consumers if tracing shows a duplicate lookup; retain fresh state checks before merge or dispatch. **Potential saving:** 0–6.6 seconds on an affected path, conditional on finding duplication; **risk: low** with that boundary. The sweep already snapshots active runs rather than doing per-PR active-run reads.

Queueing is **not measurable** here: `run_started_at` equals `created_at` for all 1,000 run rows, including cancelled run 35931394370, which reports `cancelled_before_first_step` after 1,565 seconds. Collect job queued and actual started timestamps before proposing queue changes.

## Cost Optimizations

1. **Reduce oversized review context selectively.** Review runs 35941796583 and 35943802394 emitted `CONTEXT_BUDGET_WARN` at **177,799** and **177,987** prompt tokens against a 200,000-token window and 140,000-token warning threshold. Three costly successes—35953719426 (**26.14M** total tokens), 35961512038 (**22.09M**), and 35943802394 (**19.93M**)—account for about **60.5%** of the assembled token total. First measure prompt components by byte/token count; trim repeated old-review or diff context *after* preserving required evidence. Reaching 140,000 would remove approximately **37.8K prompt tokens per flagged call**, if the content is safely reducible. **Verified savings: none yet; quality risk: high if relevant review context is dropped.**

2. **Preserve effective cache reuse; measure fragmentation before changing prompts.** The assembled totals report **91,847,843 cache-read tokens**—81.5% of reported total tokens—but aggregate `cache_hit_rate` is null. The two available run values are **67.6751%** (35951084648) and **78.8368%** (35961512038); cache-write tokens are reported as zero despite reads, so zero should not be interpreted as no cache creation. Keep static instructions ahead of changing diff, memory, and run-specific content; log a non-content stable-prefix hash and per-model cache-read ratio. Dynamic-prefix fragmentation is a **hypothesis**, not a measured cause. **Token/dollar savings: unquantifiable until comparable prompt measurements; quality risk: low** for logging and stable ordering.

3. **Avoid unnecessary work, but do not blanket-downgrade models.** Runs 35959343311 and 35960499945 used a medium-reasoning `openai/gpt-5.6-luna` summariser for ten attempts despite zero successful reviewers; their reported usage is unavailable. Eliminate that work first. Existing fingerprint caps prevented further editor work for PR 4332 in runs 35962528683 and 35963517890; suppress repeat automatic dispatches for an unchanged capped fingerprint while retaining manual/head-change re-entry. **Measured token savings: unavailable** for failed calls and capped gates; **quality risk: low** with those conditions. Log model, reasoning level, role, usage availability, and outcome before testing a cheaper model profile; the logs do not justify a fleet-wide downgrade or a dollar estimate.

Semble logged **12 distinct queries / 163,675 bytes** in the available canonical job logs: ten `target=reviewer-context` queries and two `target=overflow` queries. The roughly 14–16 KB reviewer-context responses suggest bounded retrieval, but no comparable prompt-without-Semble measurement proves net token savings. Serena logged **zero** queries, response bytes, and tool replacements; it cannot yet be credited with replacing downstream model work or blamed for noisy response bytes.

## Reliability Improvements

1. **Fix the CI prompt contract before rerunning broadly.** The repeated `tests/test_assemble_prompt.py` assertion in CI runs 35960907525 and 35961511685 indicates a template/fixture mismatch; which side is wrong is not established. Print safe mismatch diagnostics, confirm the intended prompt bytes, and retain the strict test as the rollback gate. **Expected impact:** remove the dominant identified CI failure class, **18 of 19 CI failures**; do not fail open.

2. **Classify shared reviewer failures and stop futile summariser retries.** Runs 35959343311 and 35960499945 show six reviewer slots failing with `rc=226`, followed by ten summariser failures with the same code; 35953733499 also reached zero reviewers and the final backoff. A shared dependency is an **inference**, not a diagnosed provider cause. Log sanitized HTTP/status category, stage, model, retry classification, and remaining budget—never response bodies or credentials. Skip summarisation only on zero successful reviewers; preserve the failed outcome and existing alert. **Expected impact:** fewer wasted attempts and faster, better-classified failures; **rollback:** remove the guard without changing review acceptance criteria.

3. **Recover evidence for pre-job failures.** Of 46 review failures, **36** have no failure step and zero-second duration. Review run 35956138066 and startup-failure run 35956159642 also have missing archives with HTTP 404; existing rows mark workflow validation annotations unavailable. On zero-job/empty-archive cases, collect validation annotations and record `jobs_fetch_status`, archive status, and a bounded reason code. **Expected failure-rate reduction: unknown; diagnostic coverage: potentially 36 review failures plus startup cases.** Keep these outcomes visible rather than reclassifying them as skipped.

4. **Make ancillary failures explicit without weakening gates.** Review run 35963301039 failed `Collect PR metadata` with “Could not publish the linked-issue metadata integrity digest” after a one-issue GraphQL body-text fallback; log the digest stage and sanitized failure category, then retain the current fail-closed behavior. The linked promote run 35939056453 and `Test & Mark Stable Release` run 35939081506 failed at the promote cycle and Phase 4b editor-restored canary respectively; record correlated canary attempt, retry outcome, and terminal reason before altering either release gate. **Expected impact:** faster diagnosis; failure-rate effect unknown.

**Policy and MCP signals:** Observed `BREAK_GLASS` count is **0**; the two `CONTEXT_BUDGET_WARN` events indicate prompt-size risk, not evidenced rubric pressure. The **44 reported** `SEMBLE_FALLBACK` events all have `context=contract-test` and `target=overflow`; canonical logs contain **36 distinct** such events across nine CI runs and **zero observed runtime fallbacks**. This is healthy test coverage, not proof of a broken runtime rollout. Conversely, three poller summaries show Semble unavailable without probe reasons; add an availability result rather than counting those as query fallbacks. No Serena probe failures—or Serena probes—were observed.

## AI Memory Health

In ten distinct reviewer deep dives, `AI_MEMORY_TELEMETRY` recorded **10/10 retrievals with `records_selected > 0`** (26–33 records). Average `estimated_tokens` was **1,390.3 / 1,400 budget**; `keyword_method` was **llm: 10, plain: 0, none: 0**. Runs 35959343311 and 35943802394 illustrate successful retrieval even when the later review path failed or ran long. There were no observed zero-record, `enabled: false`, or `fail_open: true` retrieval entries. Keep retrieval enabled, but log selected-record contribution alongside total prompt size before attributing context warnings to memory; its approximately 1.4K-token budget alone does not explain a 177K-token prompt.

The same canonical logs contain **654** `ledger_emit_substate record-run-event` timeout warnings across ten review runs, including successful 35951084648 and failed 35960499945. These warnings are fail-open and cluster in output; they do **not** establish 654 separate 120-second delays. Emit one aggregated timeout counter and operation-stage summary per run, and coalesce repeated telemetry-only substate attempts while preserving start/end records. Five observed memory pushes needed more than one attempt. `finalize-task`, `promote`, `compact`, and processed-command operations were not observed in these deep dives; verify their emission in their respective workflows rather than inferring they never run.

## GH API Call Audit

| Path and evidence | Audit and smallest safe change | Call-count / rate-limit impact |
|---|---|---|
| Autofix sweep, run 35966261655: **9 candidates, 5 dispatched, 4 skipped active**, 18-second run; `sweep` took about 11 seconds. | `.github/workflows/review_autofix_sweep.yml` already fetches open PRs once and snapshots `queued`, `in_progress`, and `pending` for two workflows. **Keep** this batching and its active-run guard; log page counts, status-query outcomes, and elapsed time. | At least **7 reads + 5 dispatches = 12 calls** in the single-page case; no evidenced safe normal-path reduction. Avoids per-candidate active-run fanout. |
| Same sweep’s active-run snapshot code uses `2>/dev/null || true` on status fetches. | **Inference from code:** a failed snapshot could look empty. Emit the failed workflow/status and use a bounded targeted active-run lookup before dispatching an uncertain candidate; if that also fails, defer that candidate. | Extra calls only on failure; reduces duplicate-dispatch risk. Actual failure frequency is unmeasured. |
| Review `Collect PR metadata`, runs 35963301039, 35959343311: GraphQL closing references were empty and a batched body-text fallback resolved one issue. | Preserve the existing one-call aliased fallback, the cached PR payload, and paginated comment reads. Log endpoint class, pagination, retry count, and whether a cached payload served later *read-only* work. | The observed fallback adds **one batched GraphQL hydration**, not one call per linked issue. Remove a later duplicate PR read only if tracing proves it is read-only and same-job; **conditional saving: one call per affected run**. Retain fresh merge checks. |
| Review run 35963280342: `Resolve PR for head branch` took approximately **6.6 seconds**. Collector archive reads for 35956159642 and 35956138066 returned **HTTP 404**. | Time and count branch lookups by step; classify 404 separately from 429/rate-limit and retryable failures. | Lookup redundancy and total API-call rate are **unknown**; no observed 429 supports a rate-limit claim. |

These changes follow this repository’s `CLAUDE.md` §15 rule to reuse existing payloads, batch multi-item reads, and fall back safely on cache misses. The telemetry has **no per-endpoint call counts or rate-limit headers**, so an overall call-reduction percentage cannot be estimated.

## Prompt Cache & Memory System

Review run 35961512038 reports `cache_hit_rate=0.788368` with **17,333,028 cache-read tokens**; run 35951084648 reports `0.676751` with **5,595,036**. Do not average these into a fleet hit rate: the assembled aggregate is null, usage is unavailable for **45/149 reported calls**, and the runs are selectively observed. Log per-call model, stable-prefix hash, static/dynamic input sizes, cache-read/creation availability, and hit-rate calculation. Put volatile diff, PR state, and retrieved memory *after* the stable prefix where behavior permits. This should improve comparability and may improve reuse; **token and latency gains are not measurable yet**.

Semble’s canonical `reviewer-context` events returned **151,507 bytes across ten queries**; its two overflow events returned **12,168 bytes**. Track bytes admitted to the final prompt, not just bytes returned, to test whether retrieval replaces larger context rather than adding it. For poller runs 35961923640, 35963232111, and 35965746704, log why Semble was enabled but unavailable. The two ~89%-window `CONTEXT_BUDGET_WARN` reviews warrant component-level prompt accounting; preserve the memory retrieval budget and review evidence when trimming.

## Orchestrator Health

`orchestrate_poll` completed **29/29** runs successfully (p50 **326 seconds**), but successful completion does not establish wave progression: the supplied rows do not enumerate clarification loops, deferrals, conflict-heal retries, or terminal task states. Poller runs 35961923640, 35963232111, and 35965746704 report Semble unavailable. Emit one per-cycle state-transition summary—wave, prior/new state, deferred reason, conflict attempt, active-run count, and MCP availability reason—without prompt content. **Expected impact:** distinguish healthy idle polling from stalls; speed gain unknown.

Most `clarify`, `plan`, and `implement` rows are non-success/non-failure outcomes: **128/136**, **118/127**, and **116/127** respectively, predominantly skips in the underlying run data. Do not call them clarification loops without linked task IDs. Record a structured skip reason and correlation ID. Existing autofix safeguards are visible: sweep 35966261655 skipped four active candidates, while runs 35962528683 and 35963517890 reported a fingerprint cap for PR 4332. Track repeated capped dispatches and reopen eligibility on head change; preserve the cap and human escalation.

## Pipeline Flow Bottlenecks

| Flow segment | Observed bottleneck | Diagnostic or safe next action |
|---|---|---|
| Clarify → plan → implement | All-run p50 is **1 second** in each family, dominated by skips; implement run 35957593062 took **1,702 seconds**. | Correlate task IDs and emit skip reasons before estimating productive-stage latency. |
| Review/autofix | Family p95 **5,328 seconds**; run 35943802394 lasted **10,574 seconds**; three zero-reviewer runs spent at least **2,555 seconds each** in summariser backoff. | Remove zero-success summariser work first; break reviewer time into setup, model compute, retry sleep, and post-processing. |
| CI → validation/release | CI p50 **2,048 seconds** and 18 strict-render failures; linked promotion/canary runs 35939056453 and 35939081506 failed after **2,982** and **2,897 seconds**. | Resolve the strict prompt mismatch; log canary retry outcomes without relaxing release checks. |
| Poll → merge/conflict | Poller p50 **326 seconds**; active-run skips are visible, but wave, merge-wait, and conflict-heal durations are not. | Emit correlated transition and wait-reason timestamps. |

Queue delay is unobserved; compute and retry dominate the evidenced long tail. These stage durations must **not** be added as an end-to-end estimate because overlap and task linkage are unavailable.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** long review tail (p95 **5,328 seconds**, run 35943802394 **10,574 seconds**), CI (p50 **2,048 seconds**), and zero-success summariser backoff (runs 35953733499, 35959343311, 35960499945).
- **Top failure modes:** strict-render CI assertion (**18 runs**); review failures with no step (**36 zero-second runs**); reviewer `rc=226` clusters and the metadata-integrity failure in 35963301039. The leading cost driver is review: the three identified costly review successes contributed approximately **60.5%** of assembled tokens.
- **Top three actions, in order:** **(1)** fail the review promptly after zero successful reviewers, logging a sanitized cause; **(2)** diagnose and repair the strict-render byte mismatch; **(3)** deduplicate log telemetry and add zero-job, API, memory-substate, and MCP-availability reason codes. These preserve existing acceptance and release gates.

## Metrics Appendix

**Scope:** 1,000 runs created **September 23, 2026 22:49:45 UTC–September 24, 2026 06:50:37 UTC**, one repository. Rates below use *all* runs in each row, including skips; p50/p95 likewise include them. The assembled context has telemetry on **120** runs; the underlying collector summary had **26** parsed log archives, and 42 success rows have summaries. Two selected archives returned HTTP 404. This is not full-log coverage of 1,000 runs.

| Family | Runs | Success | Failure | Cancelled | Other | Success / failure rate | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---|---|
| All / `shubhodeep1/coding-workflows` | 1,000 | 366 | 73 | 16 | 545 | 36.6% / 7.3% | 7 / 2,513 s |
| `review_autofix` | 208 | 141 | 46 | 13 | 8 | 67.8% / 22.1% | 76.5 / 5,328.1 s |
| `ci` | 43 | 24 | 19 | 0 | 0 | 55.8% / 44.2% | 2,048 / 2,547.6 s |
| `orchestrate_poll` | 29 | 29 | 0 | 0 | 0 | 100% / 0% | 326 / 350.8 s |
| `clarify` / `plan` / `implement` | 136 / 127 / 127 | 8 / 9 / 7 | 0 / 0 / 1 | 0 / 0 / 3 | 128 / 118 / 116 | Skips dominate | p50 1 / 1 / 1 s |

“Other” includes **540 skipped** and **5 startup-failure** outcomes; it is not a successful-work count.

| Assembled cost/cache metric | Value | Coverage caution |
|---|---:|---|
| OpenRouter prompt / completion / total tokens | 20,129,138 / 739,014 / **112,714,643** | 120 telemetry-bearing runs; total includes reported cache reads |
| Cache read / reported cache write tokens | 91,847,843 / 0 | Write reporting may be unavailable |
| OpenRouter calls: usage available / unavailable | 104 / 45 of 149 | **30.2%** unavailable; call counts require overlap audit |
| Aggregate `cache_hit_rate` | **null** | Run examples: 35951084648 **0.676751**; 35961512038 **0.788368** |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | **10,000 / 8,176,000** | 115 mixed, selectively observed samples; not model-call percentiles |
| `break_glass_count` / `context_budget_warn_count` | **0 / 2** | Warnings: runs 35941796583 and 35943802394 |

| GH API signal | Measured or bounded value |
|---|---|
| Sweep 35966261655 | **≥12 calls** in the single-page case: one PR-list read, six active-status reads, five dispatches; actual pagination/retries unknown |
| Sweep 35962512762 | **≥10 calls** by the same path: three dispatches |
| PR branch resolution 35963280342 | Approximately **6.6 s**; call count unknown |
| Collector log-archive HTTP 404 | **2** selected runs: 35956159642, 35956138066 |
| Per-endpoint totals / 429 events / rate-limit resets | **Not collected**; no fleet-wide rate-limit conclusion |

| MCP event | Assembled reported | Distinct canonical full-log evidence | Interpretation |
|---|---:|---:|---|
| Semble queries / logged bytes | **20 / 256,816 B** | **12 / 163,675 B**: `reviewer-context` 10 / 151,507 B; `overflow` 2 / 12,168 B | Two reported queries are confirmed aggregate/step duplicates; six additional queries / 62,346 B belong to run 35961512038 without a canonical full log here. A provisional overlap-adjusted aggregate is **18 / 226,021 B**, not a verified fleet count. |
| Semble fallbacks | **44**; contract-test **44**, runtime **0** | **36 distinct**, all CI `target=overflow`, `context=contract-test` | Runs 35960907525 and 35961511685 each report eight versus four canonical events. Runtime fallback/query rate in the 12 canonical review queries: **0/12**; a test-fallback/query rate would be misleading. |
| Serena queries / response bytes / tool calls | **0 / 0 B / 0** | No emitted events | Per-tool breakdown: **none observed**; replacement efficiency unknown. |
| Serena fallbacks / probes | **0 / 0** | No emitted events | Absence of probes is not evidence of availability. |
| Other MCP servers observed | **None** | No other emitted `<NAME>_QUERY`, `_FALLBACK`, or `_PROBE` prefixes | Keep generic-prefix coverage in the analyst so future servers are counted. |

| MCP target availability evidence | `probe_ok` | `probe_failed` | `probe_skipped` | Gap |
|---|---:|---:|---:|---|
| Semble `reviewer-context` / `overflow` | 0 | 0 | 0 | Query and synthetic-fallback events exist, but no target probe outcome was supplied. |
| Semble poller availability, target unspecified | 0 | 0 | 0 | Three poller summaries say unavailable; none supplies a probe result or reason. |
| Serena, target unspecified | 0 | 0 | 0 | No query, per-tool, fallback, or probe evidence. |

## Deep Audit — Workflows & Scripts (2026-09-24)

### Section 1: Bug & Correctness Sweep

The audit covered all 50 `.github/workflows/*.yml` files and all 90 shell and 57 Python files directly under `scripts/`. All workflows parsed as YAML; the scripts passed read-only `bash -n` or Python AST syntax checks. The existing report already identifies the strict-render CI failure and historical zero-reviewer summariser retries; those findings are not repeated here.

- **SEC-001** — **File:** `.github/workflows/test-and-mark-stable.yml:162-184`; also `.github/workflows/mark-stable.yml:44-55` and `.github/workflows/workflow-log-analysis.yml:1160-1168`. **Severity:** High. **Category:** `security`. **Description:** These `run:` bodies insert `${{ github.ref_name }}` directly into double-quoted shell assignments. The value becomes script source *before* the shell parses it, so a ref containing shell substitution syntax could execute that substitution before the subsequent ref check; the release gate explicitly accepts arbitrary refs in `gate_only` mode. Whether repository ref-naming and dispatch permissions make a malicious ref reachable needs confirmation. **[NEEDS VERIFICATION]** **Recommended fix:** Pass the ref through step `env:` and assign from the environment in the script, as `implement.yml:510-516` does for `DEFAULT_CHECKOUT_REF`. Keep the existing `stable` checks.

- **BUG-001** — **File:** `scripts/label_helpers.sh:179-229`. **Severity:** High. **Category:** `bug`. **Description:** `set_issue_phase_label_resilient` reads all labels, computes a replacement, then `PUT`s the entire set at lines 195-220. *Inference:* a concurrent label addition between the read and PUT can be erased, including an escalation label. If the read fails, lines 198-203 instead add the target without removing a conflicting phase label. **Recommended fix:** Extend the targeted phase-label mutation pattern in `scripts/orchestrate_poll_process.sh:2900-2956`: remove only observed conflicting phase labels, add the target, and re-read or retry when concurrent changes matter. Preserve unrelated labels and an explicit cache-miss fallback.

- **BUG-002** — **File:** `scripts/orchestrate_poll_process.sh:3171-3247`, `19436-19450`. **Severity:** Medium. **Category:** `bug`. **Description:** `_fetch_issue_labels_batch_graphql` requests `labels(first: 50)` without `pageInfo`. Its caller treats any returned issue key as complete and skips REST fallback. *Inference:* an issue with more than 50 labels could have a phase or escalation label omitted from reconciliation. **[NEEDS VERIFICATION]** **Recommended fix:** Request `pageInfo.hasNextPage`; omit truncated entries from the batch result and use a paginated REST lookup for those issues, following the pagination-fallback approach in `scripts/gh_helpers.sh:800-807`.

- **BUG-003** — **File:** `scripts/summarize_reviewer_consensus.sh:110-143`. **Severity:** Medium. **Category:** `bug`. **Description:** The comment says to discard files containing *only* a failure marker, but `grep -q '^.*failed after retries'` discards any nonempty reviewer output containing that phrase. If all inputs match, the script writes a “No findings reported” ledger. The occurrence in a genuine review is unverified. **[NEEDS VERIFICATION]** **Recommended fix:** Select inputs using their `status_${PREFIX}_*.txt` success files, or match the complete failure-marker format rather than a phrase anywhere in the review.

- **SEC-002** — **File:** `scripts/gh_helpers.sh:621-633`; callers include `scripts/check_failure_triage.sh:152-199`. **Severity:** Medium. **Category:** `security`. **Description:** When an API command exits successfully but yields invalid JSON, `gh_api_json_to_file` prints the first 50 *raw response* lines to the workflow log. Its callers retrieve PR and issue objects, so a malformed or truncated response could expose their content rather than a bounded diagnostic. An actual disclosure was not observed. **[NEEDS VERIFICATION]** **Recommended fix:** Log endpoint class, byte count, parse status, and a non-content digest; do not print the response body.

### Section 2: GitHub API Call Redundancy Audit

Counts below describe the cited execution paths **before retries or pagination**, not measured fleet totals. The previously reported autofix-sweep snapshot failure is already covered in the report’s GH API Call Audit and is not duplicated.

- **API-001** — **File:** `scripts/orchestrate_poll_process.sh:10431-10443`. **Severity:** Medium. **Category:** `api-redundancy`. **Description:** When `final_pr_json_snapshot` does not match `final_pr`, the same `pulls/${final_pr}` endpoint is fetched separately for `.state` and `.merged_at`. **Calls:** 2 reads → 1 read on that branch; the matching-snapshot branch already makes 0. **Recommended fix:** Fetch one JSON payload and derive both fields locally, using the function’s existing snapshot-first pattern. Keep later freshness checks before a merge.

- **API-002** — **File:** `scripts/orchestrate_poll_process.sh:13929-13936`, `16616-16624`. **Severity:** Medium. **Category:** `api-redundancy`. **Description:** Both managed and standalone reissue paths fetch the same issue twice consecutively for its title and body. **Calls:** 2 reads → 1 read per reissued issue on either path. **Recommended fix:** Fetch one issue object through `gh_retry _safe_gh_jq` and extract both fields locally; retain the existing empty-on-failure behavior.

- **BATCH-001** — **File:** `scripts/orchestrate_poll_process.sh:21284-21317`. **Severity:** Medium. **Category:** `api-batching`. **Description:** After review-blocked handling, the poller refreshes labels with one REST call per `ISSUE_NUMS` entry and another for each newly reissued number. The repository already provides `_fetch_issue_labels_batch_graphql` at lines 3171-3247. **Calls:** `N + R` reads → approximately `ceil((N + R)/25)` batch reads, plus targeted REST fallbacks for missing or truncated entries; `R` excludes numbers already read. **Recommended fix:** Batch the union of current and reissued issue numbers, then apply the existing helper’s per-key fallback contract. Do not reuse the pre-mutation label snapshot as a fresh result.

- **BATCH-002** — **File:** `scripts/orchestrate_poll_process.sh:12916-12948`. **Severity:** Medium. **Category:** `api-batching`. **Description:** `close_linked_pr` fetches each candidate PR inside its loop to inspect state, head, base, and body. **Calls:** `N` metadata reads → `ceil(N/25)` aliased GraphQL reads on a fully successful batch; up to `N` targeted reads remain necessary on batch misses. The up-to-`N` PR-close mutations are unchanged. **Recommended fix:** Add a cycle-local aliased PR-metadata helper following `_fetch_linked_pr_status_graphql` at lines 14818-14938, including all fields used by the implementation-PR check. Retain per-candidate fail-open lookup and state checks around mutations. **[NEEDS VERIFICATION]** that the candidate counts justify the added helper.

- **API-003** — **File:** `scripts/gh_helpers.sh:610-663`, `678-726`. **Severity:** Medium. **Category:** `api-redundancy`. **Description:** Unlike `gh_retry` at lines 455-465, `gh_api_json_to_file` and `curl_gh_api` do not stop on permanent failures such as HTTP 404; both can sleep even after their final attempt. **Calls:** up to 5 requests → 1 request for a classified permanent failure, with the default `GH_RETRY_MAX_ATTEMPTS`. **Recommended fix:** Apply the existing `_is_gh_permanent_failure` policy to captured `gh` errors and classify curl HTTP statuses before backoff. Preserve retries for transient and rate-limit responses.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — **File:** `scripts/label_helpers.sh:23-177`; duplicates at `scripts/orchestrate_poll_process.sh:2843-2898`, `scripts/validate_process.sh:1158-1192`, and `.github/workflows/review_autofix.yml:1529-1532,5429-5443,5622-5633`. **Severity:** Medium. **Category:** `duplication`. **Description:** Label creation, colors, descriptions, and already-exists handling have several implementations and workflow-local fallbacks. They draw on different copies or defaults for the `.github/ai/label_contract.v1.json` catalog. **Recommended fix:** Make `scripts/label_helpers.sh` own `ensure_label_exists(label_name, repo)` with the contract-file lookup and a self-contained fallback when that file is absent. Update the named script and workflow callers to stage/source it, retaining their current missing-helper fallback behavior and the poller’s cycle-local confirmation cache.

- **DUP-002** — **File:** `scripts/review_enable_auto_merge.sh:39-57`; duplicate at `.github/workflows/review_autofix.yml:1548-1566`. **Severity:** Low. **Category:** `duplication`. **Description:** Both paths independently fetch `.head.sha` and refuse merge-authorization labels if it differs from the reviewed SHA. **Recommended fix:** Put a side-effect-free `reviewed_pr_head_is_current(repo, pr_number, expected_sha)` in a new `scripts/review_head_helpers.sh`; update both callers while preserving their distinct warning text and fail-closed behavior. Do not source the executable auto-merge script merely to reuse its function.

The two small internal plan and implement wrappers share substantial structure (`.github/workflows/internal-plan.yml:11-34`, `internal-implement.yml:12-32`), but their command predicates and permissions differ. A common runtime wrapper would obscure those gates; retain the separate workflows and their existing predicate-parity tests.

### Section 4: Expression Size Limit Risk Assessment

Lengths are **static decoded `run:` body character counts**, not unknowable runtime-expanded values. Only blocks containing `${{ }}` were counted: 225 across the 50 workflows. Runtime substitutions can change the final lengths.

- **EXPR-001** — **File:** `.github/workflows/implement.yml:981-1337`. **Severity:** Medium. **Category:** `expression-limit`. **Description:** The interpolated “Stage workflow support files” block is approximately **16,985 characters**, leaving **4,015** against 21,000 and only **1,015** before the 18,000 high-risk threshold. Its three substitutions include `github.repository` and two repository variables. **Recommended fix:** Extract the body into a staged script under `scripts/`, pass expression values through step `env:`, and preserve the step name and existing outputs, as the `review_autofix_step_*.sh` extractions do.

- **EXPR-002** — **File:** `.github/workflows/implement.yml:3218-3524`. **Severity:** Low. **Category:** `expression-limit`. **Description:** The interpolated destructive-commit preflight block is approximately **14,392 characters**: **608** below the 15,000 medium-risk threshold and **6,608** below the hard limit. This is a growth watchpoint, not a current threshold breach; its `<<` strings are output delimiters, not embedded prompt templates. **Recommended fix:** If the step grows, extract it to a staged `scripts/implement_preflight_destructive_guard.sh`, passing its expression value through `env:` and retaining its outputs.

No interpolated block reached 18,000 characters. The longest `if:` value measured 739 characters; no workflow exceeded 800 KB. Separately, the repository documents a stricter **480,000-byte CI guard** than the requested 1 MB assessment: `review_autofix.yml` is 429,177 bytes, leaving 50,823 bytes to that local guard.

### Section 5: Cross-Cutting Concerns

- **SHELL-001** — **File:** `scripts/orchestrate_poll_process.sh:9262-9313`. **Severity:** Low. **Category:** `shellcheck`. **Description:** ShellCheck reports SC2155 at line 9266: `local now_epoch="$(date +%s)"` masks a failed `date` command. The value later enters arithmetic and `jq --argjson` without validation. **Recommended fix:** Declare and assign separately, check the command’s status and numeric result, and skip the staleness update on failure.

- **DEAD-001** — **File:** `scripts/orchestrate_poll_process.sh:19475-19496`. **Severity:** Low. **Category:** `dead-code`. **Description:** `LINKED_PR_NUM` is initialized and assigned during linked-PR reconciliation but has no read in the audited workflow or script sources; the loop uses the candidate JSON to derive state separately. An indirect consumer of this shell variable has not been ruled out. **[NEEDS VERIFICATION]** **Recommended fix:** Confirm no sourced caller depends on it, then remove the two assignments; otherwise document and test its output contract.

No `TODO`, `FIXME`, or `HACK` markers were found in the audited workflow and script paths. ShellCheck also reports warnings on several scripts; only the diagnostic with a traced failure path is raised above.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | SEC-001, BUG-001 |
| Medium | 10 | BUG-002, BUG-003, SEC-002, API-001, API-002, BATCH-001, BATCH-002, API-003, DUP-001, EXPR-001 |
| Low | 4 | DUP-002, EXPR-002, SHELL-001, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 3 release/analysis workflows; `scripts/label_helpers.sh` | Medium |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/gh_helpers.sh` | Medium |
| Code modularization | Label and review helpers, their script callers, and `review_autofix.yml` | Large |
| Expression size reduction | `implement.yml` and staged script(s) | Medium |
| Medium/Low fixes | Reviewer summariser, poller, API helper, and associated tests | Medium |
