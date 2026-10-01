## Executive Summary

- **Stop the repeated resolver failure first.** All four failed review/autofix runs in this window failed at the resolver scope check for PR #5596, after running for 465–1,017 seconds. Preserve the fail-closed check, but log a sanitized rejection reason and suppress another same-head attempt until its inputs change. **Impact:** potentially avoids another failed run of that duration; **confidence: high** in recurrence, medium in savings. (Runs 36792120478, 36793083550, 36794140554, 36796079841.)
- **Reviewer compute is the dominant measured critical path.** In the 10 fully logged slow, successful reviews, `Run reviewer models` took a median **1,792 seconds**; all 10 ended `clean_review_no_commit`. Measure per-model latency and agreement before testing a smaller clean-review path. **Impact:** a *hypothetical* 10% reduction in that step is about 179 seconds per affected run; **confidence: high** in the bottleneck, low in achievable savings.
- **Two independent CI defects caused the other failures.** Run 36795845630 failed inventory parity because `scripts/guard_differential.py` was referenced but undocumented; run 36796918795 failed Ruff with 24 E101 errors in `scripts/claude_issue_route.py`. Run both checks before dispatching expensive downstream work. **Impact:** prevents recurrence of these failed CI runs; **confidence: high**.
- **Fix measurement before assigning dollar savings.** Composite and split-step logs duplicate OpenRouter, Semble, and context-warning lines. The reported **689,620,106** OpenRouter tokens correspond to **344,810,053** unique logged tokens in this sample; deduplication changes the *report*, not provider charges. Only 16 of 1,000 collected rows have downloaded logs. **Impact:** roughly halves the overstatement in these reported token totals; **confidence: high**.

## Speed Optimizations

1. **Critical path — diagnose and arrest same-head resolver failures.** The four PR #5596 failures reach `Run Codex resolver, validate, stage, commit` before reporting `Resolver scope check failed closed (ValueError)`; three logged finalizations say `conflict_resolver_failed` with no edits pushed. **Likely category:** scope verification during conflict resolution; the specific invalid input is *not logged*. Add a pre-invocation input/snapshot check and emit `reason_code`, input-presence flags, changed-file counts, and resolver-attempt number on rejection. Keep the scope check fail-closed; hold repeated attempts for the same head and unchanged support inputs. **Estimated savings:** up to one otherwise repeated 465–1,017-second run when the cause is detectable or unchanged; **risk:** low for logging, medium for attempt suppression—recheck whenever inputs change.
2. **Critical path — measure a narrower clean-review path.** The 10 slow reviews took 2,054–2,570 seconds overall; their reviewer-model step took 1,085–2,003 seconds, and each finished without a commit (for example, runs 36794085787 and 36794168567). Record each pass’s duration, reasoning level, cache status, and contribution to the final verdict. Only then shadow-test conditional second-pass reduction against the existing verdict. **Estimated savings:** 179 seconds per affected run at an *assumed*, unproven 10% step reduction; **risk:** high if review coverage is reduced without parity evidence. Logging alone has no demonstrated latency saving.
3. **Secondary critical-path outlier — guard disk cleanup rather than remove it.** `Free disk space` took a median 82.5 seconds across those 10 runs, but 299 seconds in run 36794168567 and 712 seconds in 36794085787. The latter reclaimed 1.863 GB of Docker images and 4.9 GiB of tool cache. Record free space before/after each cleanup component; consider skipping a component only when a tested free-space threshold is met, with the current cleanup as fallback. **Estimated savings:** up to the skipped component’s measured time, bounded by 72–712 seconds for the *whole step* in this sample; **risk:** medium because the workflow documents prior disk exhaustion.
4. **Queueing, not compute — reduce unnecessary dispatch only after classifying skips.** Unselected summaries report hosted-runner waits of approximately 21–49 seconds in review/intake runs 36797321382, 36797334715, 36797336986, and 36797317211. Log dispatch eligibility and skip reason before changing triggers. **Estimated savings:** up to the observed wait on an affected eligible run if contention falls; **risk:** medium, since the data do not establish that skipped runs caused those waits.

## Cost Optimizations

1. **Correct token accounting first.** Across 10 logged slow reviews, the collector counts 268 OpenRouter usage lines and 689,620,106 tokens; matching composite/split-step lines yield **134 calls and 344,810,053 unique logged tokens**. Deduplicate by run, attempt, and event identity, retaining a source-step reference. **Estimated billed savings:** zero from this fix; **reporting improvement:** removes the observed 2× overcount. Twelve of the 134 unique calls have unavailable usage, so even the corrected total is incomplete.
2. **Measure model-selection and reasoning tradeoffs before changing the roster.** The 10 slow reviews emitted 74 first-pass and 60 subsequent-review usage events across multiple models; runs 36794085787, 36794168567, and 36794153315 logged reviewer reasoning effort `xhigh`. Emit per-slot elapsed time, unique tokens, cache support, and verdict contribution, then compare a lower-effort or narrower path on clean reviews. **Estimated savings:** not defensibly quantifiable without price and verdict-parity data; **quality risk:** high if independent review is removed. Do not treat `clean_review_no_commit` as proof a review was unnecessary.
3. **Reduce prompt expansion without dropping relevant evidence.** Four distinct slow runs warned at 144,963–176,914 prompt tokens against a 140,000-token warning threshold: 36794104283, 36794168567, 36794162245, and 36794153315. Keep invariant instructions at a stable prefix; put run-specific diffs, memory, and Semble results later; measure which sections grow. **Illustrative, not forecast savings:** 10% of the sample’s 51,282,230 unique logged prompt tokens is about 5.13 million tokens. **Quality risk:** medium; trim only demonstrably redundant context and compare review findings.
4. **Assess Semble against a baseline, not query volume alone.** Deduplicated logs show 10 `reviewer-context` queries returning 148,391 logged bytes and four queries each for `overflow` (22,328 bytes) and `conflict-resolver-context` (37,752 bytes). Review queries returned 12 chunks each; for example, run 36794168567 logged 14,347 bytes in 461 ms. Log source bytes and prompt bytes *before and after* insertion to determine whether Semble replaces larger expansion or adds context. **Estimated savings:** unknown without that baseline; **quality risk:** low for measurement. Serena has no query, tool-call, response-byte, or probe events here, so replacement efficiency cannot be assessed.
5. **Avoid unchanged-input reruns, but do not misprice them.** The four PR #5596 failures have zero parsed OpenRouter usage despite resolver execution. Log resolver-provider usage separately and key a safe same-head hold to the failure category. **Estimated savings:** up to a repeated resolver attempt; **dollar amount:** unavailable.

## Reliability Improvements

1. **Resolver scope-check loop — highest priority.** Four of four review/autofix failures in this window are PR #5596 resolver-scope failures; later failure reports show streak values 11–13, which are reported streaks, *not* 13 independently observed failures here. The resolver queried `overflow` for `.github/workflows/test-and-mark-stable.yml` and `conflict-resolver-context`, then rejected scope verification. Emit a structured, sanitized `RESOLVER_SCOPE_AUDIT` with validation stage, reason code, allowed/observed file counts, snapshot validity, and retry decision. Validate what can be checked before model invocation; retain refusal to retry or commit when scope is unverifiable. **Expected impact:** prevents identical-input repeat failures once classified; **rollback:** disable only suppression, never the fail-closed check.
2. **Deterministic CI defects.** `tests-release-and-log-analysis / Inventory parity` failed in run 36795845630; `static-checks / Python lint (ruff)` failed with 24 mixed-indentation errors in run 36796918795. Run the same parity and Ruff commands as an early PR check, and emit one failure summary containing check name, error count, and first repository-relative location. **Expected impact:** avoids these two defect classes reaching lengthy parallel CI; **rollback:** retain existing CI as authoritative.
3. **Distinguish fail-open tests from an outage.** The reported 16 `SEMBLE_FALLBACK` lines are eight distinct contract-test events: four each in CI runs 36795845630 and 36796918795, all `target=overflow`, `context=contract-test`, with an intentionally missing executable. Runtime fallbacks are **zero**. Tag synthetic events so operational alerts exclude them, while continuing to alert on actual runtime fallbacks. **Expected impact:** fewer false rollout alarms; **rollback:** keep raw lines accessible.
4. **Make risk signals countable.** The eight reported `CONTEXT_BUDGET_WARN` lines are four distinct review warnings, not eight independent incidents; they indicate prompt-size pressure, not observed `BREAK_GLASS` policy pressure. `BREAK_GLASS` is zero in downloaded logs. Emit a unique event ID for warnings and model/context fields without prompt contents. **Expected impact:** reliable run-level alerting; **rollback:** preserve current warning text during transition. Serena has **zero probes and zero fallbacks** here; absence of probe data cannot establish either availability or a broken rollout.

## AI Memory Health

In 13 distinct logged review runs, `AI_MEMORY_TELEMETRY` records **13/13 successful retrieves with records selected**: 100% observed hit rate, no zero-record result, average **1,382 estimated tokens of a 1,400-token budget** (98.7%), and `keyword_method=llm` in all 13 (`plain=0`, `none=0`). No parsed event has `enabled=false` or `fail_open=true`. Run 36794085787, for example, selected 24 records at 1,380 estimated tokens.

Writes succeed but merit timing diagnostics: 26 `record-run-event` and 10 `record-candidate` operations all report `ok=true` and a push; **19/36 required two or three push attempts**. Log per-attempt elapsed time, retry/backoff reason, and total memory-step duration, without record contents. The slow-review `record-run-event` start step took a median approximately 67 seconds, but the logs do not apportion that time to pushes. No `finalize-task`, `promote`, `compact`, or processed-command events appear in these deep dives; verify emission in workflows that execute those operations rather than inferring failure.

## GH API Call Audit

**Call volume and rate-limit reduction cannot be measured from this window:** the downloaded logs do not provide per-endpoint request, cache-hit, or retry counters, and no real 429 event was established. Do not interpret that absence as zero API traffic or zero rate-limit risk.

- **Review watchdog, code-identified redundancy:** `scripts/review_run_reviewers.sh` makes a PR-state preflight read and allows each reviewer watchdog to read the same PR state every ninth iteration. The logs establish multiple reviewer slots, not the number of watchdog reads. Add no-request telemetry in the existing `gh_retry` path: workflow/job/step, endpoint class, attempt count, status class, elapsed milliseconds, rate-limit wait, and cycle-cache hit. If duplicate reads are confirmed, share a short-lived state result among concurrent watchers while preserving a fresh closure check. **Conditional impact:** up to *N−1* duplicate reads per polling instant for N active watchers; **risk:** stale closure detection, requiring a bounded TTL.
- **Poller, code-identified per-item pattern:** `scripts/orchestrate_poll_process.sh` reads each unchecked advisory-follow-up issue inside `security_pass_unblock_filed_advisory_followups`; some blocked rows also paginate comments. This window has eight successful `orchestrate_poll` runs but no full poller logs, so frequency is unknown. Log row count, issue-read count, pages, and cache source. If multiple rows recur and existing prefetch cannot serve them, use the repository’s aliased-GraphQL/fail-open pattern. **Conditional impact:** N issue reads become approximately `ceil(N/batch_size)` batch reads, while legacy reads remain on batch failure.
- Follow **`CLAUDE.md` §15**: extend an existing read before adding one, prefetch outside loops, and fail open to the smallest legacy call. Aggregate the proposed counters per job/step; avoid logging URLs with sensitive parameters or response bodies.

## Prompt Cache & Memory System

The collector’s aggregate `cache_hit_rate` is **null**. Only two slow runs report a complete rate: **85.35%** in 36794115478 and **90.74%** in 36794085787. Across the deduplicated token sample, cache reads are 286,623,151 of 344,810,053 logged total tokens—an **83.1% read-token share, not a global hit rate**; 12 usage events lack complete usage. Reviewer slot logs also mark `google/gemini-3.8-flash` cache status `unsupported` on 20 slot-status emissions across the 10 slow runs. Preserve per-model availability and do not impute missing cache values as misses.

The actionable cache-fragmentation hypothesis is changing PR/diff/memory text before a reusable prefix. Log stable-prefix hash, dynamic-section sizes, cache-read/write tokens, and `CONTEXT_BUDGET_WARN` per model call; place invariant rubric first and dynamic context later, without logging contents. Compare hit rate and review findings before/after. **Estimated token/latency impact:** unknown until per-call comparison; **reliability benefit:** detecting growth toward the four observed warning thresholds. Memory retrieval is effective in the logged subset but already near its token budget; preserve its 1,400-token cap while measuring whether selected records affect verdicts.

## Orchestrator Health

`orchestrate_poll` succeeded in all **8/8** collected runs, with p50 **467 seconds** and p95 **501.6 seconds**, but has no downloaded poller deep dive. Nine `workflow_failure_heal` runs are classified as skipped, while six heal-intake runs succeeded; these outcomes do **not** reveal wave progression or whether a heal resolved PR #5596. Four `claude_twin_sync` rows are `action_required` with no job logs. Add one compact transition event at each clarify/plan/implement/review/poll boundary: task or PR identifier, prior/new state, wave, deferral reason, conflict-heal attempt, terminal reason, and elapsed time—excluding content and credentials. Track repeated same-head failure fingerprints, deferrals per wave, time since last transition, and `action_required` without a job. **Expected impact:** exposes stuck and terminal states without changing orchestration behavior.

## Pipeline Flow Bottlenecks

- **Clarify → plan → implement:** 164/185 clarify, 184/186 plan, and 183/183 implement rows are “other” outcomes, predominantly skips; the overall one-second p50 therefore does not measure an active pipeline. Log trigger eligibility and skip reason before suppressing any dispatch.
- **Review/autofix compute:** 176 runs have a 13-second p50 but a **2,064.75-second p95**; the fully logged slow subset identifies reviewer models as the dominant compute segment. Measure model passes first, then shadow-test any reduction.
- **Validate/CI and retry:** CI has 2 failures in 6 runs, at separate deterministic checks. The four scope failures are separate runs with `run_attempt=1` and `retries=0`; treat the repeated failure streak as cross-run recurrence, not an in-run retry metric.
- **Queue and merge/conflict:** unselected summaries establish 21–49-second hosted-runner waits on cited intake/review runs. PR #5596 establishes conflict-resolver failure, but the logs lack a specific scope-rejection reason and comparable merge-overhead timings. Instrument those separately before reallocating end-to-end time.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Bottlenecks:** reviewer-model execution in 10 slow clean reviews; occasional 299–712-second disk cleanup; observed hosted-runner waits. **Failure modes:** four repeated PR #5596 resolver-scope failures plus one inventory and one Ruff CI failure. **Highest-cost driver:** 134 unique logged OpenRouter usage events in the slow-review sample; the collector currently reports them twice.

**Top three actions:** (1) add resolver rejection diagnostics and unchanged-input hold while retaining fail-closed behavior; (2) deduplicate composite/split-step telemetry and add API/model-step counters; (3) put inventory parity and Ruff ahead of expensive downstream work, then use measured verdict parity to test review-cost changes.

## Metrics Appendix

Window: **September 30, 2026, 22:35:31 UTC–October 1, 2026, 00:46:31 UTC**. Rates below use the collector’s 1,000 rows; **993 run IDs are unique** (five duplicated skips and two duplicated successes). “Other” includes skipped and `action_required`, not failures.

| Scope | Rows | Success | Failure | Other | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|
| All workflows | 1,000 | 266 (26.6%) | 6 (0.6%) | 728 | 1 / 432 s |
| Review/autofix | 176 | 172 (97.7%) | 4 (2.3%) | 0 | 13 / 2,064.75 s |
| CI | 6 | 4 (66.7%) | 2 (33.3%) | 0 | 534 / 914.25 s |
| Clarify | 185 | 21 | 0 | 164 | 1 / 194 s |
| Plan | 186 | 2 | 0 | 184 | 1 / 10.75 s |
| Implement | 183 | 0 | 0 | 183 | 1 / 9 s |
| Orchestrate poll | 8 | 8 | 0 | 0 | 467 / 501.6 s |

| Logged-cost metric | Collector value | Distinct-event interpretation |
|---|---:|---:|
| Downloaded full-log runs | 16 | 16; separate from `analysis_context`’s 115 rows with some log-derived/fallback telemetry |
| OpenRouter calls; usage available/unavailable | 268; 244/24 | **134; 122/12**, in 10 slow reviews |
| OpenRouter prompt / completion / total tokens | 102,564,460 / 4,300,636 / 689,620,106 | **51,282,230 / 2,150,318 / 344,810,053** |
| Cache creation / read tokens | 9,512,960 / 573,246,302 | **4,756,480 / 286,623,151** |
| `cache_hit_rate` | Aggregate null | Two complete runs: 85.35%, 90.74% |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | Context: 1,000 / 2,303,200 (113 samples) | Full-log summary: **2,110,500 / 2,530,250** (16 samples); populations differ |
| `break_glass_count` / `context_budget_warn_count` | 0 / 8 | **0 / 4 distinct warnings**, all review |
| Codex calls / tokens | 0 / 0 | Resolver activity exists; these counters do not measure its cost |

| MCP event/target | Collector | Distinct logged events | Logged bytes | Availability |
|---|---:|---:|---:|---|
| Semble `QUERY`, `reviewer-context` | Included in 34 total queries | 10 | 148,391 | Runtime query observed |
| Semble `QUERY`, `overflow` | Included in 34 | 4 | 22,328 | Runtime query observed |
| Semble `QUERY`, `conflict-resolver-context` | Included in 34 | 4 | 37,752 | Runtime query observed |
| **Semble queries, total** | **34; 401,922 bytes** | **18** | **208,471** | No runtime fallback observed |
| Semble `FALLBACK`, `overflow` | 16 | **8 contract-test events; 0 runtime** | — | Synthetic missing-executable tests, not an availability failure |
| Serena queries / fallbacks | 0 / 0 | 0 / 0 | 0 response bytes; 0 tool calls; no per-tool breakdown | Unassessed |
| Serena target: none emitted | — | — | — | `probe_ok=0`, `probe_failed=0`, `probe_skipped=0` |
| Other MCP servers observed | None validated | — | — | No target/probe rows |

| GH API signal | Measured result | Collection action |
|---|---|---|
| Calls by endpoint/job/step; retries; cache hits | **Not emitted** in inspected full logs | Instrument existing helpers; aggregate without response bodies |
| Real rate-limit events | **Not established** in inspected full logs | Log status class and backoff; do not infer a zero rate from missing counters |
| Code-identified candidates | Review PR-state watchdog; poller per-follow-up reads | Count invocations and repeated scopes before batching or caching |

The immediate audit priority is **unique, structured diagnostic events**—especially resolver rejection reasons, API helper outcomes, and model-step timings—so the next report can distinguish repeated work from duplicated log representation.

## Deep Audit — Workflows & Scripts (2026-10-01)

### Section 1: Bug & Correctness Sweep

Read-only checks covered all 52 `.github/workflows/*.yml` files and 159 `scripts/*.sh`/`scripts/*.py` files. YAML parsed, shell scripts passed `bash -n`, and Python files passed AST parsing. Findings below are distinct from the resolver-scope failure and CI defects already described in the report.

- **BUG-001** — **File:** `.github/workflows/implement.yml:499-515` (also `scripts/resolve_integration_ref.sh:59-91`, `.github/workflows/implement.yml:574-617`). **Severity:** High. **Category:** `bug`. **Description:** A failed integration-ref lookup writes an empty `ref`; checkout then falls back to the default branch. The resolver uses an unretied issue read, so an unavailable API response is not distinguished from an issue that genuinely declares no integration branch. The same fallback appears in the clarify and clarify-respond callers. **Recommended fix:** Make the resolver return distinct outcomes for “no branch declared” and “branch could not be verified”; retry transient reads, and stop checkout on an unverified outcome. Retain default-branch fallback only for a confirmed absence of a declared branch.

- **BUG-002** — **File:** `scripts/promote_main_cycle.sh:417-435` (retry behavior: `scripts/gh_helpers.sh:439-489`). **Severity:** Medium. **Category:** `bug`. **Description:** `gh_retry gh workflow run` can repeat a non-idempotent dispatch up to five times if GitHub accepts a request but its response is lost. The run-list reconciliation starts only after that call returns; the script itself notes that competing gate runs share cancel-in-progress concurrency at `scripts/promote_main_cycle.sh:396-403`. Duplicate dispatch under that failure mode is an inference, not an observed run. **[NEEDS VERIFICATION]** **Recommended fix:** Use one dispatch attempt, reconcile the cycle-specific run identity against the runs listing, and retry dispatch only when non-acceptance is established.

- **BUG-003** — **File:** `scripts/orchestrate_poll_process.sh:3209-3239` (consumer: `scripts/orchestrate_poll_process.sh:19473-19488`). **Severity:** Medium. **Category:** `bug`. **Description:** `_fetch_issue_labels_batch_graphql` requests `labels(first: 50)` without `pageInfo`. It caches that list under the issue key, so the consumer’s REST fallback—used only when the key is absent—cannot recover labels beyond the first 50. Whether an active issue exceeds that count is unverified. **[NEEDS VERIFICATION]** **Recommended fix:** Request `pageInfo.hasNextPage`; omit truncated issues from the batch result so the existing paginated REST fallback handles them. Apply the same check to the issue-details batch’s label field at `scripts/orchestrate_poll_process.sh:14719-14789`.

- **SEC-001** — **File:** `scripts/gh_helpers.sh:439-489` (caller: `.github/workflows/review_autofix.yml:5005-5008,5116-5120`). **Severity:** High. **Category:** `security`. **Description:** `gh_retry` prints `$*` on permanent and exhausted failures. The reviewer step passes its generated editor-summary comment as a `body=` argument and leaves helper stderr in the job log. Thus a failed post can reproduce the full comment in diagnostics; sensitivity of any particular summary is unverified. **[NEEDS VERIFICATION]** **Recommended fix:** Log the endpoint class, status, and attempt—not argument values. Pass substantial bodies via files where supported, and apply the redaction rule to `gh_retry_to_file` diagnostics at `scripts/gh_helpers.sh:509-558`.

- **SEC-002** — **File:** `scripts/gh_helpers.sh:621-633` (issue-JSON caller: `scripts/workflow_failure_heal_report.sh:109-113`). **Severity:** Medium. **Category:** `security`. **Description:** On a successful HTTP response that fails JSON validation, `gh_api_json_to_file` prints the first 50 raw response lines. One caller fetches an issue object, so malformed or truncated response content could expose its body in logs. Actual sensitive content has not been established. **[NEEDS VERIFICATION]** **Recommended fix:** Replace the raw-response dump with byte count, validation failure class, and a non-reversible digest; retain the response only in the runner-local temporary file.

### Section 2: GitHub API Call Redundancy Audit

Counts are code-path estimates, not measured request totals. The existing report already identifies reviewer-state polling and advisory-follow-up reads; they are not repeated as new findings here.

- **API-001** — **File:** `scripts/orchestrate_poll_process.sh:16624-16632` (existing batch: `scripts/orchestrate_poll_process.sh:14686-14789`). **Severity:** Medium. **Category:** `api-redundancy`. **Description:** Each standalone reissue fetches the *same issue endpoint twice*, once for `.title` and once for `.body`: **2N reads for N reissues**. The earlier `_fetch_candidate_issue_details_graphql` cache does not currently include those fields. **Recommended fix:** Extend that 25-item aliased batch with title/body and reuse cached values; on a cache miss, make one `_safe_gh_jq` read returning both fields. This reduces incremental reads to **0 on batch hits, at most N on misses**; the shared batch costs `ceil(N/25)` calls for its input set.

- **API-002** — **File:** `scripts/orchestrate_poll_process.sh:21324-21353` (batch pattern: `scripts/orchestrate_poll_process.sh:3171-3248`). **Severity:** Medium. **Category:** `api-batching`. **Description:** After review-blocked handling, label refresh makes one REST read per current-wave issue and another per newly encountered reissue: **N+R reads** for N wave issues and R additional reissues. The earlier batch cannot simply be reused because labels may have changed. **Recommended fix:** Build a deduplicated post-mutation number list and call `_fetch_issue_labels_batch_graphql` afresh: **`ceil((N+R)/25)` batch calls**, plus REST only for misses or truncated results identified by BUG-003.

- **API-003** — **File:** `scripts/promote_main_cycle.sh:242-264`. **Severity:** Medium. **Category:** `api-batching`. **Description:** `last_cycle_baseline_sha` makes **1 search call plus up to 10 issue-comment reads**, one inside the result loop. **Recommended fix:** Model a GraphQL search-and-comments prefetch on `_fetch_candidate_issue_details_graphql`, preserving the existing trusted-author, ordering, and comment-window checks. The projected normal path is **1 query instead of 1+N calls** for N≤10; retain the current reads as a fallback until result parity is tested. **[NEEDS VERIFICATION]**

- **API-004** — **File:** `scripts/gh_helpers.sh:610-660,693-725` (existing classifier: `scripts/gh_helpers.sh:75-94`). **Severity:** Medium. **Category:** `api-redundancy`. **Description:** Unlike `gh_retry`, `gh_api_json_to_file` does not check `_is_gh_permanent_failure`; `curl_gh_api` backs off for non-rate-limited 4xx responses. With the default budget, a deterministic failure can cost **5 requests rather than 1**. These loops do back off; the defect is retrying permanent errors, not a tight loop. **Recommended fix:** Reuse the permanent-error classifier in `gh_api_json_to_file`, extend it for confirmed deterministic statuses, and apply equivalent status handling in `curl_gh_api`. Preserve rate-limit waits and transient retries.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — **File:** `.github/workflows/clarify.yml:61-131`, `.github/workflows/implement.yml:445-515`, `.github/workflows/orchestrate_clarify_respond.yml:115-185`. **Severity:** Medium. **Category:** `duplication`. **Description:** These three integration-ref staging/lookup `run:` bodies are identical at approximately 2,963 decoded characters apiece. A correction to BUG-001 otherwise requires three synchronized edits. **Recommended fix:** Put the pre-checkout logic in a trusted, staged `scripts/` bootstrap helper with a `resolve <issue-number> <repository> <support-ref> <runner-temp>` interface and typed success/error output. Update the three callers only after arranging their verified support checkout; retain `scripts/resolve_integration_ref.sh` as the canonical parser.

- **DUP-002** — **File:** `.github/workflows/mark-stable.yml:668-880`, `.github/workflows/test-and-mark-stable.yml:5698-5910`. **Severity:** Medium. **Category:** `duplication`. **Description:** Both workflows carry identical approximately 6,305-character tag-publication blocks and identical release-creation blocks. `scripts/auto_release_stable.sh:1-15` dispatches the gate; it does not own publication. **Recommended fix:** Add a shared `scripts/` release helper exposing `publish_stable_tags <version> <source-branch>` and `ensure_github_release <version> <source-branch> <notes-file>`. Invoke it from both workflows while preserving their step names, guards, and verified checkout source.

- **DUP-003** — **File:** `.github/workflows/review_autofix.yml:4953-4983,6452-6482`. **Severity:** Low. **Category:** `duplication`. **Description:** Normal and partial-finalize paths duplicate the approximately 1,610-character ledger-cache stage-out body. **Recommended fix:** Extend `scripts/review_issue_ledger.sh` with `review_ledger_stage_cache <workspace-root> <staging-root> <ledger-path>` and call it from both steps, passing the Actions workspace output through step `env:` rather than embedding an expression in the script.

No whole-workflow pair was established as more than 70% identical. The internal phase-wrapper predicate duplication is an intentional parity contract (`agents.md:2314-2316`, `tests/test_phase_wrapper_predicate_contract.py:28-62`), not an extraction candidate.

### Section 4: Expression Size Limit Risk Assessment

Across 776 parsed `run:` blocks, 225 contain `${{ }}`. Counts below are decoded, source-text **estimates** for the complete interpolated `run:` body; runtime substitution lengths cannot be determined statically. One exceeds 15,000 characters, none exceeds 18,000, and the largest `if:` condition is approximately 859 characters. Non-interpolated blocks were excluded from expression-risk counts.

- **EXPR-001** — **File:** `.github/workflows/implement.yml:986-1343`. **Severity:** Medium. **Category:** `expression-limit`. **Description:** “Stage workflow support files” contains three interpolations in a **16,985-character** decoded body: approximately **4,015 characters of headroom** to 21,000 before runtime expansion. **Recommended fix:** Move the post-checkout body into a staged `scripts/` helper, pass `github.repository` through step `env:`, and include the helper in this step’s explicit support-script staging list at `.github/workflows/implement.yml:1009-1025`.

- **EXPR-002** — **File:** `.github/workflows/implement.yml:3223-3530`. **Severity:** Low. **Category:** `expression-limit`. **Description:** The interpolated destructive-commit preflight is **14,392 characters**—**608 below** the 15,000-character caution threshold and **6,608 below** 21,000. Its inline output-delimiter blocks leave a substantial maintenance surface, though it is not currently over the requested threshold. **Recommended fix:** Move the preflight into a staged `scripts/` helper or an appropriate preflight mode of `scripts/implement_commit_changes.sh`; pass the repository value through `env:`.

- **DEBT-001** — **File:** `.github/workflows/review_autofix.yml:6262-6433` (size contract: `tests/test_workflow_file_size_limit.py:14-24,32-46`). **Severity:** Medium. **Category:** `tech-debt`. **Description:** The workflow is **455,461 bytes**, only **24,539 bytes below this repo’s 480,000-byte CI guard**. The repository documents a measured 512,000-byte GitHub cutoff, stricter than the 1 MB premise in the audit request. This is a *file-size*, not a per-expression, risk. **Recommended fix:** Extract enough post-support-staging `run:` bodies to restore the documented 50,000-byte guard headroom. Follow the `review_autofix_step_*.sh` registration and verified-source pattern in `scripts/stage_workflow_support.sh:53-85` and `tests/review_autofix_step_scripts.py:34-47`.

No workflow exceeds 800 KB. In particular, the approximately 57,462-character `review_autofix.yml:467-1563` block contains **no** `${{ }}` and is not an expression-limit finding.

### Section 5: Cross-Cutting Concerns

- **SHELL-001** — **File:** `scripts/orchestrate_poll_process.sh:9262-9266` (use: `scripts/orchestrate_poll_process.sh:9288-9306`). **Severity:** Low. **Category:** `shellcheck`. **Description:** Shellcheck SC2155 identifies `local now_epoch="$(date +%s)"`: `local` masks a failed `date` command, after which the value is used in state updates and arithmetic. **Recommended fix:** Declare `now_epoch` separately, assign it with checked status, and return a diagnostic failure if the clock read fails.

- **DEAD-001** — **File:** `scripts/validation_lint.py:870-874`. **Severity:** Low. **Category:** `dead-code`. **Description:** `_line_with_token` has no other reference in the repository’s scripts or tests; current lint checks do not call it. **Recommended fix:** Confirm there are no external imports, then seek the identifier-removal approval required by `CLAUDE.md` §6, or use the helper in a line-finding check if that behavior is needed.

The scanned workflow/script files contain no `TODO`, `FIXME`, or `HACK` markers. Shellcheck’s SC2015 reports on the prerequisite gates at `scripts/clarify_isolated_run.sh:13-15` describe intentional fail-closed `A && B || exit` chains, not findings. The retry inconsistency is addressed by API-004; the documented, currently reserved label-repair knobs (`README.md:1928-1931`) are not represented here as unexpected defects.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, SEC-001 |
| Medium | 11 | BUG-002, BUG-003, SEC-002, API-001, API-002, API-003, API-004, DUP-001, DUP-002, EXPR-001, DEBT-001 |
| Low | 4 | DUP-003, EXPR-002, SHELL-001, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | ~5 proposed | Medium |
| API call optimization | ~3 proposed | Medium |
| Code modularization | ~8–12 proposed, including helpers and contract tests | Large |
| Expression size reduction | ~4–6 proposed, including staging registries | Medium |
| Medium/Low fixes | ~5 proposed | Medium |
