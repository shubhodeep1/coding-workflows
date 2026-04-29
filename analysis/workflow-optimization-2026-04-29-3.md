## Executive Summary

- **The single biggest latency and reliability problem is deterministic `implement` retry burn caused by optional MCP failure poisoning the Codex tool list.** Failed implement runs `25076992830` (4,984s), `25091341828` (3,437s), `25076576679` (1,571s), and `25092547530` (1,394s) all fail at `implement / implement` → `Run Codex implementation`, with logs showing the same Azure/OpenRouter 400 shape: invalid `function` / `undefined` tool payload after Context7 handshake failure. **Estimated impact:** save ~20–75 minutes per bad run and remove the largest failure source. **Confidence:** high.

- **The pipeline is generating substantial no-op orchestration churn.** In the 1,000-run window, `other_count` is 724; recent samples show these are overwhelmingly `skipped` fan-out runs across `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`, often created in the same second. **Estimated impact:** reduce queue noise and GitHub Actions overhead across hundreds of runs; smaller per-run savings, large aggregate savings. **Confidence:** high.

- **Prompt-cache observability is currently insufficient to optimize cost safely.** The only sampled OpenRouter usage lines were 8 `review_autofix_cache_probe` entries, and every numeric field was `na` (`prompt_tokens`, `completion_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`). **Estimated impact:** medium once fixed, because cache hit-rate and fragmentation cannot currently be measured. **Confidence:** high.

- **Serena is providing large value when it works, but review flows still fall back too often.** Implement run `25091341828` reports **1,086 Serena tool calls**, **84% efficiency**, and an estimated token delta of **~166,890 with Serena vs ~649,400 without**; review run `25090934047` reports only **42% efficiency**, with **820 file-based fallback ops vs 597 Serena calls**. **Estimated impact:** meaningful token and latency reduction by preserving Serena/Git while failing open on broken optional MCPs and reducing shell/file fallbacks. **Confidence:** high.

- **The release smoke workflow is over-polling GitHub and failing on brittle assertions.** `test_and_mark_stable` run `25088532565` failed after 5,967s; its `orchestrate-decompose-test` step polled Actions every 5s/20s and failed because decomposition produced **1 child issue instead of expected >=2**. `orphan-workflows-test` used a 7,200s deadline and observed cancellation after ~37 minutes. **Estimated impact:** moderate latency/API reduction and better release-test reliability. **Confidence:** medium-high.

## Speed Optimizations

### 1. Stop retrying deterministic MCP-broken `implement` calls
- **Evidence:** Failed runs `25076992830` (4,984s), `25091341828` (3,437s), `25076576679` (1,571s), and `25092547530` (1,394s) all fail in `Run Codex implementation`. The logs repeatedly show the same issue text and the same failure class: Azure/OpenRouter rejects a malformed tool entry because `function` is `undefined`, tied to a failed Context7 MCP initialize/handshake.
- **Root cause:** Optional MCP failure is reaching the model payload, and the workflow spends its full retry budget on a deterministic error.
- **Exact change:** In `setup_serena.sh` / Codex config generation, probe each optional MCP independently before registration. Only write `[mcp_servers.<name>]` blocks for MCPs that initialize successfully. Additionally, classify the exact malformed-tool Azure/OpenRouter signature as **non-retryable** and abort remaining Codex attempts after the first occurrence.
- **Estimated time savings:** ~20–75 minutes per affected implement run; this is the largest critical-path win in the sampled data.
- **Implementation risk:** low-medium if scoped to optional MCPs and exact known error signatures.

### 2. Suppress upstream no-op fan-out before spawning downstream runs
- **Evidence:** Across 1,000 runs, `other_count` is 724. Recent windows repeatedly show same-second clusters of skipped runs for `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` at `2026-04-29 05:53`, `05:41`, `05:35`, `05:30`, `05:29`, and `05:28` UTC. Family totals are similarly dominated by skip-like outcomes: `clarify` 197/213 `other`, `plan` 176/191 `other`, `implement` 160/192 `other`, `orchestrate_clarify_respond` 189/192 `other`.
- **Root cause:** Dispatch decisions are happening too late; downstream workflows start only to immediately skip.
- **Exact change:** Move eligibility checks into the dispatching workflow/job and gate fan-out there. Only dispatch `clarify/plan/implement/orchestrate_clarify_respond` when state transitions actually require them.
- **Estimated time savings:** small per event (seconds), but large aggregate reduction in queue churn and runner contention across hundreds of runs.
- **Implementation risk:** low, provided guards reuse the same predicates already used in downstream `if:` logic.
- **Type:** critical-path system win through reduced orchestration overhead, not a single-job micro-optimization.

### 3. Replace fixed-interval GitHub polling loops in smoke workflows with backoff + shared helper
- **Evidence:** In `25088532565`:
  - `orchestrate-decompose-test` polls run registration every 5s and status every 20s, failing after ~347s.
  - `validate-standalone-test` uses a 1,800s deadline with 5s registration polling and 20s status polling.
  - `orphan-workflows-test` uses a 7,200s deadline with 5s registration polling and 15s status polling; it observed cancellation after ~37 minutes.
- **Root cause:** Each smoke step re-implements a high-frequency `gh api` watch loop.
- **Exact change:** Extract a shared dispatch/watch helper with exponential backoff, jitter, and a lower steady-state poll rate after the first few checks. Reuse the same helper across `validate-standalone`, `orchestrate-decompose`, and `orphan-workflows-test`.
- **Estimated time savings:** low on successful fast runs, moderate on slow/cancelled runs; also reduces API pressure substantially.
- **Implementation risk:** low.

### 4. Shorten active review/autofix churn by deduping overlapping review runs
- **Evidence:** `review_autofix` has **41 cancelled runs out of 80 total**. Recent samples include cancelled runs lasting `360s` (`25092700769`), `70s` (`25093145756`), `44s` (`25092684998`), `39s` (`25093135506`), and `2s` (`25092849039`), alongside fast success at `28s` (`25093170308`) and much slower success at `591s` (`25092500097`).
- **Root cause:** Review runs are being started for PR states that are quickly superseded or cancelled.
- **Exact change:** Add stronger pre-dispatch dedupe keyed by PR + head SHA, and cancel-before-dispatch when a newer eligible run already exists.
- **Estimated time savings:** ~0.5–10 minutes per avoided cancelled review run.
- **Implementation risk:** low.

### 5. Consolidate duplicated issue metadata fetches in `implement`
- **Evidence:** Run `25076992830` fetches issue labels early (`issues/{n}`), then fetches full issue metadata into `ISSUE_META_FILE`, then fetches labels again later in the same non-mutating prefix.
- **Root cause:** The workflow is not consistently reusing the cached issue JSON it already wrote.
- **Exact change:** Reuse `ISSUE_META_FILE` for label extraction until the workflow mutates labels; refetch only after a mutation point.
- **Estimated time savings:** seconds per active implement run.
- **Implementation risk:** low.
- **Type:** local micro-optimization.

## Cost Optimizations

### 1. Eliminate avoidable 5x implement reruns on deterministic MCP/OpenRouter errors
- **Evidence:** Issue context embedded in implement logs explicitly states “**5/5 Codex attempts** return exit code 1.” The same failure signature is retried across runs `25076992830`, `25091341828`, `25076576679`, and `25092547530`.
- **Root cause:** The workflow treats a deterministic payload-validation failure as transient.
- **Exact change:** Mark the exact Azure/OpenRouter malformed-tool signature as non-retryable; fail once with targeted remediation.
- **Estimated savings:** highest token and dollar savings in the sample because it avoids repeated large-context retries on doomed runs.
- **Quality-risk notes:** low if matching is narrow and fail-open remains available for unknown MCP failures.

### 2. Preserve Serena/Git value while dropping only broken optional MCPs
- **Evidence:** Implement run `25091341828` reports:
  - `Serena tool calls`: **1,086**
  - `Serena efficiency`: **84%**
  - Estimated tokens: **~166,890 with Serena** vs **~649,400 without Serena**
  Review run `25090934047` reports:
  - `Serena tool calls`: **597**
  - `Serena efficiency`: **42%**
  - Estimated tokens: **~449,600 with Serena** vs **~777,300 without Serena**
- **Root cause:** Current failure mode forces an all-or-nothing outcome: a broken optional MCP can collapse the whole tool list, even though Serena and Git are still useful.
- **Exact change:** Register optional MCPs independently and omit only failed MCPs from config/payload. Do not disable Serena/Git broadly just to work around Context7 instability.
- **Estimated savings:** hundreds of thousands of estimated tokens in complex runs while also preventing full reruns.
- **Quality-risk notes:** low; preserves the higher-value semantic tools.

### 3. Stabilize prompt prefixes and stop re-feeding identical long issue context across retries
- **Evidence:** In `25076992830`, the same issue title/body and remediation block reappear repeatedly around retry windows before the job eventually fails. This indicates repeated prompt/context expansion across identical attempts.
- **Root cause:** Retry attempts appear to rebuild near-identical context rather than reusing a stable cached prefix.
- **Exact change:** Separate static issue/problem context from dynamic retry metadata; keep the static prefix byte-stable across retries so prompt-cache reuse is possible once usage metrics are emitted correctly.
- **Estimated savings:** medium-high on bad runs; most valuable on the long-tail implement failures.
- **Quality-risk notes:** low if retry-specific diagnostics stay in a small dynamic suffix.
- **Inference note:** this recommendation is inferred from repeated issue/context blocks in the logs because cache hit/miss totals are not currently emitted.

### 4. Route low-performing review flows toward Serena-first behavior and away from shell/file fallback
- **Evidence:** Review run `25090934047` shows **820 file-based fallback ops** versus **597 Serena calls**, with **42% efficiency** below the 50% threshold; top implement tools in `25091341828` still show heavy `sh` usage (`506` shell calls).
- **Root cause:** Review/edit flows are still spending too much work in shell/file fallback instead of semantic lookup/edit operations.
- **Exact change:** Raise warnings or hard-gate when shell/file fallback dominates while Serena is healthy; tighten prompts to prefer `find_symbol`, `get_symbols_overview`, `find_referencing_symbols`, and symbol edits.
- **Estimated savings:** medium token reduction and somewhat lower turnaround time in review/edit phases.
- **Quality-risk notes:** medium; keep fail-open fallback for repos/languages Serena cannot analyze well.

### 5. Model-selection and reasoning-level review: evidence is incomplete, so change narrowly
- **Evidence:** The only structured OpenRouter usage lines in sampled logs are 8 `review_autofix_cache_probe` lines using `minimax/minimax-m2.5`, and all numeric fields are `na`. No sampled telemetry exposes reasoning/thinking-level configuration or per-model token totals.
- **Root cause:** Insufficient cost telemetry by model and no reasoning-level instrumentation in the sampled logs.
- **Exact change:** Do **not** make a broad model swap yet. First emit real per-call usage for non-probe calls; then compare expensive models and any reasoning-level knobs. In the meantime, only narrow model changes to known-bad MCP combinations.
- **Estimated savings:** unknown until telemetry is fixed.
- **Quality-risk notes:** high if changed broadly without telemetry; low for narrow denylist/no-MCP routing.

## Reliability Improvements

### 1. Fail open on broken optional MCPs before Codex starts
- **Failure evidence:** Implement failures `25076992830`, `25091341828`, `25076576679`, `25092547530`; recent merged-fix commentary in `issue_pr_status` and `review_post-merge-validate-dispatch` states that optional MCP handshake failure leaves a tool stub with `function=undefined`, causing Azure/OpenRouter HTTP 400 and exhausting retries.
- **Root cause category:** configuration / dependency initialization.
- **Exact fix:** Handshake-probe each optional MCP server during setup and omit failed servers from the config; keep Serena/Git if healthy even when Context7 is not.
- **Expected reliability impact:** highest; should remove the dominant sampled failure mode in `implement`.
- **Rollback/fail-open considerations:** continue without the failed MCP and log degraded capability.

### 2. Make the known malformed-tool Azure/OpenRouter signature non-retryable
- **Failure evidence:** Repeated identical failures and “5/5 attempts” in implement logs.
- **Root cause category:** retry classification.
- **Exact fix:** Detect the exact validation error (`expected "function"`, `received undefined`, malformed `tools[n]`) and stop retrying after first occurrence.
- **Expected reliability impact:** moderate-high; fewer exhausted runs and faster operator feedback.
- **Rollback/fail-open considerations:** keep existing retries for unknown 4xx/5xx classes.

### 3. Harden `test_and_mark_stable` against brittle orchestrator decomposition assertions
- **Failure evidence:** Run `25088532565` failed because `orchestrate-decompose-test` found **1 child issue** when it expected `>=2`.
- **Root cause category:** brittle test assertion / orchestration semantics mismatch.
- **Exact fix:** Validate decomposition using dependency semantics or wave/state evidence rather than raw child-count only. If one issue legitimately absorbed both changes, the test should distinguish “bad decomposition” from “valid consolidation.”
- **Expected reliability impact:** moderate for release-gate stability.
- **Rollback/fail-open considerations:** keep current assertion behind a stricter smoke-test flag if needed.

### 4. Repair or quarantine failing nightly validation fixtures
- **Failure evidence:** `nightly_validation_selftest` run `25089252262` failed in 95s with `fixtures=3 passed=1 failed=2`.
- **Root cause category:** test fixture instability or outdated fixture expectations.
- **Exact fix:** Review the summary artifact referenced by the job and either fix the 2 failing fixtures or mark known-bad fixtures non-blocking until corrected.
- **Expected reliability impact:** localized but immediate for nightly signal quality.
- **Rollback/fail-open considerations:** if quarantined, keep reporting failed fixture counts so regressions remain visible.

### 5. Reduce overlapping/cancelled `review_autofix` executions
- **Failure evidence:** 41/80 `review_autofix` runs are cancelled; recent cancellations consumed 2s to 360s.
- **Root cause category:** orchestration dedupe / concurrency control.
- **Exact fix:** Strengthen dedupe before dispatch and tie runs to PR head SHA so superseded work is never started.
- **Expected reliability impact:** medium; fewer partial review cycles and less operational noise.
- **Rollback/fail-open considerations:** default to current behavior if dedupe state is unavailable.

## AI Memory Health

- **Telemetry was observed** in sampled deep-dive logs and direct run logs.
- From non-analysis sampled runs, I found **46 unique memory telemetry events**:
  - `processed-command-check`: 6
  - `processed-command-claim`: 6
  - `record-run-event`: 20
  - `retrieve`: 10
  - `record-candidate`: 4

### Retrieval effectiveness
- **Retrieve hit rate:** **60.0%** (`6/10` retrieves had `records_selected > 0`)
- **Average `estimated_tokens`:** **22.0**
- **Budget comparison:** **not available in sampled logs**; only `estimated_tokens` was emitted, so actual budget adherence cannot be verified from this window.
- **`keyword_method` distribution:**
  - `plain`: 6
  - `none`: 4
  - `llm`: 0 observed

### Flags and anomalies
- **Zero-record retrieves:** 4, all in reviewer flows (`review_autofix` sampled runs), each with `estimated_tokens=0` and `keyword_method=none`
- **`fail_open: true` entries:** none observed
- **`enabled: false` entries:** none observed in sampled telemetry
- **High push retry counts:** none observed; all sampled `push_attempts` were `1`

### Interpretation
- Implementation-phase memory is working reasonably: sampled implement retrieves returned 1–3 records with low estimated token cost (`28` or `80`).
- Reviewer memory retrieval is weak in the sampled window: four reviewer retrieves returned zero records and no keywording signal.
- Recommendation: improve reviewer-side retrieval prompts/keywords and emit retrieval budget fields explicitly so memory usefulness can be evaluated against target budgets.

## GH API Call Audit

### 1. Repeated dispatch/watch polling in smoke workflows is the clearest high-volume pattern
- **Evidence:** In `25088532565`:
  - `orchestrate-decompose-test` does:
    - `GET /actions/workflows/{file}/runs?per_page=1`
    - repeated registration polls every **5s**
    - repeated status polls every **20s**
  - `validate-standalone-test` repeats the same pattern with a **1,800s** deadline
  - `orphan-workflows-test` repeats the pattern with a **7,200s** deadline and **15s** status polls; the sampled run reached cancellation after ~37 minutes, implying roughly **150** status polls before termination
- **Redundancy pattern:** identical watch-loop logic copied across multiple steps.
- **Concrete change:** extract one shared dispatch/watch helper with exponential backoff and common JSON parsing.
- **Estimated call-count reduction:** **50–80%** for these smoke steps, depending on run length.
- **Rate-limit risk reduction:** high for long-running/cancelled smoke tests.

### 2. `implement` still re-fetches issue data inside the same run even after caching it
- **Evidence:** In `25076992830`, the workflow:
  - reads labels directly from `issues/{n}` early,
  - writes full issue JSON to `ISSUE_META_FILE`,
  - then reads labels again later with another `issues/{n}` call.
- **Redundancy pattern:** repeated same-resource REST fetch in a non-mutating prefix of the workflow.
- **Concrete change:** reuse `ISSUE_META_FILE` for subsequent label reads until a known mutation point.
- **Estimated call-count reduction:** roughly **1–2 issue REST calls per active implement run**.
- **Rate-limit risk reduction:** low-moderate, but easy and safe.

### 3. Cross-reference against repo API hygiene rules: current behavior partly violates the local standard
- **Evidence from workflow logs:** embedded repo guidance explicitly says:
  - batch or extend existing calls before adding new ones,
  - use cycle-local caches,
  - fail open on cache miss instead of tight retry loops.
- **Mismatch observed:** fixed-interval smoke polling and repeated same-scope fetches continue to appear in active workflows.
- **Concrete change:** apply the repo’s own API hygiene pattern uniformly:
  - single shared watch helper,
  - cycle-local issue/PR caches,
  - one GraphQL expansion instead of multiple REST lookups when possible.
- **Estimated call-count reduction:** moderate aggregate reduction across active orchestration paths.
- **Rate-limit risk reduction:** moderate.

### 4. Missed reuse opportunity in orchestration/review state lookups
- **Evidence:** The repo’s own audit notes embedded in run logs describe cached PR JSON reuse and centralized run-state loaders as desired patterns, but sampled smoke/test steps still do isolated per-step `gh api` polling loops instead of a shared cache or helper.
- **Redundancy pattern:** per-step watch state is not reused across adjacent smoke checks.
- **Concrete change:** persist dispatch metadata (`run_id`, start time, expected workflow) and use a generic watch helper artifact/output rather than rediscovering state from scratch.
- **Estimated call-count reduction:** low-moderate per test run, high over many release/test cycles.
- **Rate-limit risk reduction:** moderate.

## MCP & Serena Efficiency

### 1. Context7/Git are appended without health checks; only Serena is validated
- **Evidence:** `25091341828` step `Setup Serena MCP server` shows:
  - `Serena MCP server appended`
  - `Context7 MCP server appended`
  - `Git MCP server appended`
  - then only **Serena** gets explicit startup validation
  - setup diagnostics confirm Context7/Git sections are present
- **Efficiency issue:** one broken optional MCP can poison the full tool list, wasting the opportunity to use Serena/Git effectively.
- **Concrete change:** validate optional MCP servers independently and remove failed config blocks immediately.
- **Expected benefit:** preserves healthy semantic/Git tooling while avoiding full-run failure.

### 2. Serena is valuable in implement, but review is still fallback-heavy
- **Evidence:**
  - `25091341828` implement: **84% efficiency**, **1,086 Serena calls**, estimated token delta **~166,890 vs ~649,400**
  - `25090934047` review: **42% efficiency**, **597 Serena calls**, **820 file-based fallback ops**
- **Efficiency issue:** review flows still spend too much effort in file/shell fallback.
- **Concrete change:** tighten prompts and runtime checks to prefer semantic operations when Serena is healthy; emit warnings when fallback ops exceed Serena calls.
- **Expected benefit:** lower token volume and better turnaround in review/edit flows.

### 3. Tool-measurement path is inconsistent
- **Evidence:** In `25091341828`, `Log token usage and Serena stats` says **“No Serena tool usage stats found”**, but the next step still generates a detailed Serena efficiency report with call counts and token estimates.
- **Efficiency issue:** two telemetry paths disagree, which makes tuning difficult.
- **Concrete change:** unify on one source of Serena usage truth and emit both raw counters and derived efficiency in the same step.
- **Expected benefit:** better operational trust and easier regression detection.

### 4. Shell usage is still too dominant in some Serena-enabled runs
- **Evidence:** `25091341828` top Serena tools show `sh` at **506** calls, ahead of semantic tools.
- **Efficiency issue:** broad shell use usually means more token-heavy context acquisition and less precise edits.
- **Concrete change:** add a warning threshold when `sh` dominates semantic tools in Serena-enabled runs; audit prompts that trigger raw shell fallbacks first.
- **Expected benefit:** moderate token savings and more deterministic edits.
- **Parallelism opportunity:** symbol overview / pattern searches and git-status/diff reads can be parallelized safely before model execution, reducing turnaround without changing behavior.

## Prompt Cache & Memory System

### Prompt cache behavior
- **Observed behavior:** sampled OpenRouter cache telemetry is not actionable.
- **Evidence:** I found **8** `INFO: openrouter usage` lines, all in `review_autofix_cache_probe`, and every numeric field was `na`, including:
  - `prompt_tokens`
  - `completion_tokens`
  - `total_tokens`
  - `cache_creation_input_tokens`
  - `cache_read_input_tokens`
- **Interpretation:** cache is being probed, but real cache creation/read usage for model calls is not visible in sampled logs.

### Cache-fragmentation risks
- **Evidence:** Implement failure run `25076992830` repeatedly reprints the same issue body/problem statement around retry windows.
- **Likely fragmentation causes:**
  - retry-local noise mixed into the prompt prefix
  - repeated restatement of the same issue/problem block
  - multiple workflow variants with slightly different instruction preambles
- **Recommendation:** keep the static problem/context prefix byte-stable across retries and workflow variants; push dynamic retry diagnostics into a small suffix.

### Memory retrieval effectiveness
- **Observed:** implement retrieval is lightweight and usually useful; reviewer retrieval is frequently empty.
- **Evidence:** 10 retrieves total; 6 hits, 4 misses; reviewer misses all had `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`.
- **Recommendation:** improve reviewer retrieval keywords and emit retrieval budget fields alongside `estimated_tokens`.

### Reliability / fail-open behavior
- **Observed:** no `fail_open: true` and no `enabled: false` entries in sampled memory telemetry.
- **Interpretation:** memory itself was not the failing component in the sampled runs.
- **Recommendation:** add explicit cache/memory “disabled/fallback reason” fields to make degraded modes measurable.

### Estimated impact
- **Tokens:** potentially high once cache/memory instrumentation is corrected and stable prefixes are enforced
- **Latency:** moderate on retry-heavy jobs
- **Reliability:** moderate because better cache/memory visibility helps avoid blind tuning

## Orchestrator Health

### 1. The orchestrator is producing excessive skip churn
- **Evidence:** 724/1000 total runs are `other`; recent samples show these are mostly immediate `skipped` runs across `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`.
- **Operational pain point:** fan-out is happening before final eligibility is known.
- **Smallest safe mitigation:** move “should I dispatch?” checks upstream and keep downstream workflows for real work only.
- **Indicator to track:** skipped-run ratio by family, especially `clarify`, `plan`, `implement`, `orchestrate_clarify_respond`.

### 2. Review/autofix flow has heavy supersession/cancellation
- **Evidence:** 41/80 `review_autofix` runs cancelled; recent cancellations consumed from 2s to 360s.
- **Operational pain point:** overlapping review cycles waste work and muddy state transitions.
- **Smallest safe mitigation:** dedupe by PR + head SHA before launching review work.
- **Indicator to track:** cancellation rate and cancelled runtime minutes by family.

### 3. Family p50/p95 metrics are skewed by skipped runs and hide active-run pain
- **Evidence:** `implement` family p50 is `1s` and p95 `292.65s`, but active sampled runs include `3880s` success and `4984s` failure. `plan` and `clarify` also have `p50=1s` because skip-heavy outcomes dominate.
- **Operational pain point:** headline percentiles understate the real cost of active work.
- **Smallest safe mitigation:** split metrics into `active` vs `skipped/other` cohorts.
- **Indicator to track:** active-run p50/p95 by family.

### 4. Smoke/release orchestration has brittle completion semantics
- **Evidence:** `25088532565` failed due to a semantic mismatch in decomposition expectations, not an infrastructure failure.
- **Operational pain point:** healthy-but-different orchestrator behavior can fail release gating.
- **Smallest safe mitigation:** assert on invariant outcomes (dependency graph / wave semantics) rather than one narrow decomposition shape.
- **Indicator to track:** smoke-test assertion failure types by step.

## Pipeline Flow Bottlenecks

### 1. Clarify → Plan → Implement fan-out
- **Dominant bottleneck:** **queue/dispatch churn**, not compute
- **Evidence:** repeated same-second skipped runs across all four families; 724 `other` runs overall
- **Fix order:** upstream eligibility gating first

### 2. Implement
- **Dominant bottleneck:** **retry overhead**
- **Evidence:** bad runs spend 1,394s–4,984s failing in the same step with the same signature
- **Fix order:** MCP fail-open preflight, then non-retryable signature handling

### 3. Review / Autofix
- **Dominant bottlenecks:** **compute + superseded-work cancellations**
- **Evidence:** p95 `1530.7s`, slow successes up to `2811s`, 41/80 cancellations, review Serena efficiency only 42% in sampled slow run
- **Fix order:** dedupe/cancel-before-start, then improve Serena-first behavior

### 4. Validate / Orchestrate smoke tests
- **Dominant bottlenecks:** **polling overhead + brittle assertions**
- **Evidence:** multiple fixed-interval `gh api` watch loops and release-gate failure on one-child decomposition
- **Fix order:** shared watcher helper, then assertion redesign

### 5. CI
- **Dominant bottleneck:** **steady compute baseline**
- **Evidence:** `ci` p50 `604.5s`, p95 `641.3s`
- **Fix order:** no safe evidence-backed CI tuning surfaced from sampled logs because step-level CI breakdown is missing
- **Gap:** collect job/step durations inside CI before changing test/build layout

### 6. Merge/conflict overhead
- **Observed:** workflow logic clearly contains conflict-heal machinery, but sampled run logs do not quantify conflict retry counts directly
- **Recommendation:** emit explicit conflict-heal counters so merge/conflict cost can be separated from normal review latency

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- Deterministic `implement` MCP/OpenRouter failures consuming full retry budgets
- Skip-heavy orchestration fan-out creating hundreds of no-op runs
- `review_autofix` cancellation churn and long-tail runtime
- Release smoke workflows with repeated Actions polling and brittle decomposition checks

**Top failure modes**
- `implement / implement` → `Run Codex implementation` malformed tool payload after optional MCP failure (`25076992830`, `25091341828`, `25076576679`, `25092547530`)
- `test_and_mark_stable` → `orchestrate-decompose-test` fails because only 1 child issue was produced (`25088532565`)
- `nightly_validation_selftest` fails 2/3 fixtures (`25089252262`)

**Highest-cost drivers**
- Repeated doomed implement retries
- Review flows with low Serena efficiency and many cancelled runs
- Unmeasurable prompt-cache behavior due `na` probe-only telemetry
- Long-lived smoke-test polling loops

**Top 3 prioritized actions**
1. **Preflight optional MCPs and strip failed ones from Codex config; make the Azure malformed-tool signature non-retryable.**
2. **Move skip/no-op guards upstream so downstream workflows are dispatched only when needed.**
3. **Extract a shared GitHub Actions dispatch/watch helper with backoff and reuse cached issue/PR metadata inside active workflows.**

## Metrics Appendix

### Overall repository window

| Repo | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 221 | 6 | 49 | 724 | 0.6% | 105.7 | 1.0 | 605.0 |

> Note: `other` is not broken out in `summary.json`, but recent run samples show it is overwhelmingly `skipped`.

### Key workflow-family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| implement | 192 | 24 | 4 | 4 | 160 | 2.08% | 113.7 | 1.0 | 292.6 |
| review_autofix | 80 | 37 | 0 | 41 | 2 | 0.0% | 338.5 | 32.0 | 1530.7 |
| ci | 48 | 48 | 0 | 0 | 0 | 0.0% | 600.7 | 604.5 | 641.3 |
| clarify | 213 | 16 | 0 | 0 | 197 | 0.0% | 10.8 | 1.0 | 75.4 |
| plan | 191 | 15 | 0 | 0 | 176 | 0.0% | 20.4 | 1.0 | 186.0 |
| orchestrate_clarify_respond | 192 | 3 | 0 | 0 | 189 | 0.0% | 1.3 | 1.0 | 2.0 |
| orchestrate_poll | 16 | 16 | 0 | 0 | 0 | 0.0% | 136.4 | 83.5 | 441.3 |
| test_and_mark_stable | 3 | 0 | 1 | 2 | 0 | 33.3% | 2602.7 | 1305.0 | 5500.8 |
| nightly_validation_selftest | 1 | 0 | 1 | 0 | 0 | 100.0% | 95.0 | 95.0 | 95.0 |

### Notable failed / slow sampled runs

| Run ID | Family | Conclusion | Duration (s) | Failure point |
|---|---|---|---:|---|
| 25088532565 | test_and_mark_stable | failure | 5967 | `orchestrate-decompose-test` → `Dispatch internal-orchestrate.yml with multi-issue project` |
| 25076992830 | implement | failure | 4984 | `implement / implement` → `Run Codex implementation` |
| 25091341828 | implement | failure | 3437 | `implement / implement` → `Run Codex implementation` |
| 25076576679 | implement | failure | 1571 | `implement / implement` → `Run Codex implementation` |
| 25092547530 | implement | failure | 1394 | `implement / implement` → `Run Codex implementation` |
| 25089252262 | nightly_validation_selftest | failure | 95 | `validation-selftest` → `Run validation self-test matrix` |

### Sampled Serena efficiency metrics

| Run ID | Family | Serena tool calls | Efficiency | Estimated tokens with Serena | Estimated tokens without Serena | Notes |
|---|---|---:|---:|---:|---:|---|
| 25091341828 | implement | 1086 | 84% | ~166,890 | ~649,400 | Top tool was `sh` (506) |
| 25090934047 | review_autofix | 597 | 42% | ~449,600 | ~777,300 | 820 file-based fallback ops; below threshold |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Unique telemetry events sampled | 46 |
| Retrieve events | 10 |
| Retrieve hit rate | 60.0% |
| Avg estimated tokens per retrieve | 22.0 |
| Retrieve `keyword_method=plain` | 6 |
| Retrieve `keyword_method=none` | 4 |
| Retrieve `keyword_method=llm` | 0 |
| Zero-record retrieves | 4 |
| `fail_open: true` observed | 0 |
| `enabled: false` observed | 0 |
| Push attempts > 1 observed | 0 |

### Prompt cache telemetry summary

| Metric | Value |
|---|---:|
| Structured OpenRouter usage lines found | 8 |
| Phases represented | `review_autofix_cache_probe` only |
| Rows with numeric token/cache fields unavailable (`na`) | 8 / 8 |
| Actionable cache hit/miss totals available | No |

### GH API audit summary (observed patterns)

| Area | Evidence | Observed pattern | Estimated reduction |
|---|---|---|---|
| `test_and_mark_stable` smoke steps | `25088532565` | Fixed-interval `gh api` registration/status polling loops | 50–80% fewer Actions API calls in those steps |
| `implement` issue metadata | `25076992830` | Same issue fetched multiple times inside one run prefix | 1–2 REST calls saved per active implement run |
| Orchestration fan-out | Recent + family metrics | Downstream workflows launched then immediately skipped | Fewer runs, lower queue pressure, lower API/control-plane churn |

If you want, I can turn this into a shorter executive memo or a backlog-style prioritized action list with owners and rollout order.
