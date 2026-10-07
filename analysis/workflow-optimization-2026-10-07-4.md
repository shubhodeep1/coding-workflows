## Executive Summary

- **Review/autofix is the dominant measured critical path.** In `shubhodeep1/coding-workflows`, 312 runs had a 557.5s median and 1,978s p95. Four slow review runs account for **all 41,737,826 reported OpenRouter tokens** and 76 reported calls. Prioritize per-phase timing and same-head dispatch diagnostics before changing models. **Impact:** potentially substantial latency and token savings, not yet quantifiable across all reviews. **Confidence:** high for measured runs; low for extrapolation.
- **CI and check-failure triage show concentrated failures.** CI failed 13/26 runs; triage failed 11/26, including six at “Post check-failure triage issue” and five at “Collect check-failure context.” Add reason-coded stage outcomes without weakening either gate. **Impact:** faster diagnosis and fewer avoidable repeat investigations. **Confidence:** high for failure locations, low for underlying causes.
- **Checkout and conflict preparation need better failure attribution.** Four review failures stopped at “Checkout PR head branch,” including runs `37647760213` (2,232s) and `37661395500` (1,899s); two stopped at conflict-prompt preparation, `37663738492` and `37665635776`. Log elapsed time and classified Git failure reasons before increasing retries. **Impact:** possible savings approaching the time spent before a diagnosed failure; actual savings unknown. **Confidence:** high for locations, low for causes.
- **Cache and MCP conclusions are coverage-limited.** Eighteen of 76 OpenRouter calls lack usable usage, making aggregate `cache_hit_rate` null. Twelve Semble queries logged 122,833 bytes; 40 Semble fallbacks are classified as **CI contract-test** events, not runtime failures. **Impact:** better attribution before any cache or rollout change. **Confidence:** high.
- **The supplied full-log directory is not mounted in this environment.** Neither its `summary.json` nor `errors/`, `slow/`, or `recent/` could be inspected. Findings below use the assembled context and run-row summaries; exact error text, API counts, and memory events require the archive. **Impact:** restores root-cause confidence. **Confidence:** high.

## Speed Optimizations

1. **Critical path — distinguish review compute from repeat dispatch.** Run `37668894733` reports roughly 406s in `review / codex-agent` within a 428s review; review-family p95 is 1,978s. PR `#6678` appears in four long runs, but head/base revisions are unavailable, so these are **not proven duplicates**. Emit a phase-duration summary and a sanitized head/base/rubric identity for each dispatch; suppress a review only when those identities and the prior successful disposition match. **Estimated saving:** up to one whole confirmed redundant run—for example, 2,354s for `37663225276` *if* it proves redundant; otherwise zero. **Risk:** medium if identity checks are incomplete; logging alone is low-risk.
2. **Failure-path latency — classify checkout early.** Runs `37647760213`, `37653635912`, `37661395500`, and `37663723863` failed at “Checkout PR head branch” after 1,161–2,232s overall. The workflow already retries fetch up to four attempts. Add a `REVIEW_CHECKOUT_DIAG` outcome with pre-step elapsed time, fetch attempts, exit category, and whether the source/workspace/metadata-head guard matched; then move only proven safe, cheap preflight checks ahead of expensive setup. **Estimated saving:** unknown, bounded by time preceding each affected failure. **Risk:** low for logging; medium for step reordering.
3. **Release scan — profile before restructuring.** In cancel-on-PR-close run `37669878803`, merge-train release took about 34s of 48s and reported `examined=37 released=0`. Add counts for queued/active/blocked/unresolved candidates, distinct file fetches, cache hits, pages, and elapsed time. Reuse the existing cycle-local file cache; optimize a measured hotspot rather than skipping the safety scan. **Estimated saving:** 0–34s on a comparable run, unproven. **Risk:** low for logging.
4. **Queueing, not compute — measure contention.** Auto-release run `37669016921` spent approximately 124s of 142s waiting for `release-check`. Emit queued-at, job-started-at, and compute-time summaries by job; correlate them with short, non-actionable dispatches before changing triggers. **Estimated saving:** unquantifiable until contention is linked; no claim that removing skips would eliminate the 124s. **Risk:** low.

## Cost Optimizations

1. **Prevent only verified redundant review work.** The four token-bearing runs are `37649595095` (9,445,823), `37657163863` (11,876,236), `37654281070` (3,738,177), and `37663225276` (16,677,590)—all review/autofix. Capture dispatch reason and revision identity, then deduplicate only an identical, already-completed review. **Estimated saving:** up to that run’s *reported total tokens* per proven duplicate; dollars and billable-token savings cannot be calculated here. **Quality risk:** high if a changed head, base, or rubric is mistaken for the same review.
2. **Measure prompt expansion before trimming it.** Runs `37649595095` and `37657163863` emitted two and four `CONTEXT_BUDGET_WARN` events respectively; together they account for 21,322,059 reported total tokens. Add per-prompt-section byte/token estimates, model, reasoning level, and cache usage to review/editor summaries. Deduplicate repeated context only after identifying the growing section. **Estimated saving:** unknown; six warnings identify candidates, not removable-token counts. **Quality risk:** retain material evidence and the existing review rubric.
3. **Keep model changes scoped.** The workflow configures multiple reviewer models, reviewer reasoning `xhigh`, and editor reasoning `high`, but the supplied run aggregates do not separate token cost or accepted findings by model/slot. Log model, reasoning level, attempts, outcome, and usage per slot; test a lighter setting on a narrowly defined, already-supported profile before changing production defaults. **Estimated dollar saving:** unavailable without per-model prices and usage. **Quality risk:** potential missed findings; do not globally downgrade.
4. **Assess MCP value from contribution, not query count.** The four slow reviews made 12 Semble queries totaling 122,833 logged bytes—about 10,236 bytes/query. Target names, `sources`, and `static_dup_bytes` contributions are not available in this context, so reduced prompt expansion is **not established**. Log target, contributed-source count, duplicated-static bytes, and bytes actually inserted into prompts. Serena has zero recorded queries/tool calls and is explicitly disabled in summaries for runs `37668880820` and `37668876036`; no replacement benefit can be claimed. **Estimated saving:** unknown. **Quality risk:** do not prune retrieval or enable Serena based on zeros alone.
5. **Avoid post-diagnosis waste where safe.** Failed triage posting runs `37655564710` and `37658984994` each recorded one Codex call and 2,026 tokens. Classify whether posting failed on marker validation, token scope, transient API error, or another condition; check locally verifiable prerequisites before diagnosis without adding an API probe. **Estimated saving:** up to 4,052 recorded Codex tokens across these two cases *only if* the failure was predictable. **Quality risk:** preserve diagnosis when posting may recover.

## Reliability Improvements

1. **CI test failures — regression or environment category, cause unverified.** CI failed 13/26 runs. “Validation self-test unit tests” failed in `37647983427`, `37649159395`, and `37651766699`; merged-PR commit-guard tests failed in `37650128310`, `37664544454`, `37666875390`, and `37668038984`. Emit failing test identifier, sanitized assertion/error category, test-suite duration, and first-failure timestamp in a compact CI summary. Fix the recurring assertion once identified; retain the gates. **Expected impact:** fewer repeated CI failures and faster repair, not a defensible percentage yet. **Rollback:** remove added diagnostics if noisy; do not fail open past tests.
2. **Triage stage failures — collection versus posting.** Six of 11 triage failures occurred at posting (for example `37655564710`); five occurred at collection (for example `37668269982`). Existing code has several distinct collection gates and a separately token-scoped posting step. Emit `stage`, `reason_code`, API status category, retry count, and whether the issue-body prerequisite passed—without body text or credentials. **Expected impact:** isolate the dominant fix and reduce unsuccessful diagnosis-to-post cycles. **Rollback:** retain current fail-closed issue validation and self-loop guard.
3. **Review checkout/conflict failures — Git or state category, cause unverified.** Record fetch exit classification and attempt durations at checkout, and merge exit, unmerged-path count, blob-backfill attempt, and classified terminal reason in `scripts/review_conflict_prepare.sh`. The script already distinguishes failed merge replay from ordinary content conflicts; do not turn either into a successful resolution. **Expected impact:** shorter time to repair the four checkout and two preparation failures. **Rollback:** diagnostics can be removed without changing Git behavior.
4. **Separate test fallbacks from rollout health.** The 40 Semble fallbacks occur in CI telemetry and are all classified `contract-test`; recorded runtime fallbacks are zero. No Serena probe failure or runtime fallback was recorded. Keep contract-test and runtime counters separate in alerts, and emit target/reason for real runtime fallbacks. **Expected impact:** fewer false rollout alarms without masking future failures. **Rollback/fail-open:** retain existing fallback behavior.
5. **Treat cleanup warnings as warnings, not failed reviews.** Successful reviews `37668894733` and `37668931133` summarize post-job `git-submodule` “without a working tree” warnings. Log checkout/post-job workspace presence and cleanup exit category; investigate the lifecycle before changing cleanup. **Expected impact:** clearer alerting and prevention of a possible future hard failure; current failure-rate benefit is unproven. **Rollback:** keep successful-run disposition unchanged.

Recorded `BREAK_GLASS` count is **0** in covered telemetry; there is no observed policy-bypass pattern. The six context-budget warnings instead warrant prompt-size investigation, not a policy-pressure conclusion.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` event is present in the supplied run rows or summaries, and the deep-dive logs are unavailable. Consequently, retrieve hit rate, average `estimated_tokens` versus `token_budget`, `keyword_method` distribution (`llm`/`plain`/`none`), zero-record retrieves, `fail_open: true`, `enabled: false`, and push retries are **not measurable**, not zero. `scripts/ai_memory.py` contains retrieve-event emission; verify that the relevant workflow steps emit it and that the collector preserves an aggregate by operation and role. Record counts and bounded numeric fields only, not retrieved records. **Expected impact:** makes memory effectiveness and failed-open operation diagnosable without changing retrieval behavior.

## GH API Call Audit

- **Observed scan, not observed call count:** cancel-on-PR-close `37669878803` examined 37 merge-train candidates in roughly 34s. `scripts/review_merge_train.sh` already caches file lists per distinct PR and documents a paginated active-run listing; 37 candidates must **not** be reported as 37 API calls. Add endpoint-class call/page counts, cache hits, retries, HTTP status categories, and elapsed time to its release summary. **Expected reduction:** zero justified from current evidence; each *newly demonstrated* duplicate file-list lookup avoided would save its REST page call(s). This follows `CLAUDE.md` §15’s reuse-before-new-call rule.
- **Existing batching should be preserved:** scheduled close cleanup fetches queued and in-progress runs with two bounded status-filtered requests and batches PR-state GraphQL lookups in groups of 50. Do not collapse the status filters into an unbounded completed-run scan. Instrument page counts, unresolved PRs, and batch failures before proposing a call-count change. **Expected reduction:** none safely established; rate-limit risk is currently unquantified.
- **Triage hotspot to measure:** `scripts/check_failure_triage.sh` fetches PR metadata, conditionally fetches a parent issue, and pages open triage issues for deduplication. Log whether each lookup was needed and reused; batch future per-item additions under §15 rather than placing lookups inside a candidate loop. **Potential reduction:** one call per confirmed redundant lookup, but the supplied runs provide no count to multiply by.

`GH_PAT_BUDGET` start/end logging exists, but no budget or endpoint totals were supplied. Attach its existing report and bounded per-step call summaries to the next analysis. Shared-PAT budget deltas are not exact per-job call counts under concurrency; report 403/429 and backoff separately. No rate-limit event is evidenced in this input.

## Prompt Cache & Memory System

Across the four token-bearing reviews, telemetry reports 30,317,000 cache-read tokens and 2,707,037 cache-write tokens, but **aggregate `cache_hit_rate` is null** because 18/76 OpenRouter calls lack usable usage. Fully reported run `37654281070` has a measured hit rate of **72.165%**; it is not a fleet rate. Add usage-availability reason and per-model, per-phase cache totals so missing calls cannot silently appear as misses.

**Inference to test:** dynamic PR/run metadata placed before reusable instructions, varying retrieved context, or growing reviewer history could fragment prompt prefixes. Log section-order identifiers and sizes—not prompt text—and put stable instructions before volatile content where behavior permits. Correlate cache results with the six context warnings in `37649595095` and `37657163863`. **Estimated token, latency, and reliability impact:** unknown until complete usage and section-size measurements are available. Memory-retrieval effectiveness remains unmeasured as described above.

## Orchestrator Health

Plan run `37668600898` reported “planning stalled. Re-triggering plan generation,” then skipped because issue `#40` already had an open PR. **Inference:** stall recovery may have acted on state that the existing-PR gate subsequently superseded; this does not prove a stuck issue. Emit tracking-issue state age, recovery trigger, open-PR lookup outcome, and final gate disposition; reuse that lookup before re-dispatch where safe. **Expected impact:** fewer unnecessary recovery dispatches, count unknown.

Clarify had 120 “other” outcomes in 124 runs, plan 113/119, implement 116/119, and orchestrate-clarify-respond 119/119. Recent rows show skipped examples, but the aggregate “other” category is not a measured clarification-loop or deferral count. Add explicit gate-outcome counters, wave transitions, deferral reason/age, conflict-heal attempts, and terminal/stuck-state age. Track *actionable runs per dispatch* and *recovery dispatches ending in an existing-PR skip*; do not loosen guards to improve these numbers.

## Pipeline Flow Bottlenecks

| Segment | Evidence | Bottleneck type and next safe action |
|---|---|---|
| Clarify → plan → implement | Recent runs `37669973120`, `37669972985`, and `37669973093` skipped; plan `37668600898` recovered then found an existing PR. | **Dispatch/gating:** record skip and recovery reasons before reducing fan-out; end-to-end saving unknown. |
| Review/autofix | 312 runs; p50 **557.5s**, p95 **1,978s**; `37668894733` spent ~406s in `codex-agent`. | **Compute:** phase and model-attempt timing first; conditionally deduplicate identical completed work. |
| Validate/CI | 13/26 failures; CI p50 **1,165s**. | **Failure/retry overhead:** surface first failing test early, without omitting the rest of required validation. |
| Orchestrate/release | Poller p50 **674.5s**; release-check waited ~124s in `37669016921`. | **Queueing:** separate scheduled wait from poll compute before tuning cadence or concurrency. |
| Merge/conflict | Preparation failures `37663738492`, `37665635776`; merge-train `37669878803` examined 37, released 0. | **Conflict/scan overhead:** count terminal reasons, blockers, and API pages; preserve conservative release checks. |

Run-row `retries=0` does not exclude in-step Git or API retry loops; instrument those separately.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** review compute and long-tail review runs; CI validation; observed release queue wait. **Top failure modes:** recurring CI test failures, triage collection/post failures, review checkout and conflict-preparation failures. **Highest measured cost:** 41,737,826 OpenRouter total tokens across four slow review runs; dollar cost and the other reviews’ token spend are not established.

**Top three actions, in order:**
1. Add reason-coded checkout, triage, and CI first-failure summaries; use them to fix the recurring cause without relaxing gates.
2. Add review phase/model/cache and revision-identity summaries; deduplicate only proven identical completed reviews.
3. Attach endpoint/page/retry and queue-time summaries to merge-train, poller, and release runs before changing API or scheduling behavior.

## Metrics Appendix

**Window and outcomes** (`other` is the collector category, not assumed to be a failure or success):

| Scope | Runs | Success | Failure | Cancelled | Other | Success / runs | Failure / runs | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1,000 | 415 | 33 | 2 | 550 | 41.5% | 3.3% | 9s / 1,280s |
| Review/autofix | 312 | 302 | 9 | 1 | 0 | 96.8% | 2.9% | 557.5s / 1,978.45s |
| CI | 26 | 12 | 13 | 1 | 0 | 46.2% | 50.0% | 1,165s / 1,623s |
| Check-failure triage | 26 | 0 | 11 | 0 | 15 | 0% | 42.3% | 6.5s / 346s |
| Orchestrate poll | 14 | 14 | 0 | 0 | 0 | 100% | 0% | 674.5s / 1,181.1s |

**Measured cost and cache coverage:** 125/1,000 runs have parsed log telemetry; the supplied full-log archive is unavailable. All nonzero OpenRouter and Semble query aggregates below come from the four identified slow review runs.

| Metric | Supplied value | Interpretation |
|---|---:|---|
| OpenRouter calls; usage available / unavailable | 76; 58 / 18 | 23.7% of calls lack usable usage |
| Prompt / completion / total tokens | 8,421,596 / 292,249 / 41,737,826 | Reported fields; total includes substantial cache activity |
| Cache write / read tokens | 2,707,037 / 30,317,000 | Not a fleet hit-rate calculation |
| `cache_hit_rate` | **null** aggregate; 72.165% in `37654281070` | Aggregate invalidated by unavailable usage |
| Codex calls / tokens | 2 / 4,052 | Both calls appear in failed triage-post runs |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 11,000 / 3,096,040; 125 samples | Collector uses **run duration fallback**; these are not model-call latencies |
| `break_glass_count` / `context_budget_warn_count` | 0 / 6 | Warnings: 2 in `37649595095`, 4 in `37657163863` |

| API/MCP signal | Supplied count and size | Availability or attribution |
|---|---|---|
| GH API calls / 403–429 / retries | **Not supplied** | `37669878803`: 37 merge-train candidates examined, **not** a call count; GH_PAT budget values absent |
| Semble queries | 12; **122,833 logged bytes** | Review/autofix; target and inserted-prompt-byte breakdown unavailable |
| Semble fallbacks | 40 contract-test; **0 classified runtime** | CI; target/reason breakdown unavailable |
| Semble bootstraps / failures / unused / measured time | 0 / 0 / 0 / 0ms recorded | Absence of bootstrap telemetry does not establish bootstrap cost or availability |
| Semble sources / static duplicated bytes | 0 / 0 recorded | Contribution fields unavailable here; do not interpret as proof of no contribution |
| Serena queries / response bytes / tool calls / query time | 0 / 0 / 0 / 0ms recorded | Disabled in cited review summaries; no per-tool breakdown observed |
| Serena fallbacks; probes ok / failed / skipped | 0; 0 / 0 / 0 recorded | No observed probe, so availability cannot be assessed |

**Per-target MCP availability:** no target-level rows were supplied. Serena target **not observed**: `probe_ok=0`, `probe_failed=0`, `probe_skipped=0` recorded; this is **not** a successful availability probe. Semble query and fallback totals cannot safely be allocated to targets. **Other MCP servers observed:** none in the supplied aggregates; unmounted full logs prevent a complete unknown-server audit.

## Deep Audit — Workflows & Scripts (2026-10-07)

### Section 1: Bug & Correctness Sweep

Read-only inventory: 54 workflows, 106 shell scripts, and 74 Python scripts. All shell scripts passed `bash -n`. The local Python 3.11 parser could not parse `scripts/workflow_retro.py:793-807`; its workflow selects Python 3.12 at `.github/workflows/workflow-log-analysis.yml:244-247`, so this is **not** a confirmed workflow defect. Local `actionlint`, `shellcheck`, and YAML parsing were unavailable; CI runs those checks at `.github/workflows/ci.yml:203-276,365-370`.

- **BUG-001** — **File:** `.github/workflows/check_failure_triage.yml:476-479`. **Severity:** High. **Category:** `bug`. **Description:** `gh_retry` wraps a non-idempotent `gh issue create`. Its pre-create fingerprint check occurs separately in `scripts/check_failure_triage.sh:348-359`; if creation succeeds but the response is lost, a retry can create a second issue. This is a failure-path inference, not an observed duplicate. **[NEEDS VERIFICATION]** **Recommended fix:** Attempt creation once; on an ambiguous result, look up the exact fingerprint marker before deciding whether to retry. Preserve the posting step’s restricted token.
- **BUG-002** — **File:** `scripts/tg_helpers.sh:169-205,240-276`. **Severity:** Medium. **Category:** `bug`. **Description:** Message IDs are appended by GET–modify–PATCH, so concurrent sends can overwrite one another. A failed GET instead creates another tracking comment; raw write calls use `curl -s` without HTTP-failure checking and discard their result. Lost tracking could leave messages uncleaned. **Recommended fix:** Use one immutable tracking entry per message ID, which the existing cleanup scan can collect, or serialize updates per issue; check HTTP status for every write.
- **BUG-003** — **File:** `scripts/validate_process.sh:4185-4227`. **Severity:** Medium. **Category:** `bug`. **Description:** Fix-up deduplication inspects at most 200 open managed issues, treats lookup failure as absence, and then creates an issue. An older matching issue outside that window—or two concurrent validations passing the lookup—can yield duplicate fix-ups. This is an inference; no duplicate is established by the supplied report. **[NEEDS VERIFICATION]** **Recommended fix:** Page the label-filtered lookup to completion, keep its existing marker match, and serialize creation for the same tracking issue and validation cycle; distinguish an incomplete lookup from a confirmed miss.
- **SEC-001** — **File:** `scripts/gh_helpers.sh:548-605`. **Severity:** High. **Category:** `security`. **Description:** `gh_retry` prints its full argument list on setup, permanent, and exhausted failures. Callers can pass comment text as `-f body=...`, as at `scripts/validate_process.sh:1153-1159`; that text can therefore enter workflow logs. Whether a covered run exposed sensitive text is unverified. **[NEEDS VERIFICATION]** **Recommended fix:** Log only a redacted command class, endpoint, attempt, and status. Send bodies via `--input` files as `post_tracking_comment` does at `scripts/orchestrate_poll_process.sh:2710-2731`; sanitize stderr separately.
- **SEC-002** — **File:** `scripts/gh_helpers.sh:738-750`. **Severity:** Medium. **Category:** `security`. **Description:** When a successful API response is invalid JSON, the helper prints its first 50 raw lines. Callers fetch full PR or issue payloads, including at `scripts/check_failure_triage.sh:233-249`; a malformed response could disclose returned content in logs. **[NEEDS VERIFICATION]** **Recommended fix:** Replace the raw preview with response byte count, digest, endpoint class, and parse-error category.
- **SEC-003** — **File:** `.github/workflows/update_workflows.yml:68-85`. **Severity:** Medium. **Category:** `security`. **Description:** The template-fetch clone embeds `GH_TOKEN` in its remote URL. This exposes the credential in command arguments and can retain it in the temporary clone’s remote configuration; the checkout path is passed to later steps at lines 104-105. The practical exposure depends on runner isolation and cleanup. **[NEEDS VERIFICATION]** **Recommended fix:** Clone with a credential-free remote URL and an environment-scoped Git authorization configuration; remove the temporary checkout after its last use.

### Section 2: GitHub API Call Redundancy Audit

Call counts below are **code-path estimates per execution**, excluding transport retries and pagination unless stated. The existing report’s 37 examined merge-train candidates are not treated as 37 API calls.

- **API-001** — **File:** `scripts/gh_helpers.sh:727-779`. **Severity:** Medium. **Category:** `api-redundancy`. **Description:** `gh_api_json_to_file` retries every command failure, including permanent 404/422 responses; it also sleeps after the final failed attempt. **Current → proposed:** up to **5 → 1** calls per permanent failure. **Recommended fix:** Apply this file’s `_is_gh_permanent_failure` classification (`scripts/gh_helpers.sh:182-185`) before backoff, and sleep only when another attempt remains. Keep retries for invalid JSON after a successful response.
- **API-002** — **File:** `scripts/gh_helpers.sh:795-843`. **Severity:** Medium. **Category:** `api-redundancy`. **Description:** `curl_gh_api` backs off on non-rate-limited permanent HTTP failures and sleeps after its last attempt. **Current → proposed:** up to **5 → 1** calls for 404/422. **Recommended fix:** Extend the `gh_retry` permanent-failure pattern to curl’s HTTP-status branch; retain reset-aware handling for rate limits and bounded backoff for transient failures.
- **API-003** — **File:** `scripts/orchestrate_poll_process.sh:21462-21498`. **Severity:** Medium. **Category:** `api-redundancy`. **Description:** Adjacent GraphQL batches fetch current-wave details and then fetch the same issues’ labels again. The first result already contains `labels` and `labels_complete` (`scripts/orchestrate_poll_process.sh:16008-16017`). **Current → proposed:** for *W* fully returned issues, **2⌈W/25⌉ → ⌈W/25⌉** GraphQL calls, before existing miss fallbacks. **Recommended fix:** Populate `LABELS_JSON` from complete detail entries; request the dedicated label batch only for incomplete or missing entries, then retain per-issue REST fallback. Extend the existing `_fetch_candidate_issue_details_graphql` cache path, not a new cache.
- **BATCH-001** — **File:** `scripts/orchestrate_poll_process.sh:5933-5965`. **Severity:** Medium. **Category:** `api-batching`. **Description:** The blocker loop makes one REST state lookup for each of *B* fix-up issues. **Current → proposed:** **B → ⌈B/25⌉** reads when a complete aliased batch succeeds; unresolved nodes retain individual fallbacks. **Recommended fix:** Add an issue-state variant of `_fetch_issue_labels_batch_graphql` (`scripts/orchestrate_poll_process.sh:3721-3789`), keyed by issue number. Preserve the current “unknown means defer” disposition. Snapshot timing and fallback parity need checking. **[NEEDS VERIFICATION]**
- **BATCH-002** — **File:** `scripts/review_merge_train.sh:158-169,248-280,793-800`. **Severity:** Medium. **Category:** `api-batching`. **Description:** The release loop caches file lists, but still fetches each distinct candidate PR separately. **Current → proposed:** for *D* distinct uncached PRs, **at least D REST pages → ⌈D/25⌉ GraphQL batches plus REST pages for incomplete/oversized file lists**. **Recommended fix:** Prefetch bounded, aliased PR-file lists following `_fetch_candidate_issue_details_graphql` (`scripts/orchestrate_poll_process.sh:15929-16000`), fill `_MT_FILES_CACHE`, and retain the existing paginated REST path for incomplete data. Verify rename-path and pagination parity before using a batch for blockers. **[NEEDS VERIFICATION]**
- **BATCH-003** — **File:** `scripts/review_resolve_review_threads.sh:259-265,267-339`. **Severity:** Low. **Category:** `api-batching`. **Description:** Each of *N* planned resolutions issues a separate GraphQL mutation; *R* ignored threads also require a reply first. **Current → proposed:** **N + R → ⌈N/25⌉ + R** requests where alias batching preserves per-thread results. **Recommended fix:** Batch only reply-eligible resolutions after their required replies, map every alias to its thread ID, and leave any failed or unconfirmed thread open. Verify partial-error and retry behavior before rollout. **[NEEDS VERIFICATION]**

**Related call path:** BUG-002’s tracked send normally performs one comments GET and one POST/PATCH per message. An immutable-entry design would change **2 → 1** API calls per message, while retaining the existing paginated cleanup reads at `scripts/tg_helpers.sh:399-416`.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — **File:** `.github/workflows/implement.yml:1003-1047`. **Severity:** Medium. **Category:** `duplication`. **Description:** Verified-source selection, required-file checks, and install loops recur in `.github/workflows/clarify.yml:232-250`, `.github/workflows/plan.yml:281-297`, and `.github/workflows/orchestrate_poll.yml:399-417`, with differing inventories. **Recommended fix:** Extend `scripts/stage_workflow_support.sh` with `stage_verified_support_files <verified_source> <fallback_source> <manifest> <destination>` and move these callers to explicit per-phase manifests. Keep immutable-source verification and each workflow’s optional-file behavior.
- **DUP-002** — **File:** `scripts/validate_process.sh:1162-1196`. **Severity:** Medium. **Category:** `duplication`. **Description:** `ensure_label_exists` is reimplemented here and at `scripts/orchestrate_poll_process.sh:3380-3434`, alongside `scripts/label_helpers.sh:153-187`. Error returns and process-local caching differ. **Recommended fix:** Make `label_helpers.sh` own `ensure_label_exists <label> [repo]`, with an optional cache argument or separate cached wrapper. Update both callers, explicitly preserving their present fail-open call-site choices.
- **DUP-003** — **File:** `.github/workflows/review_autofix.yml:3319-3350`. **Severity:** Low. **Category:** `duplication`. **Description:** Cache-hit inference and `workspace_init.sh metadata/finalize` wiring closely repeat `.github/workflows/validate.yml:520-549`. **Recommended fix:** Put `workspace_cache_match_key <cache_hit> <cache_path> <exact_key> <issue_prefix>` in `scripts/workspace_init.sh` or a sourced workspace helper; update both callers without changing their workspace identifiers or step outputs.
- **DUP-004** — **File:** `.github/workflows/review_autofix.yml:1748-1762`. **Severity:** Low. **Category:** `duplication`. **Description:** The same inline rate-limit snapshot pattern occurs in multiple jobs, including `.github/workflows/clarify.yml:1654-1665`, while `scripts/gh_helpers.sh:30-75` already implements `gh_pat_budget <phase> <workflow> <job> <snapshot_file>`. **Recommended fix:** Use that helper for repeated **post-bootstrap** snapshots; leave pre-checkout snapshots in place where moving them would lose initial-call coverage.

Internal wrapper workflows already call reusable phase workflows; their differing triggers and predicates are contract-tested (`agents.md`, “Phase wrapper predicate parity”). No additional >70%-identical workflow pair was established as a safe merge candidate.

### Section 4: Expression Size Limit Risk Assessment

Measurements are dedented, static `run:` scalar character counts **only for blocks containing `${{ }}`**. Substituted values vary at runtime, so these are estimates of expanded size, not exact runtime lengths. The largest `if:` line measured 703 characters (`.github/workflows/review_autofix.yml:5642`), well below 21,000.

- **EXPR-001** — **File:** `.github/workflows/implement.yml:1005-1395`. **Severity:** High. **Category:** `expression-limit`. **Description:** “Stage workflow support files” contains **19,132 static characters**, three interpolations, and approximately **1,868 characters of headroom** to 21,000. **Recommended fix:** Extract the staging body to a verified script under `scripts/`; pass its three expression values through step `env:` and retain the step’s guards and outputs.
- **EXPR-002** — **File:** `.github/workflows/implement.yml:3512-3836`. **Severity:** Medium. **Category:** `expression-limit`. **Description:** “Preflight destructive-commit guard” contains **15,936 static characters**, one interpolation, and approximately **5,064 characters of headroom**. **Recommended fix:** Extract its body to a verified `scripts/` preflight helper, passing repository identity through `env:` and preserving the temporary-index cleanup trap.
- **EXPR-003** — **File:** `.github/workflows/review_autofix.yml:6971-7105`. **Severity:** Medium. **Category:** `expression-limit`. **Description:** Although this block is only **7,659 static characters** and is not a 21,000-character candidate, the **whole workflow is 473,017 bytes**, just **6,983 bytes below this repository’s 480,000-byte CI guard**. The documented host limit is **512,000 bytes**, not the prompt’s general 1 MB threshold (`CLAUDE.md:1613-1641`; `tests/test_workflow_file_size_limit.py:24-51`). **Recommended fix:** Extract this warning step and further post-bootstrap inline bodies using the existing `review_autofix_step_<slug>.sh` pattern; register each in `scripts/stage_workflow_support.sh:57` and its step-script tests, targeting the documented 50 KB guard margin.
- **EXPR-004** — **File:** `.github/workflows/validate.yml:284-483`. **Severity:** Low. **Category:** `expression-limit`. **Description:** An inline support-manifest heredoc occupies part of an interpolated block of **10,837 static characters**, leaving approximately **10,163 characters**. It is below the flag thresholds but is a substantial future-growth point. **Recommended fix:** Store the manifest with verified support assets under `scripts/` and pass its path to the existing `stage_workflow_support.sh validate --manifest` invocation at line 483.

The largest non-interpolated block—`.github/workflows/implement.yml:2283` onward, approximately 63,512 scalar characters—was **excluded** from the expression count. No workflow exceeded 800 KB.

### Section 5: Cross-Cutting Concerns

- **DEAD-001** — **File:** `scripts/review_merge_train.sh:202-206`. **Severity:** Low. **Category:** `dead-code`. **Description:** `_mt_own_files` has no in-repository call; active paths use `_mt_own_files_into` or `_mt_pr_files_into`. It may nonetheless be a sourced compatibility surface. **[NEEDS VERIFICATION]** **Recommended fix:** Confirm whether the printing form is a supported external interface. If so, pin it with a compatibility test; otherwise deprecate it before removal under the repository’s identifier-immutability rule.
- **SHELL-001** — **File:** `scripts/review_merge_train.sh:789`. **Severity:** Low. **Category:** `shellcheck`. **Description:** The optional `grep -e` argument uses an unquoted `${head:+...}` expansion. Legal Git ref names constrain the practical input, but the shell still applies word splitting and pathname expansion; a ShellCheck warning was not locally verified. **[NEEDS VERIFICATION]** **Recommended fix:** Build an optional Bash argument array and pass it as `"${args[@]}"`.
- **SHELL-002** — **File:** `.github/workflows/ci.yml:365-370`. **Severity:** Low. **Category:** `shellcheck`. **Description:** CI runs ShellCheck at `--severity=error`, so warning-level findings in `scripts/*.sh` do not fail the gate. **Recommended fix:** Review and resolve warning-level results, then raise this gate to `--severity=warning`; keep the existing script glob coverage.
- **DEBT-001** — **File:** `scripts/label_helpers.sh:23-141`. **Severity:** Low. **Category:** `tech-debt`. **Description:** The 57 label colors and descriptions duplicate `.github/ai/label_contract.v1.json`. A read-only comparison found **57/57 matching today**, but additions require parallel edits. **Recommended fix:** Add a CI parity test against the JSON contract and keep the shell catalog as the documented fallback when the contract file is absent.

No literal `TODO`, `FIXME`, or `HACK` markers were found in the scoped workflow and script files. The review checkout, conflict-preparation, queue-wait, and telemetry-diagnostic requests already in the in-progress report are not repeated as findings here.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BUG-001, SEC-001, EXPR-001 |
| Medium | 13 | BUG-002, BUG-003, SEC-002, SEC-003, API-001, API-002, API-003, BATCH-001, BATCH-002, DUP-001, DUP-002, EXPR-002, EXPR-003 |
| Low | 8 | BATCH-003, DUP-003, DUP-004, EXPR-004, DEAD-001, SHELL-001, SHELL-002, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 2–4 | Medium |
| API call optimization | 4–6 | Large |
| Code modularization | 8–12 | Large |
| Expression size reduction | 5–9 | Large |
| Medium/Low fixes | 6–10 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-10-07)

### Safety Tag Legend

`SAFE_TO_MERGE` meets the stated equivalence and failure-handling checks; `NEEDS_VERIFICATION` requires the specified checks first; `RISKY_SKIP` touches a protected path and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

- **MERGE-001 — NEEDS_VERIFICATION.** **Calls:** `.github/workflows/issue_pr_status.yml:258-263` and `.github/workflows/issue_pr_status.yml:367-378`. **Current → proposed:** 2 → 1 GraphQL calls when branch- or body-derived issue numbers require the second batch and the combined query succeeds; retain existing fallback calls on incomplete results. **Endpoints:** GitHub GraphQL `repository.pullRequest.closingIssuesReferences` and aliased `repository.issue`.
  **Evidence:** The first query returns closing issues with `number`, `body`, and labels. The later query requests `number`, `body`, and labels again for `LOOKUP_ISSUE_NUMBERS`, which can also include branch- or body-derived numbers (`.github/workflows/issue_pr_status.yml:265-309`).
  **Proposed fix:** In the “Update linked issue labels when PR closes” step, derive branch/body candidate numbers before the first query and add their `issue` aliases to it. Populate the existing classification variables from that response; retain the second batch for unresolved numbers and the existing REST fallback.
  **Safety rationale:** The queries run in one step with the same token, but their issue sets differ and `ensure_label_exists` runs between them; response and failure-path equivalence is not statically proven.
  **Downstream signal:** Verify classification against closing-only, branch/body-only, overlapping, truncated, and partial-error responses; confirm that moving candidate derivation preserves every fallback and label/close disposition before combining queries.

### Redundant Re-Fetch (REUSE-###)

- **REUSE-001 — RISKY_SKIP.** **Calls:** `scripts/review_merge_train.sh:321-326` and `scripts/review_merge_train.sh:392-404`; gate caller `scripts/review_merge_train.sh:478-519`. **Current → proposed:** for an existing marker, *P* paginated comment-list pages plus 1 comment GET → *P* pages; writes remain unchanged. **Endpoints:** REST `GET /repos/{owner}/{repo}/issues/{pr}/comments` and `GET /repos/{owner}/{repo}/issues/comments/{id}`.
  **Evidence:** `_mt_find_marker_comment` reads each candidate’s body to match the marker but returns only its ID and creation time. `_mt_upsert_comment` then fetches that ID’s body to test whether a PATCH is needed.
  **Proposed fix:** If manually approved, extend `_mt_find_marker_comment` to return the selected body alongside ID and creation time, and pass it through the gate caller to `_mt_upsert_comment`; preserve its existing lookup for callers without that data.
  **Safety rationale:** The first read is paginated, and a queue-label write intervenes before the body GET; the fresh read can detect a concurrent comment edit.
  **Downstream signal:** Do not auto-implement. Manually test pagination, latest-marker selection, concurrent marker edits, and queue-label transitions before deciding whether the fresh GET may be removed.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: RISKY_SKIP — Changes retry-loop handling; manually verify permanent-error classification and rate-limit behavior.
- API-002: RISKY_SKIP — Changes curl backoff; manually verify HTTP-status and rate-limit branches.
- API-003: RISKY_SKIP — Poller cache change must preserve independently fail-open label retrieval.
- BATCH-001: RISKY_SKIP — Poller blocker reads need snapshot and unknown-state parity review.
- BATCH-002: RISKY_SKIP — Paginated file reads require completeness and path-parity review.
- BATCH-003: NEEDS_VERIFICATION — Per-thread mutation results and reply-before-resolution ordering need verification.

### Summary Counts

Net-new findings only; cross-references are excluded.

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 1 | MERGE-001 |
| RISKY_SKIP | 1 | REUSE-001 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
