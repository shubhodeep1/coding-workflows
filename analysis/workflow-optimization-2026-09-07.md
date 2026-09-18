## Executive Summary

- **Cost telemetry is overcounted by duplicate aggregate/granular logs.** Run `34078200709` reports 24 OpenRouter calls and 9.07M tokens, but full logs contain 12 unique calls and 4.54M tokens. Corrected window estimate: **61 calls / 28.45M tokens**, versus reported 73 / 32.98M. Impact: 15.9% accounting correction. **Confidence: high.**
- **Reviewer fan-out is the dominant critical path.** Run `34078200709` spent ~59.4 minutes in reviewers; pass 2 ran six models despite a `0 LOC` scoped diff, consuming 2.40M tokens and ~29.7 minutes. Skipping empty-scope pass 2 could halve that run’s AI cost. **Confidence: high.**
- **CI is sequentially bottlenecked.** CI p50 is 1,497s. Run `34082598014` spent 603s in orchestrator tests, 148s in integration-ahead tests, and 113s in collector coverage. Parallel jobs could save approximately 8–11 minutes. **Confidence: high.**
- **Poller overhead is mostly infrastructure rather than orchestration.** Run `34083198228` spent 92s on two AI-memory writes, 42s checking out the repository, and ~25s installing/indexing Semble; processing took 76s. Reusing one memory checkout and lazily initializing Semble could save 45–70s per ordinary poll. **Confidence: high for sampled run; medium fleet-wide.**
- **Reliability is generally strong but reviewer-slot degradation is common.** Four of five fully metered review runs had an empty-output, non-retryable failure, or stall kill; quorum allowed every productive run to finish. **Confidence: high.**
- **The sole hard failure was path-related.** Security Audit run `34021424100` failed during Codex execution with `No such file or directory (os error 2)`. **Confidence: high.**

## Speed Optimizations

1. **Skip pass 2 when its scoped review surface is empty — critical path.**
   - **Evidence:** Run `34078200709`: `diff is 0 LOC`; pass 2 still ran six reviewers from approximately 03:39:10–04:08:51.
   - **Root cause:** `review_run_reviewers.sh` adjusts reasoning by diff size but does not skip a zero-diff pass.
   - **Exact change:** Before pass 2, require non-zero scoped diff or actionable ledger/comment/check-run changes. Emit `REVIEWER_PASS2_SKIPPED reason=empty_scoped_diff`.
   - **Estimated savings:** ~29.7 minutes and 2.40M tokens for the observed run.
   - **Risk:** Low if unresolved comments, check failures, and ledger entries remain explicit override conditions.

2. **Split the CI lint job into parallel heavy-test lanes — critical path.**
   - **Evidence:** Run `34082598014`: total 1,275s; orchestrator tests 603s, integration-ahead tests 148s, collector coverage 113s.
   - **Root cause:** Independent test groups run sequentially in one job.
   - **Exact change:** Create parallel `orchestrator-tests`, `workflow-analysis-tests`, and `static-and-contract-tests` jobs, preserving a single aggregate required check.
   - **Estimated savings:** 8–11 minutes per CI run.
   - **Risk:** Medium; required-check names and shared setup must remain backward-compatible.

3. **Reuse the AI-memory checkout and lazily initialize Semble in pollers — critical path.**
   - **Evidence:** Poll run `34083198228`: memory start 47.9s, memory end 44.3s; Semble installation/indexing ~25s, with no Semble query.
   - **Root cause:** Memory operations clone independently; Semble initializes whenever work exists rather than when a query is required.
   - **Exact change:** Add an `AI_MEMORY_SESSION_DIR` reused by all operations in a job; initialize Semble on first query.
   - **Estimated savings:** 45–70s per no-judge poll; potentially 1–2 runner-hours across 115 cycles.
   - **Risk:** Low with existing fail-open behavior.

4. **Enforce the check-run collector’s total deadline.**
   - **Evidence:** Run `34078200709` waited the full 300s using sleeps of 20/40/80/80/78s. Cancelled run `34078160722` remained in this step at 481s.
   - **Root cause:** Retry/API time is not fully bounded by the nominal wait timeout.
   - **Exact change:** Calculate a monotonic deadline and pass the remaining budget into every API request; snapshot immediately when exhausted.
   - **Estimated savings:** Up to 181s on the observed cancellation.
   - **Risk:** Low; the collector already fails open.

5. **Treat skipped phase workflows as a micro-optimization.**
   - Clarify, plan, implement, and clarify-response produced 85 skipped runs but consumed only 256s total. Trigger-level filtering may reduce UI noise, but it is not a priority.

## Cost Optimizations

1. **Fix duplicate telemetry before making budget decisions.**
   - Run `34078200709` has both an aggregate job log and granular step logs. `cost_audit.py` counted both.
   - Exclude the aggregate parent when granular children exist; do not deduplicate solely by line text.
   - Add `telemetry_source_mode` and `duplicate_candidate_count`.
   - **Savings:** No runtime savings, but corrects 4.54M tokens and 12 calls in this window.
   - **Quality risk:** None.

2. **Eliminate empty-scope second passes.**
   - Across five metered runs, pass 2 consumed **17.89M tokens—62.9% of corrected OpenRouter usage**.
   - Run `34078200709` alone could have avoided 2.40M tokens.
   - Preserve pass 2 for changed scope, unresolved feedback, failed checks, or sensitive forced-review conditions.
   - **Quality risk:** Low under those guards.

3. **Add a real per-model prompt-budget gate.**
   - Run `34017571910` sent 2.735M prompt tokens to `mistralai/mistral-small-2603`; that reviewer then failed non-retryably.
   - Yet `context_budget_warn_count=0`.
   - Emit `PROMPT_BUDGET_V1 model=… estimated_tokens=… window=… ratio=… action=compact|skip|continue`; compact with Semble before dispatch.
   - **Potential savings:** Up to 2.86M tokens for the anomalous call.
   - **Quality risk:** Low–medium; five other reviewers remain available.

4. **Enable self-trigger suppression.**
   - Set `AUTOFIX_SKIP_SELF_TRIGGERED=true`. Paired wrapper/direct review runs produced long pending cancellations, including `34013621055` (5,627s) and `34023239644` (5,064s).
   - The direct continuation and 30-minute sweep remain safety nets.
   - **Savings:** Primarily queue/control-plane waste; potentially one duplicate model cycle if concurrency visibility fails.
   - **Quality risk:** Low.

5. **Improve cache consistency by model.**
   - Corrected weighted cache-read rate is **59.4%**. Per-model rates ranged from 0% for Mistral to ~75% for Qwen and DeepSeek.
   - Moving from 59.4% to 70% would shift approximately 3.0M input tokens from uncached to cached processing at the same workload.
   - Log a stable-prefix digest and provider cache capability before altering prompt order.
   - **Quality risk:** None for prefix stabilization.

**Semble:** 15 unique production queries returned 242,390 bytes in 8.6s total. This is low overhead and appears to replace unbounded file expansion with bounded chunks. Keep it enabled. One 62-byte result suggests adding `result_chunks` and `source_count` to identify low-value queries.

**Serena:** Zero queries, fallbacks, or probes; `SERENA_ENABLED=false` in review. It incurred no cost but provided no tool-call replacement.

## Reliability Improvements

1. **Repair Security Audit path diagnostics.**
   - **Evidence:** Run `34021424100`, `Run security audit`: Codex exited nonzero with `os error 2`.
   - **Category:** Runtime path resolution.
   - **Fix:** Validate every prompt/config/output path immediately before Codex execution and emit `SECURITY_AUDIT_EXEC_V1` with sanitized path existence and stderr tail.
   - **Impact:** Prevent or immediately localize the current 100% security-audit failure rate.
   - **Rollback/fail-open:** Retain current hard failure for security audits; logging is additive.

2. **Place reviewer runtime context inside the permitted workspace.**
   - **Evidence:** Runs `34023251321`, `34027140154`, and `34078200709` had external-directory requests for `/tmp/.../runtime_context/*` auto-rejected immediately before empty reviewer output.
   - **Category:** Sandbox/permission mismatch.
   - **Fix:** Copy read-only runtime context beneath the workspace or add a narrowly scoped read permission.
   - **Impact:** Could remove three of five observed reviewer anomalies.
   - **Rollback:** Revert to current quorum-based fail-open behavior.

3. **Make check-run polling strictly deadline-aware.**
   - **Evidence:** Timeout warning in `34078200709`; cancellation in the same step in `34078160722`.
   - **Impact:** Fewer late cancellations and bounded API usage.
   - **Fail-open:** Continue with `collection_status=timeout`.

4. **Enable reviewer health caching after a shadow period.**
   - Run `34017571910` killed a stalled Minimax attempt after 600s and retried successfully; DeepSeek and Minimax produced empty outputs in three other runs.
   - Shadow-log health decisions for one week, then enable `REVIEWER_CIRCUIT_BREAKER_ENABLED=1`.
   - Rollback is the existing variable toggle.

5. **Fix telemetry-source duplication.**
   - Besides tokens, run `34078200709` reports two Semble calls/30,002 bytes where only one call/15,001 bytes exists. CI run `34082598014` similarly doubles five contract-test fallbacks to ten.
   - Add a regression fixture covering aggregate-plus-granular archives.

`BREAK_GLASS=0` and `CONTEXT_BUDGET_WARN=0`; no policy override pressure was observed. The latter is not reassuring until reviewer prompt-budget coverage is fixed.

## AI Memory Health

- **Retrievals:** 6; **hit rate:** 100%.
- **Records selected:** 29–30 per retrieval; no zero-result retrievals.
- **Average estimated tokens:** 1,386 of 1,400 budget—**99.0% utilization**.
- **Keyword method:** 100% `llm`; no `plain` or `none`.
- **Disabled entries:** 0.
- **Fail-open entries:** 1 expected `finalize-task` event in run `34082600066`, reason `no_linked_issues`.
- **Push behavior:** 18 operations succeeded in one attempt; poll-start in `34083198228` required two attempts. No high retry counts.
- **Concern:** Retrieval is consistently saturated at its token ceiling. Add candidate-count, lowest-selected score, retrieval duration, and dropped-record count before reducing the budget.
- **Latency concern:** Memory writes consumed ~92s in poll run `34083198228`. Add `clone_ms`, `commit_ms`, `push_ms`, and `checkout_reused` fields to `AI_MEMORY_TELEMETRY`.

No sampled `promote`, `compact`, or processed-command telemetry appeared; those operations were not exercised in the inspected workflows.

## GH API Call Audit

Exact call counts are unavailable: `RUN_COST_TELEMETRY_FIELDS` in `scripts/cost_audit.py` contains no GitHub API counters.

**Observed patterns:**

- **Healthy reuse:** Review run `34078200709` cached `LINKED_ISSUES_JSON`; the sweep snapshots active runs before per-PR filtering; poll run `34083198228` detected an existing autofix run and avoided duplicate conflict dispatch.
- **High-volume family:** `orchestrate_poll` ran 115 times. Successful calls are silent, so its API footprint cannot be quantified.
- **Check-run loop:** Run `34078200709` performed five wait cycles over 300s. Each snapshot is at least one API request.
- **Sweep fan-out:** When candidates exist, `review_autofix_sweep.yml` performs one PR listing plus three status snapshots for each of two review workflows—at least seven logical GETs before dispatch.
- **Failure signal:** Poll run `34083198228` received a non-retryable HTTP 422 from `pulls/4019/update-branch`; this was a real merge conflict, not a rate-limit event.
- **Rate limits:** No production 403/429 event was found.

**Changes:**

1. Add `GH_API_CALL_V1 endpoint_class=… method=… attempt=… status=… elapsed_ms=… cache=hit|miss`.
2. Emit one `GH_API_SUMMARY_V1` per job with calls, retries, rate-limit sleeps, and endpoint-class counts.
3. In the sweep, fetch global queued/in-progress/pending runs once per status and filter both workflow IDs locally: six status requests become three, approximately a **43% reduction** for candidate-bearing sweeps.
4. Preserve the repository rule: extend `_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`, and cycle-local caches before adding calls.

## Prompt Cache & Memory System

- Collector `cache_hit_rate` is `null`; corrected token-weighted cache-read rate is **59.4%**.
- Observed run rates: **45.4%–70.8%**.
- Run `34078200709` had a 232,854-byte static prefix and 339,944-byte editor prompt, showing that cache-friendly static-first assembly is active.
- Model behavior is fragmented:
  - Qwen: 75.3%
  - DeepSeek: 75.1%
  - Kimi: 71.6%
  - Minimax: 65.0%
  - Grok: 42.1%
  - Mistral: 0%
- Add `PROMPT_CACHE_PREFIX_V1 phase=… model=… prefix_sha256=… static_bytes=… dynamic_bytes=… provider_cache_supported=…`.
- Keep timestamps, run IDs, temporary paths, and current issue state after the stable cache boundary.
- Memory retrieval is effective but fills 99% of its budget; reserve 10–15% headroom if relevance-score telemetry shows low-value tail records.
- Cache write tokens were reported as zero while reads were substantial. Record cache-write state as `unsupported|missing|zero|positive` rather than collapsing all cases to zero.

## Orchestrator Health

- **115/115 poll runs succeeded**; p50 246s, p95 268.9s. One 764s outlier occurred in run `34073418128`.
- Full run `34083198228` processed tracking issue `#3965`, encountered a real conflict on PR `#4019`, and correctly skipped duplicate dispatch because review was already active.
- No actual judge task was observed in the inspected poll run; poller telemetry recorded zero OpenRouter calls. Judge activity therefore cannot be distinguished from “judge configured but not invoked.”
- Semble ended available and indexed in `34083198228`; earlier generated summaries incorrectly captured only its initial `false` defaults.
- No clarification-loop, wave-terminalization, or recovery-budget escalation was observed.
- Add `ORCH_CYCLE_SUMMARY_V1 tracking=N issues_scanned=N judge_calls=N stall_actions=N conflict_actions=N gh_calls=N phase_ms={…}`.
- Also emit a finalized `SEMBLE_PROBE` after indexing so summaries do not mistake initialization state for final availability.

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence | Priority fix |
|---|---|---|---|
| Clarify | Mostly control-plane skips | 22/23 skipped; p50 1s | No immediate action |
| Plan | One expensive execution | Run `34074071104`, 704s | Add phase timers and prompt composition |
| Implement | No executed implementation in window | 20/20 skipped | Collection gap, not a bottleneck |
| Review/autofix | Reviewer fan-out and repeated iterations | p95 5,333s; PR `#4013` reached iterations 1–5 | Skip empty pass 2; prompt caps |
| Check collection | Fixed 300s wait | Run `34078200709` | Deadline-aware snapshot |
| Validate | Long single-stage execution | `34076893536`, 1,095s | Add subphase timings |
| CI | Sequential heavy tests | p50 1,497s | Parallel job lanes |
| Orchestrate | Memory writes, checkout, recurring setup | Run `34083198228` | Reuse memory clone; lazy Semble |
| Merge/conflict | Manual fallback path | Run `34082598056`, README conflict, PR `#4019` | Preserve fail-open; add conflict telemetry |

End-to-end order: **reviewer pass 2 → CI serialization → poll memory/setup → check-run wait → merge-conflict overhead**.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** review/autofix (51.2% of elapsed time), poller (30.8%), CI (13.9%).

**Top failure modes:** Codex path failure in security audit; reviewer sandbox permission denials; stalled/empty reviewer outputs; queued duplicate review lanes.

**Highest-cost drivers:** six-model two-pass review at high/xhigh reasoning, uncached Mistral prompts, repeated PR `#4013` autofix iterations.

**Top three actions:**

1. Skip zero-scope reviewer pass 2.
2. Parallelize CI’s heavy test groups.
3. Reuse AI-memory checkouts and add strict prompt/API/phase summary telemetry.

## Metrics Appendix

### Run outcomes

| Runs | Success | Failure | Cancelled | Skipped/other | p50 | p95 |
|---:|---:|---:|---:|---:|---:|---:|
| 314 | 222 (70.7%) | 1 (0.32%) | 6 (1.91%) | 85 (27.1%) | 12s | 1,466s |

Non-skipped success rate: **96.94%**.

### Primary workflow families

| Family | Runs | Success | Fail | Cancel | Skipped | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 68 | 62 | 0 | 6 | 0 | 8s | 5,333s |
| orchestrate_poll | 115 | 115 | 0 | 0 | 0 | 246s | 269s |
| ci | 9 | 9 | 0 | 0 | 0 | 1,497s | 1,559s |
| copilot reviewer | 8 | 8 | 0 | 0 | 0 | 126.5s | 176.6s |
| validation_refresh | 1 | 1 | 0 | 0 | 0 | 1,095s | 1,095s |
| security_audit | 1 | 0 | 1 | 0 | 0 | 84s | 84s |
| clarify | 23 | 1 | 0 | 0 | 22 | 1s | 10.9s |
| plan | 22 | 1 | 0 | 0 | 21 | 1s | 11s |
| implement | 20 | 0 | 0 | 0 | 20 | 1s | 10.1s |

### Cost telemetry

| Metric | Collector-reported | Deduplicated |
|---|---:|---:|
| OpenRouter calls | 73 | 61 |
| Prompt tokens | 12,878,027 | 11,398,380 |
| Completion tokens | 423,896 | 395,051 |
| Cache-read tokens | 19,686,213 | 16,659,243 |
| Total tokens | 32,983,855 | 28,448,428 |
| Usage unavailable | 1 | 1 |
| Codex calls/tokens | 3 / 2,027 | unchanged |
| Weighted cache-read rate | 60.5% | 59.4% |

### Additive telemetry

| cache_hit_rate | wall p50 | wall p99 | Samples | BREAK_GLASS | CONTEXT_BUDGET_WARN |
|---:|---:|---:|---:|---:|---:|
| null | 9,000ms | 5,646,800ms | 111 | 0 | 0 |

### Semble and Serena

| System/target | Calls | Bytes | Time | Fallbacks |
|---|---:|---:|---:|---:|
| Semble `reviewer-context` | 5 | 91,031 | 2,721ms | 0 |
| Semble `overflow` | 10 | 151,359 | 5,844ms | 0 |
| Semble total, unique | 15 | 242,390 | 8,565ms | 19 contract-test; 0 runtime |
| Serena | 0 | 0 | 0 | 0 |

| MCP target | probe_ok | probe_failed | probe_skipped | Note |
|---|---:|---:|---:|---|
| Semble/orchestrate_poll | not emitted | not emitted | not emitted | Final availability was healthy in run `34083198228` |
| Serena | 0 | 0 | 0 | Disabled/no production probe |
| github-mcp-server | — | — | — | Connected, 0 invocations in sampled Copilot runs |
| playwright | — | — | — | Connected, 0 invocations in sampled Copilot runs |

### AI memory

| Retrieves | Hit rate | Avg selected | Avg tokens/budget | Keyword method | Fail-open | Push retries |
|---:|---:|---:|---:|---|---:|---:|
| 6 | 100% | 29.7 | 1,386 / 1,400 | 100% LLM | 1 expected | One operation needed attempt 2 |

### Material data gaps

- No GH API call-count fields or endpoint rollups.
- No prompt-prefix fingerprints or model context-window ratios.
- No aggregate judge-cycle counter.
- Collector summaries may capture initialization values instead of final MCP availability.
- Cost parsing currently double-counts archives containing both aggregate and granular logs.

## Deep Audit — Workflows & Scripts (2026-09-07)

### Section 1: Bug & Correctness Sweep

Audit coverage: 46 workflows, 77 shell scripts, and 56 Python scripts. YAML lint, Bash syntax, Python AST parsing, workflow-script reference checks, ShellCheck error-level checks, and focused repository contract tests passed.

#### SEC-001 — Fork PRs can enter the trusted deterministic auto-merge path

- **ID:** `SEC-001`
- **File path and line range:** `.github/workflows/review_autofix_sweep.yml:93-107,280-297`; `.github/workflows/internal-review.yml:53-66`; `.github/workflows/review_autofix.yml:510-607,1080-1224,2388-2414`
- **Severity:** Critical
- **Category tag:** `security`
- **Description:** The sweep dispatches `internal-review.yml` for fork PRs, merely omitting `--ref` when `head_repo != REPOSITORY`. The dispatched workflow inherits secrets. Before the later head-repository writability check, the reusable workflow can classify a fork PR as `docs_only` or `small_diff`; `deterministic-skip-merge` then receives `GH_PAT` and executes `gh pr merge --squash --auto` without reviewer or editor execution. Thus an external fork PR within the default 10-addition/10-deletion threshold can be enabled for auto-merge by a trusted scheduled dispatch. Non-skipped fork PRs also reach secret-bearing reviewer/editor setup because `CAN_PUSH=false` is not part of those step conditions.
- **Recommended fix:** Apply the same same-repository guard used by `check_failure_triage.yml:135-145`: skip fork PRs in the sweep before dispatch. Add defense-in-depth to `review_autofix.yml` by including `.head.repo.full_name` in the gate’s existing PR fetch and forcing `should_run=false`, `deterministic_skip=false` for mismatches. Add a contract test asserting fork PRs can never reach `deterministic-skip-merge` or secret-bearing writer steps.

#### BUG-001 — Active-run API failures are interpreted as “no active review”

- **ID:** `BUG-001`
- **File path and line range:** `.github/workflows/review_autofix_sweep.yml:159-205,222-265`
- **Severity:** Medium
- **Category tag:** `bug`
- **Description:** Each status fetch ends with `|| true`. If queued, in-progress, or pending requests fail, `jq -s` still succeeds and produces `{"active":{},"stale":[]}`. The sweep then dispatches another review. This was reproduced directly with the workflow’s exact jq program and empty input.
- **Recommended fix:** Capture each status request’s return code. If any snapshot is incomplete, mark that workflow’s state `unknown` and skip dispatch for the tick. Route requests through `gh_retry`; do not convert failed requests into valid empty snapshots.

### Section 2: GitHub API Call Redundancy Audit

#### API-001 — Final-PR state and merge status use duplicate GETs

- **ID:** `API-001`
- **File path and line range:** `scripts/orchestrate_poll_process.sh:8428-8440`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** When `final_pr_json_snapshot` cannot be reused, the same `pulls/<final_pr>` endpoint is fetched twice consecutively: once for `.state` and once for `.merged_at`.
- **Current/proposed calls:** 2 GETs → 1 GET.
- **Recommended fix:** Fetch one object through `_fetch_pr_json` or `_safe_gh_jq`, then extract both fields locally. Extend the existing final-PR snapshot reuse pattern.

#### API-002 — Feature sweep re-fetches head SHA already available from PR listing

- **ID:** `API-002`
- **File path and line range:** `scripts/orchestrate_poll_process.sh:16626-16658`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** The initial `gh pr list` requests merge state but omits `headRefOid`. Every behind PR therefore incurs a separate `pulls/<n>` GET before its update-branch request.
- **Current/proposed calls:** For `N` behind PRs, `1 + 2N` → `1 + N`.
- **Recommended fix:** Add `headRefOid` to the existing `--json` field list and consume it locally, following `_merge_probe_refresh`’s list-and-cache pattern at `scripts/orchestrate_poll_process.sh:813-822`.

#### BATCH-001 — Standalone recovery performs seven phase-label list calls

- **ID:** `BATCH-001`
- **File path and line range:** `scripts/orchestrate_poll_process.sh:11937-11985,12791-12841`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** Candidate discovery loops over seven phase labels with one `gh issue list` per label, then separately invokes the two-alias marker GraphQL helper.
- **Current/proposed calls:** 7 label-list calls + 1 marker query = 8 → 1 common-path GraphQL query.
- **Recommended fix:** Extend `_fetch_standalone_marker_issues_graphql` with aliases for the seven phase-label searches and return one deduplicated candidate object. Preserve paginated REST fallback independently for aliases reporting `hasNextPage`.

#### BATCH-002 — Review metadata bypasses the existing consolidated PR-context helper

- **ID:** `BATCH-002`
- **File path and line range:** `scripts/review_collect_pr_metadata.sh:63-68,209-226`; `scripts/gh_helpers.sh:751-915`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** The collector separately fetches PR metadata, issue comments, review comments, and optionally top-level reviews. `gh_pr_with_all_comments` already combines the first three data families through GraphQL, but the collector shadows `gh_retry` with an incompatible output-file signature, preventing safe reuse.
- **Current/proposed calls:** Context family 3 calls normally or 4 with break-glass → 1. Including linked-issue lookup, 4/5 → 2.
- **Recommended fix:** Rename the local wrapper to `review_metadata_gh_to_file`, extend `gh_pr_with_all_comments` to return raw IDs/timestamps/top-level review state needed by `PR_ALL_COMMENTS_CONTEXT_FILE`, and derive all three artifact files from that response.

The existing report already records the sweep’s healthy-path workflow-status reduction from six requests to three; it is not duplicated here.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Prompt-budget telemetry wrapper is copied three times

- **ID:** `DUP-001`
- **File path and line range:** `scripts/review_apply_fixes.sh:164-202`; `scripts/review_rb_judge.sh:256-294`; `scripts/review_run_reviewers.sh:69-107`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** `emit_context_budget_warn_for_prompt` is byte-for-byte duplicated across three critical model paths. Any telemetry or budget-gate correction must be applied three times.
- **Recommended fix:** Move it to `scripts/codex_helpers.sh` with signature `emit_context_budget_warn_for_prompt <phase> <prompt_path> <model>`, retaining `cost_audit.py::build_context_budget_warn_line_for_file` as the calculation authority.

#### DUP-002 — Bounded Semble query-section rendering is triplicated

- **ID:** `DUP-002`
- **File path and line range:** `scripts/review_apply_fixes.sh:909-918`; `scripts/review_conflict_prepare.sh:596-605`; `scripts/review_run_reviewers.sh:1760-1769`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** Identical `append_semble_query_section` implementations appear in three scripts.
- **Recommended fix:** Add `semble_append_query_section <label> <path> [max_bytes]` to `scripts/semble_helpers.sh` and update all three callers.

#### DUP-003 — Support-file staging remains duplicated across phase workflows

- **ID:** `DUP-003`
- **File path and line range:** `.github/workflows/clarify.yml:216-351`; `.github/workflows/implement.yml:853-1132`; `.github/workflows/orchestrate.yml:341-469`; `.github/workflows/orchestrate_clarify_respond.yml:279-412`; `.github/workflows/orchestrate_poll.yml:333-542`; `scripts/stage_workflow_support.sh:4-225`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** These workflows independently implement branch/main fallback selection, script installation, schema copying, prompt copying, overlay loading, and generated `.gitignore` handling. `validate.yml` already demonstrates manifest-driven staging through `stage_workflow_support.sh`.
- **Recommended fix:** Generalize the helper to `stage_workflow_support.sh manifest --manifest <path> --target-mode <runtime|in-tree>`. Store one phase manifest per caller and retain phase-specific required/optional/main-primary classifications there.

#### DUP-004 — Thread-reuse asset resolution duplicates an existing helper

- **ID:** `DUP-004`
- **File path and line range:** `scripts/codex_thread_reuse.sh:407-423`; `scripts/review_apply_fixes.sh:680-710`; `scripts/review_conflict_resolve.sh:150-171`; `scripts/validate_process.sh:2890-2911`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** Three phase-specific asset resolvers duplicate `codex_thread_reuse_resolve_asset`; three equivalent enabled predicates also repeat.
- **Recommended fix:** Source `codex_thread_reuse.sh` first, call `codex_thread_reuse_resolve_asset <repo_path>`, and add a shared `codex_thread_reuse_enabled` predicate for all callers.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Implement support-staging expression exceeds the medium-risk threshold

- **ID:** `EXPR-001`
- **File path and line range:** `.github/workflows/implement.yml:853-1132`
- **Severity:** Medium
- **Category tag:** `expression-limit`
- **Description:** The interpolated `run:` body is approximately **15,162 characters**, exceeding the 15,000-character threshold. Remaining headroom is approximately **5,838 characters** to the hard limit and **2,838 characters** to the high-risk threshold.
- **Recommended fix:** Replace the inline staging implementation with a phase manifest consumed by `scripts/stage_workflow_support.sh`.

#### EXPR-002 — Validate embeds a large support manifest inside an interpolated run block

- **ID:** `EXPR-002`
- **File path and line range:** `.github/workflows/validate.yml:205-424`
- **Severity:** Low
- **Category tag:** `expression-limit`
- **Description:** The block is approximately **13,096 characters**, including a large JSON heredoc. It has approximately **7,904 characters** of hard-limit headroom. Every added validation asset expands the expression.
- **Recommended fix:** Move the manifest to `scripts/support_manifests/validate.json`; keep the workflow step limited to resolving and invoking `stage_workflow_support.sh`.

#### EXPR-003 — Implement preflight guard block has a growing heredoc-heavy expression

- **ID:** `EXPR-003`
- **File path and line range:** `.github/workflows/implement.yml:2916-3168`
- **Severity:** Low
- **Category tag:** `expression-limit`
- **Description:** The interpolated block is approximately **13,794 characters**, with approximately **7,206 characters** remaining to the hard limit. Multiple multiline output heredocs make future guard additions likely to increase it.
- **Recommended fix:** Extract the destructive/scope preflight logic to `scripts/implement_preflight_guard.sh` and return structured outputs through `$GITHUB_OUTPUT`.

No interpolated `run:` block exceeds 18,000 characters, no large `if:` expression approaches the limit, and no workflow exceeds 800 KB. The largest workflow is `review_autofix.yml` at 454,419 bytes.

### Section 5: Cross-Cutting Concerns

#### CONSIST-001 — Safety-latch GitHub mutations bypass shared retry handling

- **ID:** `CONSIST-001`
- **File path and line range:** `scripts/implement_handle_guard_block.sh:33-66,142-195`; `.github/workflows/implement.yml:883-886`
- **Severity:** Medium
- **Category tag:** `consistency`
- **Description:** The destructive and scope-block handlers use raw `gh label`, `gh issue edit`, and `gh issue view` calls. A transient failure can leave `ai:scope-blocked` or `ai:destructive-blocked` absent, so the documented redispatch latch is not active. The script detects this but cannot repair it.
- **Recommended fix:** Preserve a runtime copy of `gh_helpers.sh` beside the handler and route all latch creation, mutation, verification, and comment calls through `gh_retry`. Keep the current red-job and Telegram fallback behavior.

#### SHELL-001 — Reviewer state contains confirmed unused assignments

- **ID:** `SHELL-001`
- **File path and line range:** `scripts/review_run_reviewers.sh:753-761,3533-3548,4120-4128`
- **Severity:** Low
- **Category tag:** `shellcheck`
- **Description:** ShellCheck reports SC2034 for `RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE`, `RAW_REVIEWER_SYMBOL_DIFF_SUMMARY_FILE`, `REVIEWER_HEALTH_LAST_OPEN_UNTIL_EPOCH`, and `REVIEWER_ATTEMPT_WD_REASON`. Repository-wide reference checks found assignments but no consumers.
- **Recommended fix:** Remove the raw aliases if obsolete. Otherwise wire health-open expiry and watchdog reason into `REVIEWER_HEALTH`/slot summary telemetry so the assignments serve a documented contract.

No exact-word `TODO`, `FIXME`, `HACK`, or `XXX` markers were found. Documented reserved/deprecated surfaces are explicitly identified as such and were not treated as integrity failures.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 1 | SEC-001 |
| High | 0 | — |
| Medium | 7 | BUG-001, BATCH-001, BATCH-002, DUP-001, DUP-003, EXPR-001, CONSIST-001 |
| Low | 7 | API-001, API-002, DUP-002, DUP-004, EXPR-002, EXPR-003, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3 workflows + tests | Medium |
| API call optimization | 3 scripts | Medium |
| Code modularization | 10–14 workflows/scripts | Large |
| Expression size reduction | 2 workflows + support manifests/helpers | Medium |
| Medium/Low fixes | 3–5 workflows/scripts | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-09-07)

### Safety Tag Legend

`SAFE_TO_MERGE` is directly actionable; `NEEDS_VERIFICATION` requires specified checks; `RISKY_SKIP` must not be auto-implemented because polling, pagination, recovery, or other safety semantics are involved.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — Clarify fetches overlapping comment histories

- **ID:** `MERGE-001`
- **Safety tag:** `RISKY_SKIP`
- **File path and line ranges:** `.github/workflows/clarify.yml:455-486`
- **Current/proposed calls:** Semantic-cache-enabled path: 2 logical GETs → 1; disabled path remains 1.
- **Endpoints:** `GET /repos/{repo}/issues/{issue}/comments` with identical ordering but different `per_page` and pagination.
- **Evidence:**
  ```bash
  gh_retry gh api ".../comments?sort=created&direction=asc&per_page=50" > "${ISSUE_COMMENTS_FILE}"
  gh_retry gh api --paginate --slurp ".../comments?sort=created&direction=asc&per_page=100" |
  ```
  The paginated result contains the first 50 comments already fetched for `ISSUE_COMMENTS_FILE`.
- **Proposed fix:** When semantic caching is enabled, fetch all pages once, write `(add // [])[0:50]` to `ISSUE_COMMENTS_FILE`, and render the full array into `THREAD_HISTORY_FILE`. Retain the bounded GET as the fallback if pagination fails.
- **Safety rationale:** Pagination semantics differ, triggering mandatory `RISKY_SKIP`, and the fallback must preserve the current prompt-success/cache-bypass behavior.
- **Downstream signal:** Do not auto-implement; manually test 0, 50, 51, 100, and 101-comment threads plus paginated-fetch failure.

#### MERGE-002 — Auto-merge guard separately fetches PR labels and metadata

- **ID:** `MERGE-002`
- **Safety tag:** `RISKY_SKIP`
- **File path and line ranges:** `scripts/review_enable_auto_merge.sh:45-76,110-192`
- **Current/proposed calls:** 2 GETs → 1.
- **Endpoints:** `GET /repos/{repo}/issues/{pr}/labels?per_page=100`; `GET /repos/{repo}/pulls/{pr}`.
- **Evidence:**
  ```bash
  PR_LABELS_RAW="$(gh_retry gh api --paginate ".../issues/${PR_NUMBER}/labels?per_page=100" ...)"
  _ORCH_PR_META_JSON="$(gh_retry gh api ".../pulls/${PR_NUMBER}" ...)"
  ```
  The PR payload already supplies `labels`, `head.ref`, and `body`.
- **Proposed fix:** Move the existing PR metadata fetch before the e2e guard, derive `PR_LABELS_RAW` from `.labels[]?.name`, and reuse the payload for head-ref/body checks.
- **Safety rationale:** The removed call is paginated and protects a safety-critical auto-merge exclusion, requiring `RISKY_SKIP`.
- **Downstream signal:** Do not auto-implement; manually prove `/pulls/{pr}` returns the complete label set and verify every API/JSON failure still suppresses auto-merge.

#### MERGE-003 — Reissue paths fetch issue title and body separately

- **ID:** `MERGE-003`
- **Safety tag:** `RISKY_SKIP`
- **File path and line ranges:** `scripts/orchestrate_poll_process.sh:11278-11280` (`execute_stall_recovery_action`); `scripts/orchestrate_poll_process.sh:13647-13655` (`run_standalone_stall_recovery`); `scripts/orchestrate_poll_process.sh:18213-18215` (implementation-failed sweep).
- **Current/proposed calls:** Across all three paths, 6 GETs → 3; each path is 2 → 1.
- **Endpoint:** `GET /repos/{repo}/issues/{issue}`.
- **Evidence:**
  ```bash
  orig_title="$(...issues/${issue_num} --jq '.title...')"
  orig_body="$(...issues/${issue_num} --jq '.body...')"

  IF_TITLE="$(...issues/${if_issue} --jq '.title')"
  IF_BODY="$(...issues/${if_issue} --jq '.body')"
  ```
- **Proposed fix:** Fetch one issue object per path and extract both `.title` and `.body` locally before closing or reissuing.
- **Safety rationale:** These calls are inside `orchestrate_poll_process.sh` recovery paths, and consolidation couples two currently independent failure outcomes.
- **Downstream signal:** Do not auto-implement; manually validate all managed, standalone, and implementation-failed reissue tests under successful and failed issue fetches.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — Failure handling discards freshly fetched PR metadata between adjacent steps

- **ID:** `REUSE-001`
- **Safety tag:** `NEEDS_VERIFICATION`
- **File path and line ranges:** `.github/workflows/review_autofix.yml:6574-6592,6594-6650`
- **Current/proposed calls:** Empty-`PR_META_FILE` fallback path: 2 GETs → 1; retain the second GET when the first fetch fails.
- **Endpoint:** `GET /repos/{repo}/pulls/{pr}`.
- **Evidence:**
  ```bash
  pr_meta="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" ...)"
  ...
  PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' ...)"
  ```
- **Proposed fix:** Have `Check PR state before failure alerts` persist a validated `{title,body}` snapshot under `RUNTIME_DIR`; make `Mark linked issues review-blocked` consult `PR_META_FILE`, then that snapshot, then its existing live fallback.
- **Safety rationale:** The calls occur in different workflow steps, so the same-step precondition and acceptable cache-staleness window are not statically proven.
- **Downstream signal:** Verify identical `RUNTIME_DIR` visibility across steps, successful-fetch reuse, retained live fallback after first-fetch failure, and whether concurrent PR title/body edits require a fresh read.

### Dead Calls (DEAD-API-###)

#### DEAD-API-001 — Standalone conflict sweep fetches an unused default branch

- **ID:** `DEAD-API-001`
- **Safety tag:** `RISKY_SKIP`
- **File path and line ranges:** `scripts/orchestrate_poll_process.sh:19720-19735,19815-20221`
- **Current/proposed calls:** 1 unconditional GET per poll tick → 0.
- **Endpoint:** `GET /repos/{repo}`.
- **Evidence:**
  ```bash
  CONFLICT_SWEEP_FIXED=0
  DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"

  for (( sidx=0; sidx<STANDALONE_COUNT; sidx++ )); do
  ```
  No subsequent conflict/noop-suspicious sweep expression or invoked helper consumes this assignment.
- **Proposed fix:** Remove the assignment at line 19733.
- **Safety rationale:** Although statically dead, the call is inside `orchestrate_poll_process.sh`, which mandates `RISKY_SKIP` because of its race-defensive orchestration role.
- **Downstream signal:** Do not auto-implement; manually recheck indirect function reads of global `DEFAULT_BRANCH` and run standalone conflict/noop recovery tests before removal.

### Cross-References to Deep Audit Section

- API-001: RISKY_SKIP — Exact duplicate endpoint, but it is inside `orchestrate_poll_process.sh`.
- API-002: RISKY_SKIP — Valid reuse opportunity, but it changes a poller recovery path.
- BATCH-001: RISKY_SKIP — Poller placement and paginated REST fallback require manual page-boundary review.
- BATCH-002: RISKY_SKIP — Multiple paginated comment/review connections require preserving `hasNextPage`-driven REST fallback semantics.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 1 | REUSE-001 |
| RISKY_SKIP | 8 | MERGE-001, MERGE-002, MERGE-003, DEAD-API-001, API-001, API-002, BATCH-001, BATCH-002 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
