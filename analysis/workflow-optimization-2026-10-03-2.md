## Executive Summary

- **Fix CI’s repeated deterministic failures first.** All 8 failures among 24 CI runs occurred in `shubhodeep1/coding-workflows`; six runs show inventory-parity failures, five show missing event-mirror logging callsites, and several have additional failures. Run `37127904831` reported four failed jobs. Fixing the checks could prevent repeated 9–16-minute failed runs. **Impact: high; confidence: high.**
- **Review/autofix is the largest measured compute bottleneck.** In run `37124247419` for PR #6126, reviewer models took about 1,220 seconds and editor fixes 830 seconds—79% of the 2,581-second run. Preserve the review gate, but measure work by head SHA before attempting reuse. **Impact: high; confidence: high for the bottleneck, low for savings from reuse.**
- **Reported cost and warning totals are inflated by mirrored logs.** Run `37124247419` records the same 14 OpenRouter usage events twice: its reported 28 calls and 99.66 million tokens represent 14 distinct events and 49.83 million reported tokens. Its two context warnings are one distinct event. Fix collector deduplication before using totals for budgets. **Impact: high for measurement accuracy; confidence: high.**
- **Merge-train waiting is real, not a reason to bypass safety.** Run `37127631768` examined 16 queued PRs and released none; its logs identify older overlapping PRs as blockers. Prioritize the oldest blocker and log queue age and blocked reason. **Impact: potentially high for flow; confidence: high for the stall, low for its end-to-end duration.**
- **API and queue-time attribution are missing.** No per-endpoint API-call counts or retry totals were supplied, and all 1,000 collector rows have identical created and started timestamps. Instrument existing helpers and distinguish actual runner wait from compute before changing poll cadence or batching. **Impact: high diagnostic value; confidence: high.**

## Speed Optimizations

1. **Critical path—review/autofix.** Run `37124247419`, `review / codex-agent`, spent about 1,220 seconds in *Run reviewer models* and 830 seconds in *Apply fixes with editor model*. Review/autofix p50/p95 duration is 393/1,437 seconds across 217 runs. **Likely cause:** model and editor work, not setup micro-steps. **Change:** log per-pass elapsed time, head SHA, rubric version, finding count, and whether the same-head result was already validated; reuse a result only when those inputs and required safety checks match. **Potential saving:** up to one full review run when identical work is proven; no such opportunity is established by this sample. **Risk:** medium—never reuse across changed heads or rubrics.

2. **Failed-CI critical path.** In CI run `37127904831`, Actionlint failed roughly 15 seconds after creation, but the run lasted 975 seconds while parallel poll-process tests continued; its longest poll shard ran about 884 seconds. **Change:** put quick Actionlint, inventory, and event-mirror checks in a preflight dependency for expensive shards, while retaining a full-suite path for diagnostics. **Potential saving:** approximately 14–15 minutes on a similarly failing run; successful runs gain a preflight dependency. **Risk:** medium because early gating can hide additional failures.

3. **Poll-test imbalance.** Run `37127904831` had poll-process shards of roughly 884, 675, 639, and 636 seconds. **Change:** emit per-test durations and move an independent slow test group from shard 0 only after verifying isolation. **Potential saving:** at most about 209 seconds on a similarly balanced run, not a guaranteed improvement. **Risk:** medium.

4. **Secondary, not critical-path-first:** run `37124247419` spent about 130 seconds in *Free disk space*. Log space before/after and the cleanup action; avoid or narrow it only where a safe-space threshold is already met. **Potential saving:** up to that step’s measured time on qualifying runs. **Risk:** low if low-space runs retain cleanup.

## Cost Optimizations

1. **Correct measurement before tuning spend.** The collector’s whole-line deduplication in `scripts/collect_workflow_logs.py` misses matching aggregate/step events whose microsecond timestamps differ. In `37124247419`, each of 14 usage records appears in both logs. Deduplicating only matched parent/child step events yields approximately **89 rather than 103** OpenRouter calls and **302.39 million rather than 352.22 million** reported OpenRouter tokens for this log-covered cohort. These are corrected *telemetry estimates*, not a billing reconciliation. Add a stable event ID or normalize timestamps within a bounded parent/child match; retain repeated events within one step. **Saving:** no demonstrated billing saving; prevents misdirected optimization. **Quality risk:** none if raw records remain available for audit.

2. **Avoid only proven repeated reviews.** PR #6126 appears in long successful review runs `37118616001`, `37114980790`, and `37124247419`; the supplied evidence does **not** establish identical heads or work. Record head SHA, prompt/rubric version, and verdict so exact-repeat work can be identified without skipping changed-head review. **Potential saving:** one eligible run could avoid tens of minutes and its model usage; current avoidable-run count is unknown. **Quality risk:** high if identity checks are weakened.

3. **Reduce uncached context cautiously.** Log-covered OpenRouter usage includes 25.52 million prompt tokens, 4.14 million cache-write tokens, and 320.27 million cache-read tokens; aggregate `cache_hit_rate` is unavailable because one of 103 reported calls lacks usage. Run `37106320510` reports a 65.2% hit rate versus 94.7% in `37121763028`. Keep static instructions ahead of changing diff/run material, and log prefix hash, dynamic-context bytes, and cache status per call before attributing that gap to fragmentation. **Scenario, not forecast:** trimming 10% of the roughly 25.7 million *deduplicated* prompt-plus-write tokens would remove about 2.6 million such tokens. **Quality risk:** context removal may lose findings.

4. **Test model effort and retrieval value, rather than downgrading globally.** Run `37124247419` configured reviewer reasoning `xhigh` and editor reasoning `high`. A lower-effort trial should be restricted to measured small-diff cohorts and compared on findings and later autofix cycles; dollar savings cannot be estimated without prices and a quality baseline. Semble logged 14 queries/151,241 bytes as collected, or 13 distinct queries/136,542 bytes after the demonstrated mirror correction. Reviewer-context responses are about 14.5 KB each; an implement overflow query in `37105860038` returned just 13 bytes. Log *bytes selected for prompts* and subsequent model usage, and omit near-empty retrievals if tests confirm no loss. The current evidence cannot show that Semble reduces prompt expansion. Serena logged no runtime queries or tool calls and was disabled in sampled reviews, so replacement value and noisy-response risk cannot yet be assessed.

## Reliability Improvements

1. **Deterministic CI regression; highest priority.** Inventory parity reports unexpected `scripts/smoke_review_dispatch.sh` in six failed runs, including `37123734645`; event-mirror tests report no `AUTOFIX_DISPATCH_SKIPPED` echo callsites in five, including `37127904831`. Run `37127904831` also has three tests missing a `tmp_path` argument and Actionlint rejects constant-false conditions in `.github/workflows/orchestrate_poll.yml`. **Category:** code, fixture, and contract drift. **Fix:** update the inventory; restore the intended skip diagnostic or adjust its contract only after verifying equivalent behavior; run fixture-dependent tests under their fixture-aware harness or give them explicit temporary directories; remove disabled Actionlint-flagged steps without inadvertently enabling them. Log every failing job and step, not just the collector’s single `failure_point`. **Expected impact:** addresses recurring CI failures; rerun reduction cannot be measured until fixes land. **Rollback:** keep existing checks mandatory; revert individual fixes rather than suppressing checks.

2. **Preserve integration-intent protection.** CI run `37127904831`, poll shard 1, includes a failed `test_judge_prompt_caps_embedded_pr_diffs_by_bytes`; its logs also exercise fingerprint guards that refuse a merge-resolve commit when sub-issue intent is lost. **Category:** poll-test regression, with safety checks also appearing in test output. Reproduce the actual failed assertion independently and log test name, shard, and elapsed time; do not classify every expected fingerprint-guard error line as a production failure or disable the guard. **Expected impact:** cleaner failure attribution and fewer repeated failed shards. **Rollback:** retain fail-closed merge verification.

3. **Separate MCP tests from availability incidents.** The collector reports 40 `SEMBLE_FALLBACK` lines, all marked `context=contract-test`, target `overflow`, with **zero runtime fallbacks**; mirrored logs account for four of those 40. This is healthy test coverage, not evidence of 40 production outages. Poll run `37127624660` separately reports Semble and its index unavailable, without a runtime query failure. Emit distinct runtime availability and contract-test counters, with target and reason, to reveal a broken rollout without alarming on fixtures. Serena has zero runtime queries, fallbacks, and probes; `SERENA_ENABLED: false` in sampled reviews explains why zero failures do not prove availability. **Expected impact:** fewer false incident signals and earlier detection of real unavailability. **Rollback:** keep current fail-open paths.

4. **Track non-fatal cleanup warnings.** Successful review runs including `37124243456` and `37124221658` report post-job Git submodule cleanup failing without a working tree. Log checkout/cleanup ordering and a cleanup-warning count before changing it. **Expected impact:** clearer signal; no demonstrated run-failure reduction. **Rollback:** do not make cleanup a new blocking gate.

`BREAK_GLASS` count is zero. The two collected `CONTEXT_BUDGET_WARN` records in review run `37124247419` are one mirrored warning: review prompt 155,859 tokens against a 200,000-token window and 140,000-token threshold. It signals **prompt-size pressure**, not observed rubric or break-glass pressure.

## AI Memory Health

Nine distinct `AI_MEMORY_TELEMETRY` retrieves in the slow-run logs—all seven review retrieves and two implement retrieves—selected records: **9/9 hit (100%)**, averaging **1,433 estimated tokens against a 1,444-token average budget**. Keyword methods were `llm` 7, `plain` 2, `none` 0. No observed retrieve returned zero records or reported `enabled: false` or `fail_open: true`. Run `37124247419` selected 25 reviewer records at 1,389/1,400 tokens; the near-full budget warrants relevance measurement before trimming.

Observed operations also include successful candidate records, run events, implement command claims/completions, and task finalization. A `record-run-event` in `37107182581` needed five push attempts; log retry cause and elapsed backoff, and alert on repeated high attempts without blocking review. No `promote` or `compact` event appears in these deep dives; memory-maintenance run `37106035607` has no full telemetry here. Verify those operations’ emission rather than inferring inactivity.

## GH API Call Audit

| Observed workflow/job/step | Evidence | Safe action and expected call impact |
|---|---|---|
| Review Autofix Sweep, run `37124210897` | Summary records 17 candidate dispatches in about 38 seconds. | Log dispatch endpoint category, status, attempt count, and latency. Dispatches are per candidate; **do not claim** they can be batched. Savings unknown. |
| Cancel on PR Close, run `37127631768`, *Release merge-queued PRs* | 16 examined, 0 released; step took about 14 seconds. `scripts/review_merge_train.sh` already lists PRs once and caches each distinct PR file list per process. | Log per-cycle distinct file fetches versus `_MT_FILES_CACHE` hits, pagination, and blocked-reason counts. Preserve the cache and fail-open behavior; no redundant-call reduction is established. |
| Orchestrate Poller, 17 runs | p50 duration 476 seconds; per-endpoint calls, retries, and rate-limit events are not supplied. | Add a sanitized per-job summary to existing `scripts/gh_helpers.sh`: endpoint *template*, method, calls, attempts, cache hits, status class, rate-limit waits, and elapsed milliseconds. Avoid URL parameters, bodies, and tokens. Then extend existing batched GraphQL/cycle-local caches where a repeated lookup is demonstrated, consistent with `codex.md` §15. |

No genuine rate-limit event or HTTP-retry count can be quantified from these selected logs; test-fixture errors and the run-level `retries: 0` field are not API-retry measurements. Instrument the existing wrapper rather than adding an API call to measure API usage. A proven same-cycle duplicate lookup could save its repeat calls; the present data does not justify a numeric call-reduction claim.

## Prompt Cache & Memory System

Cache reads dominate recorded OpenRouter input, but the official aggregate `cache_hit_rate` is **null** because one usage record is unavailable. Individual measured rates range from **65.2%** (`37106320510`) to **94.7%** (`37121763028`); `37124247419` reports **92.1%**, unchanged by removing its mirrored records. These differences alone do not prove unstable prefixes. Emit a privacy-safe static-prefix hash, rubric version, dynamic section sizes, and per-call creation/read counts; place run-specific noise after a stable prefix where behavior permits. Track the 155,859-token review warning by distinct event ID so prompt growth is not double-reported.

Memory retrieval is effective by *hit count* (9/9) but nearly fills its budget; record selection reason and downstream usefulness, not record text. Keep retrieval fail-open. Semble’s logged `bytes` measure returned context, not demonstrated model-token savings; Serena was disabled, so compare tool response bytes and downstream tool/model calls in a controlled enabled cohort before promoting it.

## Orchestrator Health

The observed clarify, plan, and implement families are predominantly skipped (150/151, 146/149, and 144/152 respectively). That explains the overall two-second run median; it does **not** establish a clarification loop or stuck state. Log each skip’s reason and tracking-issue/state transition to distinguish expected no-ops from stalled waves.

The merge train supplies stronger stall evidence: `37127631768` left 16 PRs queued, with #6127 blocked by #6126 and later PRs listing progressively more blockers; `37121385157` also reported 16 examined and none released. Investigate and advance the oldest safe blocker first, without bypassing overlap checks. Emit oldest-blocker age, queued age, `released`, and reason counts (`overlap`, active review, incomplete listing, API failure) each tick. Run `37126656841` spent about 34 of 47 seconds in heal intake and identified duplicate issue #6055; keep deduplication and log duplicate-hit latency. Conflict-heal retry and terminal-state rates cannot be determined from the supplied rows; add transition and retry counters keyed by state, not issue content.

## Pipeline Flow Bottlenecks

| Flow segment | Measured signal | Bottleneck type and next diagnostic |
|---|---|---|
| Clarify → plan → implement | Most runs skipped; implement outlier `37105902515` took 3,040 seconds and logged 13 Codex calls. | **Compute or intentional no-op:** log active-phase start/end and skip reason; avoid interpreting family medians of one second as work latency. |
| Review/autofix | p50/p95 393/1,437 seconds; `37124247419` spent about 2,050 seconds in reviewer/editor steps. | **Compute:** per-pass and editor timing, same-head identity, and findings count. |
| Validate/CI | 8/24 failed; `37127904831` failed quick checks early but ran 975 seconds. | **Parallel-test tail/repeat failures:** fix deterministic checks, then evaluate preflight gating. |
| Orchestrate/merge | Poller p50 476 seconds; release scan `37127631768` left 16 overlapping PRs queued. | **Merge wait:** blocker-age and per-cycle reason logging; do not equate safe queueing with API failure. |
| Queue/retry overhead | All 1,000 rows show `run_started_at == created_at`; API retry counts absent. | **Unattributed:** collect actual job queued/start timestamps and wrapper backoff before estimating queue or retry savings. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Bottlenecks:** review/editor compute, long CI poll-test shards, and an overlapping-PR merge train. **Failure modes:** recurring inventory, event-mirror, fixture, and Actionlint CI regressions; misleading single-point failure attribution and mirrored cost events. **Highest-cost drivers:** OpenRouter review usage and long implement runs; pricing and avoidable same-head work are unmeasured.

**Top three actions:** (1) fix and preflight the deterministic CI checks without suppressing them; (2) correct parent/step telemetry deduplication and emit per-stage timing; (3) instrument merge-train blocker age and existing GH API wrapper counts before changing API strategy. Expected outcomes are fewer failed CI cycles, trustworthy cost baselines, and an attributable queue stall.

## Metrics Appendix

**Scope and coverage.** Collector generated October 3, 2026, 14:18 UTC. Its `summary.json` has **23** runs with full log telemetry; the supplied assembled context reports **117** runs with log telemetry or added summaries. The configured success sample rate is 7%, but the collector reports **0 randomly sampled success runs**; successful deep dives are targeted. Do not combine the two cohorts’ wall-clock percentiles.

| Family | Runs | Success / failure / cancelled / skipped | Duration p50 / p95 |
|---|---:|---:|---:|
| All, `shubhodeep1/coding-workflows` | 1,000 | 340 / 8 / 5 / 647 | 2 / 554 s |
| CI | 24 | 15 / 8 / 1 / 0 | 786 / 1,216 s |
| Review/autofix | 217 | 211 / 0 / 4 / 2 | 393 / 1,437 s |
| Orchestrate poll | 17 | 17 / 0 / 0 / 0 | 476 / 565 s |
| Implement | 152 | 8 / 0 / 0 / 144 | 1 / 506 s |

Overall failure rate is **0.8% of all runs**; CI failure rate is **33.3% of its 24 runs**. Skipped runs make the overall success rate unsuitable as an active-work success measure.

| Log-covered metric | Collected value | Qualification |
|---|---:|---|
| Codex usage | 26 calls; 2,652,382 tokens | Implement family |
| OpenRouter usage | 103 calls; 352,215,361 total tokens | Includes cached tokens and mirrored records; approximately 89 calls / 302,387,231 tokens after the demonstrated correction |
| OpenRouter prompt / completion | 25,516,795 / 2,285,244 tokens | Collected, before mirror correction |
| Cache creation / read | 4,141,981 / 320,274,506 tokens | Collected, before mirror correction; 102 usage-available calls, 1 unavailable |
| `cache_hit_rate` | null aggregate | Individual observed range 65.2%–94.7%; do not substitute a weighted rate for the unavailable official metric |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 12,000 / 3,022,220 | Assembled context, 115 samples; full-log collector instead reports 940,000 / 3,585,220 on 23 samples |
| `break_glass_count` / `context_budget_warn_count` | 0 / 2 | Warnings are **1 distinct** mirrored event in `37124247419` |

| MCP target / source | Queries; logged bytes | Fallbacks | `probe_ok` / `probe_failed` / `probe_skipped` |
|---|---:|---:|---:|
| Semble `reviewer-context`, review/autofix | 8; 116,940 | 0 | 0 / 0 / 0; no probe signal |
| Semble `overflow`, implement and CI contract tests | 6; 34,301 | 40 collected, all CI contract-test; 0 runtime | 0 / 0 / 0; no probe signal |
| Serena `serena` | 0; 0 response bytes, 0 tool calls, 0 query ms | 0 | 0 / 0 / 0; no runtime probe observed |

After confirmed mirrored-line correction, Semble has **13 distinct queries/136,542 bytes** and **36 distinct contract-test fallbacks**: reviewer-context 7/102,241 bytes and overflow 6/34,301 bytes. Query-to-fallback rates are **not meaningful** here because the fallbacks are deliberate tests, not failed runtime queries. Serena per-tool breakdown: **none observed**. **Other MCP servers observed:** none in the supplied runtime telemetry.

| GH API activity proxy | Observed value | Missing measurement |
|---|---:|---|
| Sweep `37124210897` | 17 candidate dispatches, about 38 s | Actual API attempts/statuses |
| Merge release `37127631768` | 16 PRs examined, 0 released, about 14 s in step | Distinct API calls, cache-hit count, pagination |
| Rate-limit events / HTTP retries | Not quantified | Sanitized wrapper counters and backoff duration |

## Deep Audit — Workflows & Scripts (2026-10-03)

### Section 1: Bug & Correctness Sweep

The repository contains 52 `.github/workflows/*.yml` files and 162 `scripts/*.sh` or `scripts/*.py` files. All parsed in a read-only syntax sweep; ShellCheck reported warnings but the shell scripts passed `bash -n`. The inventory, event-mirror, Actionlint, and collector-telemetry regressions already described in the current report are not repeated below. No workflow or script TODO/FIXME/HACK marker was found.

- **ID:** BUG-001 · **File path:** `scripts/label_helpers.sh:209-243` · **Severity:** High · **Category tag:** `bug`  
  **Description:** `set_issue_phase_label_resilient` reads all issue labels, computes a replacement set, then sends `PUT /labels`. A label added by another job between the GET and PUT is absent from that replacement and can be lost. This is a read–modify–write race on the entire label set.  
  **Recommended fix:** Serialize phase-label writers per issue or use targeted phase-label removal and addition rather than replacing unrelated labels; preserve the contract-derived phase set used by `scripts/orchestrate_poll_process.sh:2905-2961`.

- **ID:** BUG-002 · **File path:** `scripts/gh_helpers.sh:780-805` · **Severity:** High · **Category tag:** `bug`  
  **Description:** The REST fallback for `gh_pr_with_all_comments` converts either failed paginated comments read into `[]` and returns successful-looking context. `scripts/review_rb_judge.sh:1100-1103` and `scripts/orchestrate_poll_process.sh:20628-20640` then consume those arrays as judge context. An API failure can therefore be indistinguishable from a PR with no comments.  
  **Recommended fix:** Check each read’s exit status before constructing context; return an explicit incomplete-context status. Make judge callers defer a decision that requires missing comments, while retaining their intended fail-open behavior for advisory context.

- **ID:** BUG-003 · **File path:** `scripts/tg_helpers.sh:330-374` and `scripts/tg_helpers.sh:399-445` · **Severity:** Medium · **Category tag:** `bug`  
  **Description:** Both cleanup loops fetch page 1, delete matching comments, then advance to page 2. Deletion shifts the remaining offset-paginated collection: with a full first page, an original comment near the page boundary can be skipped. This is an inference from the fetch–delete–increment sequence.  
  **Recommended fix:** Collect the matching comment IDs across pages before deleting any, then delete from that fixed snapshot; retain per-comment best-effort cleanup.

- **ID:** BUG-004 · **File path:** `scripts/tg_helpers.sh:189-205` and `scripts/tg_helpers.sh:262-277` · **Severity:** Medium · **Category tag:** `bug`  
  **Description:** Tracking-comment PATCH and POST calls use `curl -s`, discard responses, and end with `|| true`. HTTP error responses do not establish a successful write, yet the newly sent Telegram message ID is not retried or reported as untracked.  
  **Recommended fix:** Check HTTP status through a write-aware GitHub API helper, report a failed tracking write, and retain the message ID for a bounded retry. Do not blindly retry a POST without checking whether its comment was created.

- **ID:** SEC-001 · **File path:** `scripts/gh_helpers.sh:647-659` · **Severity:** Medium · **Category tag:** `security`  
  **Description:** When a successful API command returns invalid JSON, `gh_api_json_to_file` prints the first 50 lines of its raw response to the Actions log. The helper is used for PR comment context at `scripts/gh_helpers.sh:886-891`; a malformed response could expose private response content in logs. Whether such content has actually been logged is unknown. **[NEEDS VERIFICATION]**  
  **Recommended fix:** Log response byte count, endpoint category, and parse failure—not response bytes; retain the response only in the restricted temporary file until it is removed.

- **ID:** SEC-002 · **File path:** `.github/workflows/orchestrate_clarify_respond.yml:486-500` · **Severity:** Medium · **Category tag:** `security`  
  **Description:** An issue title read from the API is appended to `GITHUB_ENV` using the single-line `ISSUE_TITLE=value` form without newline validation. If an API-supplied title can contain a line break, subsequent text can be interpreted as another environment assignment. Whether GitHub admits such a title needs verification. **[NEEDS VERIFICATION]**  
  **Recommended fix:** Reject CR/LF before the single-line write, or use a collision-resistant multiline environment-file delimiter; the title is already available as step environment input at line 51.

### Section 2: GitHub API Call Redundancy Audit

Counts below are *logical calls on the stated path*, before pagination or retries. Batching proposals retain per-item fallbacks on missing or invalid cached data.

- **ID:** API-001 · **File path:** `scripts/orchestrate_poll_process.sh:19847-19871` · **Severity:** Medium · **Category tag:** `api-redundancy`  
  **Description:** The current-wave details batch already requests `labels(first: 50)` at lines 14881-14885, but the same issues receive a second labels-only GraphQL batch. **Current:** `2 × ceil(N/25)` batch calls for N wave issues on a healthy path. **Proposed:** `ceil(N/25)`, plus fallback calls only for absent entries.  
  **Recommended fix:** Populate `LABELS_JSON` from `_current_wave_details_json`; invoke the existing `_fetch_issue_labels_batch_graphql` for missing entries only, preserving the independently fail-open fallback documented at this callsite.

- **ID:** API-002 · **File path:** `scripts/orchestrate_poll_process.sh:10436-10448` · **Severity:** Low · **Category tag:** `api-redundancy`  
  **Description:** On a `final_pr_json_snapshot` miss, adjacent reads fetch the same PR once for `.state` and again for `.merged_at`. **Current:** 2 PR GETs per miss. **Proposed:** 1.  
  **Recommended fix:** Fetch one JSON object and extract both fields with `_jq_field`, following the existing `_fetch_pr_json` pattern at `scripts/orchestrate_poll_process.sh:1479-1495`; no new batching helper is needed.

- **ID:** API-003 · **File path:** `scripts/orchestrate_poll_process.sh:14090-14092`, `scripts/orchestrate_poll_process.sh:16805-16806`, and `scripts/orchestrate_poll_process.sh:22022-22024` · **Severity:** Medium · **Category tag:** `api-redundancy`  
  **Description:** Three reissue paths independently fetch an issue’s title and body from the same endpoint. **Current:** 2 GETs per reissued issue, or `2N` for N issues taking one path. **Proposed:** 1 GET per issue, or `N`.  
  **Recommended fix:** Capture one issue JSON response and extract both fields locally. If batching multiple wave reissues proves freshness-safe, extend the 25-item `_fetch_candidate_issue_details_graphql` alias pattern to carry title and body, with per-issue REST fallback. **[NEEDS VERIFICATION]**

- **ID:** API-004 · **File path:** `scripts/orchestrate_poll_process.sh:19895-19923` · **Severity:** Medium · **Category tag:** `api-batching`  
  **Description:** The current-wave loop calls `_fetch_pr_json` once per cross-referenced PR candidate to verify implementation-PR identity. **Current:** N PR REST GETs for N candidates, in addition to existing discovery calls. **Projected:** `ceil(N/25)` GraphQL alias calls for candidate metadata, plus REST calls on misses. Matching all fields required by `_pr_json_is_issue_implementation_pr` has not been verified. **[NEEDS VERIFICATION]**  
  **Recommended fix:** Collect candidate PR numbers before the loop and extend the aliased batching approach of `_fetch_linked_pr_status_graphql`; normalize its result to the verifier’s existing PR shape and keep `_fetch_pr_json` as the cache-miss path.

- **ID:** API-005 · **File path:** `scripts/gh_helpers.sh:637-680` and `scripts/gh_helpers.sh:704-752` · **Severity:** Medium · **Category tag:** `api-redundancy`  
  **Description:** Unlike `gh_retry` at lines 481-491, `gh_api_json_to_file` has no permanent-error check; `curl_gh_api` likewise backs off on non-rate-limited HTTP errors without distinguishing permanent 4xx responses. With the default five attempts, a repeatable 404 or 422 can consume **5 calls instead of 1** in either helper. `gh_api_json_to_file` also sleeps after its final failed attempt.  
  **Recommended fix:** Apply the existing `_is_gh_permanent_failure` classification to the JSON helper, classify curl HTTP statuses before retrying, and sleep only when another attempt remains. No GraphQL batching applies.

### Section 3: Code Duplication & Modularization Opportunities

- **ID:** DUP-001 · **File path:** `scripts/review_run_reviewers.sh:69-107`, `scripts/review_consolidate.sh:268-307`, `scripts/review_apply_fixes.sh:255-293`, and `scripts/review_rb_judge.sh:251-289` · **Severity:** Medium · **Category tag:** `duplication`  
  **Description:** Four `emit_context_budget_warn_for_prompt` implementations perform the same Python import, file check, and warning emission. A fix to telemetry behavior must be repeated four times.  
  **Recommended fix:** Put `emit_context_budget_warn_for_prompt <phase> <prompt_path> <model>` in a staged `scripts/context_budget_helpers.sh`; source that verified helper from all four callers and update their support-script inventory.

- **ID:** DUP-002 · **File path:** `scripts/review_autofix_step_changes_lost_redispatch.sh:83-123` and `scripts/review_autofix_step_post_commit_retrigger.sh:148-189` · **Severity:** Low · **Category tag:** `duplication`  
  **Description:** The two review redispatch tails repeat candidate ordering, caller-workflow validation, and dispatch. Their different pre-dispatch guards are outside the repeated block and should remain separate.  
  **Recommended fix:** Move only the common tail to a verified `scripts/review_dispatch_helpers.sh` function, `dispatch_review_default_branch <pr_number> <allow_workflow_edits> <caller_workflow_ref>`, and call it from both scripts.

- **ID:** DUP-003 · **File path:** `.github/workflows/review_autofix.yml:5820-5860`, `.github/workflows/review_autofix.yml:6003-6048`, and `.github/workflows/review_autofix.yml:6940-6965` · **Severity:** Medium · **Category tag:** `duplication`  
  **Description:** Late review steps carry repeated inline `ensure_label_exists` and POST-only `set_issue_phase_label_resilient` fallbacks. The POST-only behavior also differs from the phase-replacing canonical helper at `scripts/label_helpers.sh:193-243`.  
  **Recommended fix:** Stage a verified copy of `label_helpers.sh` outside commit cleanup for late steps. If an inline-free fallback remains necessary, centralize `review_load_label_helpers <support_dir>` in one verified helper and keep its failure policy explicit; update these callers together.

No pair of the inspected internal wrapper workflows provides evidence of a >70%-identical *workflow body* suitable for merging: they have distinct triggers and reusable-workflow inputs.

### Section 4: Expression Size Limit Risk Assessment

Sizes below are YAML-parsed `run:` values **after indentation removal**, before runtime interpolation; repository-variable expansion can change the final size. Measuring raw indented file lines would overstate expression size. Of 776 `run:` entries inspected, 223 contain `${{ }}`; non-interpolated bodies were excluded.

- **ID:** EXPR-001 · **File path:** `.github/workflows/implement.yml:986-1342` · **Severity:** Medium · **Category tag:** `expression-limit`  
  **Description:** The support-staging `run:` value is approximately **16,985 characters**, with three interpolations. Static headroom is **4,015** to 21,000 characters and **1,015** to the 18,000-character risk threshold. Final expansion depends on repository-variable values. **[NEEDS VERIFICATION]**  
  **Recommended fix:** Prefer extracting the staging body to a script in the verified support checkout, passing the three expression values through step `env:`; preserve the existing staged-support ledger and required-file checks.

No other interpolated `run:` value reaches 15,000 characters: the next largest is 14,392 at `.github/workflows/implement.yml:3223-3529`. The largest parsed `if:` is approximately 859 characters, so none approaches the stated expression threshold. No workflow exceeds 800 KB; the largest, `.github/workflows/review_autofix.yml`, is **451,837 bytes**. It is nevertheless only **28,163 bytes** below this repository’s stricter 480,000-byte CI split guard documented in `CLAUDE.md:2240-2269`.

### Section 5: Cross-Cutting Concerns

- **ID:** DEAD-001 · **File path:** `scripts/review_rb_judge.sh:2135-2144` · **Severity:** Low · **Category tag:** `dead-code`  
  **Description:** The fresh `PR_HEAD_SHA` assignment is unused (also reported by ShellCheck SC2034). The merge path instead checks `RB_JUDGED_HEAD_SHA` at lines 2204-2207; the adjacent comment incorrectly suggests the assigned value binds the merge.  
  **Recommended fix:** Remove the unused extraction and correct the comment without weakening the judged-head merge guard.

- **ID:** SHELL-001 · **File path:** `scripts/workspace_init.sh:110-113` · **Severity:** Low · **Category tag:** `shellcheck`  
  **Description:** ShellCheck reports SC1083 on the unquoted Git revision argument `HEAD^{tree}`. The intended argument is a literal revision expression, not shell brace syntax.  
  **Recommended fix:** Quote that revision argument literally; leave the surrounding fingerprint fallback unchanged.

- **ID:** DEBT-001 · **File path:** `.github/workflows/ci.yml:363-371` · **Severity:** Low · **Category tag:** `tech-debt`  
  **Description:** CI runs ShellCheck at `--severity=error`, so its warning-level findings are not gated. The read-only warning sweep surfaced, among others, DEAD-001 and SC1083 above; some other warnings reflect intentional dynamic shell patterns and need triage rather than blanket changes.  
  **Recommended fix:** Resolve actionable warnings, add narrow documented suppressions for intentional patterns, then raise this gate to `--severity=warning`.

The differing permanent-error retry behavior is covered by API-005 rather than repeated as a consistency finding. The supplied report already covers collector deduplication and current CI contract drift.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, BUG-002 |
| Medium | 11 | BUG-003, BUG-004, SEC-001, SEC-002, API-001, API-003, API-004, API-005, DUP-001, DUP-003, EXPR-001 |
| Low | 5 | API-002, DUP-002, DEAD-001, SHELL-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/label_helpers.sh`, `scripts/gh_helpers.sh`, judge callers | Medium |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/gh_helpers.sh` | Medium |
| Code modularization | Four reviewer scripts, two redispatch scripts, `review_autofix.yml`, staged helpers | Large |
| Expression size reduction | `implement.yml`, one staged script, support inventory | Medium |
| Medium/Low fixes | `scripts/tg_helpers.sh`, `orchestrate_clarify_respond.yml`, `scripts/review_rb_judge.sh`, `scripts/workspace_init.sh`, `ci.yml` | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-10-03)

### Safety Tag Legend

`SAFE_TO_MERGE` is ready for implementation; `NEEDS_VERIFICATION` requires the stated checks first; `RISKY_SKIP` must not be auto-implemented because it touches a protected pagination, retry, or race-sensitive path.

### Consolidation Candidates (MERGE-###)

- **ID:** MERGE-001 · **Safety tag:** `RISKY_SKIP`  
  **Calls:** `scripts/review_merge_train.sh:267-271` (`_mt_find_marker_comment_id`) and `scripts/review_merge_train.sh:285-300` (`_mt_upsert_comment`); the gate passes the listed ID at `scripts/review_merge_train.sh:368-401`.  
  **Current → proposed:** 2 → 1 logical reads when an existing queue-marker comment is upserted; conditional writes remain unchanged.  
  **Endpoints:** `GET /repos/{repo}/issues/{pr}/comments?per_page=100` and `GET /repos/{repo}/issues/comments/{comment_id}`.  
  **Evidence:** The paginated listing selects a matching comment’s `.id`; `_mt_upsert_comment` then fetches that comment’s `.body` solely to compare it with the proposed body.  
  **Proposed fix:** After manual review, extend `_mt_find_marker_comment_id` to return the selected ID *and* body as one snapshot, and pass both to `_mt_upsert_comment`. Preserve the current last-match selection and its behavior on an incomplete listing.  
  **Safety rationale:** `--paginate` is a `RISKY_SKIP` trigger, and replacing the later GET also changes how fresh the body comparison is.  
  **Downstream signal:** Do not auto-implement. Manually verify pagination failure handling and whether a concurrent comment edit must be detected by the later GET; retain that GET if freshness is required.

### Redundant Re-Fetch (REUSE-###)

No findings.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: `RISKY_SKIP` — Poller batching must preserve independent cache-miss fallbacks and label completeness.
- API-002: `RISKY_SKIP` — The adjacent reads are in the race-sensitive poller; preserve each read’s failure behavior.
- API-003: `RISKY_SKIP` — Reissue includes stall-recovery paths, where freshness and failure behavior require manual review.
- API-004: `RISKY_SKIP` — Batched candidate metadata must not weaken the poller’s per-PR identity verification.
- API-005: `RISKY_SKIP` — Changing calls inside retry loops requires manual rate-limit and permanent-error classification review.

### Summary Counts

Counts include the net-new finding and the five reviewed Deep Audit cross-references.

| Tag | Count | IDs |
|---|---:|---|
| `SAFE_TO_MERGE` | 0 | — |
| `NEEDS_VERIFICATION` | 0 | — |
| `RISKY_SKIP` | 6 | MERGE-001, API-001–API-005 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
