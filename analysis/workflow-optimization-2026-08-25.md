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
