## Executive Summary

- **Fix the cost collector before using its totals for decisions.** In `shubhodeep1/coding-workflows`, the reported 6,614,747 Codex tokens include matches against echoed shell examples; the only directly visible Codex usage line in these logs is 13,290 tokens in run `38005498136`. Combined-job and per-step logs also duplicate four review runs’ usage records: 121 reported OpenRouter calls reduce to 68 distinct calls. **Impact:** prevents materially misleading spend and cache analysis. **Confidence: high.**
- **Stop no-edit implementation work before invoking Codex.** Runs `37998830303` and `38000054481` ended in deliberate `BLOCKED` verdicts because the approved work required no change, after 1,813 and 1,837 seconds respectively. Add a guarded plan/head-state preflight, without automatically closing issues. **Potential saving:** about 30 minutes per recurrence. **Confidence: high on cause; medium on future savings.**
- **Review is the principal active-run latency bottleneck.** Review/autofix has a 1,450-second active-run median; `Run reviewer models` took approximately 1,240 of 2,214 seconds in run `38007443357`. Mistral’s upstream rate limit triggered repeated reviewer-slot attempts in that run and runs `38006063177` and `38005890746`. Make retry decisions reason-aware while preserving vote requirements. **Potential saving:** failed slot attempts; end-to-end savings are unmeasured because slots run concurrently. **Confidence: high.**
- **The five failures have identifiable, different paths.** Three are implementation failures—two deliberate blocks and one no-action exploration loop—and two are CI test failures (`38001382784`, `38007443083`). Fix their specific contracts rather than attributing CI failures to Semble fallback lines emitted by tests. **Impact:** addresses all five observed failures; future rate reduction unknown. **Confidence: high.**
- **API and orchestration diagnosis needs better emission.** Review run `38007443357` reports `GH_PAT_BUDGET used_in_job=unknown`; post-merge run `38010444206` twice failed a memory force-tick write and dispatched without a persisted cooldown claim. Add endpoint-count and dispatch-claim outcome logs before changing API or poll behavior. **Impact:** exposes call hotspots and duplicate-dispatch risk; neither is currently quantified. **Confidence: high.**

## Speed Optimizations

1. **Critical path—preflight no-edit plans.** The `Run Codex implementation` step in `37998830303` and `38000054481` reached a deliberate `BLOCKED` verdict on attempt 2: respectively, a verification-only/superseded plan and a fix already on `main`. **Root cause:** implementation was entered before checking whether the approved plan still required edits. **Change:** before agent startup, compare the approved plan’s edit requirement with current head and task state; route an unambiguous no-edit result to an explained terminal decision, retaining the existing path when uncertain. **Estimated saving:** up to the observed 1,813–1,837 seconds per comparable run. **Risk:** low if ambiguity fails open to the current implementation path.

2. **Critical path—classify reviewer retries by failure reason.** In `38007443357`, `Run reviewer models` lasted about 1,240 seconds; its Mistral slot was rate-limited on two attempts in each of two review passes. The same two-attempt pattern appears in `38006063177` and `38005890746`. **Root cause:** cheaper reasoning retries cannot resolve an upstream rate limit. **Change:** after a classified upstream limit, use a bounded, slot-local circuit breaker and record an unavailable vote; do not silently lower quorum or invent a reviewer result. Log whether the failed slot was the pass’s latency tail. **Estimated saving:** roughly one failed, approximately 85-second slot attempt per affected pass; **end-to-end saving could be zero** where another concurrent slot determines completion. **Risk:** medium; preserve existing review gates.

3. **Critical-path diagnosis—separate execution from startup and waiting.** Successful implementation `38005498136` spent about 1,200 of 1,942 seconds in `Run Codex implementation`. Conversely, recent cancel run `38010444185` lasted 78 seconds while its `cancel-active-runs` job log spans about 6 seconds; the supplied summary identifies roughly 72 seconds before the next step. **Change:** emit job-allocated, first-step-started, agent-started, and agent-finished timestamps. **Estimated saving:** none directly; enables targeting the otherwise unattributed roughly 72 seconds. **Risk:** low.

4. **Micro-optimization—retain lazy Semble startup.** Five distinct review-context queries each used a successful lazy bootstrap; none was logged unused. Bootstrap cost totaled 186,743 ms, about 37 seconds each, versus approximately 0.4–0.7 seconds for each query. **Change:** log bootstrap-to-first-use and reuse across review passes before attempting optimization; do not make startup eager. **Estimated saving:** unproven. **Risk:** low for logging.

## Cost Optimizations

1. **Correct measured spend first.** `scripts/cost_audit.py` matches “tokens used” inside echoed shell examples in implementation logs; the successful run `38005498136` also prints an actual `tokens used` / `13,290` pair that the current match does not correctly capture. Four review runs contain the same structured usage in both combined-job and per-step logs. **Change:** parse emitted, timestamp-normalized usage lines rather than echoed commands; identify an event by run, attempt, job, step, and event identity before aggregation. Add fixtures reproducing these logs. **Savings:** *no direct token saving*; avoids decisions based on 6,614,747 spurious Codex tokens and duplicated review usage. **Quality risk:** none.

2. **Avoid unnecessary agent runs, not necessary reviews.** The two deliberate blocks consumed about 30 minutes each; failed-run Codex usage is **not reliably measurable** from these archives. Apply the guarded preflight above. **Savings:** two observed unnecessary executions if caught before dispatch; dollars and tokens cannot be estimated. **Quality risk:** require positive evidence of no needed edit.

3. **Reduce unsuccessful model work selectively.** Distinct OpenRouter usage records include 16 Mistral calls with unavailable usage and one unavailable Minimax failback call; reviewer logs classify repeated Mistral failures as upstream `rate_limit`. Run `38007443357` configured `xhigh` reviewer reasoning and retried at cheaper reasoning without clearing that limit. **Change:** record per-attempt provider outcome and available usage, then test a rate-limit-specific skip/failback policy without changing required vote counts. **Savings:** failed attempts may be avoided, but billed-token and dollar savings are unknown. **Quality risk:** medium if a mandatory reviewer is omitted.

4. **Bound context using observed pressure.** Run `38007443357` emitted a review `CONTEXT_BUDGET_WARN` at 174,622 prompt tokens against a 200,000-token window (ratio 0.8731). Five distinct Semble reviewer-context queries returned 69,826 logged bytes and 34 sources; their 834 `static_dup_bytes` do not establish a counterfactual prompt saving. **Change:** log prompt bytes by static prefix, diff, retrieved memory, and Semble contribution; trim repeated *dynamic* material only after comparing review outcomes. **Savings:** not estimable from byte counts alone. **Quality risk:** preserve files needed for review.

**Serena:** zero queries, response bytes, and tool-call replacements were recorded; recent review run `38010448435` reports it disabled. There is no evidence to claim either Serena savings or low-value Serena response noise.

## Reliability Improvements

1. **Implementation decision handling:** `37998830303` and `38000054481` are deliberate `BLOCKED` verdicts, while `37998736315` aborted after two consecutive attempts with no actionable output. All three currently appear as `Run Codex implementation` failures, and post-Codex validation diagnostics were absent. **Category:** task-state/preflight mismatch for two; agent-progress stall for one. **Fix:** emit a structured final disposition—`blocked_no_edit`, `no_actionable_output`, or validation failure—with attempt count, changed-file count, and diagnostics-present flag; preflight only the unambiguous no-edit cases. **Expected impact:** removes the two observed avoidable implementation failures if caught early; preserves the existing abort and fail-open path for uncertain cases. `BREAK_GLASS` was recorded **zero** times, so it does not explain these failures.

2. **CI contracts:** `38001382784` fails an alert-level test because `orchestrate_poll.yml` reads `vars.ALERT_MSG_LEVEL` rather than checking the smoke environment override first. **Fix:** restore env-first precedence and run that contract before full CI. `38007443083` fails `test_security_pass_exhaustion_judge_accepts_all_findings_and_passes` in `orchestrate-poll (2)`; its failure output has no explanatory assertion. **Fix:** rerun that isolated test and emit expected/actual state and command exit status before changing production behavior. **Expected impact:** resolves the identified CI regressions once their assertions pass; no blanket retry or gate bypass. **Rollback:** revert the narrow contract fix if it changes non-smoke behavior.

3. **Memory dispatch claim:** run `38010444206`, `Force orchestrate poll after merged PR handling`, emitted two `force-tick-put` failures with `fail_open: true` and warned that it was dispatching without a persisted cooldown claim. **Category:** persistence/claim failure; duplicate dispatch is a risk, **not an observed outcome**. **Fix:** log a bounded reason and attempt count plus a non-sensitive dispatch identity; retain fail-open dispatch and use bounded retries for the claim. **Expected impact:** makes repeated unclaimed dispatch diagnosable and may reduce it. **Rollback:** retain the current fail-open route.

4. **MCP availability, not CI causation:** implementation runs `37998736315` and `38000054481` logged three `SEMBLE_FALLBACK target=overflow reason=binary-unavailable` events combined. The two failing CI runs logged eight **distinct** `target=overflow` fallbacks from intentional missing-binary contract fixtures; neither CI failure was caused by those fixtures. **Fix:** on first real overflow use, emit one capability result and fallback reason per target/run, retain the existing local-context fallback, and alert only on repeated *runtime* absence. **Expected impact:** distinguishes a broken rollout from healthy fail-open tests; no justified failure-rate estimate. No Serena probe failures—or Serena probes—were observed.

The one distinct review context-budget warning indicates **prompt-size risk**, not a proven rubric or policy bypass; the collector reports it twice because the same line appears in two log representations.

## AI Memory Health

Across the deep-dive logs, **12 distinct `retrieve` operations all selected records**: 100% sampled hit rate, average estimated 1,447 tokens versus a 1,467-token average budget, and keyword methods **8 `llm`, 4 `plain`, 0 `none`**. Review run `38007443357` selected 25 records at an estimated 1,396/1,400 tokens; implementation run `38005498136` selected 31 at 1,600/1,600. No sampled retrieve returned zero records or reported `enabled: false`. **Recommendation:** retain retrieval, but log records considered versus selected and truncation at budget; the near-full budgets leave little room to detect growth. Expected impact is earlier prompt-pressure detection, not a measured saving.

The sample also includes `record-candidate`, `record-run-event`, `finalize-task`, and processed-command claim/complete emissions. Seven distinct operation records reported `push_attempts: 2`; none in this sample reported more. The two timestamp-distinct `force-tick-put` fail-open events in `38010444206` warrant the dispatch-claim diagnosis above. Retrieval results are sampled deep dives, **not a pipeline-wide hit rate**.

## GH API Call Audit

- **Measured hotspot is unavailable.** In review `38007443357`, the `codex-agent` and `gate` `GH_PAT_BUDGET` records show `remaining=5000` at start and end but `used_in_job=unknown`. That does **not** establish zero calls. No GitHub API rate-limit event or endpoint-level call count can be substantiated from these runs; the reviewer `rate_limit` events are **model-provider**, not GitHub, events. **Change:** summarize `GH_API_CALL` counts by workflow/job/step, endpoint *template*, response class, retry reason, cache hit, and elapsed time—without URLs containing issue data or response bodies. **Expected impact:** measurable call budgets and rate-limit diagnosis; numerical reduction unknown.
- **Conditional high-volume code path:** `scripts/review_merge_train.sh` documents and implements two sweeps of five active-run statuses, normally **10 list requests per invocation**; it already reuses a completed listing within a release evaluation. This is a code-path bound, **not a measured count for a supplied run**. Instrument invocations and snapshot shifts first. Only if tests show the second sweep can safely reuse a sufficiently fresh snapshot would one sweep reduce **10 to 5 calls per invocation**; do not remove its race protection on the present evidence.
- **Conditional repeated lookup:** `scripts/pr_checks_lib.sh` documents one branch-protection read per required-check resolution. If instrumentation finds repeated resolutions of the same base within one job, use a cycle-local result with safe miss fallback—**up to \(k-1\) calls for \(k\) repeated lookups**, with no observed \(k\) here.

These changes follow this repository’s `CLAUDE.md §15`: check existing calls first, reuse cycle-local data, batch per-item reads where appropriate, and fail open on prefetch misses. No unbatched per-item API hotspot is proven by this telemetry; collect endpoint counts before claiming one.

## Prompt Cache & Memory System

The aggregate `cache_hit_rate` is **unavailable**, because 17 of 68 distinct OpenRouter call records have unavailable usage. Run `38007057656` is the one measured exception: **0.905401** across its usage-complete review calls. Across distinct records, 52,630,896 cache-read and 1,871,231 cache-write tokens are logged; their read share is **not** a pipeline-wide cache-hit rate. `google/gemini-3.1-flash-lite` reviewer-slot logs report `cache_status=unsupported`, while other sampled slots report supported; do not interpret the unsupported slot as prefix fragmentation.

Run `38007443357`’s 174,622-token warning makes prompt growth a concrete concern. The logs do **not** identify an unstable prefix or where dynamic noise crosses a cache breakpoint. **Change:** emit non-sensitive stable-prefix hash, dynamic-bytes-before-breakpoint, cache eligibility, and per-call creation/read/usage-availability fields; keep fixed instructions before changing PR, diff, and run-state context. Compare those fields and review outcomes before altering prompt assembly. **Expected impact:** makes token and latency improvements testable; magnitude presently unknown. Keep memory retrieval within its logged budget and preserve its fail-open behavior.

## Orchestrator Health

`orchestrate_poll` had **10 successes and 3 cancellations** in 13 runs; `38003666074` and `38001257450` were cancelled before their first poll step after 839 and 392 seconds of run lifetime. The supplied timestamps have no measurable created-to-run-start queue interval, so that interval cannot distinguish scheduling, job wait, or cancellation overhead. **Change:** emit upstream dispatch decision, job allocation, first-step start, cancellation reason, and last persisted wave state. **Expected impact:** identifies whether the cancellations waste capacity or represent intended supersession.

There are bounded stall signals, not a demonstrated project-wide deadlock: readiness run `38010448125` reported **3/7 sub-issues still unchecked** on tracking issue `#6902`; judge dispatch `38009871084` reported `scope-blocked` for `#6804`. Record per-project wave, deferred reason, judge decision, conflict-heal attempt, and terminal transition at each poll. That permits a stuck-state rate; current logs do not. The two failed force-tick claims in `38010444206` need the fail-open claim logging specified above.

## Pipeline Flow Bottlenecks

| Stage | Observed bottleneck | Smallest next action |
|---|---|---|
| Clarify → plan | Active medians **334s** (5 clarify) and **763.5s** (10 plan); many other dispatches skip. | Log upstream skip reason and plan edit requirement; do not treat skipped runs as fast completed work. |
| Implement | **1,659.5s** active median; two no-edit blocks and one exploration abort among 14 active runs. | Guard unambiguous no-edit plans; log agent disposition and changed-file count. |
| Review/autofix | **1,450s** median; `38007443357` spent ~1,240s in reviewer models. | Capture per-slot critical-path tail and apply reason-aware retries without weakening review. |
| Validate/CI | **815s** median; two different failing tests, runs `38001382784` and `38007443083`. | Run the narrow contracts early while preserving the full required CI gate. |
| Orchestrate/merge | Poll median **839s**; three cancellations, two before a step; one post-merge force-poll step took ~89s with two claim failures. | Log wait/compute/cancel boundaries, wave state, and claim outcome before tuning polling or merge behavior. |

The dominant measured work is **compute and internal reviewer retry**, not GitHub queue time. Merge/conflict overhead and API retry time cannot be apportioned from this window; add stage-span and endpoint-attempt summaries rather than assigning the residual duration to either.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** active implementation and review medians of 1,659.5s and 1,450s; CI and poll medians of 815s and 839s. **Top failure modes:** two deliberate no-edit implementation blocks, one no-action implementation abort, and two distinct CI test regressions. **Highest-cost drivers:** review-model work with 68 distinct OpenRouter call records and 64,790,346 reported tokens on usage-available calls; reported Codex totals are invalid.

**Prioritized actions:** (1) repair usage/event deduplication and Codex parsing; (2) preflight unambiguous no-edit implementation plans and preserve an explicit block decision; (3) instrument reviewer-slot tails, API endpoints, and post-merge claim failures before changing retry or poll policy.

## Metrics Appendix

**Window and coverage.** The folder’s `summary.json` covers **1,000 runs**. Its raw deep-dive telemetry covers **23 runs**; the assembled context reports **115 runs with log telemetry** after wider coverage, but reproduces the same cost totals and different wall-clock sample statistics. Treat these as **different coverage populations**, not interchangeable denominators. Seven recent skipped runs had empty log archives.

| Scope | Runs | Success | Failure | Cancelled | Skipped | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| Repository, all conclusions | 1,000 | 178 (17.8%) | 5 (0.5%) | 4 | 813 | 1s / 815s |
| Repository, non-skipped | 187 | 178 | 5 | 4 | 173s / 1,850.6s |
| Implement, non-skipped | 14 | 11 | 3 | 0 | — | 1,659.5s / 2,084.8s |
| Review/autofix, non-skipped | 39 | 38 | 0 | 1 | — | 1,450s / 2,215.9s |
| CI | 21 | 19 | 2 | 0 | — | 815s / 909s |
| Orchestrate poll | 13 | 10 | 0 | 3 | — | 839s / 1,269.4s |

**Token and review telemetry.** “Distinct” removes combined-job/per-step copies in the inspected logs. OpenRouter totals remain a **lower bound on actual usage** where usage is unavailable; they are not dollar costs.

| Metric | Collector-reported | Distinct / verified interpretation |
|---|---:|---:|
| OpenRouter calls; usage available/unavailable | 121; 91/30 | **68; 51/17** |
| OpenRouter prompt / completion / total tokens | 17,597,896 / 756,636 / 113,846,974 | **9,835,050 / 454,138 / 64,790,346** logged |
| OpenRouter cache read / write tokens | 92,242,683 / 3,251,395 | **52,630,896 / 1,871,231** logged |
| Codex calls / tokens | 57 / 6,614,747 | **Not reliably aggregatable**; one visible real usage result is **13,290** in `38005498136` |
| `cache_hit_rate` | Aggregate null | **0.905401** only in usage-complete run `38007057656` |
| `break_glass_count` | 0 | 0 observed |
| `context_budget_warn_count` | 2 | **1 distinct** warning, run `38007443357` |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | Folder: 1,813,000 / 2,436,580 (23 samples) | Assembled context: 1,000 / 2,339,470 (110 samples); populations differ |

| MCP target / event | Collector-reported | Distinct deep-dive events | Logged contribution or availability |
|---|---:|---:|---|
| Semble `reviewer-context` queries | 9; 125,047 bytes | **5; 69,826 bytes** | 34 sources; 834 static-duplicate bytes |
| Semble lazy bootstrap | 9; 335,217 ms | **5; 186,743 ms** | 0 failed, 0 unused |
| Semble `overflow` runtime fallbacks | 3 | **3** | `binary-unavailable`; implementation runs `37998736315`, `38000054481` |
| Semble `overflow` contract-test fallbacks | 12 | **8** | Intentional missing-binary fixtures in CI runs `38001382784`, `38007443083` |
| Serena queries / fallbacks / response bytes / tool calls | 0 / 0 / 0 / 0 | **0 / 0 / 0 / 0** | No per-tool breakdown exists because no query was observed |

| MCP target | `probe_ok` | `probe_failed` | `probe_skipped` | Interpretation |
|---|---:|---:|---:|---|
| Semble `reviewer-context` | 0 | 0 | 0 | Query and bootstrap success observed; no probe emission |
| Semble `overflow` | 0 | 0 | 0 | Runtime capability absent in two implementation runs; no probe emission |
| Serena—no target observed | 0 | 0 | 0 | Disabled in recent run `38010448435`; absence is not a failed probe |

**Other MCP servers observed:** none in emitted deep-dive telemetry. **GH API summary:** endpoint call counts, retries, and rate-limit events are not collected here; sampled review budget logs show `remaining=5000` and `used_in_job=unknown`. The merge-train helper’s normally ten status-list calls are a **documented conditional code path**, not a measured run total.

## Deep Audit — Workflows & Scripts (2026-10-10)

### Section 1: Bug & Correctness Sweep

Scope: 54 workflow files and 195 top-level scripts. Read-only `bash -n` passed for all 120 shell scripts. Local Python 3.11 could not parse `scripts/workflow_retro.py:793-794`; its CI and workflow entry points pin Python 3.12, so this is not classified as a workflow defect. Actionlint and a YAML parser were unavailable locally. The existing report already covers the cost-collector error, no-edit implementation runs, and two CI regressions; they are not repeated here.

- **BUG-001** — **High · `bug`** — `scripts/implement_diagnose_post_codex_failure.sh:801-841`. **Description:** Each fix-up issue is created through `gh_retry gh issue create`. A create is non-idempotent: if GitHub accepts a request but the CLI reports failure, the retry can create another issue while the local map records only the eventual returned URL. The repository explicitly avoids this pattern for security follow-ups at `scripts/security_audit.sh:2197-2206`. Duplicate creation is an inferred failure mode, not an observed event. [NEEDS VERIFICATION] **Recommended fix:** Create once with a stable local-ID marker in the body; on an ambiguous result, reconcile that marker against existing issues before any new attempt, following the security-audit one-shot pattern.

- **SEC-001** — **High · `security`** — `scripts/gh_helpers.sh:575-605`; caller `scripts/implement_diagnose_post_codex_failure.sh:802-826`. **Description:** On permanent or exhausted failures, `gh_retry` prints `$*`, including arguments such as the generated issue’s `--title` and `--body`; its final path also prints raw stderr. Failure logs can therefore expose supplied text and permit newline-bearing arguments to shape log output. **Recommended fix:** Log a bounded command class, endpoint template, attempt and response class—not argv or raw stderr. Apply `_gh_actions_escape` to any retained diagnostic text.

- **SEC-002** — **Medium · `security`** — `scripts/gh_helpers.sh:739-749`. **Description:** When a successful API command returns invalid JSON, `gh_api_json_to_file` prints the first 50 raw response lines. Callers use it for issue and run payloads, so a malformed response containing sensitive or untrusted text would enter Actions logs. That content-dependent exposure is an inference. [NEEDS VERIFICATION] **Recommended fix:** Retain the response in the temporary file for diagnosis but log only byte count, parse-error class and a non-sensitive endpoint template; do not print its body.

- **BUG-002** — **Medium · `bug`** — `.github/workflows/review_autofix.yml:5422-5441,5632-5652`; `scripts/label_helpers.sh:189-235`. **Description:** When the staged label helper is absent, the inline `set_issue_phase_label_resilient` fallbacks only POST the new label. The canonical helper first calculates a replacement set that removes other phase labels. Thus the fallback can leave contradictory phase labels on an issue. **Recommended fix:** Stage and verify `label_helpers.sh` before phase mutation; if it is unavailable, preserve existing labels and report the unavailable transition rather than substituting a POST-only implementation.

- **BUG-003** — **Medium · `bug`** — `scripts/orchestrate_poll_process.sh:121-131,16179-16235`. **Description:** These aliased GraphQL readers check command success or `.data.repository` shape but do not reject a nonempty top-level `.errors` array. A partial response can consequently be converted into a missing or empty cache entry and skip an issue-dependent decision. This requires a partial-success response from GitHub. [NEEDS VERIFICATION] **Recommended fix:** Validate `.errors` and required alias shapes before accepting a batch; fall back per missing issue or skip the affected decision explicitly, as `gh_pr_with_all_comments` does for GraphQL errors in `scripts/gh_helpers.sh:988-999`.

- **BUG-004** — **Low · `bug`** — `scripts/review_autofix_step_merge_topology_gate.sh:7-7,140-145`. **Description:** Under `set -euo pipefail`, the diagnostic `printf ... | head -20` is unguarded. With sufficiently large untracked-file output, `head` can close the pipe before `printf` finishes, turning a display limit into a pre-review gate failure. Whether current workloads reach that size is unverified. [NEEDS VERIFICATION] **Recommended fix:** Limit the displayed lines without an early-closing pipe, or explicitly handle the expected SIGPIPE status.

### Section 2: GitHub API Call Redundancy Audit

Counts below describe code paths, not measured run totals. The existing report already identifies the merge-train status sweeps and conditional branch-protection lookup; those are not duplicated.

- **API-001** — **Medium · `api-redundancy`** — `scripts/workflow_failure_heal_intake.sh:442-445,718-724`. **Description:** For a self-repository phase report with numeric `SOURCE_GEN` and no `PHASE_COMMENT_AUTHOR`, both paths read `GET /user`. **Current → proposed:** 2 → 1 identity reads on that path. **Recommended fix:** Reuse validated `PROVENANCE_LOGIN` for `HEAL_TRUSTED_AUTHOR`, retaining the existing read on a missing value. This extends the poller’s cycle-local cache pattern; no GraphQL batch is needed.

- **API-002** — **Medium · `api-redundancy`** — `scripts/label_helpers.sh:153-185`; caller `scripts/orchestrate_poll_process.sh:4829-4833`. **Description:** `ensure_label_exists` attempts `gh label create` on every invocation, including repeated uses of an already-known label during a cycle. The frequency of repeats in production is unmeasured. [NEEDS VERIFICATION] **Current → proposed:** *k* create requests → 1 per distinct repository/label per cycle, with a fresh attempt after an uncertain result. **Recommended fix:** Add a success-or-already-exists memo to `label_helpers.sh`, scoped like the poller’s other cycle-local caches; preserve a safe retry when a later mutation reports the label missing.

- **API-003** — **Medium · `api-redundancy`** — `scripts/gh_helpers.sh:727-779,795-844`. **Description:** Unlike `gh_retry`, `gh_api_json_to_file` and `curl_gh_api` have no permanent-error branch; they retry a deterministic 404 or 422 up to the default five attempts, including sleeps. **Current → proposed:** up to 5 → 1 calls per permanent failure. **Recommended fix:** Reuse `_is_gh_permanent_failure` from `scripts/gh_helpers.sh:165-185`, classify HTTP status in the curl path, and reserve backoff for transient failures. No batching pattern applies.

- **API-004** — **Low · `api-redundancy`** — `scripts/orchestrate_poll_process.sh:33-35,142-153,200-203`. **Description:** The sweep-only entry point calls both functions sequentially, and each independently reads `gh api rate_limit` to enforce the same 500-remaining threshold. **Current → proposed:** 2 → 1 snapshot reads per enabled sweep. **Recommended fix:** Obtain one bounded, cycle-local budget value before both functions, with the existing skip behavior if it is unavailable. No GraphQL batching applies.

- **BATCH-001** — **Medium · `api-batching`** — `scripts/orchestrate_poll_process.sh:33-51,93-129`. **Description:** Reclarify replay reads the issue and all comment pages inside its queued-issue loop, although the neighboring blocked-comment detector already demonstrates 25-issue GraphQL aliases. A replacement must preserve *complete* comment history for authorization. [NEEDS VERIFICATION] **Current → proposed:** at least `3 + 2N` reads for *N* queued issues—budget, queue, identity and two per issue—→ `3 + ceil(N/25)` on complete batched pages, plus per-issue REST fallbacks for incomplete histories. **Recommended fix:** Extend the pre-sweep `_fetch_blocked_issue_routing_graphql` pattern with replay-specific state, labels and comment fields; check comment pagination and retain the current REST authorization path on a miss.

- **BATCH-002** — **Medium · `api-batching`** — `scripts/orchestrate_poll_process.sh:4575-4587,4624-4675`. **Description:** The close-merged sweep fetches a timeline per issue and PR metadata per merged cross-reference. The issue list cannot establish whether a cross-reference is the issue’s implementation PR, but an aliased issue query could include the source PR’s body, head and merge fields. Field and pagination parity need verification. [NEEDS VERIFICATION] **Current → proposed:** ordinarily `2 + N + K` reads for *N* inspected issues and *K* candidate-PR reads → `2 + ceil(N/25)` batched reads, plus incomplete-page or failed-alias fallbacks; close writes remain unchanged. **Recommended fix:** Extend the `_fetch_candidate_issue_details_graphql` alias-building pattern with a purpose-specific complete cross-reference query. Preserve `_pr_json_is_issue_implementation_pr` semantics—do not substitute `willCloseTarget`—and retain the existing per-issue path on uncertain results.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — **Medium · `duplication`** — `.github/workflows/clarify.yml:75-143`, `.github/workflows/plan.yml:124-195`, `.github/workflows/implement.yml:454-535`, `.github/workflows/orchestrate_clarify_respond.yml:161-229`, `.github/workflows/validate.yml:109-177`. **Description:** Five pre-checkout steps repeat the integration-resolver clone, authenticated fetch, main fallback and output handling; implement adds a retarget operation. **Recommended fix:** Put the common operation in a trusted, pre-checkout-stageable `scripts/resolve_integration_ref_stage.sh` with interface `resolve_integration_ref_stage.sh <issue> <repo> <support-ref> [default-branch]`, emitting `ref=...`; update all five callers while retaining implement’s optional retarget output and existing fallback behavior.

- **DUP-002** — **Low · `duplication`** — `scripts/watchdog_helpers.sh:186-201`, `scripts/review_run_reviewers.sh:406-420`, `scripts/review_rb_judge.sh:329-343`, `scripts/review_conflict_resolve.sh:286-300`, `scripts/self_heal_validation.sh:143-158`. **Description:** Four callers reproduce the same `read_codex_stall_guard_state` body already owned by `watchdog_helpers.sh`; the reviewer even sources that helper before redefining it at `scripts/review_run_reviewers.sh:67-74`. **Recommended fix:** Keep `read_codex_stall_guard_state <status-file>` in `watchdog_helpers.sh`, ensure its trusted copy is staged for the other three callers, and replace local definitions with guarded sourcing.

### Section 4: Expression Size Limit Risk Assessment

Measurement uses the YAML-dedented `run:` script text before runtime interpolation. Expanded values are unavailable, so stated headroom is **static headroom**, not a guaranteed runtime margin. Of 867 scanned `run:` blocks, 232 contain `${{ }}`; only these two exceed 18,000 decoded characters.

- **EXPR-001** — **High · `expression-limit`** — `.github/workflows/implement.yml:3552-3944`. **Description:** The interpolated destructive-commit preflight is approximately **19,826 characters**, leaving approximately **1,174** before the specified 21,000-character limit. **Recommended fix:** Extract the preflight body to `scripts/implement_preflight_destructive_guard.sh`; pass `github.repository` through the step’s `env:` and retain its current step ID and outputs.

- **EXPR-002** — **High · `expression-limit`** — `.github/workflows/implement.yml:1007-1397`. **Description:** The interpolated support-staging step is approximately **19,305 characters**, leaving approximately **1,695** static characters. **Recommended fix:** Extract its body to a staged support script, passing `github.repository` and the two repository-variable values through `env:`; keep the existing support-source fallback.

No interpolated decoded block falls between 15,000 and 18,000 characters. The largest scanned `if:` line is 712 characters, and no workflow exceeds 800 KB. The largest, `review_autofix.yml`, is 429,229 bytes—below this repository’s stricter 480,000-byte CI guard.

### Section 5: Cross-Cutting Concerns

- **DEAD-001** — **Low · `dead-code`** — `scripts/orchestrate_poll_process.sh:14115-14137`. **Description:** `issue_nums` and the purported fallback `branch_nums` use equivalent branch-selection and number-capture patterns on the same `fresh_runs`; their results are then deduplicated. The second extraction adds no distinct branch class. **Recommended fix:** Remove the second jq pass and emit the first result; add examples for each documented branch form to its extractor test.

- **SHELL-001** — **Low · `shellcheck`** — `.github/workflows/test-and-mark-stable.yml:899-899,1191-1191,1491-1491,2464-2464,3804-3804`. **Description:** Five timeout tests use unquoted `$IDLE` and `$INACTIVITY_LIMIT` in `[ ]`, an SC2086-style splitting hazard. They are arithmetic-derived on the inspected paths, so this is presently a low-risk consistency issue, not a demonstrated timeout failure. **Recommended fix:** Quote both operands and keep numeric validation at their input boundaries.

- **DEBT-001** — **Low · `tech-debt`** — `scripts/orchestrate_poll_process.sh:4738-4754,4823-4839`. **Description:** Label reconciliation plans and applies edits without runtime gates for `ENABLE_LABEL_REPAIR_SWEEP`, `LABEL_REPAIR_DRY_RUN` or `LABEL_REPAIR_MAX_ISSUES_PER_CYCLE`. `README.md:2093-2095` explicitly calls these controls *reserved*, so this is unfinished operator control rather than a regression. **Recommended fix:** Before advertising them as operational switches, wire enablement, dry-run logging and a per-cycle mutation cap around the existing repair path, with contract tests for each mode. No TODO/FIXME/HACK markers were found in the audited workflows or scripts.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 4 | BUG-001, SEC-001, EXPR-001, EXPR-002 |
| Medium | 9 | SEC-002, BUG-002, BUG-003, API-001, API-002, API-003, BATCH-001, BATCH-002, DUP-001 |
| Low | 6 | BUG-004, API-004, DUP-002, DEAD-001, SHELL-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 2 existing scripts: `gh_helpers.sh`, `implement_diagnose_post_codex_failure.sh` | Medium |
| API call optimization | 4 existing scripts: `gh_helpers.sh`, `label_helpers.sh`, `orchestrate_poll_process.sh`, `workflow_failure_heal_intake.sh` | Large |
| Code modularization | 5 workflows, 5 existing stall-guard scripts, and 1 new resolver-staging script | Large |
| Expression size reduction | `implement.yml` and approximately 2 extracted scripts | Medium |
| Medium/Low fixes | Approximately 5 additional workflow/script files; overlaps with rows above | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-10-10)

### Safety Tag Legend

`SAFE_TO_MERGE` permits implementation without further review. `NEEDS_VERIFICATION` requires the stated checks first. `RISKY_SKIP` identifies a possible saving that must not be auto-implemented because it touches a protected retry, pagination, authorization, or race-sensitive path.

### Consolidation Candidates (MERGE-###)

- **MERGE-001 — `NEEDS_VERIFICATION`** — `.github/workflows/review_autofix.yml:1875-1888` and `.github/workflows/review_autofix.yml:1891-1900`, in **Dispatch standalone validate for orchestrator short-circuit issues**. **Current → proposed:** 2 → 1 reads when the cached issue nodes and PR text are empty and the GraphQL lookup succeeds with no linked issues; retain both reads on an incomplete GraphQL response. **Endpoints:** GraphQL `repository.pullRequest.closingIssuesReferences` and REST `GET /repos/{repo}/pulls/{pr}`. **Evidence:** The GraphQL call already selects the PR; on the empty-nodes path, a subsequent REST call fetches that PR’s title and body. **Proposed fix:** Add `title` and `body` to the existing pull-request GraphQL selection, retain them alongside the issue nodes, and let the PR-text fallback use them only after a validated complete response. Follow the response-validation pattern of `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`; keep REST as the miss/error fallback. **Safety rationale:** The reads are in one step with the same token and no intervening PR mutation, but different endpoints and the current GraphQL `--jq` projection do not establish equivalent partial-error behavior. **Downstream signal:** Verify title/body parity, top-level GraphQL-error handling, and tests for empty references, partial responses, and GraphQL failure before removing the conditional REST read.

### Redundant Re-Fetch (REUSE-###)

- **REUSE-001 — `RISKY_SKIP`** — `scripts/review_merge_train.sh:792-804,949-956` and `scripts/review_merge_train.sh:863-875,985-991`, in `_mt_find_marker_comment` and `_mt_upsert_comment`. **Current → proposed:** *p* paginated comment-list requests + 1 comment GET → *p* list requests on the queued, existing-marker path, where *p* is the page count. **Endpoints:** `GET /repos/{repo}/issues/{pr}/comments?per_page=100` and `GET /repos/{repo}/issues/comments/{id}`. **Evidence:** The listing filters comments by author and marker but projects only ID and creation time; `_mt_upsert_comment` then fetches the selected ID’s body to decide whether a PATCH is needed. **Proposed fix:** Extend `_mt_find_marker_comment` to return a safely encoded body with the selected ID and timestamp, and pass it to `_mt_upsert_comment`; retain its GET when supplied only an ID or when the snapshot cannot be trusted. **Safety rationale:** The source listing uses `--paginate`, and reusing its earlier body could alter the upsert’s response to a concurrent comment edit. **Downstream signal:** Do not auto-implement; manually review page selection, body encoding, and concurrent-edit behavior, then test that an unchanged body still suppresses the PATCH.

- **REUSE-002 — `RISKY_SKIP`** — `scripts/orchestrate_poll_process.sh:20401-20403` and `scripts/orchestrate_poll_process.sh:20445-20447`, in the project-processing loop. **Current → proposed:** 2 → 1 `default_branch` reads on the merge-conflict path **only when the first read succeeded**; retain the second read otherwise. **Endpoint:** `GET /repos/{repo}`. **Evidence:** `DEFAULT_BRANCH_TRACKING` is resolved before the merge-conflict branch, which unconditionally resolves `FINAL_DEFAULT_BRANCH` from the same repository again. **Proposed fix:** Carry a validated successful `DEFAULT_BRANCH_TRACKING` value into `FINAL_DEFAULT_BRANCH`; preserve a fresh read when the first call failed or supplied its `"main"` fallback. **Safety rationale:** This is inside `orchestrate_poll_process.sh`, and the current fallback discards whether the first API read succeeded—both trigger manual race-sensitive review. **Downstream signal:** Do not auto-implement; manually trace merge-conflict transitions and default-branch changes, and test success, failed-first-read, and fallback paths.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: `RISKY_SKIP` — Both reads establish the authenticated account for trust decisions; manually verify identity provenance before reuse.
- API-002: `RISKY_SKIP` — The memo would affect retried label creation inside the poller; manually review missing-label recovery.
- API-003: `RISKY_SKIP` — Both proposed changes alter retry/backoff loops, rather than merge fetched data.
- API-004: `RISKY_SKIP` — These are rate-limit probes inside the poller; a shared snapshot can change the second sweep’s budget decision.
- BATCH-001: `RISKY_SKIP` — Replay authorization depends on complete paginated comments inside the poller.
- BATCH-002: `RISKY_SKIP` — Timeline pagination and poller race handling require manual field and completeness review.

### Summary Counts

Counts cover **net-new findings above**, not cross-references.

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 1 | MERGE-001 |
| RISKY_SKIP | 2 | REUSE-001, REUSE-002 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
