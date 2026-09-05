## Executive Summary

- **Review/autofix dominates latency.** Family p95 was **5,341.6s**, while run `33714582910` queued **7,685s** before 5,712s of active work and run `33731321556` queued **4,957s** only to discover the PR was closed. Per-PR cross-workflow deduplication could remove **83–128 minutes** from stale runs. **Impact: very high; confidence: high.**
- **Reviewer fan-out dominates AI cost.** Review/autofix consumed all **66.70M OpenRouter tokens across 107 calls**. Slow successful runs normally executed two six-model passes (`call=pass1` and `call=review`), despite risk-tier selection being disabled. Conditional second-pass execution could save roughly **30–50% of review tokens and compute time**. **Impact: very high; confidence: medium-high.**
- **Three of four failures were deterministic repository-contract drift.** CI runs `33712460685`, `33712816637`, and `33723955367` failed on inventory, prompt-byte parity, and README wrapper predicates. Fixing synchronization and fail-fast ordering would have eliminated **75% of observed failures** and saved **349–486s per failed CI run**. **Impact: high; confidence: high.**
- **The implement failure was semantically a no-op.** Run `33711184784` used **1.324M collector-reported Codex tokens** before returning that the requested gates already existed and tests passed; this was recorded as `phase_failed`. Introduce an `already_satisfied` terminal outcome rather than treating every `BLOCKED` result as failure. **Impact: high; confidence: high.**
- **The orchestrator is reliable but expensive for mostly polling work.** All **93 poller runs succeeded**, but p50/p95 were **254s/275.4s**. Recent runs spent 63–82s recording memory start/end events, 52–69s processing one issue, 26–36s checking out, and 7–15s reinstalling Semble. **Impact: medium-high; confidence: high.**
- **Cache reuse is valuable but incompletely measured.** Official aggregate `cache_hit_rate` is null; observed available-token ratio was **75.1%**, with **49.59M cache-read tokens**, zero reported cache-write tokens, and 5/107 calls missing usage. **Impact: medium; confidence: high for totals, low for cache-creation behavior.**
- **AI memory retrieval is healthy.** Deep-dive logs showed **9/9 retrieval hits**, averaging **1,401.9 estimated tokens against a 1,422.2-token budget**. No zero-result, disabled, or fail-open retrieves appeared. **Impact: positive signal; confidence: high within sampled logs.**

## Speed Optimizations

### 1. Cancel stale review work before runner allocation — critical path

- **Evidence:** `33714582910` waited 7,685s; `33731321556` waited 4,957s and then finalized `pr_closed` after 98s. Runs `33717556471` and `33723193397` were cancelled after 5,335s and 5,835s; the former never started its codex-agent job.
- **Root cause:** Queue contention plus deduplication that does not uniformly span `Internal: AI Review & Autofix` and `Codex PR Self-Healing Semantic Agent`.
- **Exact change:** Use one concurrency key based on repository and PR across both workflow names. Cancel only when a newer head SHA exists or the PR closes. Extend cancel-on-close to queued runs.
- **Estimated saving:** 83–128 minutes on stale queued runs; potentially >50% review p95 reduction.
- **Risk:** Medium. Never cancel a run for a different PR or the same unchanged head.
- **Logging:** Emit `WORKFLOW_QUEUE_V1` with `queued_ms`, hashed concurrency key, head SHA, cancellation reason, and superseding run ID.

### 2. Make the second six-model pass conditional — critical path

- **Evidence:** Seven full slow reviews emitted 12 baseline reviewer calls; retries raised this to 13–15. Active elapsed time ranged from 4,873s to 10,839s. Risk-tier selection logged `enabled=false`, leaving the six-reviewer panel as the default.
- **Root cause:** Every substantive review performs both `pass1` and `review`, even when the first pass is low-disagreement or a prior round already converged.
- **Exact change:** Run the second panel only when pass one has actionable disagreement, security findings, parser uncertainty, or material changes since the previous ledger. Missing metrics must fail closed to the current full path.
- **Estimated saving:** 30–90 minutes and approximately six model calls on qualifying runs.
- **Risk:** Medium; force full review for security-sensitive, large, conflict-resolution, or uncertain changes.
- **Logging:** Add `REVIEW_PASS_DECISION_V1 pass=2 reason=... disagreement_count=... actionable_count=... prior_fingerprint_match=...`.

### 3. Shorten check-run waiting that ends with an incomplete snapshot — critical path

- **Evidence:** Six sampled review runs requested **1,315s** of polling sleep across 26 iterations. Runs `33702970190` and `33717572211` reached the 300s timeout and proceeded anyway.
- **Root cause:** Review startup waits for long CI checks that regularly exceed the wait budget.
- **Exact change:** Wait only for relevant required checks; cap the initial wait at 60s when remaining checks are known long-running CI jobs. Recheck immediately before commit/push.
- **Estimated saving:** Up to 166s average across the six sampled runs, without removing the final safety check.
- **Risk:** Low-medium.
- **Logging:** Include pending check names, age, required status, conclusion, and snapshot freshness in `CHECK_RUNS_WAIT_V1`.

### 4. Reuse the AI-memory checkout during poller execution

- **Evidence:** Recent pollers spent 32–42s recording start and 31–41s recording completion.
- **Root cause:** Independent clone/fetch/push transactions for two events.
- **Exact change:** Retain the memory checkout after the start event and reuse it for completion. Preserve current push semantics.
- **Estimated saving:** 25–40s per poll.
- **Risk:** Low if the final push retains existing rebase/retry handling.
- **Logging:** Add `elapsed_ms`, `clone_ms`, `fetch_ms`, `rebase_ms`, `push_ms`, and `retry_sleep_ms` to `AI_MEMORY_TELEMETRY`.

### 5. Run fast contract/parity checks before the long CI suite

- **Evidence:** Failures occurred after 349–486s:
  - `33712460685`: missing `scripts/workflow_wrapper_refs.py` inventory entry.
  - `33712816637`: assembled prompt bytes differed from legacy prompt bytes.
  - `33723955367`: README wrapper `uses` value differed from expectation.
- **Exact change:** Move inventory, prompt parity, and wrapper predicate tests to the start of the lint job.
- **Estimated saving:** Approximately 5–8 minutes on equivalent failures; no material success-path regression.
- **Risk:** Low.

### 6. Lazy-install Semble in pollers — micro-optimization

- **Evidence:** Recent pollers installed Semble in 7–15s, but emitted no Semble queries.
- **Exact change:** Install Semble only when issue processing selects a query-capable path, or restore a version-keyed existing cache.
- **Estimated saving:** 7–15s on ordinary polls.
- **Risk:** Low.

## Cost Optimizations

### 1. Eliminate unnecessary second reviewer passes

- **Evidence:** OpenRouter accounted for **66.70M tokens**, versus **2.65M Codex tokens**. Deep-dive reviews used 94 OpenRouter calls; baseline was two six-model passes.
- **Estimated saving:** Approximately **20–33M total tokens** if 30–50% of second passes can be safely skipped.
- **Quality risk:** Medium; retain forced-full conditions and fail closed on missing data.

### 2. Enable risk-tier selection only after fixing diff metrics

- **Evidence:** Runs logged `tier=disabled loc=0 files=0`, even for PRs with 10–18 changed files and hundreds of additions.
- **Root cause:** Tiering is disabled, and its input metrics are not trustworthy.
- **Exact change:** First emit verified changed-file/LOC metrics. Then use one reviewer for truly small low-risk changes, three for standard changes, and six for high-risk changes.
- **Estimated saving:** 25–60% of reviewer calls on eligible PRs.
- **Quality risk:** Medium. Security, workflow-permission, conflict, and orchestrator-state changes must remain full-tier.

### 3. Reduce prompt duplication

- **Evidence:** Reviewer prompts were **317–498KB**; editor prompts were **311–596KB**. Full-window non-cache OpenRouter prompt usage was **16.45M tokens**.
- **Root cause:** Large static policy, repository context, prior findings, and diff material are repeatedly embedded.
- **Exact change:** Keep stable instructions in a canonical prefix; place dynamic metadata at the end; deduplicate repeated repository guidance and prior-review text.
- **Estimated saving:** 15–25% of non-cache prompt tokens, approximately **2.5–4.1M tokens**.
- **Quality risk:** Low-medium; preserve security and acceptance criteria.

### 4. Skip same-head clean convergence reruns

- **Evidence:** Run `33714582910` performed 14 OpenRouter calls and **2.90M tokens**, then finalized `clean_review_no_commit`.
- **Exact change:** Persist a fingerprint of head SHA, reviewed diff, reviewer roster, prompt version, and clean outcome. Skip a full panel only when all fields match.
- **Estimated saving:** Up to one complete review panel per unchanged clean head.
- **Quality risk:** Medium; invalidate on any prompt, model-roster, base-branch, or check-result change.

### 5. Add deterministic “already satisfied” implement prechecks

- **Evidence:** `33711184784` reported existing gates and passing focused tests after **1.324M collector-reported Codex tokens**.
- **Exact change:** Execute plan-provided validation commands before implementation. If they pass and the plan explicitly permits no-op completion, request a lightweight confirmation rather than full implementation/diagnosis.
- **Quality risk:** Medium; do not infer satisfaction solely from a clean worktree.

### Semble and Serena assessment

- **Semble:** 19 queries returned **439,383 bytes**; deep queries completed in 0.36–0.68s. Runtime fallbacks were zero. It appears to provide targeted context cheaply, but reviewer prompts remain hundreds of KB, so its actual avoided-prompt value is unknown.
- **Required logging:** Add `candidate_bytes`, `returned_bytes`, `included_bytes`, `deduplicated_bytes`, and `prompt_bytes_avoided` to `SEMBLE_QUERY`.
- **Serena:** No queries, tool calls, response bytes, fallbacks, or probes were captured. Logs repeatedly showed `SERENA_AVAILABLE=false`; therefore efficiency cannot be evaluated.

## Reliability Improvements

### 1. Prevent contract/documentation drift

- **Failure evidence:** Three CI failures represented synchronized-file drift rather than transient infrastructure errors.
- **Exact fix:** Generate inventory and wrapper examples from canonical manifests, or add a pre-commit synchronization check.
- **Expected impact:** Would remove 3/4 failures in this window.
- **Rollback:** Keep generated output reviewable; do not auto-commit during CI.
- **Diagnostic improvement:** Prompt and predicate assertions must print expected value, actual value, hashes, and the first differing line/byte. Current failures only emit `AssertionError`.

### 2. Separate `already_satisfied` from true blocked implementation

- **Failure evidence:** `33711184784` returned a deliberate blocked verdict because no edits were required, then emitted `phase_failed`.
- **Exact fix:** Introduce terminal statuses `implemented`, `already_satisfied`, `blocked_external`, `clarification_required`, and `failed`.
- **Expected impact:** Avoid false failure labels, unnecessary diagnostics, and repeated approvals.
- **Fail-open:** Unknown or malformed results remain failures.
- **Diagnostic improvement:** Emit `IMPLEMENT_OUTCOME_V1 status=... changed_files=... validation_passed=... baseline_dirty=...`.

### 3. Preserve and instrument reviewer stall recovery

- **Evidence:** `33703003420`, `33714597372`, and `33717572211` killed and recovered stalled reviewer attempts; minimax and moonshot required cheaper-reasoning retries or failback.
- **Exact fix:** Retain the 600s stall guard, but aggregate internal model retries into workflow telemetry.
- **Expected impact:** Lower tail latency and fewer complete reruns.
- **Diagnostic improvement:** Record first/last output timestamps, kill reason, provider request ID hash, retry model, and recovered outcome. Assert that run-summary stall counts match emitted `REVIEWER_ADVANCE` events.

### 4. Fix stale queued-run cancellation

- **Evidence:** Two reviews queued for 83–128 minutes; another was cancelled before its first codex-agent step.
- **Exact fix:** Apply the same per-PR concurrency group across all review entry points and cancel queued reviews when PR-close events arrive.
- **Rollback:** Disable cancellation while retaining queue telemetry if incorrect cross-PR cancellation appears.

### 5. Remove misleading post-job Git warnings and unbounded environment logging

- **Evidence:** Implement run `33711184784` emitted `git submodule ... cannot be used without a working tree` during post-job cleanup because `GIT_DIR`/`GIT_WORK_TREE` remained set.
- **Exact fix:** Unset worktree-pinning variables before checkout post-actions.
- **Additional issue:** Multiline issue content was repeatedly reproduced in step environment blocks. Pass it through files, logging only size and digest.
- **Expected impact:** Cleaner failure signals and lower accidental sensitive-content exposure.

### Policy and MCP signals

- `BREAK_GLASS`: **0** — no evidence of rubric or policy override pressure.
- `CONTEXT_BUDGET_WARN`: **0** — but large prompt sizes and an unset `MAX_PROMPT_TOKENS_FOR_PHASE` mean this is not proof of low prompt pressure.
- `SEMBLE_FALLBACK`: **35**, all marked `context=contract-test`; runtime fallbacks were **0**. This is healthy test coverage, not an outage.
- `SERENA_PROBE`: **0** despite repeated unavailability. Emit probe-skipped or probe-failed results to distinguish disabled rollout from broken availability.

## AI Memory Health

- **Retrieval effectiveness:** 9/9 retrieves selected records: **100% hit rate**.
- **Budget use:** Average estimated tokens **1,401.9**, average budget **1,422.2** — **98.6% utilization**.
- **Keyword method:** `llm=8`, `plain=1`, `none=0`.
- **Failures:** No `records_selected=0`, `enabled=false`, `fail_open=true`, or `ok=false` retrievals.
- **Push retries:** At least four operations required two attempts, including run-event writes in `33723955549`, `33741403478`, and `33746209469`.
- **Learning activity:** Seven `record-candidate` operations appeared; 13 `write_lessons_learned` operations wrote zero records.
- **Lifecycle gap:** No `finalize-task`, `promote`, or `compact` telemetry was observed.
- **Claim gap:** `33711184784` emitted `processed-command-claim` for the approval command but no corresponding completion event in the deep log. Emit `processed-command-complete status=success|failed|released` from a finalizer to make stale claims diagnosable.
- **Recommended additions:** Retrieval latency, records considered, score distribution, truncation count, post-selection token count, and per-stage Git persistence timings.

## GH API Call Audit

Numeric API call totals were not collected, so rate-limit conclusions are bounded. No HTTP 429 or primary/secondary rate-limit event appeared.

### Highest-risk patterns

1. **Per-reviewer PR-state polling**
   - `scripts/review_run_reviewers.sh` defaults to 10s watchdog sleeps and checks PR state every nine iterations: one GET approximately every 90s per active reviewer.
   - With six reviewers, this violates the repository principle that cycle-local state should not be re-fetched independently per iteration.
   - **Fix:** One shared run-level watcher writes PR state to a local file consumed by all reviewer slots.
   - **Estimated reduction:** Up to **6:1** for this endpoint.

2. **Check-run polling**
   - Six sampled reviews emitted 26 poll sleeps and 1,315s of requested wait.
   - **Fix:** Query only required checks, log names/statuses, and reuse the result in later preflight stages.
   - **Estimated reduction:** 50–80% in timeout-prone runs.

3. **Repeated implement issue reads**
   - `33711184784` fetched the issue payload for context, labels, and later diagnostics.
   - **Fix:** Populate `ISSUE_META_FILE` once, derive body/labels locally, and refresh only after a mutation.
   - **Estimated reduction:** 2–3 calls per implement run.

### Existing good practice

The poller already defines canonical batched GraphQL helpers `_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql`, with cycle-local caches `ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`, and `_candidate_details_json`.

### Required telemetry

Wrap API calls with `GH_API_CALL_V1` containing logical operation, normalized endpoint template, method, cache hit/miss, attempt, status, latency, response bytes, rate-limit remaining/reset, and retry sleep. Avoid logging query bodies or authentication data.

## Prompt Cache & Memory System

- **Official aggregate cache hit rate:** unavailable/null because 5/107 OpenRouter calls lacked usage.
- **Observed available-token ratio:** **75.1%**.
- **Totals:** 49.59M cache-read tokens, 16.45M prompt tokens, zero reported cache-write tokens.
- **Slow-run cache rates:** available examples ranged from **62.5% to 83.7%**.
- **Fragmentation signal:** Deep-sample model ratios ranged from 50.8% to 82.3%. `REVIEWER_CACHE prompt_reused=true` was sometimes followed by near-zero provider cache reads, so it describes local prompt reuse rather than a confirmed provider cache hit.
- **Exact change:** Rename that signal to `prompt_file_reused`; emit post-call `PROMPT_CACHE_V1` with prefix hash, cacheable-prefix bytes, dynamic bytes, prompt tokens, cache-read tokens, and actual hit ratio.
- **Context pressure:** Reviewer/editor prompts reached 498KB/596KB while `CONTEXT_BUDGET_WARN` remained zero. Configure model context windows and emit warnings from runtime calls, not only collector inference.
- **Expected impact:** Stable prefixes and reduced dynamic variance should save 10–20% of non-cache input tokens and reduce latency, with no behavioral change.
- **Memory interaction:** Retrieval consistently consumes nearly its full budget. Keep the high hit rate, but reserve 10–15% headroom and log truncation/ranking so prompt growth does not silently crowd out task-specific context.

## Orchestrator Health

- **Healthy progression:** `orchestrate_poll` succeeded **93/93** times. Clarify, plan, and clarify-response gates usually skipped in 1–2s rather than launching unnecessary work.
- **Healthy deduplication:** Review sweeps repeatedly skipped PR `#3977` because an active review existed (`33741962371`, `33744864131`, `33747371559`).
- **Conflict healing worked:** Review run `33717572211` detected a content conflict on PR `#3974`, resolved it, pushed changes, and finalized `conflict_resolved_pushed`.
- **Pain point:** Poller p50 is 254s despite processing only one active tracking issue in recent runs.
- **Stall ambiguity:** Runs `33746209469` and `33746932183` emitted `STALL_FRESH_PUSH_FALLBACK ... source=branch_name`; add `age_seconds`, source confidence, branch, and head SHA.
- **Judge coverage gap:** Sampled full reviews skipped `rb_judge`, and `JUDGE_INTERIM_ENABLED=false`. Runtime judge health is therefore untested in this window.
- **Track:** queue delay, active-vs-skipped phases, continuation dispatches, per-PR concurrent run count, reviewer stall recoveries, conflict-heal attempts, and time since last state transition.

## Pipeline Flow Bottlenecks

| Phase | Dominant evidence | Bottleneck type | Priority fix |
|---|---|---|---|
| Clarify | 74/78 runs were fast non-executing outcomes; p50 1s | Gate noise, not compute | Aggregate skip reasons; no performance change needed |
| Plan | p95 174.8s; runs `33732751749` and `33710861093` reached ~577–581s | Model/setup | Reuse setup and emit phase timings |
| Implement | Successful `33733550513` took 1,090s; failure `33711184784` took 544s | Model compute and false terminal classification | Deterministic precheck plus `already_satisfied` |
| Review/autofix | p95 5,341.6s; active work 81–181m; queue up to 128m | Queue + model fan-out + polling | Cross-workflow concurrency and conditional second pass |
| Validate/CI | p50 1,389s, p95 1,421.7s; three deterministic failures | Compute plus late fail | Run parity/contract checks first |
| Orchestrate poll | p50 254s across 93 runs | Repeated setup/memory persistence | Reuse memory checkout; lazy Semble |
| Merge/promote | 44–85s, successful | Minor Git overhead | No immediate action |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** Review queueing and two-pass six-model panels; 18–24 minute CI; 4-minute recurring pollers.
- **Top failure modes:** Three synchronized-contract drift failures and one no-op implementation misclassified as failure.
- **Highest-cost driver:** Review/autofix generated the full **66.70M OpenRouter tokens** and 107 calls.
- **Top actions:**
  1. Apply one per-PR concurrency/deduplication key across both review workflow names.
  2. Gate the second reviewer pass and enable risk tiers only with verified diff metrics.
  3. Move deterministic contract checks to the start of CI and add actionable expected-versus-actual diagnostics.

## Metrics Appendix

### Run outcomes and durations

| Workflow family | Runs | Success | Failure | Cancelled | Other | Success rate | Failure rate | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Overall | 597 | 300 | 4 | 10 | 283 | 50.3% | 0.7% | 9s | 1,409s |
| CI | 27 | 24 | 3 | 0 | 0 | 88.9% | 11.1% | 1,389s | 1,421.7s |
| Review/autofix | 92 | 82 | 0 | 10 | 0 | 89.1% | 0% | 10.5s | 5,341.6s |
| Orchestrate poll | 93 | 93 | 0 | 0 | 0 | 100% | 0% | 254s | 275.4s |
| Implement | 73 | 5 | 1 | 0 | 67 | 6.8% | 1.4% | 1s | 230.8s |
| Clarify | 78 | 4 | 0 | 0 | 74 | 5.1% | 0% | 1s | 29.3s |
| Plan | 74 | 5 | 0 | 0 | 69 | 6.8% | 0% | 1s | 174.8s |
| Orchestrate clarify respond | 73 | 0 | 0 | 0 | 73 | 0% | 0% | 1s | 10s |
| Copilot PR reviewer | 18 | 18 | 0 | 0 | 0 | 100% | 0% | 184s | 349.1s |
| Integration readiness | 23 | 23 | 0 | 0 | 0 | 100% | 0% | 10s | 12s |
| PR-body lint | 23 | 23 | 0 | 0 | 0 | 100% | 0% | 9s | 12.9s |
| Cancel on PR close | 8 | 8 | 0 | 0 | 0 | 100% | 0% | 12s | 14.3s |

### Cost and additive telemetry

| Metric | Value |
|---|---:|
| Codex tokens / calls | 2,654,409 / 29 |
| OpenRouter prompt tokens | 16,448,209 |
| OpenRouter completion tokens | 664,839 |
| OpenRouter cache-read tokens | 49,589,707 |
| OpenRouter cache-write tokens | 0 |
| OpenRouter total tokens / calls | 66,698,392 / 107 |
| Usage available / unavailable | 102 / 5 |
| Official `cache_hit_rate` | null |
| Observed available-token cache ratio | 75.1% |
| Overall wall-clock p50 / p99 | 9,000ms / 10,617,570ms |
| Review telemetry wall-clock p50 / p99 | 4,940,000ms / 13,059,060ms |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |

### MCP telemetry

| Server/target | Queries | Bytes | Fallbacks | Notes |
|---|---:|---:|---:|---|
| Semble overall | 19 | 439,383 | 35 | 35 contract-test; 0 runtime |
| `reviewer-context` deep sample | 7 | 186,784 | 0 | 0.38–0.55s typical |
| `overflow` deep sample | 6 | 151,494 | 15 | Sample fallbacks are contract tests |
| `conflict-resolver-context` | 1 | 21,358 | 0 | Run `33717572211` |
| Serena overall | 0 | 0 | 0 | No queries, tool calls, or response bytes |

### MCP availability/probes

| Server/target | probe_ok | probe_failed | probe_skipped | Gap |
|---|---:|---:|---:|---|
| Serena / all targets | 0 | 0 | 0 | Repeated `SERENA_AVAILABLE=false`, but no probe outcome |
| Semble / poller | 0 | 0 | 0 | Installation success is visible; standardized probe telemetry absent |

**Other MCP servers observed:** No standardized unknown `<NAME>_QUERY/FALLBACK/PROBE` prefixes. Copilot summaries mentioned `github-mcp-server` and `playwright` sessions with 0–1 invocations, but without comparable query-byte telemetry.

### AI memory

| Metric | Value |
|---|---:|
| Retrieves | 9 |
| Retrievals with records | 9 (100%) |
| Average estimated tokens | 1,401.9 |
| Average token budget | 1,422.2 |
| Keyword methods | 8 LLM, 1 plain |
| Zero-result/fail-open/disabled retrieves | 0 |
| Record candidates | 7 |
| Zero-count lesson writes | 13 |

### GH API summary

| Hotspot | Observed evidence | Estimated reduction |
|---|---|---:|
| Per-reviewer PR-state GET | Six slots; API check approximately every 90s per active slot | Up to 83% via shared watcher |
| Check-run polling | 26 poll sleeps, 1,315s across six reviews | 50–80% on timeout-prone runs |
| Repeated implement issue reads | Same issue payload used across context, labels, diagnostics | 2–3 calls/run |
| Rate-limit events | None observed | Current risk latent; call totals missing |
