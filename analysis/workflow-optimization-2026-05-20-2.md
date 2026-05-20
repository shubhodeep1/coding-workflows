## Executive Summary

- `review_autofix` is the dominant bottleneck. In run `26146860961` (`Internal: AI Review & Autofix`), `Collect PR check-run failures` took `1200.4s`, `Run reviewer models` took `1495.1s`, and `Apply fixes with editor model` took `589.1s`—`3284.6s` of a `3497s` run (`93.9%`). **Estimated impact:** save `15–25 min` on slow review runs. **Confidence:** high.
- Small PRs are over-reviewed. Run `26149111119` (`Codex PR Self-Healing Semantic Agent`) had `files=3 additions=4` yet still used the full 6-model reviewer roster plus `gpt-5.4` editor and `gpt-5.4-mini` summariser, and still took `1274s`. **Estimated impact:** cut small-diff review cost/latency by `30–50%` and `8–12 min`. **Confidence:** high.
- CI is serialized into one job. `.github/workflows/ci.yml` has a single `lint` job with many sequential steps; successful runs `26149950534` and `26150442692` took `963s` and `971s`, both dominated by `lint`. **Estimated impact:** save `5–8 min` per CI run by sharding. **Confidence:** high.
- `test_and_mark_stable` is the main reliability incident: `33` failures in `34` runs (`97.1%` family failure rate). **Inference:** `scripts/orchestrate_poll_process.sh:4452-4467` dispatches `test-and-mark-stable.yml` without `--ref stable`, while `.github/workflows/test-and-mark-stable.yml:115-123` hard-rejects non-`stable` refs. **Estimated impact:** remove most release-path failures. **Confidence:** medium, because `14` recent failures have missing log archives.
- AI memory retrieval is effectively non-working for reviewer context: `7/7` retrieves had `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`. Root cause is visible in `.github/workflows/review_autofix.yml:1477-1486`, which passes only `--pr-number`. **Estimated impact:** medium quality/relevance gain, modest token savings. **Confidence:** high.
- Semble is not the cost problem. Selected deep-dive logs show only `26` operational `SEMBLE_QUERY` lines (`240,981` logged bytes total) and `5` `SEMBLE_FALLBACK` lines, all from one successful contract-style run (`26142167478`) with a missing test binary. Serena showed `0` operational query/fallback/probe lines and sampled review runs explicitly logged it disabled/unavailable. **Estimated impact:** avoid a bad optimization. **Confidence:** high.

## Speed Optimizations

1. **Critical path: shorten and narrow the review check-run wait loop**
   - **Evidence:** Run `26146860961`, step `step-027-review_codex-agent_Collect_PR_check-run_failures_CI_lint_autofix_context.log`, spent `1200.364s` polling, printed `59` wait lines, then ended with `CHECK_RUNS_WAIT_TIMEOUT reached after 1200s with 1 check-run(s) still queued/in_progress`. It produced only `578` bytes of check-run context.
   - **Root cause:** `.github/workflows/review_autofix.yml:1937-2011` waits up to `1200s` for *all* queued/in-progress check-runs, polling every `20s`, even though the step is already designed to fail open.
   - **Exact change:** Lower the default `CHECK_RUNS_WAIT_TIMEOUT_SECS` from `1200` to `180–300`, and filter `_in_flight` to only required checks for autofix context (for example CI/lint/autofix-relevant names), not every queued check on the SHA.
   - **Estimated time savings:** `900–1020s` on outlier runs; also reduces dozens of API polls per run.
   - **Implementation risk:** low; timeout already snapshots partial data and continues.

2. **Critical path: make reviewer pass 2 actually size-aware**
   - **Evidence:** `.github/workflows/review_autofix.yml:109-118` explicitly says the pass-2 diff gate is currently a no-op because both small and large defaults are `xhigh`. In run `26146860961`, pass 2 alone took `643.1s`. In run `26149111119`, a `3`-file / `4`-addition PR still used the full 6-model roster.
   - **Root cause:** Full second-pass reviewer fan-out runs even on tiny diffs.
   - **Exact change:** For small diffs, set `REVIEWER_PASS2_REASONING_SMALL` below `xhigh` and/or skip pass 2 entirely unless pass 1 finds high-confidence blocking issues, reviewers disagree, or workflow/shell files changed.
   - **Estimated time savings:** `6–11 min` on small-diff `review_autofix` runs.
   - **Implementation risk:** medium; keep full pass 2 for high-risk file types.

3. **Critical path: split CI into parallel jobs**
   - **Evidence:** `.github/workflows/ci.yml` has one `lint` job (`lines 13-260`) running workflow lint, Python compile checks, and many test groups sequentially. Successful runs `26149950534` and `26150442692` took `963s` and `971s`, and both log summaries say `lint` dominated runtime.
   - **Root cause:** No job-level parallelism.
   - **Exact change:** Split into at least 3 jobs: `workflow-lint`, `orchestrator-tests`, and `review/validation-contract-tests`, or use a small matrix to shard the Python test files.
   - **Estimated time savings:** `300–480s` per CI run.
   - **Implementation risk:** low-medium; setup duplication is the main tradeoff.

4. **Secondary: reduce queue pressure by shrinking heavy review runs**
   - **Evidence:** `review_autofix` system logs show queue waits of `2090.928s` in run `26110263808` and `2002.101s` in run `26145607995` before the job even started.
   - **Root cause:** Hosted-runner pressure plus long-lived review jobs.
   - **Exact change:** Prioritize items 1-3 first; then track queue wait p95 and gate tiny-diff reviews more aggressively so the queue is not filled with full-panel runs.
   - **Estimated time savings:** indirect, but necessary to shrink `30–35 min` queue outliers.
   - **Implementation risk:** low.

**Micro-optimizations not worth prioritizing first:** Copilot artifact cleanup in run `26150445505` does an `N+1` artifact list/delete loop, but it only cost fractions of a second in the sampled run.

## Cost Optimizations

1. **Highest ROI: shrink the reviewer roster on small diffs**
   - **Evidence:** Run `26149111119` (`files=3 additions=4`) still used reviewers `minimax`, `kimi`, `deepseek`, `mistral-small`, `qwen`, and `grok-4.20`. In sampled slow runs, `deepseek/deepseek-v4-pro` was usually the slowest reviewer:  
     - `26120104978`: `621.6s` pass 1, `613.2s` pass 2  
     - `26146174572`: `866.2s` pass 1, `590.4s` pass 2  
     - `26146860961`: `738.6s` pass 1, `476.8s` pass 2  
     - `26109784758`: `729.2s` pass 1, `521.3s` pass 2 (second-slowest because `mistral-small` retried)
   - **Root cause:** The full expensive roster is used even where diff size is tiny.
   - **Exact change:** For small diffs, run a 2-3 model pass 1, keep `deepseek` as escalation/tie-breaker only, and preserve the full panel only for large diffs or risky file types.
   - **Estimated savings:** `30–50%` reviewer-model spend on small PRs, plus `5–12 min` latency reduction.
   - **Quality-risk notes:** medium; mitigate by escalating on reviewer disagreement or workflow/script edits.

2. **Stop paying for a full pass 2 when pass 1 already converged**
   - **Evidence:** In run `26146860961`, pass 1 took `850.6s`, pass 2 took `643.1s`, pass-1 summariser prompt size was `33,998` bytes, and pass-2 summariser prompt size was `63,773` bytes.
   - **Root cause:** Repeated reviewer/summariser prompt expansion on low-risk changes.
   - **Exact change:** Use pass-1 consensus as a gate: skip pass 2 when pass 1 has no high-confidence blockers and no cross-reviewer disagreement.
   - **Estimated savings:** one full summariser call plus up to six reviewer calls per skipped pass 2.
   - **Quality-risk notes:** medium; keep pass 2 for workflow/shell/merge-risk changes.

3. **Semble appears net-helpful; do not optimize it away**
   - **Evidence:** Selected deep-dive logs show `26` `SEMBLE_QUERY` lines totaling `240,981` bytes and `12,327ms`. By target: `reviewer-context` `7` queries / `82,077` bytes, `overflow` `17` / `137,031`, `conflict-resolver-context` `2` / `21,873`. One editor prompt in run `26146860961` was `301,988` bytes by itself.
   - **Interpretation:** **Inference:** Semble is probably reducing downstream prompt expansion rather than causing it.
   - **Exact change:** Keep Semble enabled. Only add within-run memoization if the same overflow file is queried repeatedly in one run.
   - **Estimated savings:** low direct savings; avoids a likely negative regression in prompt size.
   - **Quality-risk notes:** low. The `5` fallbacks were healthy fail-open behavior in one successful test run, not evidence of a broken rollout.

4. **Serena is not adding spend in this window**
   - **Evidence:** Operational telemetry showed `0` `SERENA_QUERY`, `0` `SERENA_FALLBACK`, and `0` `SERENA_PROBE` lines. Run `26149111119` logged `SERENA_ENABLED: false`; run `26146860961` step `step-050-review_codex-agent_Log_token_usage.log` logged `SERENA_AVAILABLE: false`.
   - **Interpretation:** Serena is disabled/unavailable here, not noisily failing.
   - **Exact change:** No cost action required until Serena is re-enabled; when it is, emit per-tool response-byte stats so its replacement value can be measured.
   - **Estimated savings:** none now.
   - **Quality-risk notes:** none.

5. **Prompt-cache savings are blocked by missing metrics**
   - **Evidence:** `OPENROUTER_PROMPT_CACHE_DISABLED: false` is present in sampled review runs, and `.github/workflows/review_autofix.yml:1444-1446` intentionally builds a stable cacheable prefix. But `workflow_log_report.json` has `0` occurrences of `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens`. Step `step-050-review_codex-agent_Log_token_usage.log` only prints editor summary size.
   - **Root cause:** No provider token/cache counters are emitted.
   - **Exact change:** Emit per-call provider counters for reviewer/summariser/editor invocations, then aggregate them in the existing token-usage/log-summary step.
   - **Estimated savings:** measurement-enabler; no credible dollar estimate yet.
   - **Quality-risk notes:** low.

**Avoidable reruns note:** `review_autofix` family cancellation counts should not be treated as pure spend yet. Near-synchronous pairs like `26143023700` (cancelled `2671s`) → `26143031259` (success `2669s`, `14s` later) and `26145988191` → `26145997432` line up with intentional `workflow_dispatch` continuation logic in `.github/workflows/review_autofix.yml:5208-5399`. Measure parent/child handoff separately before charging this to “waste.”

## Reliability Improvements

1. **Highest impact — add `--ref stable` to comprehensive release dispatch**  
   **Inference**
   - **Failure evidence:** `test_and_mark_stable` had `34` runs, `33` failures, `1` success, and a `97.1%` failure rate. `14` recent failures are missing log archives and only surfaced as `partial_data:missing_log_archive ... HTTP 404`, so direct step-level confirmation is unavailable.
   - **Root cause category:** branch/ref mismatch on dispatch.
   - **Exact fix:** In `scripts/orchestrate_poll_process.sh:4452-4467`, add `--ref stable` to `gh workflow run test-and-mark-stable.yml`. This matches `.github/workflows/promote-main-to-stable.yml:298-318`, which already does so, and satisfies `.github/workflows/test-and-mark-stable.yml:115-123`, which rejects any non-`stable` ref.
   - **Expected reliability impact:** likely removes the dominant release-path failure mode.
   - **Rollback / fail-open:** safe; if `stable` is absent, dispatch fails immediately with a clear existing error.

2. **Fix the active CI regression immediately**
   - **Failure evidence:** CI run `26151309804` failed at `lint / Orchestrate poll process unit tests`; `step-006-lint_Orchestrate_poll_process_unit_tests.log` shows `NameError: name 'json_line' is not defined` in `tests/test_orchestrate_integration_ahead_by_gate.py:589`.
   - **Root cause category:** test regression / unguarded variable use.
   - **Exact fix:** Initialize or guard `json_line` before parsing, and add a direct assertion that the test produced a JSON line before `json.loads(...)`.
   - **Expected reliability impact:** removes the current `ci` family failure trigger.
   - **Rollback / fail-open:** none needed.

3. **Fix malformed AI memory telemetry emission**
   - **Failure evidence:** Selected deep-dive logs contain malformed concatenated lines such as run `26120104978` `step-001-codex-agent.log:7117` and run `26146174572` `step-001-review_codex-agent.log:7141`, where record JSON is glued directly to `AI_MEMORY_TELEMETRY: {...}`.
   - **Root cause category:** observability/logging bug.
   - **Exact fix:** Emit telemetry on stderr or force a newline before `AI_MEMORY_TELEMETRY:` in the `record-candidate` path.
   - **Expected reliability impact:** improves parser correctness and dashboard trust; low direct workflow-failure effect.
   - **Rollback / fail-open:** telemetry-only; very safe.

4. **Keep Semble fail-open, but mark expected fallback noise**
   - **Failure evidence:** All `5` `SEMBLE_FALLBACK` lines were in successful run `26142167478`, step `step-002-validate-scripts.log`, all `target=overflow`, all caused by a missing path ending in `missing_semble`.
   - **Root cause category:** expected fail-open contract-test behavior.
   - **Exact fix:** Keep runtime fail-open semantics; in the validation harness, either stub the binary explicitly or mark the fallback lines as expected so they do not look like rollout failures.
   - **Expected reliability impact:** reduces false alarms, not user-facing failures.
   - **Rollback / fail-open:** do **not** remove fail-open behavior.

**Collector note:** the log collector already handles missing archive 404s sanely. `tests/test_collect_workflow_logs.py:500-541` shows missing archives are cached after a single retry (`call_retries == [1]`), and `tests/test_collect_workflow_logs.py:697-728` verifies sanitized detail fields. The bigger problem is the upstream missing archive itself, not collector thrash.

## AI Memory Health

- AI memory telemetry was observed only in sampled `review_autofix` deep dives: runs `26109784758`, `26110263808`, `26120104978`, `26123989450`, `26145607995`, `26146174572`, and `26146860961`.
- Observed operations: `record-run-event=14`, `retrieve=7`, `record-candidate=7`.
- **Retrieve hit rate:** `0%` (`0/7` had `records_selected > 0`).
- **Average `estimated_tokens`:** `0`.
- **Budget comparison:** telemetry does not emit budget. Code fallback is `900` tokens in `scripts/ai_memory_lib.py:1291`, but that is only a code reference, not an observed per-run metric.
- **`keyword_method` distribution:** `none=7`, `plain=0`, `llm=0`.
- **`fail_open: true` retrieves observed:** `0`.
- **`enabled: false` retrieves observed:** `0`.
- **Push retry outlier:** one `record-candidate` event used `push_attempts=2` in run `26109784758`; the other six used `push_attempts=1`.

**Why retrieval is failing**
- `.github/workflows/review_autofix.yml:1477-1486` calls `memory_retrieve ... --role reviewer --pr-number "${PR_NUMBER}"`.
- `scripts/ai_memory_lib.py:1204-1205` returns `keyword_method="none"` when both title and body are empty.
- The CLI already supports `--issue-title` and `--issue-body-file` (`scripts/ai_memory.py:1014-1020`, `177-185`).

**Recommendation**
- Pass `PR_TITLE` and `PR_BODY` into `memory_retrieve` so retrieval can at least use `plain` keywords, and optionally `llm` keywords when the API key is present.
- After that change, track:
  - retrieve hit rate,
  - average `estimated_tokens`,
  - `keyword_method` mix,
  - and whether malformed telemetry lines disappear.

## GH API Call Audit

**Method note:** counts below are lower bounds from selected deep-dive logs after de-duplicating aggregate `step-001` logs where split-step logs existed. Polling loops undercount because `gh` command echoes appear once while the loop executes many times.

1. **`review_autofix` check-run polling is the main API hotspot**
   - **Evidence:** Lower-bound sampled counts for `review_autofix` were `190` REST, `7` GraphQL, `22` `gh workflow run`, `12` `gh pr merge`, `2` `gh issue edit`. But run `26146860961` step `step-027-...check-run...` printed only one `gh api --paginate --slurp` command while the log shows `59` wait iterations across `1200s`, implying about `60` real check-run polls to `repos/{repo}/commits/{sha}/check-runs`.
   - **Redundancy pattern:** repeated polling of the same endpoint on the same SHA.
   - **Concrete change:** Reuse a single snapshot per polling interval, filter to required checks only, and lower the timeout budget. This can remove `50+` REST calls from a single outlier run and materially lower rate-limit risk.

2. **`test_and_mark_stable` `e2e-smoke-test` repeatedly fetches the same run and PR state**
   - **Evidence:** Run `26142167478`, step `step-010-e2e-smoke-test.log`, had a lower-bound `71` REST calls and `4` workflow dispatches. The step repeatedly issues:
     - `STATUS=$(gh api "repos/${REPO}/actions/runs/${RID}" --jq '.status // ""')`
     - `FINAL_STATUS=$(gh api "repos/${REPO}/actions/runs/${RID}" --jq '.status // "unknown"')`
     in multiple places (`413/426`, `755/768`, `1134/1147`, `2385/2398`, `4884/4897` in the log), plus repeated `pulls/${PR_NUMBER}` fetches (`1317`, `1319`, `1346`, `5027`, `5092`, `5515`).
   - **Redundancy pattern:** same-resource re-fetch inside the same step/loop.
   - **Concrete change:** Fetch each run JSON once per poll iteration and parse status/conclusion locally; cache PR metadata within the step; delay `jobs` endpoint calls until the run is terminal.
   - **Estimated call-count reduction:** roughly `20–30` REST calls from this one step alone.
   - **Rate-limit risk reduction:** meaningful, especially on release validation days.

3. **Copilot artifact cleanup is an `N+1` pattern**
   - **Evidence:** Run `26150445505` step `step-029-Cleanup_artifacts_Get_artifact_IDs.log` lists artifact IDs via `gh api /actions/runs/.../artifacts`, then `step-030-Cleanup_artifacts_Delete_artifacts.log` loops `gh api -X DELETE` once per artifact.
   - **Redundancy pattern:** list once, then delete each item individually.
   - **Concrete change:** Keep artifact count low, skip cleanup when the list is empty, and prefer one retained result artifact where possible.
   - **Estimated call-count reduction:** low today; keep as a micro-optimization.

4. **Positive hygiene already exists—preserve it**
   - **Evidence:** `review_autofix` and `test_and_mark_stable` both contain rate-limit-aware wrappers around `gh api`, and the collector has explicit missing-archive caching/sanitization tests.
   - **Assessment:** No confirmed `403`/`429` hit lines were observed in the selected deep dives; the current issue is redundant polling, not visible rate-limit storms.
   - **Concrete change:** Keep `gh_retry`/backoff behavior; focus optimization on loop structure and reuse.

## Prompt Cache & Memory System

- **What is working**
  - `.github/workflows/review_autofix.yml:1435-1475` intentionally pre-assembles a stable prefix so provider-side prompt caching can work.
  - In run `26146860961`, `step-049-review_codex-agent_Apply_fixes_with_editor_model.log` logged:
    - `./pre_assembled_static.txt bytes: 106825`
    - `Editor prompt bytes: 301988`
  - That means about a third of the editor prompt is a stable reusable prefix.

- **What is not measurable**
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` appears in sampled review runs, so cache is intended to be on.
  - But `workflow_log_report.json` has `0` occurrences of:
    - `prompt_tokens`
    - `completion_tokens`
    - `total_tokens`
    - `cache_creation_input_tokens`
    - `cache_read_input_tokens`
  - The existing `Log token usage` step only reports editor summary size, not provider token/cache counters.

- **Likely fragmentation status**
  - The workflow is already doing the right thing structurally by keeping dynamic per-PR content after the static prefix.
  - I did **not** find evidence that Semble or Serena is fragmenting the cache path in this window.
  - The main issue is observability, not obviously bad cache layout.

- **Concrete improvements**
  1. Emit provider token/cache counters per reviewer/summariser/editor call.
  2. Log a stable-prefix hash and byte count so cache-hit analysis can be correlated to prefix stability.
  3. Keep dynamic noise after `pre_assembled_static.txt`; do not prepend volatile per-run fields ahead of it.
  4. Fix reviewer memory inputs so retrieval can return real prior context rather than always empty output.

- **Estimated impact**
  - **Tokens:** unknown until metrics exist.
  - **Latency:** likely moderate if cache hits are currently happening; unquantifiable now.
  - **Reliability:** high improvement in cost debugging and regression detection.

## Orchestrator Health

- **Control-plane fan-out is noisy but cheap**
  - `clarify`: `136` runs, `131` other/skipped.
  - `plan`: `129` runs, `125` other/skipped.
  - `implement`: `129` runs, `123` other/skipped, `2` cancelled.
  - `orchestrate_clarify_respond`: `129` runs, `128` other/skipped.
  - Recent runs `26151307084` (`clarify`), `26151306902` (`plan`), `26151306904` (`implement`), and `26151306998` (`orchestrate_clarify_respond`) all exited in `0–2s` because their `if:` conditions evaluated false.
  - **Assessment:** this is UI noise and observability clutter, not a compute bottleneck.

- **`review_autofix` family metrics mix real work with intentional continuation/handoff**
  - `.github/workflows/review_autofix.yml:5208-5399` explicitly re-dispatches review via `workflow_dispatch` after commits.
  - Near-synchronous pairs like:
    - `26143023700` cancelled `2671s` → `26143031259` success `2669s` (`14s` later)
    - `26145988191` cancelled `2316s` → `26145997432` success `2311s` (`13s` later)
    - `26120093075` cancelled `4502s` → `26120104978` success `4509s` (`13s` later)
    strongly suggest raw family-level cancel counts overstate true “abandoned work.”  
    **Inference:** some of this is designed hand-off/continuation behavior, not accidental churn.
  - **Smallest safe mitigation:** split dashboards and collector summaries by `workflow_name` and dispatch source, and track paired hand-off cancellations separately from true aborts.

- **Conflict-handling is not the current pain point**
  - Only `2` `SEMBLE_QUERY target=conflict-resolver-context` lines were seen in the selected deep dives.
  - No recurring merge-conflict failure pattern dominated this window.

- **Observable indicators teams should track**
  - `review_autofix` queue wait p95
  - `CHECK_RUNS_WAIT_TIMEOUT` count
  - paired internal/direct review hand-offs
  - `test_and_mark_stable` failure rate
  - AI memory retrieve hit rate
  - emitted prompt-cache/token counters once added

## Pipeline Flow Bottlenecks

1. **Clarify → plan → implement**
   - Mostly control-plane skips, not compute.
   - Bottleneck type: **none significant**.
   - Recommendation: reduce dashboard noise before changing behavior.

2. **Review / autofix**
   - Dominant bottleneck by far.
   - Bottleneck types:
     - **Queueing:** `2002–2091s` waits before job start (`26145607995`, `26110263808`)
     - **Compute:** reviewer/editor stack dominates runtime (`26146860961`)
     - **Retry/continuation overhead:** explicit workflow re-dispatch after pushes
   - Recommendation order: shorten check-run wait, size-gate pass 2, then tune model roster.

3. **CI**
   - Secondary compute bottleneck.
   - Bottleneck type: **serialized validation**.
   - Recommendation: shard the single `lint` job.

4. **Validate / release path**
   - Main failure bottleneck for stable promotion.
   - Bottleneck types:
     - **Immediate failures:** `test_and_mark_stable` `33/34` failures
     - **API-heavy smoke checks:** `e2e-smoke-test` step call volume
   - Recommendation: fix dispatch ref first, then de-duplicate smoke-test API polling.

5. **Merge/conflict overhead**
   - Not dominant in this window.
   - Evidence: low Semble conflict-resolver activity and no conflict-heavy failure cluster.

**Best end-to-end fix order**
1. Fix `test_and_mark_stable` dispatch ref.
2. Shorten/narrow `review_autofix` check-run wait.
3. Make review pass 2 truly size-aware.
4. Split CI into parallel jobs.
5. De-duplicate release smoke-test API polling.
6. Add token/cache and hand-off observability so future tuning is measurable.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` runtime and queueing (`avg 1151.0s`, `p95 3353.8s`)
  - serial CI (`avg 837.5s`, `p95 927.5s`)
  - release-path smoke/API overhead in `test_and_mark_stable`

- **Top failure modes**
  - `test_and_mark_stable` `33/34` failures, many with missing log archives
  - CI regression in run `26151309804` (`json_line` `NameError`)
  - observability gaps: no prompt/cache counters, malformed AI memory telemetry lines

- **Highest-cost drivers**
  - 6-model, 2-pass reviewer stack on small diffs
  - `1200s` check-run wait budget before reviewers start
  - large editor/reviewer prompt bodies without measurable cache-hit data

- **Top 3 prioritized actions**
  1. In `review_autofix`, reduce the `CHECK_RUNS_WAIT_TIMEOUT_SECS` default and wait only on required checks.
  2. Make pass 2 and the slowest reviewer model(s) conditional on diff size/risk.
  3. Add `--ref stable` to `dispatch_comprehensive_release_workflow()` in `scripts/orchestrate_poll_process.sh`.

## Metrics Appendix

**Method notes**
- Repo/workflow-family metrics come from `summary.json` and `analysis/analysis_context.json`.
- GH API, MCP, and AI memory counts below are from selected deep-dive logs in `errors/`, `slow/`, and `recent/`, after de-duplicating aggregate `step-001` logs when split-step logs existed.
- GH API counts are **lower bounds** because looped requests do not echo every individual call.

| Repository | Runs | Success | Failure | Cancelled | Other | Failure rate | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 407 | 37 | 47 | 509 | 3.7% | 314.6 | 2.0 | 2316.7 |

| Workflow family | Runs | Success | Failure | Cancelled | Other | Failure rate | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cancel_on_pr_close | 15 | 15 | 0 | 0 | 0 | 0.0% | 8.5 | 9.0 | 10.3 |
| ci | 79 | 76 | 3 | 0 | 0 | 3.8% | 837.5 | 853.0 | 927.5 |
| clarify | 136 | 5 | 0 | 0 | 131 | 0.0% | 6.1 | 1.0 | 9.5 |
| copilot_pull_request_reviewer | 41 | 40 | 1 | 0 | 0 | 2.4% | 203.3 | 209.0 | 321.0 |
| forward_merge_stable_to_main | 8 | 8 | 0 | 0 | 0 | 0.0% | 27.4 | 27.5 | 32.3 |
| implement | 129 | 4 | 0 | 2 | 123 | 0.0% | 10.0 | 1.0 | 10.0 |
| integration_pr_readiness | 32 | 32 | 0 | 0 | 0 | 0.0% | 9.8 | 10.0 | 12.9 |
| issue_pr_status | 15 | 15 | 0 | 0 | 0 | 0.0% | 24.1 | 15.0 | 63.9 |
| lint_pr_body_auto_close | 29 | 29 | 0 | 0 | 0 | 0.0% | 9.5 | 10.0 | 12.0 |
| memory_maintenance | 1 | 1 | 0 | 0 | 0 | 0.0% | 36.0 | 36.0 | 36.0 |
| nightly_validation_selftest | 1 | 1 | 0 | 0 | 0 | 0.0% | 120.0 | 120.0 | 120.0 |
| orchestrate | 1 | 1 | 0 | 0 | 0 | 0.0% | 302.0 | 302.0 | 302.0 |
| orchestrate_clarify_respond | 129 | 1 | 0 | 0 | 128 | 0.0% | 1.4 | 1.0 | 2.0 |
| orchestrate_poll | 21 | 21 | 0 | 0 | 0 | 0.0% | 92.1 | 64.0 | 137.0 |
| plan | 129 | 4 | 0 | 0 | 125 | 0.0% | 4.5 | 1.0 | 6.0 |
| review_autofix | 195 | 149 | 0 | 45 | 1 | 0.0% | 1151.0 | 241.0 | 3353.8 |
| test_and_mark_stable | 34 | 1 | 33 | 0 | 0 | 97.1% | 140.4 | 0.0 | 0.0 |
| update_workflows | 1 | 0 | 0 | 0 | 1 | 0.0% | 2.0 | 2.0 | 2.0 |
| validate | 1 | 1 | 0 | 0 | 0 | 0.0% | 136.0 | 136.0 | 136.0 |
| validation_refresh | 2 | 2 | 0 | 0 | 0 | 0.0% | 318.0 | 318.0 | 342.3 |
| workflow_log_analysis | 1 | 1 | 0 | 0 | 0 | 0.0% | 3503.0 | 3503.0 | 3503.0 |

| Metric | Value | Evidence |
|---|---|---|
| Provider prompt/completion/total tokens | Not emitted in current log window | `workflow_log_report.json` contains 0 occurrences of `prompt_tokens`, `completion_tokens`, and `total_tokens`; `review_autofix` `step-050-...Log_token_usage.log` only logs editor summary size. |
| Prompt cache creation tokens | Not emitted | 0 occurrences of `cache_creation_input_tokens` in `workflow_log_report.json`. |
| Prompt cache read tokens | Not emitted | 0 occurrences of `cache_read_input_tokens` in `workflow_log_report.json`. |
| Prompt cache enabled flag | `false` for disable flag (cache enabled) | `OPENROUTER_PROMPT_CACHE_DISABLED: false` in sampled `review_autofix` runs. |
| Stable cacheable prefix size | 106825 bytes | Run `26146860961` `step-049-review_codex-agent_Apply_fixes_with_editor_model.log`. |
| Full editor prompt size | 301988 bytes | Same run/step. |
| Pass-1 summariser prompt size | 33998 bytes | Run `26146860961` `step-041-review_codex-agent_Run_reviewer_models.log`. |
| Pass-2 summariser prompt size | 63773 bytes | Same run/step. |

| Workflow family | REST `gh api` | GraphQL `gh api graphql` | `gh workflow run` | `gh pr merge` | `gh issue edit` | Notes |
|---|---:|---:|---:|---:|---:|---|
| review_autofix | 190 | 7 | 22 | 12 | 2 | Lower bound; actual count is higher because polling loops do not echo every request. |
| test_and_mark_stable | 122 | 0 | 11 | 0 | 0 | Dominated by `step-010-e2e-smoke-test.log` (`71` REST + `4` dispatches). |
| copilot_pull_request_reviewer | 7 | 0 | 0 | 0 | 0 | Includes artifact cleanup list/delete pattern. |
| Total (selected logs, excl. workflow_log_analysis) | 319 | 7 | 33 | 12 | 2 | Shell-echo sample only. |

| Step log | Workflow family | Sampled calls |
|---|---|---|
| `test_and_mark_stable` run `26142167478` `step-010-e2e-smoke-test.log` | test_and_mark_stable | `71` REST + `4` dispatches |
| `review_autofix` run `26123989450` `step-001-review_codex-agent.log` | review_autofix | `46` REST + `1` GraphQL + `1` issue edit + `1` merge |
| `review_autofix` run `26109784758` `step-001-review_codex-agent.log` | review_autofix | `23` REST + `1` GraphQL + `9` merges + `3` dispatches |
| `review_autofix` run `26146860961` `step-027 ...check-run...` | review_autofix | `~60` actual check-run polls inferred from `59` wait lines |

| Server | Event | Count | Logged bytes | Logged response bytes | Logged ms | Notes |
|---|---|---:|---:|---:|---:|---|
| Semble | QUERY | 26 | 240981 | — | 12327 | Targets: reviewer-context `7`, overflow `17`, conflict-resolver-context `2`. |
| Semble | FALLBACK | 5 | — | — | 0 | All in run `26142167478` `step-002-validate-scripts.log`; all `target=overflow`; missing test binary. |
| Semble | PROBE | 0 | — | — | — | No probe lines observed. |
| Serena | QUERY | 0 | — | — | — | No operational lines observed. |
| Serena | FALLBACK | 0 | — | — | — | No operational lines observed. |
| Serena | PROBE | 0 | — | — | — | No operational lines observed; sampled review runs show Serena disabled/unavailable. |

| Semble target | Query count | Bytes | ms |
|---|---:|---:|---:|
| reviewer-context | 7 | 82077 | 3392 |
| overflow | 17 | 137031 | 7984 |
| conflict-resolver-context | 2 | 21873 | 951 |

| MCP server | Targets observed | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---:|---:|---:|---|
| Semble | reviewer-context, overflow, conflict-resolver-context | 0 | 0 | 0 | No `SEMBLE_PROBE` lines observed. |
| Serena | none | 0 | 0 | 0 | No `SERENA_PROBE` lines observed; runtime logs show disabled/unavailable. |

Other MCP servers observed: none.

| AI memory metric | Value |
|---|---|
| Retrieve operations observed | 7 |
| Retrieve hit rate (`records_selected > 0`) | 0% (0/7) |
| Average `estimated_tokens` per retrieve | 0 |
| Budget comparison | Budget not emitted; code fallback is 900 tokens in `scripts/ai_memory_lib.py:1291`, so utilization cannot be measured directly. |
| `keyword_method` distribution | `none`: 7, `plain`: 0, `llm`: 0 |
| Retrieve `fail_open: true` observed | 0 |
| Retrieve `enabled: false` observed | 0 |
| Record-candidate push retries | 1 event with `push_attempts=2` (run `26109784758`); 6 events with `push_attempts=1` |

## Deep Audit — Workflows & Scripts (2026-05-20)

### Section 1: Bug & Correctness Sweep

- **ID** — `BUG-001`  
  **File path** — `.github/workflows/issue_pr_status.yml:253-351,383-386,501-517`; `.github/workflows/orchestrate.yml:1046-1057`; `scripts/orchestrate_lib.py:759-761`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — `issue_pr_status.yml` already classifies linked issues as tracking vs. managed in the `Update linked issue labels when PR closes` step, but it only exports `LINKED_ISSUE_NUMBERS`. The later `Send PR merged Telegram alert` step re-fetches each issue body and suppresses alerts only when the body contains the exact marker `Managed by: AI Orchestrator`. Child issues created by `orchestrate.yml` include that exact marker, but tracking issue bodies created by `scripts/orchestrate_lib.py` do not; they use different text plus the `ai:orchestrator-tracking` label. Result: PRs linked only to a tracking issue can still send the “non-orchestrator” merged alert even though the step comment says orchestrator projects should be handled by the poller.  
  **Recommended fix** — Export the earlier classification result (`TRACKING_ISSUES`, `MANAGED_ISSUES`, or a small JSON map) to `$GITHUB_ENV`/a temp file and make the alert step consult that cached classification instead of re-detecting from issue bodies.

- **ID** — `CONSIST-001`  
  **File path** — `.github/workflows/issue_pr_status.yml:196-210`; `.github/workflows/review_autofix.yml:533-543,642-655,4664-4677,4786-4799,5713-5726`; `scripts/review_rb_judge.sh:319-346`  
  **Severity** — High  
  **Category tag** — `consistency`  
  **Description** — `issue_pr_status.yml` explicitly narrowed its fallback issue-link regex to avoid treating bare prose like `issue #N` or `issues/N` as closing links, because that previously caused wrong orchestrator state transitions (`issue #1469`). `review_autofix.yml` and `scripts/review_rb_judge.sh` still use the older broad regex that matches those loose references. In `review_autofix`, those fallback paths can dispatch standalone validation and set `ai:ready-to-merge`/`ai:review-blocked`; in `review_rb_judge.sh`, the first inferred issue drives parent-body/label propagation. `review_autofix.yml:642-655` already documents that this regex shape is unsafe for incidental references, but the other fallback sites still use it.  
  **Recommended fix** — Extract one shared helper in `scripts/gh_helpers.sh`, e.g. `extract_closing_issue_numbers <repo> <input_file>`, using the narrowed `issue_pr_status.yml` regex, and replace every fallback parser in `review_autofix.yml` and `scripts/review_rb_judge.sh` with that helper.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`  
  **File path** — `.github/workflows/issue_pr_status.yml:253-351,383-386,501-517`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The close-handling step already performs `1` batched GraphQL lookup to classify linked issues, but the merged-alert step later loops over `LINKED_ISSUE_NUMBERS` and does `N` extra `_safe_gh_jq repos/.../issues/{n}` calls to re-read bodies. Current call count on that path is `1 + N`; proposed call count is `1` total (`0` additional after classification).  
  **Recommended fix** — Export the first step’s classification JSON and reuse it in the alert step. If a reusable helper is preferred, extend the aliased issue-detail batching pattern in `scripts/orchestrate_poll_process.sh:6824-6941` so both steps consume one cached batch payload.

- **ID** — `API-002`  
  **File path** — `.github/workflows/implement.yml:143-169,3921-3941`; `scripts/gh_helpers.sh:968-1116`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — On the duplicate-PR recovery path, `implement.yml` fetches the same issue timeline twice: once in `Safety check for existing PR`, then again after a failed `gh pr create`. Current call count on that failure path is `2` timeline fetches; proposed call count is `1`.  
  **Recommended fix** — Persist the first timeline payload and reuse it after `gh pr create` fails, or move support-script staging earlier and switch both sites to `gh_issue_timeline_with_cross_refs <owner> <repo> <issue_number>` from `scripts/gh_helpers.sh:968-1116`, which is already the repo’s GraphQL-first timeline helper.

- **ID** — `BATCH-001`  
  **File path** — `.github/workflows/review_autofix.yml:1582-1617,1683-1697`; `scripts/gh_helpers.sh:735-899`; `scripts/orchestrate_poll_process.sh:6824-6941`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — `Collect PR metadata` currently does `4` REST calls for PR payload/comments/reviews/review-comments, `1` GraphQL call for linked issues, then up to `20` per-issue REST calls in the body-text fallback loop. Current call count is `5 + N` (`N <= 20`); proposed call count is `2` worst-case (`1` consolidated PR context call + `1` aliased linked-issue detail batch), and `1` when `closingIssuesReferences` is non-empty.  
  **Recommended fix** — Extend `gh_pr_with_all_comments` in `scripts/gh_helpers.sh:735-899` to emit submitted reviews as well as meta/comments/review-comments, then batch fallback linked-issue hydration with the aliased GraphQL pattern already used by `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh:6824-6941`.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`  
  **File path** — `scripts/label_helpers.sh:110-197`; `scripts/orchestrate_poll_process.sh:1368-1496`; `scripts/validate_process.sh:919-1027`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The repo has three independent label-management implementations. They are near-duplicates, but they already diverge: the canonical helper returns non-zero on create failure, while the inline copies in `orchestrate_poll_process.sh` and `validate_process.sh` swallow failures and return success. That raises drift risk for label colors, descriptions, return codes, and phase-label replacement behavior.  
  **Recommended fix** — Make `scripts/label_helpers.sh` the single owner of `ensure_label_exists <label_name> [repo]`, `set_issue_phase_label_resilient <issue_number> <target_label> <repo>`, and `get_issue_labels_json <issue_number> [repo]`. Update `scripts/orchestrate_poll_process.sh` and `scripts/validate_process.sh` to source it and keep only thin workflow-specific wrappers for notification/logging.

- **ID** — `DUP-002`  
  **File path** — `scripts/review_apply_fixes.sh:435-444`; `scripts/review_conflict_prepare.sh:447-457`; `scripts/review_run_reviewers.sh:348-357`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — `append_semble_query_section()` is copied verbatim into three review scripts. The bodies are effectively identical.  
  **Recommended fix** — Move it into `scripts/semble_helpers.sh` as `append_semble_query_section <label> <path> [max_bytes]`, source that helper in all three scripts, and keep the callers unchanged apart from the source line.

- **ID** — `DUP-003`  
  **File path** — `.github/workflows/implement.yml:3548-3565`; `scripts/orchestrate_poll_process.sh:5443-5495`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The “ancestor no-op chain” walk (`Re-issued from #N` + scan parent comments for `produced no repository changes`) exists twice: once inline in `implement.yml`, once as `count_noop_ancestors()` in `orchestrate_poll_process.sh`. The logic is materially the same but already differs in helper usage (`gh api` vs `_safe_gh_jq`) and default depth handling.  
  **Recommended fix** — Move the logic into a shared helper module, e.g. `scripts/issue_lineage_helpers.sh` with `count_noop_ancestors <repo> <issue_num> [max_depth]`, and call it from both `implement.yml` and `scripts/orchestrate_poll_process.sh`.

- **ID** — `DUP-004`  
  **File path** — `.github/workflows/clarify.yml:164-257`; `.github/workflows/plan.yml:215-306`; `.github/workflows/orchestrate_clarify_respond.yml:204-309`; `.github/workflows/orchestrate.yml:289-406`; `.github/workflows/orchestrate_poll.yml:234-378`; `.github/workflows/implement.yml:756-891`; `.github/workflows/review_autofix.yml:878-1205`; `.github/workflows/validate.yml:207-583`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The repo repeats the same “resolve support ref → checkout primary/fallback/main → copy required scripts/prompts/schemas → write scripts/.gitignore” workflow bootstrap pattern across most major workflows. This is now one of the largest duplicated blocks in the repository and is also the direct source of several expression-size risks below.  
  **Recommended fix** — Extract a new bootstrap module, e.g. `scripts/workflow_support_bootstrap.sh`, with a CLI such as `bootstrap_workflow_support --script-ref <ref> --manifest <path> --dest-root <path> [--main-fallback]`. Update the listed workflows to pass per-workflow manifests instead of inlining long copy loops.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`  
  **File path** — `.github/workflows/review_autofix.yml:1498-1886`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The `Collect PR metadata` `run:` block is an estimated `21,049` characters with `${{ }}` interpolation, leaving about `-49` characters of headroom against the `21,000`-character limit. The estimate includes the local retry wrapper, multiple `gh` fetches, fallback parsing, and embedded Python writers. `[NEEDS VERIFICATION]`  
  **Recommended fix** — Extract the step into `scripts/review_collect_pr_metadata.sh` plus a small Python helper for comment-context rendering, leaving only env wiring in YAML.

- **ID** — `EXPR-002`  
  **File path** — `.github/workflows/validate.yml:211-583`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The `Fetch workflow support files` `run:` block is an estimated `20,817` characters with interpolation, leaving about `183` characters of headroom. The inline clone/copy/bootstrap logic is large enough that small edits could push the workflow over the hard limit. `[NEEDS VERIFICATION]`  
  **Recommended fix** — Move the entire support-fetch/bootstrap sequence into an external script such as `scripts/bootstrap_workflow_support.sh` or a composite action, and keep the workflow step as a thin wrapper.

- **ID** — `EXPR-003`  
  **File path** — `.github/workflows/review_autofix.yml:930-1205`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The `Stage workflow support files` block is an estimated `18,105` characters with interpolation, leaving about `2,895` characters of headroom. It is below the hard limit today, but the bootstrap list, prompt staging, and AI-memory staging are still large enough to be high-risk. `[NEEDS VERIFICATION]`  
  **Recommended fix** — Reuse the same extracted support-bootstrap script recommended in `DUP-004` so this workflow no longer carries the large manifest inline.

- **ID** — `EXPR-004`  
  **File path** — `.github/workflows/review_autofix.yml:1896-2209`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Collect PR check-run failures` block is an estimated `16,443` characters with interpolation, leaving about `4,557` characters of headroom. Most of the size comes from embedded shell polling plus the inline Python log-tail writer. `[NEEDS VERIFICATION]`  
  **Recommended fix** — Split the shell poller from the Python serializer, or move the serializer into `scripts/collect_check_run_context.py` and keep the workflow step focused on orchestration.

- **ID** — `EXPR-005`  
  **File path** — `.github/workflows/orchestrate_clarify_respond.yml:862-1144`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Parse and post answer` block is an estimated `15,141` characters with interpolation, leaving about `5,859` characters of headroom. The loop-guard logic, alerting, and memory bookkeeping all sit in one interpolated scalar. `[NEEDS VERIFICATION]`  
  **Recommended fix** — Split loop-guard escalation, answer posting, and memory finalization into separate steps, or extract them into `scripts/post_clarify_answer.sh`.

- Workflow file size check: no workflow exceeded `800 KB`; the largest is `.github/workflows/review_autofix.yml` at `360,754` bytes.

### Section 5: Cross-Cutting Concerns

- **ID** — `SHELL-001`  
  **File path** — `scripts/orchestrate_poll_process.sh:842-849,862-869`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — `_validate_phase_threshold()` clears invalid variables with `eval "${var_name}="`. The current call sites pass a fixed allowlist of variable names, so this is not an immediate injection bug, but it is still avoidable `eval` usage and a recurring shellcheck smell.  
  **Recommended fix** — Replace `eval` with `unset "${var_name}"` or `printf -v "${var_name}" '%s' ''` after validating `var_name` against the existing fixed allowlist.

- **ID** — `DEBT-001`  
  **File path** — `.github/workflows/workflow-log-analysis.yml:16-20`; `.github/workflows/comprehensive-test-and-release.yml:151-156`; `.github/workflows/orchestrate_poll.yml:7-20`; `.github/workflows/internal-orchestrate-poll.yml:16-18`; `.github/workflows/memory_maintenance.yml:39-55`; `scripts/validation_refresh_runner.py:390-399`  
  **Severity** — Low  
  **Category tag** — `tech-debt`  
  **Description** — Several documented no-op knobs are still exposed and, in some cases, still passed by internal callers: `workflow-log-analysis.yml` keeps `codex_mode` as a deprecated no-op while `comprehensive-test-and-release.yml` still dispatches it; `orchestrate_poll.yml` keeps `caller_workflow` as a deprecated no-op while the internal wrapper still passes it; `memory_maintenance.yml` still accepts/logs batch/Codex-related controls even though it unconditionally logs `batch_noop` and `codex_contract_noop`; `validation_refresh_runner.py` still exposes deprecated `--commit-message` and `--pr-title`. That compatibility surface is now maintenance overhead with no behavior behind it.  
  **Recommended fix** — Stop passing the deprecated knobs from internal callers first, then warn on use for one release window, then delete the no-op inputs/options or hard-fail when they are supplied.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 4 | `CONSIST-001`, `EXPR-001`, `EXPR-002`, `EXPR-003` |
| Medium | 9 | `BUG-001`, `API-001`, `API-002`, `BATCH-001`, `DUP-001`, `DUP-003`, `DUP-004`, `EXPR-004`, `EXPR-005` |
| Low | 3 | `DUP-002`, `SHELL-001`, `DEBT-001` |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 4 | Medium |
| API call optimization | 5 | Medium |
| Code modularization | 14 | Large |
| Expression size reduction | 3 | Large |
| Medium/Low fixes | 6 | Small |
