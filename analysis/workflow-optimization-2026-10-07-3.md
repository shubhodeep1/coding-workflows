## Executive Summary

- **Queueing is the clearest measured latency loss.** In `shubhodeep1/coding-workflows`, integration-readiness run `37624388873` spent about 138 of 148 seconds waiting for a hosted runner; `cancel_on_pr_close` run `37623918885` waited about 510 of 645 seconds. Reducing *optional* workflow fan-out could recover tens to hundreds of seconds during contention, but the effect needs measurement. **Confidence: high on the waits; medium on the saving.**
- **Failures cluster at four boundaries:** check-failure triage (5 runs), the CI merged-PR guard test step (4), implementation (2), and review checkout (2). Add reason-coded, timed failure events at those boundaries before changing retry policy. The potential benefit is fewer failed cycles; a defensible reduction cannot yet be estimated. **Confidence: high on clustering; low on root causes.**
- **Review is the largest measured OpenRouter token driver:** five telemetry-bearing `review_autofix` runs account for 51,269,846 reported total tokens. One run, `37615213934`, accounts for 28,751,940. Record model, reasoning level, review tier, round, and per-call usage before changing quality-sensitive settings. **Estimated savings: unknown pending attribution. Confidence: high on measured totals.**
- **Semble’s 24 fallbacks are contract-test events, not observed runtime failures.** Separately, poller run `37624857038` reported Semble enabled but binary and index unavailable. Log availability by target so an unavailable rollout does not disappear behind a zero runtime-fallback count. **Estimated impact: faster detection, not a measured speed saving. Confidence: high.**
- **The evidence has a material collection gap.** The supplied summary reports log telemetry for 121/1,000 runs, and the specified full-log directory is absent in this environment. Findings below distinguish measured outcomes from unverified causes. Restoring failed-run logs is the highest-value diagnostic addition. **Confidence: high.**

## Speed Optimizations

1. **Reduce avoidable runner demand — critical-path opportunity.** Runs `37625197758` and `37624388873` waited about 92 and 138 seconds in `Integration PR readiness check`; run `37623918885` waited about 510 seconds in `heal-pr-reconcile`. The window also contains 763 skipped runs, including near-simultaneous skipped clarify, plan, implement, and orchestration runs around `37625485051`, `37625484977`, and `37625485365`. **Inference:** optional dispatch fan-out may contribute to contention; the logs do not measure its share. Emit `dispatch_decision` with eligibility reason and event key, then suppress only demonstrably redundant *optional* dispatches upstream. **Saving:** potentially 15–510 seconds on affected queue-bound runs, not guaranteed. **Risk: medium.** Do **not** skip the integration-readiness job: its repository workflow intentionally posts a status even for non-orchestrator PRs so the required context exists.

2. **Classify expensive failures sooner — critical-path opportunity.** Review runs `37615186690` and `37616298146` failed at `review / codex-agent → Checkout PR head branch` after 2,909 and 1,868 seconds of run time. The step already retries `git fetch`; total run time is **not** evidence that fetch itself took that long. Emit a bounded `review_checkout` event with entry time, fetch attempts and elapsed time, exit category (`fetch`, missing workspace, invalid ref, stale head, checkout), and job queue time. Apply an earlier gate only to a category proven permanent. **Saving:** unknown until step timing exists; **risk: low** for logging, higher for any new gate.

3. **Measure review and implementation compute separately from waiting — critical-path opportunity.** Successful review `37615213934` lasted 2,506 seconds; successful implement `37621495660` lasted 1,985 seconds. Emit per-round/per-attempt start, end, model, tool-call count, changed-file outcome, and idle/wall-time classification without transcript contents. Use that breakdown to target repeated no-change attempts rather than shortening all runs. **Saving:** not measurable from run durations alone; **risk: low** for instrumentation.

4. **Keep context trimming a measured micro-optimization.** Implement recorded 46 Semble queries and 303,060 logged bytes; review recorded 10 and 98,024 bytes. Record query target, elapsed milliseconds, selected chunks, and bytes actually inserted into the prompt. Trim consistently unused chunks only after relevance checks. **Saving:** unknown; **risk: medium** if useful context is removed.

## Cost Optimizations

1. **Attribute review usage before changing models.** `review_autofix` reports 76 OpenRouter calls, 51,269,846 total tokens, 40,807,016 cache-read tokens, and 2,592,092 cache-write tokens. Run `37615213934` alone reports 32 calls and 28,751,940 total tokens, with usage unavailable on 10 calls. The repository already has review tiers and a smaller-diff second-pass reasoning setting; the telemetry does not establish which tier or model caused this outlier. Emit per-call `model`, reasoning level, tier, pass, round, usage-available flag, and outcome. Trial a cheaper setting only for a measured low-risk tier, retaining the existing fallback and quality checks. **Estimated dollar/token saving: unknown without model prices and comparable tier outcomes. Quality risk: material if reviewer depth is reduced indiscriminately.**

2. **Investigate failed implementation spend.** Failed runs `37618246580` and `37620975934` each report 22 Codex token-count matches and 2,644,278 tokens—5,288,556 reported tokens together—at `Run Codex implementation`. Log each internal attempt’s terminal reason and whether it changed files; stop only on a verified permanent verdict, preserving transient retries. The identical totals also warrant reconciliation against individual usage records, **not** an assumption of duplicate billing. **Potential saving:** up to one affected run’s reported 2,644,278 tokens *if* a future full-work rerun is shown avoidable; no such rerun is established here. **Quality risk: low for logging, medium for early termination.**

3. **Test Semble’s net context value rather than treating queries as savings.** Across implement and review there were 56 `SEMBLE_QUERY` events and 401,084 logged output bytes, about 7,162 bytes/query. Run `37620975934` contributed 8 queries/64,340 bytes. The input does not show query targets or what material those bytes displaced, so neither prompt-expansion savings nor noisy low-value context can be established. Log target, bytes returned versus inserted, and downstream read/tool calls; cap by measured usefulness. **Estimated saving: unknown. Quality risk: avoid trimming relevant results.**

4. **Do not assume Serena is an alternative yet.** Serena has zero reported queries, tool calls, response bytes, fallbacks, and probes; `implement.yml` and `review_autofix.yml` default `SERENA_ENABLED` to false. There is no evidence that Serena replaced model/tool work—or added noisy bytes—in this window. Keep model selection and reasoning-level changes behind usage and outcome comparisons. **Estimated saving: none established.**

## Reliability Improvements

1. **Diagnose the CI guard-test cluster without blaming Semble.** All four CI failures reached `tests-hooks-and-orchestrator → Merged-PR commit guard hook tests`; examples `37614235364`, `37615186167`, `37621516620`, and `37622361139` lasted 687–892 seconds. Their 24 `SEMBLE_FALLBACK` events are all classified `context=contract-test`; zero are classified runtime fallbacks. Preserve the failing guard. Collect failed test names, assertion category, and a bounded traceback as a structured test artifact, then fix the demonstrated assertion or fixture. **Expected impact:** addresses a cluster affecting 4/11 CI runs if one cause is confirmed. **Rollback:** remove diagnostic output independently; do not make the guard fail-open.

2. **Instrument both triage failure stages.** `check_failure_triage` has 5 failures in 13 runs and no successes: collect failed in `37614754749`, `37615539647`, and `37624187656`; issue posting failed in `37616500676` and `37623261808`. The script fetches PR/parent metadata and lists open triage issues; the posting step uses a separate token. **Root-cause category remains unknown without logs.** Emit stage, sanitized API operation, HTTP/error class, retry count, elapsed time, and whether an issue was created or deduplicated—never token or issue-body contents. Keep existing self-loop and lineage protections; retry only classified transient failures. **Expected impact:** a diagnosable path to reducing 5 affected runs; **rollback:** instrumentation-only initially.

3. **Separate checkout failures from stale-head safety skips.** The two failed review runs ended at checkout, while the checkout code also has deliberate stale-head and unwritable-head exits. Emit distinct outcomes and fetch-attempt timing so a transient transport failure cannot be mistaken for a policy skip. Retain stale-head fail-closed behavior; adjust only a proven transient fetch path. **Expected impact:** fewer unnecessary failed review cycles if transport is confirmed; rate unknown.

4. **Expose availability separately from fallback.** CI’s 24 Semble fallbacks are simulated; the supplied telemetry records zero runtime Semble fallbacks. Yet poller run `37624857038` reported `SEMBLE_ENABLED=true`, `SEMBLE_AVAILABLE=false`, and `SEMBLE_INDEX_AVAILABLE=false` without a counted runtime fallback. Emit a per-target availability result and reason even when no query is attempted. Treat rare query fail-open as healthy, but alert on repeated unavailable targets in an enabled rollout. Serena has zero probes, so neither probe failure nor availability can be inferred. `BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts are both zero **in parsed coverage**, not proof of no policy or prompt pressure across all runs.

## AI Memory Health

No deep-dive excerpts were accessible at the stated logs path. One supplied `log_summary` does preserve a positive event: `issue_pr_status` run `37623918910` reports `AI_MEMORY_TELEMETRY` `finalize-task`, `ok=true`, `did_push=true`, `final_state=merged` for issue `6210`.

No `retrieve` entries are available here. **Retrieve hit rate (`records_selected > 0`), mean `estimated_tokens` versus `token_budget`, and `keyword_method` distribution (`llm`/`plain`/`none`) are undefined—not zero.** Zero-record retrievals, `fail_open=true`, `enabled=false`, and push retries likewise cannot be counted. Restore the failed/slow logs and aggregate the existing `AI_MEMORY_TELEMETRY` JSON by operation, workflow, and run; emit a coverage count when a memory-enabled step produces no telemetry. This would distinguish ineffective retrieval from absent collection without changing memory behavior.

## GH API Call Audit

**No observed per-job API call counts, endpoint histogram, rate-limit events, or inner retry counts were supplied.** A high-volume claim or numerical call reduction would be speculative.

| Auditable code path | Specific low-risk action | Call impact to test |
|---|---|---|
| `check_failure_triage.sh`, collect stage; three failures above | Count PR GET, conditional parent-issue GET, paginated open-triage listing, and check-run listing separately; attach sanitized error class and elapsed time. Reuse already-fetched PR fields within the stage. | Baseline and reduction unknown; diagnose failures without adding lookups. |
| `review_merge_train.sh`, review release | Its documented active-run check queries five statuses twice, normally 10 listing calls before follow-up pages. Emit calls/pages per status and pass, plus incomplete-listing reason; preserve the second pass until tests establish that reuse is safe. | **Code-path budget**, not an observed count or proven redundancy. |
| `implement.yml`, issue metadata steps | Record when repeated issue reads use an immutable cached field versus require fresh labels/state. Reuse only demonstrably unchanged data. | Conditional reduction; no measured duplicate-call count. |

Apply `CLAUDE.md` §15: extend an existing response or cycle-local cache before adding a lookup, batch genuine per-item reads, and use the smallest safe legacy call on cache miss. Add a sanitized `GH_API_CALL` rollup by workflow/job/step and endpoint *template*, with attempts, latency, response class, and rate-limit wait. The repository already emits `GH_PAT_BUDGET` in some jobs; bring its start/end values into this report rather than interpreting absent API data as zero. Never log credentials, request bodies, or full URLs containing parameters.

## Prompt Cache & Memory System

OpenRouter reports 40,807,016 cache-read and 2,592,092 cache-write tokens, but aggregate `cache_hit_rate` is **null** because usage is unavailable on 18/76 calls. The one supplied valid run-level rate is **71.0896%** for review run `37619963052` (16/16 calls with usage). It cannot be generalized to the other runs. Cache-read tokens are usage, not tokens that can simply be removed from a cost total.

`implement.yml` already puts a static prefix before targeted-file, memory, and implementation context. Preserve that ordering. Log a non-sensitive static-prefix fingerprint and section byte counts per attempt, alongside breakpoint enabled/fallback and provider usage availability; compare misses across the *same* model and phase before blaming dynamic noise or changing prompt order. Its existing stdin-cap warning should be paired with section sizes. With zero collected `CONTEXT_BUDGET_WARN` events and incomplete coverage, prompt-size risk is unquantified. This addition can expose token and latency opportunities while keeping cache fail-open behavior; expected savings remain unknown until coverage improves. Memory-retrieval effectiveness is likewise unmeasured, as detailed above.

## Orchestrator Health

`orchestrate_poll` completed 5/5 runs, with p50 315 seconds; run `37624857038` succeeded in 279 seconds despite Semble being unavailable. `orchestrate_clarify_respond` shows 12 successes and 160 skips across 172 runs; `workflow_failure_heal` shows 92 skips across 92 runs. **Inference:** much of this is intentional gating, but the supplied rows cannot distinguish healthy no-ops from repeated deferrals. The one observed `finalize-task` event in run `37623918910` is a positive completion signal, not a throughput rate.

Emit one bounded transition record per project tick: current/next phase, wave, decision reason (`ineligible`, waiting, deferred, conflict-heal retry, terminal), age in state, and linked run ID. Track skipped dispatches, clarification-loop count, waves advanced per poll, conflict-heal attempts, and time since last state change. Do not infer stuck projects or judge-cycle counts from workflow names alone; no linked state transitions or verdicts were supplied.

## Pipeline Flow Bottlenecks

| Flow component | Evidence | Next diagnostic or safe fix |
|---|---|---|
| **Queueing** | Readiness run `37624388873`: ~138/148 seconds waiting; PR-close run `37623918885`: ~510/645 seconds waiting. | Emit created→job-start wait per job; reduce verified optional dispatches while retaining required status checks. |
| **Clarify → plan** | Clarify: 190 runs, p50 1 second, p95 432 seconds; plan: 171 runs, p50 1 second, p95 811 seconds. Both contain many skips. | Log eligibility and phase-transition IDs; do not interpret their skip-dominated medians as work time. |
| **Implement compute/retry** | Runs `37618246580` and `37620975934` failed at implementation with 22 token-count matches each. | Log internal attempt outcomes and changed-file state; distinguish internal attempts from GitHub run retries. |
| **Review/autofix** | 102 runs, p50 524.5 seconds, p95 1,566.2 seconds; run `37615213934` took 2,506 seconds. | Attribute queue, reviewer passes, editor rounds, checkout, and merge/conflict time separately. |
| **Validation/CI** | CI: 11 runs, p50 1,063 seconds, p95 1,412.5 seconds; four fail at the guard-test step. | Capture failed test cases and step timing. No separate `validate` workflow family appears in the supplied window. |

The data does not link issue→PR→run transitions, so it cannot produce an end-to-end critical-path percentile or split review time into compute, retry, and merge/conflict overhead. Add a shared transition correlation key and the timing fields above before ranking those subcomponents.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** observed hosted-runner waits up to ~510 seconds (`37623918885`); review p95 1,566.2 seconds and CI p95 1,412.5 seconds. **Top failure modes:** five triage failures, four CI guard-test failures, two review-checkout failures, two implement failures. **Highest measured costs:** review’s 51,269,846 OpenRouter total tokens and implementation’s 18,530,206 reported Codex tokens; these are different usage measures and should not be added as a dollar estimate.
- **Prioritized actions:** **(1)** restore full failed/slow-run logs and emit reason-coded step timing; **(2)** measure optional dispatches and queue time before suppressing verified redundant work, preserving required checks; **(3)** attribute review model/cache usage and implement attempt outcomes before changing reasoning or retries. These actions preserve existing safety gates while targeting the largest observed losses.

## Metrics Appendix

*Scope:* supplied GitHub Actions API analysis for one repository, with `insufficient_data=false`. The specified `/home/runner/work/_temp/workflow-log-output/summary.json` and its `errors/`, `slow/`, and `recent/` directories were absent here. Run-specific claims therefore use the supplied rows and `log_summary` fields; failure causes, per-target MCP lines, and API hotspots could not be checked against full logs. `sampled_success_runs=0` is also inconsistent with success rows carrying cost telemetry; audit that collector field. Durations below are **run** durations, not step durations.

| Repository / family | Runs | Success | Failure | Other, predominantly skipped | Success / failure of all runs | p50 / p95 run duration |
|---|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows`, all | 1,000 | 224 | 13 | 763 | 22.4% / 1.3% | 2 / 856 s |
| `review_autofix` | 102 | 100 | 2 | 0 | 98.0% / 2.0% | 524.5 / 1,566.2 s |
| `implement` | 173 | 12 | 2 | 159 | 6.9% / 1.2% | 1 / 1,446.6 s |
| `ci` | 11 | 7 | 4 | 0 | 63.6% / 36.4% | 1,063 / 1,412.5 s |
| `check_failure_triage` | 13 | 0 | 5 | 8 | 0% / 38.5% | 9 / 444.2 s |
| `clarify` / `plan` | 190 / 171 | 15 / 15 | 0 / 0 | 175 / 156 | — | 1 / 432.2 s; 1 / 811 s |
| `orchestrate_clarify_respond` / `orchestrate_poll` | 172 / 5 | 12 / 5 | 0 / 0 | 160 / 0 | — | 1 / 681.35 s; 315 / 339 s |

Across **concluded success/failure** runs only, success is 224/237 (94.5%) and failure is 13/237 (5.5%); the all-run rates above include skips in the denominator.

| Parsed-cost and review metric | Supplied value | Coverage qualification |
|---|---:|---|
| Runs with log telemetry | 121/1,000 (12.1%) | Full-log archive unavailable here |
| Codex tokens / count matches | 18,536,284 / 167 | Implementation: 18,530,206 / 164 |
| OpenRouter total / prompt / completion tokens | 51,269,846 / 7,539,574 / 331,644 | 76 calls; 58 usage-available, 18 unavailable |
| OpenRouter cache read / creation tokens | 40,807,016 / 2,592,092 | Aggregate `cache_hit_rate=null`; run `37619963052`: 71.0896% |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 2,000 / 2,425,250 ms | 120 sampled run durations; p50 is skip-dominated |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 | Parsed coverage only |
| GH API calls / rate-limit events / inner retries | **Not supplied** | `GH_PAT_BUDGET` values not present in the assembled metrics |

| MCP metric | Supplied value | Interpretation |
|---|---:|---|
| Semble queries / logged bytes | 56 / 401,084 | Implement 46 / 303,060; review 10 / 98,024; query targets unavailable |
| Semble fallbacks | 24 | All 24 CI `contract-test`; **0 logged runtime** fallbacks |
| Serena query rollups / underlying tool calls / response bytes / query ms | 0 / 0 / 0 / 0 | Per-tool breakdown: none observed |
| Serena fallbacks; probes ok / failed / skipped | 0; 0 / 0 / 0 | No availability conclusion without probes |

| MCP availability target evidenced in supplied data | `probe_ok` | `probe_failed` | `probe_skipped` | Other availability signal |
|---|---:|---:|---:|---|
| Semble, `orchestrate_poll` run `37624857038` | Not emitted | Not emitted | Not emitted | Enabled; binary and index reported unavailable |
| Semble, CI contract-test targets not supplied | Not emitted | Not emitted | Not emitted | 24 simulated fallbacks across four failed CI runs |
| Serena, target not observed | 0 | 0 | 0 | No query, fallback, or probe lines reported |

**Other MCP servers observed:** none in the supplied aggregates or summaries; raw logs are required to verify that none were missed.

## Deep Audit — Workflows & Scripts (2026-10-07)

### Section 1: Bug & Correctness Sweep

Read-only sweep: 54 workflows, 106 shell scripts, and 74 Python scripts. All shell scripts passed `bash -n`. Local Python 3.11 parsed 73 Python scripts; `workflow_retro.py` uses f-string syntax supported by the Python 3.12 version installed by its workflow (`.github/workflows/workflow-log-analysis.yml:244-247`). No files were changed.

- **BUG-001** — **File path and line range:** `scripts/check_failure_triage.sh:305-316`. **Severity:** High. **Category tag:** `bug`. **Description:** A failed or incomplete open-triage-issue listing is converted to `[]`. The script then treats deduplication as successful and can proceed toward creating another issue with the same fingerprint. **Recommended fix:** Distinguish a confirmed empty listing from a fetch or parse failure; defer issue creation and log a reason-coded dedup failure when the listing is unconfirmed.

- **BUG-002** — **File path and line range:** `.github/workflows/check_failure_triage.yml:476-479`; also `scripts/check_failure_triage.sh:664-671` and `scripts/gh_helpers.sh:558-605`. **Severity:** High. **Category tag:** `bug`. **Description:** Both triage issue-creation paths pass the non-idempotent `gh issue create` operation through `gh_retry`. If GitHub accepts a creation but its response is lost, an internal retry can create a second issue; the job’s concurrency group does not serialize attempts *within* that call. **Recommended fix:** Attempt creation once, then reconcile an uncertain outcome by searching for the existing fingerprint marker before any further create attempt. Keep the separate posting token.

- **SEC-001** — **File path and line range:** `scripts/gh_helpers.sh:548-605,628-674,733-775`; example caller `.github/workflows/plan.yml:1473-1486`. **Severity:** High. **Category tag:** `security`. **Description:** Failure diagnostics print `$*`, and some paths print raw response or stderr. A caller passes a generated issue-comment body as an argument, so a failed request can copy that body into job logs. This establishes a content-exposure path; it does **not** establish that a credential was exposed. **Recommended fix:** Log an operation name, endpoint template, response class, and bounded byte counts instead of argv, bodies, or raw responses. Retain stderr internally for retry classification but sanitize any emitted diagnostic.

- **SEC-002** — **File path and line range:** `.github/workflows/test-and-mark-stable.yml:164-180`; also `.github/workflows/mark-stable.yml:44-49` and `.github/workflows/workflow-log-analysis.yml:1199-1205`. **Severity:** Medium. **Category tag:** `security`. **Description:** `github.ref_name` is substituted directly into double-quoted shell source, before the release steps validate the ref. **Inference:** if a dispatchable ref can contain shell command-substitution syntax, Bash will evaluate it during assignment; the applicable ref-naming and dispatch permissions were not verified here. **[NEEDS VERIFICATION]** **Recommended fix:** Put `${{ github.ref_name }}` in step `env:` and assign or validate the resulting environment variable in Bash; apply this to all three sites.

- **BUG-003** — **File path and line range:** `scripts/review_collect_pr_metadata.sh:256-284`; related skip path `.github/workflows/review_autofix.yml:2304-2316`. **Severity:** Medium. **Category tag:** `bug`. **Description:** The linked-issue query requests `closingIssuesReferences(first:50)` without `pageInfo`, so a PR closing more than 50 issues is silently truncated. Its `--jq ... // []` projection also makes a successful response lacking the expected connection indistinguishable from a confirmed empty connection. Whether `gh` returns success for the relevant partial-error response needs verification. **[NEEDS VERIFICATION]** **Recommended fix:** Inspect the unprojected GraphQL response for errors and connection shape, request `pageInfo`, paginate when necessary, and export `LINKED_ISSUES_JSON=[]` only after confirming completeness. Apply the same completeness rule to the related skip-path query.

- **SHELL-001** — **File path and line range:** `scripts/workflow_retro_fanout.sh:153-175,207-210,333-349`. **Severity:** Medium. **Category tag:** `shellcheck`. **Description:** `run_consumer_retro` is called under `if !`, which suppresses `errexit` inside the function—the script explicitly notes this. The prompt-building group and sanitizer invocation lack explicit failure checks, so a failed read or write can be followed by a model attempt with an incomplete prompt. **Recommended fix:** Add explicit `|| return 1` checks to prompt assembly and sanitizer failure paths; retain the per-repository fail-open loop.

### Section 2: GitHub API Call Redundancy Audit

Counts below are **code-path estimates per execution**, not observed API telemetry; pagination, retries, and unrelated writes are excluded unless stated.

- **API-001** — **File path and line range:** `scripts/gh_helpers.sh:727-779`; contrasting classifier `scripts/gh_helpers.sh:165-185,575-581`. **Severity:** Medium. **Category tag:** `api-redundancy`. **Description:** `gh_api_json_to_file` retries failed `gh api` requests without applying the permanent-failure classifier used by `gh_retry`. A deterministic 404 or 422 can consume the default five attempts and backoffs. **Current → proposed calls:** up to **5 → 1** for a classified permanent failure. **Recommended fix:** Apply the existing `_is_gh_permanent_failure` check before backoff in `gh_api_json_to_file`; retain transient retries and its JSON-validation behavior. No batching helper is needed.

- **API-002** — **File path and line range:** `scripts/review_merge_train.sh:290-296,360-376,444-486`. **Severity:** Low. **Category tag:** `api-redundancy`. **Description:** `_mt_find_marker_comment` lists comments but returns only ID and timestamp; `_mt_upsert_comment` then fetches the selected comment again solely to compare its body. Reusing the listed body could remove a read, provided the second read is not an intentional freshness check against concurrent edits. **[NEEDS VERIFICATION]** **Current → proposed calls:** **2 → 1** for an upsert finding an existing marker, excluding a subsequent PATCH. **Recommended fix:** Extend the cycle-local result of `_mt_find_marker_comment` with the selected body; retain the fresh GET on branches where concurrency tests show it is required. This extends the merge train’s existing per-run cache pattern.

- **API-003** — **File path and line range:** `scripts/review_collect_pr_metadata.sh:209-226,251-269`; existing helper `scripts/gh_helpers.sh:926-1064`. **Severity:** Medium. **Category tag:** `api-redundancy`. **Description:** A normal PR metadata pass makes separate PR, issue-comment, review-comment, and closing-issue reads; break-glass adds a top-level-reviews read. The existing `gh_pr_with_all_comments` GraphQL helper overlaps those reads, but its normalized output does not yet preserve the raw artifact contracts used by review callers. **[NEEDS VERIFICATION]** **Current → proposed calls:** **4 → 2** metadata calls normally, **5 → 2** with break-glass, excluding the separate `gh pr diff` and extra pages; retain **4/5** as fallback on incomplete GraphQL data. **Recommended fix:** Extend the helper’s query with closing references and required review fields, transform into the existing artifact shapes, and parity-test raw IDs, timestamps, review comments, and pagination before switching callers.

- **BATCH-001** — **File path and line range:** `scripts/orchestrate_poll_process.sh:6732-6768`; existing batch pattern `scripts/orchestrate_poll_process.sh:3714-3781`. **Severity:** Medium. **Category tag:** `api-batching`. **Description:** The advisory-follow-up loop reads state and labels with one REST GET per unchecked issue; blocked issues add a paginated comments GET. The state/label portion is batchable, but partial label pages must still miss the cache. **[NEEDS VERIFICATION]** **Current → proposed calls:** for **N** issues, **B** blocked, **N + B → ceil(N/25) + B** reads on complete batched responses; comment POSTs are unchanged. **Recommended fix:** Extend the `_fetch_issue_labels_batch_graphql` alias pattern to return state and complete labels, then fall back to the existing REST GET only for missing or incomplete entries.

- **BATCH-002** — **File path and line range:** `.github/workflows/review_autofix.yml:2304-2328`. **Severity:** Medium. **Category tag:** `api-batching`. **Description:** After one closing-issue query, the deterministic-skip step makes one label POST per linked issue. Aliased GraphQL mutations are a candidate, subject to partial-success handling and availability of the label’s node ID. **[NEEDS VERIFICATION]** **Current → proposed calls:** **1 + N → 1 + ceil(N/25)** for **N** linked issues on a complete path, excluding label creation; retain per-issue fallback for uncertain results. **Recommended fix:** Extend the closing-issue query to return issue node IDs, resolve the ensured label ID, and batch `addLabelsToLabelable` mutations using the alias construction pattern in `_fetch_candidate_issue_details_graphql`.

- **BATCH-003** — **File path and line range:** `scripts/workflow_retro_fanout.sh:222-227,333-350`. **Severity:** Low. **Category tag:** `api-batching`. **Description:** Each active consumer retro separately lists up to 50 tracker candidates. Cross-repository GraphQL aliases could prefetch the same bounded selection fields; variable opt-out reads remain separate. Field and ordering parity require verification. **[NEEDS VERIFICATION]** **Current → proposed calls:** **R → ceil(R/10)** candidate-list reads for **R** active repositories, excluding the unchanged **R** variable reads and comment operations. **Recommended fix:** Add a cycle-local aliased tracker prefetch in `workflow_retro_fanout.sh`, following `_fetch_candidate_issue_details_graphql`; use the existing `gh issue list` for each missing or incomplete repository result.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — **File path and line range:** `.github/workflows/mark-stable.yml:691-839,846-901`; `.github/workflows/test-and-mark-stable.yml:6148-6296,6303-6358`. **Severity:** Medium. **Category tag:** `duplication`. **Description:** Both release workflows repeat the tag-publication step and the release lookup/create/reconciliation step. The existing `scripts/mark-stable.sh:1-111` has different manual-path semantics and cannot replace their tested-head guard unchanged. **Recommended fix:** Put the workflow-specific behavior in `scripts/release_publication_helpers.sh` as `publish_release_tags <version> <source_branch> <tested_sha>` and `ensure_github_release <version> <source_branch> <notes_file>`; update both workflow callers while retaining their step names, gates, and exact output behavior.

- **DUP-002** — **File path and line range:** `scripts/label_helpers.sh:145-187`, `scripts/validate_process.sh:1161-1195`, and `scripts/orchestrate_poll_process.sh:3373-3428`. **Severity:** Low. **Category tag:** `duplication`. **Description:** Three `ensure_label_exists` implementations repeat catalog lookup, create, and already-exists handling, but differ in cache and failure semantics. A blanket replacement would change those semantics. **Recommended fix:** Extend `scripts/label_helpers.sh` with backward-compatible optional contract-file and failure-policy arguments to `ensure_label_exists <label> [repo] [contract_file] [failure_policy]`; retain thin poller and validation wrappers for caching and notification, and test each existing failure mode.

- **DUP-003** — **File path and line range:** `.github/workflows/workflow-log-analysis.yml:945-1038,1585-1661,2091-2167`. **Severity:** Medium. **Category tag:** `duplication`. **Description:** Analysis, deep-audit, and API-redundancy passes each construct nearly identical `AI_PHASE_FAILURE_V1` payloads, comments, events, and failure labels, differing principally in step name and heading. **Recommended fix:** Add `scripts/workflow_log_failure_helpers.sh::report_log_analysis_failure <failed_step> <failure_mode> <attempts> <summary> <tracking_issue>`; source it in all three checked-out jobs and preserve each step-specific heading and existing marker fields.

### Section 4: Expression Size Limit Risk Assessment

Measured decoded, de-indented static `run:` bodies containing `${{ }}` across all 54 workflows: **234 interpolated blocks**. Runtime substitution values can change final lengths. Non-interpolated blocks were excluded. The largest measured `if:` predicate is approximately **935 characters**, not near 21,000.

- **EXPR-001** — **File path and line range:** `.github/workflows/implement.yml:1003-1392`. **Severity:** High. **Category tag:** `expression-limit`. **Description:** The interpolated “Stage workflow support files” body is approximately **19,132 characters**, leaving **1,868** before the specified 21,000-character limit, before substitution. **Recommended fix:** Extract the body to a trusted-support script under `scripts/`; have this pre-staging step resolve it from the already checked-out verified support source, preserve its main-snapshot fallback, and pass GitHub expression values through step `env:`.

- **EXPR-002** — **File path and line range:** `.github/workflows/implement.yml:3509-3828`. **Severity:** Medium. **Category tag:** `expression-limit`. **Description:** The interpolated preflight destructive-commit guard is approximately **15,517 characters**, leaving **5,483** before the specified limit, before substitution. **Recommended fix:** Extract the guard to a script staged by the preceding support-files step; keep its `id`, `if`, and step-level guard-variable overrides in YAML, with expression inputs supplied through `env:`.

**File-size check:** No workflow exceeds the requested **800 KB** flag threshold. This repository also enforces a tighter **480,000-byte** CI guard and documents a **512,000-byte** runtime limit (`tests/test_workflow_file_size_limit.py:24-27`; `CLAUDE.md:1613-1641`). The largest workflow is `review_autofix.yml` at **472,478 bytes**; its guard headroom is addressed in DEBT-001. No additional inline heredoc or prompt-bearing interpolated block reached the 15,000-character threshold.

### Section 5: Cross-Cutting Concerns

- **CONSIST-001** — **File path and line range:** `scripts/unblock_judge.sh:83-91,466-476,815-823`; caller `.github/workflows/unblock_judge.yml:140-164`. **Severity:** Low. **Category tag:** `consistency`. **Description:** The judge runs from verified support but makes raw `gh api` reads for comments, identity, item metadata, and cited runs. A transient read failure skips or omits evidence, unlike poller paths using `gh_retry` from that same support helper family. **Recommended fix:** Source the verified `scripts/gh_helpers.sh` and use `gh_retry` for read-only calls, preserving each existing fail-closed skip and pagination check.

- **SHELL-002** — **File path and line range:** `.github/workflows/test-and-mark-stable.yml:754,1046,1346,2212,3552`. **Severity:** Low. **Category tag:** `shellcheck`. **Description:** Five `[ $IDLE -ge $INACTIVITY_LIMIT ]` tests leave operands unquoted. Their current arithmetic producers constrain practical exposure, but the form remains vulnerable to splitting or test-argument errors if a producer changes. **Recommended fix:** Use `(( IDLE >= INACTIVITY_LIMIT ))` after numeric validation, or quote both operands consistently.

- **DEBT-001** — **File path and line range:** `.github/workflows/review_autofix.yml:6960-7099`; guard `tests/test_workflow_file_size_limit.py:39-53`. **Severity:** Medium. **Category tag:** `tech-debt`. **Description:** At **472,478 bytes**, the workflow has only **7,522 bytes** before its repository-enforced guard. The cited inline warning step is one extraction candidate; moving it alone would not reach the documented 50,000-byte headroom goal. **Recommended fix:** Extract enough eligible inline steps to remove at least approximately 43 KB, starting with the cited block, using the verified `review_autofix_step_*.sh` staging, fallback, and test-registry pattern in `agents.md:985-1033`. Preserve failure-path behavior when support staging is unavailable.

No actionable `TODO`/`FIXME`/`HACK` markers were found in the requested files. The never-run `claude-fixer-auto-merge` job is explicitly retained as a compatibility check (`.github/workflows/review_autofix.yml:2355-2365`), so it is not a removal finding. `shellcheck` and `actionlint` binaries were unavailable locally; CI’s script ShellCheck gate and its disabled inline-run ShellCheck integration are recorded at `.github/workflows/ci.yml:254-276,365-370`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 4 | BUG-001, BUG-002, SEC-001, EXPR-001 |
| Medium | 11 | SEC-002, BUG-003, SHELL-001, API-001, API-003, BATCH-001, BATCH-002, DUP-001, DUP-003, EXPR-002, DEBT-001 |
| Low | 5 | API-002, BATCH-003, DUP-002, CONSIST-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

Projected distinct primary files or new modules—not files changed by this audit.

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | ≈3 | Medium |
| API call optimization | ≈6 | Large |
| Code modularization | ≈8 | Large |
| Expression size reduction | ≈3 | Medium |
| Medium/Low fixes | ≈7 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-10-07)

### Safety Tag Legend

`SAFE_TO_MERGE` is authorized for direct implementation; `NEEDS_VERIFICATION` requires the stated checks; `RISKY_SKIP` must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

No findings.

### Redundant Re-Fetch (REUSE-###)

- **REUSE-001 — `RISKY_SKIP`**
  - **Calls and endpoint:** `scripts/orchestrate_poll_process.sh:3256-3272` (`unblock_trusted_login`) and `scripts/orchestrate_poll_process.sh:26015-26030` (noop-cap identity check) both read `GET /user` for the authenticated login.
  - **Current → proposed call count:** **2 → 1**, only on a tick where the first read succeeded before the noop-cap check; otherwise retain the existing call.
  - **Evidence:** `UNBLOCK_TRUSTED_LOGIN_STATE="ok"` records a validated login, while the noop-cap path independently fetches `.login` and lowercases it.
  - **Proposed fix:** Have the noop-cap path use a lowercased `UNBLOCK_TRUSTED_LOGIN` when its cache state is `ok`; retain its current `/user` lookup when the cache is unset or failed.
  - **Safety rationale:** Both calls serve identity verification, an explicit `RISKY_SKIP` trigger; a cached failure must not suppress the later probe or change its dispatch decision.
  - **Downstream signal:** Do not auto-implement. Manually verify token scope throughout the tick, call ordering, transient-failure recovery, and the noop-cap skip and warning outcomes before changing either probe.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: `RISKY_SKIP` — The proposed classifier change is inside a retry and rate-limit-aware path.
- API-002: `RISKY_SKIP` — Marker discovery is paginated; replacing the fresh body GET also needs concurrency review.
- API-003: `RISKY_SKIP` — The proposed replacement spans paginated comment and review reads with distinct artifact contracts.
- BATCH-001: `RISKY_SKIP` — The calls sit in the poller’s merge-follow-up path, which defends against races; its comment read is paginated.
- BATCH-002: `NEEDS_VERIFICATION` — Batched writes need partial-success and label-node-ID checks before replacing per-issue POSTs.
- BATCH-003: `NEEDS_VERIFICATION` — Cross-repository selection must preserve the bounded list’s fields, ordering, and fallback behavior.

### Summary Counts

*Net-new findings only; Deep Audit cross-references are excluded.*

| Tag | Count | IDs |
|---|---:|---|
| `SAFE_TO_MERGE` | 0 | — |
| `NEEDS_VERIFICATION` | 0 | — |
| `RISKY_SKIP` | 1 | REUSE-001 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
