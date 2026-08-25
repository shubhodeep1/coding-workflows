## Executive Summary

- **CI has a systemic timeout/cancellation pattern**: 11/15 CI runs were cancelled, with recent run `32818345444` cancelled at `1818s` in `lint / Orchestrate poll process unit tests`; that step alone spanned `1250.6s`. **Impact:** split/shard this test path to eliminate most 30-minute cancelled runs. **Confidence: high.**
- **Review/autofix dominates tail latency**: `review_autofix` p95 is `3745.5s`; slow runs include `32807381395` at `4126s`, `32810013028` at `4068s`, and `32717542227` at `3805s`, mostly inside `codex-agent`. **Impact:** heartbeat + phase timers + cancellation diagnostics should expose where 55–68 minute runs stall. **Confidence: high for symptom, medium for root cause.**
- **No-op orchestrate poll runs are expensive**: 78 successful `orchestrate_poll` runs have p50 `129.5s`; recent runs `32819960917`, `32821737109`, and `32823564275` found `0 active tracking issue(s)` but still ran ~107–131s. **Impact:** fast-path no-op polls could save ~60–95s per no-work run. **Confidence: high.**
- **Release validation is the single longest compute path**: `test_and_mark_stable` run `32716299147` took `5370s`, with `e2e-smoke-test` spanning `5189.2s`. **Impact:** reserve full E2E polling for release/tag gates; use narrower smoke checks elsewhere. **Confidence: high.**
- **Cost telemetry coverage is incomplete**: assembled telemetry reports `67` OpenRouter calls but `0` prompt/completion/cache tokens and `cache_hit_rate=null`; one reviewer summary reports a `50,531` token prompt. **Impact:** add per-call cache/token logging before tuning model spend. **Confidence: high.**
- **Semble looks healthy in runtime use, but contract-test fallback noise is counted**: assembled totals show `7` Semble queries, `98,649` bytes, `40` fallbacks, all contract-test fallbacks and `0` runtime fallbacks. **Impact:** keep Semble enabled, but separate contract-test fallback counters from runtime SLOs. **Confidence: high.**

## Speed Optimizations

1. **Shard `Orchestrate poll process unit tests` and add per-subset timeout diagnostics**
   - **Evidence:** CI run `32818345444` cancelled after `1818s`; failure point was `lint / Orchestrate poll process unit tests`; `step-098-lint_Orchestrate_poll_process_unit_tests.log` spanned `1250.6s` and ended with `The operation was canceled`. Across CI, `11/15` runs were cancelled around `1817–1821s`.
   - **Root cause:** monolithic `python3 tests/test_orchestrate_poll_process.py "${remaining_tests[@]}"` step with insufficient progress/timing visibility.
   - **Exact change:** split by logical subset, e.g. merge/finalize, validation, stall recovery, conflict/sync; run as separate CI steps or matrix jobs; emit top-N slow test durations and “currently running test” heartbeat every 60s.
   - **Estimated savings:** prevents repeated 30-minute cancellations; likely reduces CI p95 from ~30m to bounded subset time.
   - **Risk:** low-medium; keep original full command available behind a manual or nightly fallback.

2. **Fast-path no-op `orchestrate_poll` before checkout, memory branch writes, and state publishing**
   - **Evidence:** `orchestrate_poll` has 78 successful runs, p50 `129.5s`, p95 `156.5s`. Run `32819960917` found `0 active tracking issue(s)` in ~2.7s, but still spent ~28.8s in checkout and ~33.8s + ~33.0s recording poll start/end memory events.
   - **Root cause:** no-work poll still performs full repository checkout, state snapshot/publish, and two memory pushes.
   - **Exact change:** after active-issue query returns zero, exit before heavyweight steps; optionally emit one lightweight summary event without committing to the memory branch.
   - **Estimated savings:** ~60–95s per no-op poll; material because poller is high-volume.
   - **Risk:** low if full path remains unchanged when active issues exist.

3. **Instrument and bound review/autofix `codex-agent` tail**
   - **Evidence:** review runs `32807381395` and `32810013028` spent `4106.7s` and `4053.6s` in `review_codex-agent`; run `32811737441` spent `3585.9s` in `review_codex-agent_system` before a `155.6s` agent step.
   - **Root cause:** mixed model/runtime wait, hosted-runner/system wait, and agent execution are not distinguishable.
   - **Exact change:** add phase heartbeat logs for memory retrieval, Semble query, model call start/end, apply-fixes, push, and re-dispatch; add first-user-step timestamp; log concurrency group and canceling run ID on cancellation.
   - **Estimated savings:** diagnostic-first; enables safe timeout/abort of 55–68 minute tails.
   - **Risk:** low for logging; medium only if later enforcing timeouts.

4. **Gate full stable-release E2E validation**
   - **Evidence:** `test_and_mark_stable` run `32716299147` took `5370s`; `e2e-smoke-test` spanned `5189.2s`, with additional long steps `orphan-workflows-test` `1746.5s`, `validate-scripts` `1573.8s`, and `e2e-alt-model-test` `952s`.
   - **Root cause:** multiple workflow-dispatch polling loops are exercised in one release-validation path.
   - **Exact change:** run full E2E only for release/tag promotion; use targeted smoke subsets for ordinary validation.
   - **Estimated savings:** up to 60–80 minutes on non-release validation paths.
   - **Risk:** medium; preserve full gate for release-critical paths.

## Cost Optimizations

1. **Add missing OpenRouter/cache token telemetry before model tuning**
   - **Evidence:** assembled cost telemetry reports `or_calls=67` but `or_prompt_tokens=0`, `or_completion_tokens=0`, `or_total_tokens=0`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, and `cache_hit_rate=null`.
   - **Root cause:** calls are counted, but token/cache fields are not emitted or parsed.
   - **Exact change:** emit one structured line per model call with model, phase, reasoning effort, prompt tokens, completion tokens, cache read/write tokens, cache hit/miss, and stable prompt-prefix fingerprint.
   - **Estimated savings:** unknown until measured; enables likely 10–30% token/latency reduction through cache-prefix fixes.
   - **Quality risk:** none; logging-only.

2. **Cap reviewer prompt expansion and downshift models for low-risk reviews**
   - **Evidence:** Copilot reviewer run `32811740761` built a `50,531` token prompt using `gpt-5.2[ReasoningEffort=medium]`; runs `32810016190` and `32814395518` logged default-settings warnings for `gpt-5.6-luna` with high/xhigh reasoning paths.
   - **Root cause:** large prompt assembly and high reasoning effort are used even for moderate/single-comment reviews.
   - **Exact change:** enforce prompt budget tiers, use Semble/memory snippets before broad file expansion, configure model-specific CCR settings, and route simple/moderate findings to lower reasoning.
   - **Estimated savings:** 20–50% reviewer token reduction on simple PRs.
   - **Quality risk:** medium; roll out behind severity/changed-files thresholds.

3. **Keep Semble, but separate runtime value from contract-test fallback noise**
   - **Evidence:** deep logs show five structured runtime `SEMBLE_QUERY target=reviewer-context` lines, each `12` chunks, `8705–15596` bytes, `415–612ms`; assembled totals show `7` queries and `98,649` bytes. All `40` fallbacks are contract-test fallbacks, with `0` runtime fallbacks.
   - **Root cause:** Semble appears cheap and fast, but fallback totals mix expected contract tests with runtime availability.
   - **Exact change:** report `semble_runtime_fallbacks` as the SLO metric and keep contract-test fallbacks in a separate test-health counter.
   - **Estimated savings:** avoids false-positive investigations; no direct token savings unless saved-context metrics are added.
   - **Quality risk:** none.

4. **Avoid model/cache setup on no-op poll paths**
   - **Evidence:** no-op poll run `32819960917` repeatedly logs `MODEL_EDITOR: openai/gpt-5.5`, `MODEL_REASONING_EFFORT_JUDGE: xhigh`, `SEMBLE_AVAILABLE: false`, and `SEMBLE_INDEX_AVAILABLE: false` despite `0 active tracking issue(s)`.
   - **Root cause:** heavy configuration is initialized before knowing whether work exists.
   - **Exact change:** move model/Semble setup after active-work detection.
   - **Estimated savings:** small per run, but poller volume is high.
   - **Quality risk:** low.

## Reliability Improvements

1. **Fix CI cancellation loop**
   - **Failure evidence:** `ci` has `11` cancelled runs out of `15`; recent run `32818345444` cancelled in `Orchestrate poll process unit tests`.
   - **Root cause category:** timeout/test-suite partitioning.
   - **Exact fix:** shard tests and add watchdog progress logs.
   - **Expected impact:** large reduction in reruns/cancellations.
   - **Rollback/fail-open:** keep nightly full-suite execution while PR CI uses shards.

2. **Add review/autofix cancellation and system-wait diagnostics**
   - **Failure evidence:** `review_autofix` includes cancelled run `32717742382` at `3631s` with failure point `review / codex-agent`, `cancelled_before_first_step`; run `32811737441` had `3585.9s` in `codex-agent_system`.
   - **Root cause category:** concurrency/runner wait/model-agent stall ambiguity.
   - **Exact fix:** log concurrency group, triggering event, PR/head ref, previous run that caused cancellation, first-step-start timestamp, and agent phase heartbeats.
   - **Expected impact:** reduces masked long waits and makes safe timeout policy possible.
   - **Rollback/fail-open:** logging-only; no behavior change.

3. **Correct parameterized `gh api` GET calls**
   - **Failure evidence:** review logs for runs including `32807381395` contain `AUTOFIX_PEER_QUERY_FAILED pr=3764 branch=ai/issue-3763 reason=api_error`; log context indicates `gh api` inferred POST when form parameters were supplied without explicit method.
   - **Root cause category:** API method ambiguity.
   - **Exact fix:** add `--method GET` to list/query calls using `-f/-F`; wrap calls with method/path/status/duration/retry/rate-limit logging.
   - **Expected impact:** fewer peer-query misses and easier API failure triage.
   - **Rollback/fail-open:** safe; GET is intended behavior for list endpoints.

4. **Classify Semble fallback health correctly**
   - **Failure evidence:** assembled telemetry shows `40` Semble fallbacks, all `semble_contract_test_fallbacks=40`, `semble_runtime_fallbacks=0`; deep logs show missing-binary fallbacks under `context=contract-test`.
   - **Root cause category:** expected contract-test fail-open counted beside runtime health.
   - **Exact fix:** alert only on runtime fallback rate; include `context`, `target`, `reason`, and `source` in summary rows.
   - **Expected impact:** fewer false reliability alarms.
   - **Rollback/fail-open:** preserve current fail-open behavior.

5. **No `BREAK_GLASS` or `CONTEXT_BUDGET_WARN` pressure observed**
   - **Evidence:** assembled telemetry has `break_glass_count=0`, `context_budget_warn_count=0`; deep-log grep found no such markers.
   - **Interpretation:** no direct policy/rubric pressure or prompt-size warning signal in this window.
   - **Exact fix:** continue emitting these counters per workflow/run.

## AI Memory Health

- **Telemetry found:** 42 valid `AI_MEMORY_TELEMETRY` events in deep logs: `record-run-event=26`, `retrieve=7`, `record-candidate=5`, `write_lessons_learned=3`, `finalize-task=1`.
- **Retrieve quality:** `7/7` retrieves selected records, for a **100% hit rate**. Average `estimated_tokens=1396.9` against average `token_budget=1400`; all used `keyword_method=llm`; selected `30–31` records.
- **No zero-record retrieves observed** in parsed deep logs.
- **Fail-open:** one benign-looking `finalize-task` fail-open in `issue_pr_status` run `32818344891`, reason `no_linked_issues`.
- **Push retries:** two `record-candidate` events used `push_attempts=2` in review runs `32807381395` and `32811753208`; both succeeded.
- **Recommendation:** keep retrieval enabled, but add per-op duration and retry histogram; consider reducing selected-record count slightly because retrieval is consistently at the `1400` token budget ceiling.

## GH API Call Audit

- **Polling loops are the main API-risk hotspot.**
  - **Evidence:** `test_and_mark_stable` run `32716299147` used long workflow-dispatch polling; `e2e-smoke-test` spanned `5189.2s`, and logs show repeated child-run polling/status checks.
  - **Recommendation:** batch status reads where possible, increase backoff after stable `in_progress` states, and emit call count + rate-limit remaining per polling loop.
  - **Expected reduction:** medium; likely dozens to hundreds fewer API calls during long E2E validations.

- **No-op orchestrate polls still perform GitHub/state operations.**
  - **Evidence:** recent poll runs found `0 active tracking issue(s)` but still spent ~2 minutes total.
  - **Recommendation:** short-circuit after active-issue lookup; avoid checkout, state artifact upload, branch publish, and memory event pushes when no work exists.
  - **Expected reduction:** fewer git/API writes per no-op run.

- **Peer PR lookup has a method bug.**
  - **Evidence:** `AUTOFIX_PEER_QUERY_FAILED ... reason=api_error` in review logs; root cause is inferred POST for parameterized `gh api` calls.
  - **Recommendation:** force `--method GET` and log `method`, `path`, `status`, `duration_ms`, `retry_count`, `x-ratelimit-remaining`.
  - **Expected reduction:** fewer false peer-query failures; lower rerun/debug cost.

- **No explicit rate-limit or secondary-rate-limit events were observed** in the sampled deep logs. Keep adding rate-limit telemetry because current evidence is incomplete.

## Prompt Cache & Memory System

- **Cache metrics are unavailable:** assembled telemetry has `cache_hit_rate=null` and zero cache read/write tokens despite `67` OpenRouter calls.
- **Likely cache-fragmentation risks, inference:** logs include dynamic runtime paths, run IDs, branch refs, and repeated environment/config blocks. If these appear before stable prompt prefixes, they will reduce cache reuse.
- **Concrete fixes:**
  - Emit `PROMPT_CACHE_TELEMETRY` per call: model, phase, prefix fingerprint, read/write tokens, hit/miss, cache-disabled reason.
  - Move stable system/rubric text before run-specific paths, timestamps, PR IDs, and transient environment variables.
  - Add prompt-size bucket logging and top contributors to prompt expansion.
- **Memory retrieval is effective but budget-saturated:** all 7 retrieves hit, but average tokens are `1396.9/1400`; add a guardrail for records selected and tokens consumed.
- **Context pressure:** no `CONTEXT_BUDGET_WARN` markers observed; continue tracking because large reviewer prompts already exist.

## Orchestrator Health

- **Healthy completion, inefficient idle flow:** `orchestrate_poll` had 78/78 successes, but high-volume no-op runs still take ~2 minutes.
- **Phase workflows often exit as no-op/other:** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` have many `other` outcomes with p50 `1–2s`, suggesting gates/skip paths are functioning but should be separately reported from failures.
- **Review/autofix is the operational pain point:** p95 `3745.5s`, with both long agent execution and system-wait signatures.
- **Track next:** active issue count, no-op fast-exit count, poll memory-push duration, review first-step lag, agent heartbeat age, concurrency cancellation source, and conflict-heal/re-dispatch counts.

## Pipeline Flow Bottlenecks

- **Clarify → plan → implement:** generally fast on median (`clarify` p50 `2s`, `plan` p50 `1s`, `implement` p50 `1s`), but p95s show occasional stalls (`clarify` `161.4s`, `plan` `283.2s`, `implement` `593s`).
- **Poll/orchestrate loop:** dominant recurring overhead; `orchestrate_poll` p50 `129.5s` even when no active work exists.
- **Review/autofix loop:** dominant tail; multiple successful runs exceed 55 minutes.
- **Validation/CI loop:** repeated 30-minute CI cancellations are the clearest reliability bottleneck.
- **Release validation:** single longest path at `5370s`, driven by E2E workflow polling.
- **Queue/runner overhead:** summaries and deep logs show hosted-runner wait/system-wait signatures in poll/review paths; add explicit queue-vs-execution timing.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** `test_and_mark_stable` `5370s`; `review_autofix` p95 `3745.5s`; CI cancellation cluster around `1818s`; `orchestrate_poll` p50 `129.5s`.
- **Top failure modes:** no direct failures, but `17` cancellations overall; CI has `11/15` cancelled; review has long system waits and cancelled-before-first-step cases.
- **Highest-cost drivers:** large reviewer prompts (`50,531` tokens in run `32811740761`), unmeasured OpenRouter token/cache usage, and avoidable no-op poll work.
- **Top 3 prioritized actions:**
  1. Shard `tests/test_orchestrate_poll_process.py` CI execution with per-subset timing and watchdog logs.
  2. Add no-op fast path for `orchestrate_poll` before checkout/state/memory writes.
  3. Add review/autofix phase heartbeat, first-step lag, and concurrency-cancel diagnostics.

## Metrics Appendix

### Overall run metrics

| Metric | Value |
|---|---:|
| Total runs | 412 |
| Success | 218 |
| Failures | 0 |
| Cancelled | 17 |
| Other/skipped/etc. | 177 |
| Success rate | 52.9% |
| Failure rate | 0.0% |
| Avg duration | 242.0s |
| p50 duration | 9.0s |
| p95 duration | 1817.0s |

### Major workflow-family metrics

| Workflow family | Runs | Success | Cancelled | Other | p50 | p95 | Avg |
|---|---:|---:|---:|---:|---:|---:|---:|
| orchestrate_poll | 78 | 78 | 0 | 0 | 129.5s | 156.5s | 134.3s |
| review_autofix | 68 | 63 | 3 | 2 | 8.0s | 3745.5s | 649.8s |
| clarify | 53 | 5 | 0 | 48 | 2.0s | 161.4s | 20.6s |
| implement | 46 | 4 | 3 | 39 | 1.0s | 593.0s | 57.0s |
| plan | 46 | 4 | 0 | 42 | 1.0s | 283.2s | 28.5s |
| ci | 15 | 4 | 11 | 0 | 1818.0s | 1820.3s | 1749.3s |
| copilot_pull_request_reviewer | 9 | 9 | 0 | 0 | 131.0s | 321.0s | 174.7s |
| test_and_mark_stable | 1 | 1 | 0 | 0 | 5370.0s | 5370.0s | 5370.0s |

### Cost, cache, and wall-clock telemetry

| Metric | Value |
|---|---:|
| Runs with log telemetry | 115 |
| Codex calls | 4 |
| Codex tokens used | 8,104 |
| OpenRouter calls | 67 |
| OpenRouter total tokens | 0 reported |
| OR cache read tokens | 0 reported |
| OR cache write tokens | 0 reported |
| Cache hit rate | null / unavailable |
| Wall-clock p50 | 14,000ms |
| Wall-clock p99 | 4,117,880ms |
| Break-glass count | 0 |
| Context-budget warnings | 0 |

### Semble / Serena telemetry

| System / target | Queries | Logged bytes | Fallbacks | Runtime fallbacks | Probe OK | Probe failed | Probe skipped |
|---|---:|---:|---:|---:|---:|---:|---:|
| Semble / reviewer-context | 7 | 98,649 | 0 | 0 | n/a | n/a | n/a |
| Semble / contract-test overflow | 0 | 0 | 40 | 0 | n/a | n/a | n/a |
| Serena / all | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Other MCP structured telemetry | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

### Deep-log Semble runtime query samples

| Run ID | Workflow | Target | Chunks | Bytes | Duration |
|---:|---|---|---:|---:|---:|
| 32717542227 | review_autofix | reviewer-context | 12 | 15,185 | 524ms |
| 32807381395 | review_autofix | reviewer-context | 12 | 15,596 | 612ms |
| 32810013028 | review_autofix | reviewer-context | 12 | 14,551 | 529ms |
| 32811753208 | review_autofix | reviewer-context | 12 | 8,705 | 530ms |
| 32814410068 | review_autofix | reviewer-context | 12 | 14,465 | 415ms |

### AI memory metrics

| Metric | Value |
|---|---:|
| Valid telemetry events | 42 |
| Retrieve operations | 7 |
| Retrieve hit rate | 100% |
| Avg retrieve estimated tokens | 1,396.9 |
| Avg retrieve budget | 1,400 |
| Keyword method distribution | `llm=7`, `plain=0`, `none=0` |
| Fail-open entries | 1 benign `no_linked_issues` |
| Push retries >1 | 2 |

### GH API audit summary

| Hotspot | Evidence | Risk | Recommended logging |
|---|---|---|---|
| CI/process tests | `32818345444` cancelled at `1818s` | reruns, slow feedback | per-test durations, current-test heartbeat |
| E2E workflow polling | `32716299147` `e2e-smoke-test` `5189.2s` | call volume, long waits | poll count, interval, status histogram, rate-limit remaining |
| Review peer query | `AUTOFIX_PEER_QUERY_FAILED ... reason=api_error` | missed peer PR context | method/path/status/duration/retry |
| No-op poll | `0 active tracking issue(s)` but ~107–131s runs | wasted writes/API work | active-count, skipped-heavy-steps, memory-write duration |

## Deep Audit — Workflows & Scripts (2026-08-25)

### Section 1: Bug & Correctness Sweep

- **ID:** BUG-001  
  **File path and line range:** `scripts/orchestrate_poll_process.sh:10716-10721`, `scripts/orchestrate_poll_process.sh:13118-13121`, `scripts/orchestrate_poll_process.sh:14178-14189`  
  **Severity:** High  
  **Category tag:** `bug`  
  **Description:** Three `gh api "search/issues"` call sites pass `-f q=...` / `-f per_page=...` without an explicit GET method. This conflicts with the repo’s documented pattern in `scripts/gh_helpers.sh:1198-1205`, which states that `gh api` infers POST whenever `-f/-F` is present unless `-X GET` is supplied. Evidence: marker fallback calls at `10719-10720`, state reconstruction at `13119-13121`, and deferred duplicate backstop at `14187-14189`. Inference: these paths can silently return empty/fallback results or skip duplicate detection when the search endpoint rejects inferred POSTs.  
  **Recommended fix:** Add `-X GET` / `--method GET` to all three `search/issues` call sites. Keep the existing GraphQL alias fast path at `10704-10708`; this fix preserves current call count but makes fallback calls valid. Current call count: up to 4 REST search calls across these paths. Proposed call count: same 4, but explicit GET. Existing pattern to copy: `autofix_retrigger_has_inflight_peer` in `scripts/gh_helpers.sh:1198-1212`.

- **ID:** SHELL-001  
  **File path and line range:** `scripts/orchestrate_poll_process.sh:17026-17030`  
  **Severity:** Low  
  **Category tag:** `shellcheck`  
  **Description:** `_sorted_issue_nums="$(printf '%s\n' ${ISSUE_NUMS} | sort -un)"` expands `${ISSUE_NUMS}` unquoted. If the variable ever contains glob characters or unexpected whitespace, shell word-splitting/glob expansion occurs before `printf`. The following loop also iterates unquoted words from `_sorted_issue_nums`.  
  **Recommended fix:** Normalize explicitly without glob exposure, e.g. print the variable quoted, split on whitespace/newlines deliberately, then sort: `printf '%s\n' "${ISSUE_NUMS}" | tr '[:space:]' '\n' | sed '/^$/d' | sort -un`. Keep numeric validation before using each issue number.

### Section 2: GitHub API Call Redundancy Audit

Prior report sections already cover the long release-validation polling loop and no-op `orchestrate_poll` overhead; those are not duplicated here. API method-correctness finding `BUG-001` is counted in Section 1.

- **ID:** BATCH-001  
  **File path and line range:** `.github/workflows/review_autofix.yml:936-963`, `.github/workflows/review_autofix.yml:990-1029`  
  **Severity:** Medium  
  **Category tag:** `api-batching`  
  **Description:** The standalone validation dispatch path correctly includes labels in the GraphQL `closingIssuesReferences` query at `943-948`, but when it falls back to parsing issue numbers from PR text it builds `labels: null` at `990`, then reads labels once per linked issue with `gh issue view` at `999`.  
  **Recommended fix:** For the body/title fallback path, batch label retrieval before the loop. Current fallback call count: `N` label reads + up to 1 workflow dispatch + up to `N` label edits. Proposed call count: `ceil(N/25)` GraphQL label reads + up to 1 dispatch + up to `N` edits. Extend the existing batching pattern `_fetch_issue_labels_batch_graphql` from `scripts/orchestrate_poll_process.sh:2700-2777`, or move an equivalent helper into `scripts/gh_helpers.sh`.

- **ID:** BATCH-002  
  **File path and line range:** `.github/workflows/review_autofix.yml:5087-5116`, `.github/workflows/review_autofix.yml:5277-5306`, `scripts/label_helpers.sh:166-217`  
  **Severity:** Medium  
  **Category tag:** `api-batching`  
  **Description:** Both “mark linked issues ready-to-merge” and “mark linked issues review-blocked” loop over linked issues and call `set_issue_phase_label_resilient` for each issue. The canonical helper performs `ensure_label_exists`, a labels GET at `183-184`, and a labels PUT at `205-207` on the normal path.  
  **Recommended fix:** Add a cached/batched helper such as `set_issue_phase_labels_resilient_cached <repo> <target_label> <issue_numbers_json>`, using `_fetch_issue_labels_batch_graphql` for the read side and one PUT per changed issue. Current normal-path call count: roughly `1 + 3N` API attempts per step (`ensure_label_exists` before the loop, per-issue ensure, per-issue labels GET, per-issue labels PUT). Proposed call count: `1 + ceil(N/25) + N` for one label ensure, batched reads, and necessary writes.

- **ID:** API-001  
  **File path and line range:** `.github/workflows/review_autofix.yml:6279-6298`, `scripts/gh_helpers.sh:1187-1215`, `scripts/gh_helpers.sh:1300-1334`  
  **Severity:** Low  
  **Category tag:** `api-redundancy`  
  **Description:** The editor-changes-lost redispatch step can call `autofix_retrigger_has_inflight_peer` and then `autofix_changes_lost_head_retry_consumed` back-to-back. Both helpers issue the same branch-scoped `GET /repos/{repo}/actions/runs` query with `branch=<head_branch>&per_page=30`; comments at `1300-1304` acknowledge the overlap.  
  **Recommended fix:** Add `autofix_fetch_branch_runs_once <head_branch> <current_run_id>` returning the fetched run list, then pass that JSON to both predicates. Current call count: 2 REST calls on this path. Proposed call count: 1 REST call. Existing pattern to extend: `gh_api_json_to_file` / JSON-transform helpers in `scripts/gh_helpers.sh`.

- **ID:** BATCH-003  
  **File path and line range:** `scripts/orchestrate_poll_process.sh:8480-8616`  
  **Severity:** Low  
  **Category tag:** `api-batching`  
  **Description:** `_load_actions_runs_cached` uses one cached in-progress `actions/runs` request at `8555`, then two additional status-specific requests for queued and completed runs at `8609` and `8614` on a cache miss. A one-call replacement may not preserve status coverage when completed runs dominate the first page [NEEDS VERIFICATION].  
  **Recommended fix:** Evaluate replacing the three status-specific REST calls with one wider `actions/runs?per_page=100` client-filtered fetch, or retain the three calls only when the one-call result cannot prove coverage. Current call count: 3 REST calls per cache miss. Proposed call count: 1 REST call when coverage is safe, fallback to 3 otherwise. Existing pattern to extend: `_ACTIONS_RUNS_BLOB_CACHE` at `8471-8484`.

### Section 3: Code Duplication & Modularization Opportunities

- **ID:** DUP-001  
  **File path and line range:** `.github/workflows/clarify.yml:215-351`, `.github/workflows/plan.yml:278-413`, `.github/workflows/implement.yml:851-1148`, `.github/workflows/orchestrate.yml:340-469`, `.github/workflows/orchestrate_clarify_respond.yml:277-412`, `.github/workflows/orchestrate_poll.yml:331-523`, `.github/workflows/review_autofix.yml:1502-1587`, `.github/workflows/validate.yml:202-424`, `scripts/stage_workflow_support.sh:16-31`  
  **Severity:** Medium  
  **Category tag:** `duplication`  
  **Description:** Six workflows still inline large “Stage workflow support files” loops with similar `wf_source`, fallback source selection, `install`, optional support assets, prompt staging, schema staging, and `.gitignore` handling. `review_autofix.yml` and `validate.yml` already demonstrate the shared-helper pattern by invoking `scripts/stage_workflow_support.sh`.  
  **Recommended fix:** Make `scripts/stage_workflow_support.sh` the single owner. Add a manifest/profile interface such as `stage_workflow_support.sh <phase> --manifest <json>` and migrate `clarify`, `plan`, `implement`, `orchestrate`, `orchestrate_clarify_respond`, and `orchestrate_poll` to call it with phase-specific required/optional assets.

- **ID:** DUP-002  
  **File path and line range:** `.github/workflows/orchestrate.yml:1010-1029`, `.github/workflows/plan.yml:2009-2029`, `scripts/check_failure_triage.sh:56-79`, `scripts/implement_diagnose_post_codex_failure.sh:49-62`, `.github/workflows/review_autofix.yml:908-926`, `.github/workflows/review_autofix.yml:1112-1124`  
  **Severity:** Low  
  **Category tag:** `duplication`  
  **Description:** Multiple workflows/scripts duplicate fallback definitions for `_safe_gh_jq` and simplified `gh_retry`. The canonical implementations already exist in `scripts/gh_helpers.sh:407-462` and `scripts/gh_helpers.sh:548-562`, but call sites reimplement temp-file handling and retry behavior inconsistently.  
  **Recommended fix:** Add a tiny bootstrap helper, e.g. `scripts/bootstrap_gh_helpers.sh`, with `load_gh_helpers <support_dir>`, and make inline callers source it. Function contract: define `gh_retry`, `_safe_gh_jq`, and `gh_api_json_to_file` or fail open with one audited fallback body.

- **ID:** DUP-003  
  **File path and line range:** `.github/workflows/review_autofix.yml:5045-5084`, `.github/workflows/review_autofix.yml:5228-5274`, `scripts/label_helpers.sh:130-217`  
  **Severity:** Low  
  **Category tag:** `duplication`  
  **Description:** Late review/autofix steps duplicate reduced `ensure_label_exists` / `set_issue_phase_label_resilient` fallback bodies instead of reusing the canonical label helper. This is understandable because helper artifacts may be removed by cleanup, but it creates two divergent label-update implementations.  
  **Recommended fix:** Preserve `label_helpers.sh` through the fetched-manifest cleanup or stage a minimal fallback file owned by `scripts/label_helpers.sh`. Keep the public signatures `ensure_label_exists <label_name> [repo]` and `set_issue_phase_label_resilient <issue_number> <target_label> [repo]`, then remove inline fallback bodies.

### Section 4: Expression Size Limit Risk Assessment

No workflow file exceeds the 800 KB early-warning threshold. Largest observed workflow sizes: `review_autofix.yml` ≈451,578 chars, `test-and-mark-stable.yml` ≈291,779 chars, `implement.yml` ≈282,386 chars.

- **ID:** EXPR-001  
  **File path and line range:** `.github/workflows/plan.yml:1009-1299`  
  **Severity:** High  
  **Category tag:** `expression-limit`  
  **Description:** The interpolated `run:` block for “Run Codex planning” is estimated at 19,616 characters, leaving only ~1,384 characters before GitHub’s 21,000-character expression limit. It embeds a large prompt heredoc at `1015-1221` and contains `${{ github.repository }}` interpolations at `1246` and `1288`, making the whole block expression-sensitive.  
  **Recommended fix:** Move the prompt body to `prompts/mode-plan.txt` or a dedicated external script, and keep the workflow step as a small `bash scripts/run_plan_codex.sh` invocation. Prefer reading/rendering prompt files at runtime with `scripts/render_prompt.sh`.

- **ID:** EXPR-002  
  **File path and line range:** `.github/workflows/implement.yml:3677-3922`  
  **Severity:** High  
  **Category tag:** `expression-limit`  
  **Description:** The destructive/scope block handler’s interpolated `run:` block is estimated at 18,345 characters, leaving ~2,655 characters of headroom. The block contains multiple `${{ github.repository }}` / `${{ github.run_id }}` interpolations and large inline comment/Telegram templates.  
  **Recommended fix:** Extract to `scripts/implement_handle_guard_block.sh` with explicit env inputs for repository, run URL, issue number, destructive fields, and scope fields. If helper cleanup is the reason for inlining, preserve the small handler script separately from cleanup rather than growing the workflow expression.

- **ID:** EXPR-003  
  **File path and line range:** `.github/workflows/implement.yml:853-1148`  
  **Severity:** Medium  
  **Category tag:** `expression-limit`  
  **Description:** The “Stage workflow support files” interpolated `run:` block is estimated at 15,966 characters, leaving ~5,034 characters of headroom. This is also part of the duplicated support-staging pattern covered by `DUP-001`.  
  **Recommended fix:** Replace the inline staging block with `scripts/stage_workflow_support.sh implement --manifest <manifest.json>`, following `validate.yml:202-424` and `review_autofix.yml:1502-1517`.

- **ID:** EXPR-004  
  **File path and line range:** `.github/workflows/memory_maintenance.yml:45-391`  
  **Severity:** Medium  
  **Category tag:** `expression-limit`  
  **Description:** The repository learnings extraction step is estimated at 15,168 characters, leaving ~5,832 characters of headroom. It embeds two large Python heredocs plus `${{ github.run_id }}`, `${{ github.run_attempt }}`, and `${{ github.actor }}` interpolations near `362-378`.  
  **Recommended fix:** Move the Python logic into `scripts/memory_maintenance_extract_learnings.py` and keep the workflow step to environment setup plus `python3 -B scripts/memory_maintenance_extract_learnings.py`.

### Section 5: Cross-Cutting Concerns

No `TODO`, `FIXME`, or `HACK` markers were found under `.github/workflows` or top-level `scripts/*.sh` / `scripts/*.py`. No high-confidence dead-code finding is reported in this pass.

- **ID:** CONSIST-001  
  **File path and line range:** `scripts/tg_helpers.sh:103-145`, `.github/workflows/validation-refresh.yml:262-267`, `.github/workflows/test-and-mark-stable.yml:5436-5440`, `.github/workflows/implement.yml:3774-3778`, `.github/workflows/implement.yml:3908-3912`, `.github/workflows/implement.yml:4036-4040`, `.github/workflows/issue_pr_status.yml:625-631`, `.github/workflows/update_workflows.yml:642-648`  
  **Severity:** Low  
  **Category tag:** `consistency`  
  **Description:** Telegram sends are centralized in `tg_helpers.sh`, including alert-level filtering and consistent `disable_web_page_preview`, but several workflows still call `curl` directly. Some direct calls are intentional fallback paths when helpers are unavailable; others bypass the helper unconditionally [NEEDS VERIFICATION].  
  **Recommended fix:** Route direct sends through `tg_send_msg` / `tg_send_tracked` where helpers are available. For bootstrap or cleanup-sensitive paths, add a minimal `tg_send_msg_fallback <text> <level>` shim in `tg_helpers.sh` and source/copy only that shim.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 |  |
| High | 3 | BUG-001, EXPR-001, EXPR-002 |
| Medium | 5 | BATCH-001, BATCH-002, DUP-001, EXPR-003, EXPR-004 |
| Low | 6 | API-001, BATCH-003, DUP-002, DUP-003, SHELL-001, CONSIST-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 8 | Large |
| Expression size reduction | 4 | Medium |
| Medium/Low fixes | 7 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-08-25)

### Safety Tag Legend
`SAFE_TO_MERGE` means the implement stage may consolidate directly; `NEEDS_VERIFICATION` means the overlap is real but freshness/error/cache semantics must be checked first; `RISKY_SKIP` means the opportunity is visible but must not be auto-implemented because it touches pagination, race-recovery, poller, or fail-closed safety paths.

### Consolidation Candidates (MERGE-###)

- **ID:** MERGE-001  
  **Safety tag:** `RISKY_SKIP`  
  **File path and line ranges:** `scripts/review_enable_auto_merge.sh:30-76`, `scripts/review_enable_auto_merge.sh:78-140`  
  **Current call count:** 2  
  **Proposed call count:** 1 only if equivalence is proven; otherwise keep 2  
  **Endpoint(s):** `GET /repos/{repo}/issues/{pr_number}/labels?per_page=100` via `--paginate`; `GET /repos/{repo}/pulls/{pr_number}`  
  **Evidence:** The script first fetches labels solely to detect `e2e-smoke-test`, then fetches full PR metadata for head ref/body checks:
  ```bash
  if PR_LABELS_RAW="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/labels?per_page=100" --jq '.[].name' 2>"${_label_err_file}")"; then
  ```
  ```bash
  if ! _ORCH_PR_META_JSON="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" 2>"${_orch_pr_meta_err_file}")"; then
  ```
  **Proposed fix:** Do not auto-change. If manual review proves the PR REST payload’s `.labels[].name` is complete for the repo’s label cardinality, fetch `repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}` first, derive `PR_LABELS_RAW` from `_ORCH_PR_META_JSON`, and reuse the same payload for `_orch_pr_head_ref` / `_orch_pr_body`.  
  **Safety rationale:** `RISKY_SKIP` because the current label call is explicitly paginated and fail-closed for the e2e auto-merge suppressor; changing it could alter page-boundary and safety semantics.  
  **Downstream signal:** Do not auto-implement; manually verify label completeness for PR REST payloads with >100 labels or design an equivalent single GraphQL query with identical fail-closed behavior.

- **ID:** MERGE-002  
  **Safety tag:** `RISKY_SKIP`  
  **File path and line ranges:** `.github/workflows/clarify.yml:455-486`  
  **Current call count:** 2 when `SEMANTIC_CACHE_BACKEND != none`; 1 otherwise  
  **Proposed call count:** 1 when semantic cache is enabled; unchanged when disabled  
  **Endpoint(s):** `GET /repos/{repo}/issues/{issue_number}/comments?sort=created&direction=asc&per_page=50`; paginated `GET /repos/{repo}/issues/{issue_number}/comments?sort=created&direction=asc&per_page=100`  
  **Evidence:** The bounded prompt-context fetch is immediately followed by a full paginated fetch for thread history when semantic cache is enabled:
  ```bash
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=50" > "${ISSUE_COMMENTS_FILE}"
  ```
  ```bash
  if ! gh_retry gh api --paginate --slurp "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=100" \
  ```
  **Proposed fix:** In the `Fetch issue comments` step, when semantic cache is enabled, fetch the paginated comments once, write `THREAD_HISTORY_FILE` from the full array, and write `ISSUE_COMMENTS_FILE` from the first 50 comments to preserve bounded prompt behavior.  
  **Safety rationale:** `RISKY_SKIP` because one involved call uses `--paginate` and the two current calls have different failure handling contracts.  
  **Downstream signal:** Do not auto-implement; manually verify page ordering, first-50 equivalence, and that a failed unified fetch fails the prompt-context path the same way as the current first call.

### Redundant Re-Fetch (REUSE-###)

- **ID:** REUSE-001  
  **Safety tag:** `NEEDS_VERIFICATION`  
  **File path and line ranges:** `scripts/review_collect_pr_metadata.sh:251-279`, `.github/workflows/review_autofix.yml:4955-4999`, `.github/workflows/review_autofix.yml:5179-5213`, `scripts/review_rb_judge.sh:831-852`  
  **Current call count:** 2 GraphQL linked-issue reads on the review-blocked judge path when early metadata collection succeeded  
  **Proposed call count:** 1 GraphQL linked-issue read, with live fallback only when cache is missing/invalid  
  **Endpoint(s):** GraphQL `repository.pullRequest(number).closingIssuesReferences(first:50)`  
  **Evidence:** `review_collect_pr_metadata.sh` already fetches linked issues and exports `LINKED_ISSUES_JSON`:
  ```bash
  if gh_retry "${_linked_tmp}" api graphql \
    -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){closingIssuesReferences(first:50){nodes{number title body}}}}}' \
  ```
  ```bash
  printf 'LINKED_ISSUES_JSON=%s\n' "${_linked_numbers}" >> "${GITHUB_ENV}"
  ```
  The later cache step explicitly treats that env var as reusable, but `review_rb_judge.sh` still re-fetches closing references before falling back:
  ```bash
  ISSUE_NUMBERS="$(gh_retry gh api graphql \
    -f query='query($owner:String!, $name:String!, $number:Int!) { repository(owner:$owner, name:$name) { pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number } } } } }' \
  ```
  **Proposed fix:** Extend `scripts/review_collect_pr_metadata.sh` to export an explicit `LINKED_ISSUES_CACHE_KNOWN=true` on successful GraphQL fetch, then update `scripts/review_rb_judge.sh` to initialize `ISSUE_NUMBERS` from valid `LINKED_ISSUES_JSON` before calling live GraphQL. Preserve the live GraphQL fallback for missing, malformed, or unknown cache state.  
  **Safety rationale:** `NEEDS_VERIFICATION` because the reuse crosses workflow-step/script boundaries and must preserve the existing freshness behavior for PR body edits between metadata collection and judge execution.  
  **Downstream signal:** Verify whether linked-issue references are allowed to change between `review_collect_pr_metadata.sh` and `review_rb_judge.sh`; test linked, unlinked, empty-cache, malformed-cache, and judge retry cases before implementation.

### Dead Calls (DEAD-API-###)

- **ID:** DEAD-API-001  
  **Safety tag:** `RISKY_SKIP`  
  **File path and line ranges:** `scripts/orchestrate_poll_process.sh:17949-17963`, `scripts/orchestrate_poll_process.sh:17964-18024`  
  **Current call count:** 1  
  **Proposed call count:** 0  
  **Endpoint(s):** `GET /repos/{repo}` for `.default_branch`  
  **Evidence:** The standalone PR conflict sweep assigns `DEFAULT_BRANCH`, but the subsequent loop uses `S_BASE`, `S_HEAD`, and per-PR REST payload fields; the assigned variable is not consumed downstream in this sweep:
  ```bash
  STANDALONE_PRS="$(gh_retry gh pr list \
    --repo "${GITHUB_REPOSITORY}" \
    --state open \
    --json number,headRefName,baseRefName \
    --limit 100 2>/dev/null || echo "[]")"
  ```
  ```bash
  DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
  ```
  **Proposed fix:** Remove the `DEFAULT_BRANCH=...` assignment in the standalone PR conflict sweep if manual review confirms no hidden dependency via sourced traps, later log parsing, or future fall-through use.  
  **Safety rationale:** `RISKY_SKIP` because the call is inside `scripts/orchestrate_poll_process.sh`, which the prompt designates as race-sensitive poller code.  
  **Downstream signal:** Do not auto-implement; manually confirm the variable remains unused across the full sweep and that removing it does not alter expected diagnostic output.

### Cross-References to Deep Audit Section

- BATCH-001: `NEEDS_VERIFICATION` — Agreed; batching fallback issue-label reads is plausible, but body/title fallback semantics and label freshness need validation.
- BATCH-002: `RISKY_SKIP` — Agreed on overlap, but the canonical helper uses paginated label reads and label mutation semantics, so this must not be auto-implemented.
- API-001: `NEEDS_VERIFICATION` — Agreed; both helpers hit the same branch-scoped Actions runs endpoint, but their opposite fail-open/fail-closed behavior must be preserved explicitly.
- BATCH-003: `RISKY_SKIP` — Agreed; this is inside `orchestrate_poll_process.sh` and involves status-specific Actions run reads where page-boundary coverage is uncertain.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| `SAFE_TO_MERGE` | 0 | — |
| `NEEDS_VERIFICATION` | 3 | REUSE-001, BATCH-001, API-001 |
| `RISKY_SKIP` | 5 | MERGE-001, MERGE-002, DEAD-API-001, BATCH-002, BATCH-003 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
