## Executive Summary

- **CI is the dominant latency bottleneck.** The `ci` family ran 12 times with p50 `1441.5s` and p95 `1521.1s`; slow run `26933120515` lasted `1542s`, and an inferred `Orchestrate poll process unit tests` segment alone consumed about `1250s` of that run. **Estimated impact:** cut CI by ~`10–15m` if sharded well. **Confidence:** high.
- **A single CI regression caused both observed hard failures.** Runs `26930300571` and `26934022980` both failed in `lint / Review autofix review-pipeline plumbing contract test` with the same assertion about feeding Codex from `${attempt_prompt_file}`; both failures happened ~`228s` after run start. **Estimated impact:** restore CI from `10/12` to `12/12` in this window and save ~`4m` on repeat failures. **Confidence:** high.
- **Implement cost is extremely concentrated.** Run `26934436094` (`implement`) used `1,324,165` of the repo’s `1,326,192` Codex tokens (`99.85%`), across `12` Codex calls in `553s`. `implement.yml` defaults to `openai/gpt-5.4` with `xhigh` reasoning. **Estimated impact:** ~`15–35%` token reduction on implement-heavy runs with a guarded reasoning step-down. **Confidence:** high.
- **Prompt-cache telemetry is effectively blind.** Across `110` log-parsed runs, `cache_hit_rate` was always `null`, all `or_*` token/cache counters were `0`, and no actual `CONTEXT_BUDGET_WARN` events were counted. **Estimated impact:** medium; this blocks evidence-based cache and model tuning. **Confidence:** high.
- **Semble is not the main cost problem, but its fallback metrics are noisy.** Repo aggregate shows only `6` Semble queries / `59,930` logged bytes, versus `55` fallbacks. Deep-dive runtime fallbacks were `50` lines, all in CI test fixtures (`target=overflow`, `missing_semble`), not production rollout failures. **Estimated impact:** medium for alert quality, low for direct spend. **Confidence:** high.
- **`validate` failures are not yet actionable workflow evidence.** Runs `26930298218`, `26934020726`, and `26934701491` were all `failure`, `duration_seconds=0`, `jobs=null`, and log archive fetches returned HTTP `404`. **Estimated impact:** medium; likely an observability/collector gap before it is a runtime reliability issue. **Confidence:** medium.
- **Comment-trigger wrapper noise is very high.** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` account for `493/602` runs; `490` were skipped. **Estimated impact:** medium on dashboards/queue noise, low on critical-path runtime. **Confidence:** high.

## Speed Optimizations

1. **Shard the `Orchestrate poll process` CI suite across parallel jobs** *(critical path)*  
   - **Evidence:** `ci` p50 is `1441.5s`; p95 is `1521.1s`. Slow run `26933120515` took `1542s`. In that run, the log block from `05:43:14.508Z` to `06:04:04.876Z` lasted about `1250s`; by `.github/workflows/ci.yml` step order, that block corresponds to `Orchestrate poll process unit tests` (**inference**).  
   - **Root cause:** one monolithic test segment sits inside a single serial `lint` job with `72` steps.  
   - **Exact change:** split `tests/test_orchestrate_poll_process.py` into `2–3` runnable shards and execute them as parallel CI jobs; keep static lint and smaller contracts separate.  
   - **Estimated time savings:** about `10–15m` from CI p95 if shards are balanced.  
   - **Implementation risk:** medium.

2. **Move `Review autofix review-pipeline plumbing contract test` near the top of CI** *(critical path on failures)*  
   - **Evidence:** failed runs `26930300571` and `26934022980` both died in `lint / Review autofix review-pipeline plumbing contract test`. In `26934022980`, the failing step started at `06:07:06.417Z`, threw the assertion at `06:07:08.941Z`, but the run itself started at `06:03:21Z`—so the regression was discovered ~`228s` late.  
   - **Root cause:** a high-churn contract test is scheduled deep into the serial CI sequence.  
   - **Exact change:** run that contract test immediately after dependency install/static lint, or isolate it into a tiny presubmit job.  
   - **Estimated time savings:** ~`3.5–4m` per regression failure.  
   - **Implementation risk:** low.

3. **Reduce comment-trigger wrapper churn with a single comment router** *(non-critical-path but high-volume)*  
   - **Evidence:** `clarify` (`124` runs, `123` skipped), `plan` (`123`, `122` skipped), `implement` (`123`, `122` skipped), and `orchestrate_clarify_respond` (`123`, `123` skipped) together produced `493` runs. Wrapper files `internal-clarify.yml`, `internal-plan.yml`, `internal-implement.yml`, and `internal-orchestrate-clarify-respond.yml` all trigger broadly on `issue_comment.created`.  
   - **Root cause:** four workflows subscribe to every comment, then self-filter later.  
   - **Exact change:** replace them with one lightweight comment-router workflow that matches `/reclarify`, `/answer`, `/approved`, or “Clarification required”, then dispatches only the needed workflow.  
   - **Estimated time savings:** only ~`13m` total runner time in this window, but it removes ~`82%` of run-volume noise.  
   - **Implementation risk:** medium.

4. **Fix the post-merge validate dispatch default workflow name** *(micro-optimization)*  
   - **Evidence:** `.github/workflows/review_autofix.yml` defaults `VALIDATE_WORKFLOW_NAME` to `ai-validate.yml`, but the repo contains `internal-validate.yml` and `validate.yml`, not `ai-validate.yml`. The dispatch step explicitly falls back to `internal-validate.yml`. Run `26933192841` spent most of its `41s` in `review / post-merge-validate-dispatch`.  
   - **Root cause:** one avoidable failed `gh workflow run` attempt before fallback.  
   - **Exact change:** change the default from `ai-validate.yml` to `internal-validate.yml`.  
   - **Estimated time savings:** a few seconds and one failed API call per post-merge validate dispatch.  
   - **Implementation risk:** low.

## Cost Optimizations

1. **Add a difficulty/risk gate to `implement` reasoning level**  
   - **Evidence:** run `26934436094` (`Internal: AI Implement`) consumed `1,324,165` tokens, `12` Codex calls, `553s`, plus `5` Semble queries / `44,219` bytes. `implement.yml` defaults to `MODEL_EDITOR=openai/gpt-5.4` and `MODEL_REASONING_EFFORT=xhigh`.  
   - **Root cause:** all implement work pays `gpt-5.4` + `xhigh` by default, even though only one run in this window carried the entire cost burden.  
   - **Exact change:** use `high` reasoning for first-pass implement on low/medium-risk issues; keep `xhigh` for retries, workflow/prompt changes, and repair paths.  
   - **Estimated savings:** roughly `15–35%` on runs shaped like `26934436094` (about `0.20M–0.46M` tokens).  
   - **Quality-risk notes:** medium; fail back to `xhigh` automatically on retry or when touching `.github/workflows/`, `prompts/`, or core orchestration scripts.

2. **Do not disable Semble to chase savings; it is bounded and probably net-helpful here**  
   - **Evidence:** repo aggregate shows only `6` Semble queries and `59,930` bytes total. The one observed deep-dive runtime query was run `26930968999`, `target=reviewer-context`, `chunks=12`, `bytes=15711`, `ms=527`, inside a `1738s` review run.  
   - **Root cause:** model execution, not retrieval bytes, is the dominant spend.  
   - **Exact change:** keep current Semble byte caps; optimize model reasoning and CI/test placement first.  
   - **Estimated savings:** disabling Semble would likely save little in this window and may increase downstream prompt expansion.  
   - **Quality-risk notes:** high risk if removed blindly; current evidence suggests Semble is small and bounded, not noisy enough to be the primary target.

3. **Repair token/cache telemetry before tuning prompt cache or reviewer-model mix**  
   - **Evidence:** across `110` log-parsed runs, `cache_hit_rate` was always `null`; `or_prompt_tokens`, `or_completion_tokens`, `or_total_tokens`, `or_cache_write_tokens`, `or_cache_read_tokens`, and `or_calls` were all `0`.  
   - **Root cause:** the pipeline is not emitting or not ingesting prompt-cache/OpenRouter usage for the expensive review paths.  
   - **Exact change:** make review/plan/implement/validate consistently emit `or_*` usage plus `cache_hit_rate`; only after that tune model mix or cache strategy.  
   - **Estimated savings:** not measurable from this window because the telemetry is missing.  
   - **Quality-risk notes:** low; this is an observability-first change.

4. **Do not spend optimization effort on Serena yet**  
   - **Evidence:** all `serena_*` metrics are `0`; no runtime `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines were found. One evidence-grade summary (`26934023108`) shows `SERENA_ENABLED: false`.  
   - **Root cause:** Serena is effectively not in the serving path.  
   - **Exact change:** leave Serena disabled until there is a concrete replacement target and telemetry to prove value.  
   - **Estimated savings:** `0` in this window.  
   - **Quality-risk notes:** low.

Additional cost notes:
- No actual runtime `CONTEXT_BUDGET_WARN` or `BREAK_GLASS` events were observed; repo aggregate counts are both `0`.
- The only actual Codex-token-consuming runs in this window were `26934436094` (`implement`) and `26934079662` (`plan`, `2027` tokens). `plan` is a latency problem here, not a token problem.

## Reliability Improvements

1. **Fix the repeated review-pipeline contract regression immediately**  
   - **Failure evidence:** `CI` runs `26930300571` and `26934022980` both failed in `lint / Review autofix review-pipeline plumbing contract test` with `AssertionError: codex stdin must be fed from the per-attempt prompt file...`.  
   - **Root cause category:** code regression / contract break.  
   - **Exact fix:** restore use of the per-attempt prompt file (`${attempt_prompt_file}`) in the relevant review apply-fixes path, then keep `tests/test_review_autofix_editor_noop_cascade_contract.py` as the guardrail.  
   - **Expected reliability impact:** removes the only confirmed functional failures in the sampled CI window.  
   - **Rollback / fail-open:** none needed; the contract test is already the safety net.

2. **Filter CI Semble fixture fallbacks out of rollout health signals**  
   - **Failure evidence:** repo aggregate shows `55` Semble fallbacks. Deep-dive runtime logs show `50` actual `SEMBLE_FALLBACK` lines, all in `ci / lint`, all `target=overflow`, all `reason=...missing_semble`, across runs including `26930300571`, `26934022980`, `26931904835`, `26932433658`, `26933192932`, `26932537676`, `26930969008`, `26932664629`, `26933120515`, and `26931941768`. The fixture pattern matches `tests/test_targeted_file_context.py`, which explicitly uses `missing_semble`.  
   - **Root cause category:** telemetry pollution from expected fail-open unit tests.  
   - **Exact fix:** tag these CI-only fallbacks as test-fixture events or exclude them from repo-level MCP reliability rollups; keep runtime fail-open behavior unchanged.  
   - **Expected reliability impact:** better signal-to-noise; real Semble rollout regressions will stop being masked by fixture noise.  
   - **Rollback / fail-open:** no runtime behavior change; only reporting changes.

3. **Fix `validate` observability before treating the three failures as product failures**  
   - **Failure evidence:** `validate` runs `26930298218`, `26934020726`, and `26934701491` all show `failure`, `duration_seconds=0`, `jobs=null`; all log archive downloads failed with HTTP `404`.  
   - **Root cause category:** observability / reusable-workflow reporting gap (**inference**).  
   - **Exact fix:** teach the collector/report to link reusable `workflow_call` child runs back to their caller (`Internal: AI Validate`) or suppress child-only rows when logs/jobs are unavailable.  
   - **Expected reliability impact:** removes likely false failure noise and makes true validation failures debuggable.  
   - **Rollback / fail-open:** preserve raw rows alongside normalized rows until confidence is high.

4. **Do not count raw `review_autofix` cancellations as rerun failures until parent/child dedupe is added**  
   - **Failure evidence:** examples such as `26931904881` (`Internal: AI Review & Autofix`, `cancelled`, `985s`) immediately precede `26931912113` (`Codex PR Self-Healing Semantic Agent`, `success`, `985s`); a similar pattern repeats for `26931941878`→`26931949125`.  
   - **Root cause category:** reporting artifact from caller/callee reusable-workflow runs (**inference**).  
   - **Exact fix:** dedupe caller/callee run pairs in analysis before using cancellation counts as reliability signals.  
   - **Expected reliability impact:** cleaner failure-rate and rerun-rate reporting.  
   - **Rollback / fail-open:** keep raw counts in an appendix/footnote.

Additional reliability notes:
- `break_glass_count=0`; no runtime `BREAK_GLASS` lines observed.
- `context_budget_warn_count=0`; no runtime `CONTEXT_BUDGET_WARN` lines observed.
- No runtime `SERENA_FALLBACK` or `SERENA_PROBE` events were observed, so there is no evidence of a masked Serena rollout failure.

## AI Memory Health

- **Structured deep-dive memory telemetry was sparse but real.** I found `4` structured `AI_MEMORY_TELEMETRY` events, all in `review_autofix` run `26930968999` (`review / codex-agent`): `record-run-event` start, `retrieve`, `record-candidate`, and `record-run-event` completion.
- **Retrieve hit rate:** `0/1 = 0%`. The only observed `retrieve` had `enabled=true`, `records_selected=0`, `estimated_tokens=0`, `keyword_method="llm"`, `role="reviewer"`.
- **Budget use:** average observed `estimated_tokens` on retrieve was `0`, so token budget pressure was not the issue in the observed sample.
- **Keyword method distribution:** `llm=100%`, `plain=0%`, `none=0%` in observed retrieves.
- **Fail-open / disabled retrievals:** no `fail_open: true` and no `enabled: false` were observed in structured deep-dive events.
- **Push health:** all push-capable events showed `push_attempts=1`; no high retry counts were observed.
- **Additional evidence-grade summaries:** clarify run `26934048119` logged an `AI_MEMORY_TELEMETRY` phase-completion event for `auto_answered_by_orchestrator`; `copilot_pull_request_reviewer` run `26934039033` also mentioned memory telemetry, but the payloads were truncated.

**Recommendation:** memory is enabled, but the observed problem is **recall**, not budget. Verify record promotion/selection coverage before increasing token budgets.

## GH API Call Audit

1. **Remove the guaranteed failed dispatch in post-merge validate**
   - **Evidence:** `review_autofix.yml` defaults `VALIDATE_WORKFLOW_NAME` to `ai-validate.yml`, then falls back to `internal-validate.yml`; the repo has `internal-validate.yml`, not `ai-validate.yml`. Run `26933192841` spent most of its `41s` in `review / post-merge-validate-dispatch`.  
   - **Pattern:** one avoidable failed `gh workflow run` per validate-dispatch event.  
   - **Concrete change:** change the default to `internal-validate.yml`.  
   - **Estimated reduction:** one failed API call plus some dispatch latency per affected PR.  
   - **Rate-limit risk reduction:** low but free.

2. **Cache active autofix runs once per poll cycle in `orchestrate_poll_process.sh`**
   - **Evidence:** source helper `_has_active_autofix_run()` calls `gh run list` for up to `3` workflows (`ai-review.yml`, `internal-review.yml`, `review_autofix.yml`) for each PR check. `_dispatch_review_for_conflicts()` can reach it from multiple code paths in the same poll cycle.  
   - **Pattern:** repeated per-PR active-run lookups; this is a source-level redundancy (**inference**, not directly counted in logs).  
   - **Concrete change:** port the `snapshot_active_review_runs()` approach already used in `review_autofix_sweep.yml` into the poller and query an in-memory map instead of GitHub for each PR.  
   - **Estimated reduction:** up to `3` GitHub calls per PR dispatch evaluation.  
   - **Rate-limit risk reduction:** medium if conflicted/open-PR counts grow.

3. **`review_autofix_sweep` is already reasonably API-hygienic; keep it that way**
   - **Evidence:** run `26940470018` (`Internal: AI Review Autofix Sweep`) had `1` candidate and correctly skipped dispatch because an active `internal-review.yml` run already existed. The workflow snapshots active runs once per workflow rather than per PR.  
   - **Pattern:** fixed per-tick floor of one PR enumeration plus active-run snapshots, but no N×PR workflow-run fanout.  
   - **Concrete change:** none urgent for this repo size; monitor only if open-PR volume increases.  
   - **Rate-limit risk reduction:** current design is acceptable. No `429` or secondary rate limits were observed.

Positive hygiene already present:
- `orchestrate_poll_process.sh` caches `ensure_label_exists`, which should prevent repeated label lookups in one cycle.
- No `429`, secondary rate-limit, or backoff storms were observed in the inspected logs or evidence-grade summaries.

## Prompt Cache & Memory System

- **Prompt-cache telemetry is missing, not merely weak.** Across `110` parsed runs, `cache_hit_rate` was always `null`, and every `or_*` usage/cache field was `0`.  
- **The “cache hit” lines in runs `26934079662` and `26934436094` were GitHub Actions dependency caches** (`setup-uv`, `codex-v0.114.0-v2`), **not** model prompt-cache hits.
- **No runtime prompt-pressure alerts fired.** Actual `CONTEXT_BUDGET_WARN` count is `0`; only the configured threshold (`CONTEXT_BUDGET_WARN_RATIO: 0.7`) appeared in logs.
- **Semble is bounded.** Repo aggregate: `6` queries / `59,930` bytes. Deep-dive observed query: `15711` bytes in `527ms`. That is small enough that it is unlikely to be the main source of cache fragmentation.
- **Memory retrieval is not yet increasing prompt size in practice.** The only observed retrieve used `estimated_tokens=0` and returned `0` records.

**Concrete improvements**
1. Emit prompt-cache metrics from every review/plan/implement/validate model path.
2. Keep stable system/header content at the top of prompts; move volatile state, run IDs, memory inserts, and Semble inserts to the tail.
3. Keep per-attempt nonces only in attempt-specific prompt files, not shared/static prompt prefixes.

**Estimated impact:** currently unquantifiable because cache telemetry is absent; likely upside is lower prompt variance, better future cache reuse, and easier root-cause analysis.

## Orchestrator Health

- **The poller itself looks healthy.** `orchestrate_poll` had `17/17` successes, p50 `159s`, p95 `184.2s`. `internal-orchestrate-poll.yml` runs every `5` minutes, and the reusable poller serializes runs with concurrency control.
- **Clarify auto-answering works.** Run `26934048119` (`Internal: AI Clarify`) succeeded in `90s` and logged a memory phase-completion event with outcome `auto_answered_by_orchestrator`.
- **Most orchestration wrapper volume is control-plane noise, not work.** `493/602` runs were comment-trigger wrappers, and `490` of those were skipped.
- **Readiness gating is working as intended.** Run `26934022977` (`Integration PR readiness check`) reported `8/21 sub-issues on #3042 still unchecked`; that is useful orchestration state, not a workflow failure.
- **No signs of policy pressure or fallback abuse.** `BREAK_GLASS=0`, `CONTEXT_BUDGET_WARN=0`, Serena inactive, no probe failures.

**Track these indicators**
- skipped-wrapper ratio
- `orchestrate_poll` p95 duration
- `review_autofix` p95 duration (use the internal-review workflow, not sweep rows)
- validate-log coverage rate
- AI memory retrieve hit rate

## Pipeline Flow Bottlenecks

1. **Merge-gate / CI compute bottleneck**  
   - `CI` is the largest end-to-end blocker: p50 `1441.5s`, p95 `1521.1s`.  
   - Dominant issue: monolithic `lint` job, especially the inferred `Orchestrate poll process unit tests` segment.

2. **Review/autofix compute bottleneck**  
   - Raw `review_autofix` family p95 is `1188s`, but that family mixes sweep/noise with actual review runs.  
   - The meaningful workflow is `Internal: AI Review & Autofix`, which had p95 about `1354s`; outlier run `26930968999` took `1738s`.

3. **Implement compute + cost bottleneck**  
   - The only active `implement` run (`26934436094`) took `553s` and almost the entire token budget for the window.

4. **Plan latency bottleneck (not cost bottleneck)**  
   - Run `26934079662` took `600s` but used only `2027` tokens, so its problem is not token volume.

5. **Queueing / runner-pickup overhead on small control jobs**  
   - Examples: clarify run `26934048119` spent much of `90s` waiting for runner pickup; review gate `26934023108` also waited despite the run lasting only `9s`.  
   - This is secondary to CI/review/implement compute time.

6. **Validate observability bottleneck**  
   - The three `validate` failures cannot yet be mapped to a real step/job/root cause because there are no logs/jobs attached.

**Ordered fixes by end-to-end impact**
1. shard CI orchestrate-poll tests  
2. move the review-pipeline contract test earlier  
3. add an implement reasoning gate  
4. collapse wrapper-trigger noise with a comment router  
5. cache active review-run lookups in poller  
6. normalize `validate` reusable-workflow reporting

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `CI` is the biggest latency wall (`p50 1441.5s`, `p95 1521.1s`).
- `Internal: AI Review & Autofix` has long outliers (`26930968999` at `1738s`).
- `implement` is the dominant token spender (`26934436094`: `1,324,165` tokens).

**Top failure modes**
- Repeated CI contract regression in `Review autofix review-pipeline plumbing contract test`.
- `validate` rows are failing without logs/jobs, so current validate failure reporting is unreliable.
- Semble fallback counts are inflated by CI test fixtures, which can mask real rollout issues.

**Highest-cost drivers**
- Implement reasoning/model defaults (`gpt-5.4`, `xhigh`).
- Review paths are likely expensive too, but reviewer/OpenRouter token telemetry is missing.
- Semble bytes are modest; Serena is not active.

**Top 3 prioritized actions**
1. Shard and parallelize the orchestrate-poll CI suite; move review contract tests earlier for fail-fast behavior.
2. Add a guarded reasoning step-down in `implement.yml` and keep `xhigh` only for retries/high-risk scopes.
3. Fix reporting: filter CI Semble fixture fallbacks, and normalize reusable-workflow `validate` rows before treating them as runtime failures.

## Metrics Appendix

### Overall repository metrics

| Repository | Total runs | Success | Failure | Cancelled | Skipped/other | Failure rate | p50 duration | p95 duration | Runs w/ log telemetry | cache_hit_rate | wall_clock_p50_ms | wall_clock_p99_ms | break_glass_count | context_budget_warn_count |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 602 | 102 | 5 | 5 | 490 | 0.83% | 2s | 219s | 110 | null | 1000 | 1538580 | 0 | 0 |

### Workflow family summary

| Family | Runs | Success | Failure | Cancelled | Skipped | p50 duration | p95 duration | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| ci | 12 | 10 | 2 | 0 | 0 | 1441.5s | 1521.1s | Main latency bottleneck |
| review_autofix | 41 | 36 | 0 | 5 | 0 | 10s | 1188s | Mixed family: sweep + parent/child review runs |
| orchestrate_poll | 17 | 17 | 0 | 0 | 0 | 159s | 184.2s | Healthy poller |
| clarify | 124 | 1 | 0 | 0 | 123 | 1s | 3s | Mostly skipped wrapper noise |
| plan | 123 | 1 | 0 | 0 | 122 | 1s | 3s | Mostly skipped wrapper noise |
| implement | 123 | 1 | 0 | 0 | 122 | 1s | 7s | Mostly skipped wrapper noise; one expensive active run |
| validate | 3 | 0 | 3 | 0 | 0 | 0s | 0s | No logs/jobs; likely reporting gap |

### Token / cache / MCP telemetry

| Scope | Codex tokens | Codex calls | OR total tokens | OR cache write | OR cache read | Semble queries | Semble bytes | Semble fallbacks | Serena queries | Serena response bytes | Serena fallbacks | Probe ok | Probe failed | Probe skipped |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Repo total | 1,326,192 | 15 | 0 | 0 | 0 | 6 | 59,930 | 55 | 0 | 0 | 0 | 0 | 0 | 0 |
| implement family | 1,324,165 | 12 | 0 | 0 | 0 | 5 | 44,219 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| plan family | 2,027 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| review_autofix family | 0 | 0 | 0 | 0 | 0 | 1 | 15,711 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| ci family | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 55 | 0 | 0 | 0 | 0 | 0 | 0 |

### Highest-cost runs

| Run ID | Workflow family | Workflow name | Conclusion | Duration | Codex tokens | Codex calls | Semble queries | Semble bytes |
|---|---|---|---|---:|---:|---:|---:|---:|
| 26934436094 | implement | Internal: AI Implement | success | 553s | 1,324,165 | 12 | 5 | 44,219 |
| 26934079662 | plan | Internal: AI Plan | success | 600s | 2,027 | 3 | 0 | 0 |

### AI memory telemetry

| Observed source | Structured events | Retrieve ops | Retrieve hit rate | Avg estimated_tokens | keyword_method | fail_open true | enabled false | Max push_attempts |
|---|---:|---:|---:|---:|---|---:|---:|---:|
| Deep-dive logs (`26930968999`) | 4 | 1 | 0% | 0 | llm | 0 | 0 | 1 |
| Evidence-grade summaries (`26934048119`, `26934039033`) | partial only | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

### GH API call summary

| Workflow / source | Evidence | Observed or reconstructed API pattern | 429 / secondary rate limit evidence |
|---|---|---|---|
| `review_autofix_sweep` run `26940470018` | step `sweep` | 1 open-PR snapshot + active-run snapshots; skipped dispatch because active run existed | none observed |
| `review_autofix.yml` post-merge validate dispatch | source | default `ai-validate.yml` dispatch attempt then fallback to `internal-validate.yml` | none observed |
| `scripts/orchestrate_poll_process.sh` `_has_active_autofix_run()` | source | up to 3 `gh run list` calls per PR check (**inference**) | none observed |
| Poller overall | logs + source | `gh_retry` / label cache already present | none observed |

### MCP per-target / availability summary

| Server | Target | Query calls | Query bytes | Fallbacks | Probe ok | Probe failed | Probe skipped | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---|
| Semble | reviewer-context | 1 observed deep-dive | 15,711 | 0 | n/a | n/a | n/a | run `26930968999` |
| Semble | overflow | 0 observed queries | 0 | 50 observed deep-dive (`55` aggregated repo total) | n/a | n/a | n/a | CI test-fixture fail-open path |
| Serena | none observed | 0 | 0 | 0 | 0 | 0 | 0 | no runtime Serena activity |
| Other MCP servers observed | none | 0 | 0 | 0 | 0 | 0 | 0 | no other MCP prefixes observed |

**Notes**
- `review_autofix` cancellation counts likely include reusable-workflow caller/callee artifacts; do not treat all 5 raw cancellations as failed reruns without deduping.
- `validate` rows are currently evidence-poor: zero duration, no jobs, no logs.

## Deep Audit — Workflows & Scripts (2026-06-04)

### Section 1: Bug & Correctness Sweep

- **ID** — `BUG-001`  
  **File path** — `scripts/review_rb_judge.sh:628-645`  
  **Severity** — High  
  **Category tag** — `bug`  
  **Description** — The fallback linked-issue regex still accepts bare `issues/N` and `issue #N` references when GraphQL returns no `closingIssuesReferences`. That fallback feeds `ISSUE_NUMBERS`, and later judge branches phase-swap every matched issue to `ai:ready-to-merge` (`1350-1354`, `1423-1427`, `1902-1906`) or use `FIRST_ISSUE` as the parent for reissue metadata and label propagation (`2093-2116`), so a prose mention in the PR body can mutate the wrong issue.  
  **Recommended fix** — Replace the fallback with the stricter closing-keyword/repo-path-only extractor already used in `.github/workflows/issue_pr_status.yml:272-288`, or skip fallback entirely for label-mutating judge paths the way `.github/workflows/review_autofix.yml:895-915` already does.

- **ID** — `BUG-002`  
  **File path** — `.github/workflows/review_autofix.yml:767-819`  
  **Severity** — High  
  **Category tag** — `bug`  
  **Description** — In `post-merge-validate-dispatch`, the fallback regex again treats bare `issues/N` and `issue #N` as linked issues when GraphQL returns no nodes. The job then dispatches validate once and removes `ai:orchestrator-validate-required` from every matched issue at `816-818`, so an incidental issue mention in a merged PR title/body can clear the validation-required label on an unrelated issue.  
  **Recommended fix** — Reuse the stricter fallback from `.github/workflows/issue_pr_status.yml:272-288`; if there is no explicit closing-keyword or repo-scoped issue URL/path, log and skip dispatch instead of mutating labels.

- **ID** — `BUG-003`  
  **File path** — `.github/workflows/internal-review.yml:84-112`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The push-only no-PR path interpolates raw `HEAD_REF` into `gh api "repos/.../pulls?state=open&head=owner:ref"` at `99-101`. This workflow does not percent-encode the branch name, unlike `scripts/post_review_comment.sh:223-239`; refs containing URL-reserved characters can miss an existing open PR and incorrectly fall through to the no-PR review route.  
  **Recommended fix** — Percent-encode `HEAD_REF` with `jq -nr --arg ref "${HEAD_REF}" '$ref | @uri'` before building the query string, matching `scripts/post_review_comment.sh:223-239`.

- **ID** — `BUG-004`  
  **File path** — `scripts/tg_helpers.sh:155-206,227-278`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — `tg_store_msg_id` and `tg_store_phase_msg_id` both do read-modify-write updates to a shared GitHub tracking comment: fetch comment list, append a message ID locally, then `PATCH` the whole body. Two concurrent writers can race and silently drop one writer’s IDs, which breaks later Telegram cleanup.  
  **Recommended fix** — Stop appending into shared comment bodies. Emit one tracking comment per message/phase instead; `tg_cleanup_phase_msgs` and `tg_cleanup_msgs` already scan and delete multiple matching comments (`300-357`, `365-428`), so append-only tracking fits the existing cleanup design.

- **ID** — `BUG-005`  
  **File path** — `.github/workflows/review_autofix.yml:5216-5236,5399-5419,6388-6407`  
  **Severity** — High  
  **Category tag** — `bug`  
  **Description** — Three later label-mutating tails in `review_autofix.yml` still use the same broad fallback regex that accepts bare `issues/N` and `issue #N`. Those branches then set `ai:ready-to-merge` (`5231-5236`) or `ai:review-blocked` (`5414-5419`, `6403-6406`) on every matched issue, so a documentation-only reference can advance or block the wrong issue. The same workflow already documents this hazard and avoids fallback in the deterministic-skip path at `895-915`.  
  **Recommended fix** — Extract one strict linked-issue helper and reuse it here; for label transitions, allow only `closingIssuesReferences` or the stricter closing-keyword/repo-path fallback already used in `.github/workflows/issue_pr_status.yml:272-288`.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`  
  **File path** — `.github/workflows/review_autofix.yml:1916-1922; scripts/gh_helpers.sh:735-864`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The common PR path fetches PR context with 4 separate calls: PR payload, issue comments, reviews, and review comments. **Current call count:** 4. **Proposed call count after fix:** 1 common-case GraphQL call by extending `gh_pr_with_all_comments()` to include review `state/body/submittedAt` and writing the four output files from that single response.  
  **Recommended fix** — Extend `scripts/gh_helpers.sh::gh_pr_with_all_comments` and add a small wrapper such as `gh_pr_context_to_files <owner> <repo> <pr_number> <out_dir>`; update `review_autofix.yml` to reuse that helper instead of open-coding four fetches.

- **ID** — `API-002`  
  **File path** — `scripts/review_rb_judge.sh:628-663; .github/workflows/issue_pr_status.yml:253-265`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The judge first asks GraphQL only for linked issue numbers, then does up to `N` REST issue fetches to get body/labels for the first usable linked issue. **Current call count:** `1 + up to N`. **Proposed call count after fix:** `1`, using the richer `closingIssuesReferences { nodes { number body labels { ... } } }` shape already present in `issue_pr_status.yml`.  
  **Recommended fix** — Promote that richer GraphQL shape into a shared helper in `scripts/gh_helpers.sh`, then populate `FIRST_ISSUE`, `FIRST_ISSUE_BODY`, and `FIRST_ISSUE_LABELS_JSON` from one response.

- **ID** — `API-003`  
  **File path** — `.github/workflows/review_autofix.yml:774-819; scripts/orchestrate_poll_process.sh:2504-2564`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — In `post-merge-validate-dispatch`, fallback issue numbers are converted to `labels: null`, and the workflow then calls `gh issue view` inside the loop for every issue with unknown labels at `792-799`. **Current call count:** `1` GraphQL + optional `1` PR REST fetch + `N` issue-label fetches. **Proposed call count after fix:** `1` GraphQL + optional `1` PR REST fetch + `1` batched label query.  
  **Recommended fix** — Reuse the alias-batching pattern from `scripts/orchestrate_poll_process.sh::_fetch_issue_labels_batch_graphql` and batch fallback issue numbers before the loop.

- **ID** — `API-004`  
  **File path** — `scripts/check_external_branch_advance.sh:175-202`  
  **Severity** — Low  
  **Category tag** — `api-batching`  
  **Description** — [NEEDS VERIFICATION] The spoof-defense path loops over `self_subject_shas` and calls `gh api repos/${REPOSITORY}/commits/${sha}` once per SHA. The file comment says the set is “usually tiny (0–2 SHAs),” but this is still a per-item GitHub API pattern. **Current call count:** `M`. **Proposed call count after fix:** `1` aliased GraphQL batch if this path ever starts seeing larger commit sets.  
  **Recommended fix** — If commit fan-out grows, extract a generic batched commit-attribution helper in `scripts/gh_helpers.sh` using the same GraphQL alias pattern as `scripts/orchestrate_poll_process.sh:10109-10185`.

- **ID** — `API-005`  
  **File path** — `.github/workflows/review_autofix.yml:867-876,1838-1871; scripts/gh_helpers.sh:391-445`  
  **Severity** — Low  
  **Category tag** — `api-redundancy`  
  **Description** — `review_autofix.yml` carries two inline retry wrappers that retry every failure up to 4 or 5 times unless the stderr text looks rate-limit-like. Permanent 4xx failures therefore burn extra API calls that the canonical `gh_helpers.sh::gh_retry` would avoid. **Current call count:** up to `4-5` attempts per permanent failure. **Proposed call count after fix:** `1` on non-transient failures.  
  **Recommended fix** — Source or vendor the canonical retry semantics from `scripts/gh_helpers.sh:391-445` so permanent failures stop after the first attempt.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`  
  **File path** — `.github/workflows/clarify.yml:215-308; .github/workflows/plan.yml:266-358; .github/workflows/implement.yml:831-1059; .github/workflows/orchestrate.yml:340-447; .github/workflows/orchestrate_poll.yml:289-419; .github/workflows/orchestrate_clarify_respond.yml:259-370; .github/workflows/review_autofix.yml:1228-1555; .github/workflows/validate.yml:212-637`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The workflow-support bootstrap/staging block is near-copy-pasted across 8 workflows. Those copies have already drifted in length and behavior, and the two largest copies are also the highest expression-limit risks (`validate` at 20,065 chars and `review_autofix` at 18,675 chars).  
  **Recommended fix** — Move ownership to a new `scripts/bootstrap_workflow_support.sh` with a function/interface like `stage_workflow_support <mode> <wf_source> <script_ref> <support_root>`; update the 8 callers to pass mode-specific flags for required scripts, prompts, AI memory, Semble, and Serena.

- **ID** — `DUP-002`  
  **File path** — `.github/workflows/internal-clarify.yml:1-16; .github/workflows/internal-plan.yml:1-17; .github/workflows/internal-implement.yml:1-18; .github/workflows/internal-orchestrate-clarify-respond.yml:1-14`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — The four internal comment-trigger wrappers are near-identical shells around reusable workflows; they mostly differ by name, permissions, and tiny `if:` guards. The current report already covers the runtime noise this causes; this finding is only about the duplicated source structure.  
  **Recommended fix** — Implement the existing report’s router recommendation with one `internal-comment-router.yml` that parses the comment command and dispatches the reusable workflow, leaving the reusable workflows as the real execution entry points.

- **ID** — `DUP-003`  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/mark-stable.yml:369-396,518-545; .github/workflows/orchestrate_poll.yml:82-112; .github/workflows/review_autofix.yml:1838-1871; .github/workflows/test-and-mark-stable.yml:4813-4826`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — The same inline GitHub rate-limit/backoff helper appears in multiple workflows, and twice inside `mark-stable.yml`. This duplication has already drifted: the `review_autofix` copy now diverges from `scripts/gh_helpers.sh` enough to create `API-005`.  
  **Recommended fix** — Centralize on `scripts/gh_helpers.sh::gh_retry` / `gh_retry_to_file`, or wrap them in a tiny composite action used by these workflows.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`  
  **File path** — `.github/workflows/validate.yml:212-637`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The `Fetch workflow support files` `run:` block contains `${{ }}` interpolations inside a scalar measured at about **20,065 characters**, leaving only **935 characters of headroom** before GitHub’s 21,000-character expression ceiling. This block already contains support checkout, prompt copying, Semble/Serena staging, and env export, so minor edits can make the whole workflow unparsable.  
  **Recommended fix** — Extract the bootstrap logic to `scripts/bootstrap_workflow_support.sh` and keep the workflow step to a short script invocation plus env wiring.

- **ID** — `EXPR-002`  
  **File path** — `.github/workflows/review_autofix.yml:1228-1555`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The `Stage workflow support files` block is about **18,675 characters**, leaving **2,325 characters of headroom**. It is a large inline bootstrap script with many `${{ }}`-backed values and is already one of the repo’s most complex interpolated `run:` blocks.  
  **Recommended fix** — Move the staging/bootstrap body into an external script under `scripts/` and keep the workflow block as a thin wrapper.

- **ID** — `EXPR-003`  
  **File path** — `.github/workflows/review_autofix.yml:1831-2221`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Collect PR metadata` block is about **17,408 characters**, leaving **3,592 characters of headroom**. It mixes retry helpers, no-PR synthetic metadata, multiple API fetches, linked-issue context construction, and diff capture in one interpolated `run:` body.  
  **Recommended fix** — Split this into smaller steps or extract it to a dedicated script such as `scripts/review_collect_pr_context.sh`.

- **ID** — `EXPR-004`  
  **File path** — `.github/workflows/implement.yml:3164-3501`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Commit changes` block is about **15,275 characters**, leaving **5,725 characters of headroom**. The block combines traps, stderr capture, destructive-change guardrails, no-op handling, and the final commit path in one interpolated script.  
  **Recommended fix** — Extract the step to `scripts/implement_commit_changes.sh` or split it into separate preflight/error-capture and commit/no-op steps.

- No workflow file exceeded the **800 KB** audit threshold; the largest is `.github/workflows/review_autofix.yml` at **401,929 bytes**.  
- I did not find any `if:` expressions near the 21,000-character limit; the largest observed `if:` was **739 characters** in `.github/workflows/test-and-mark-stable.yml:4728-4738`.

### Section 5: Cross-Cutting Concerns

- No `TODO` / `FIXME` / `HACK` markers were present under `.github/workflows/` or `scripts/`.

- **ID** — `DEAD-001`  
  **File path** — `scripts/orchestrate_lib.py:1162-1545`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — The contradiction-evidence path (`parse_phase_failure_markers`, `evaluate_phase_failure_resume`, `resolve_label_repair_evidence`, `choose_most_advanced_conclusive_evidence`) is implemented but not called outside this module. Repo docs also say it is “contract/reserved and not yet wired” (`agents.md:240-247`, `README.md:1199`), so roughly 380 lines of logic are dormant on this branch.  
  **Recommended fix** — Either wire this path into the active poller reconciliation flow, or move it behind an explicit reserved/feature-flag boundary with tests that document its intentionally dormant status.

- **ID** — `CONSIST-001`  
  **File path** — `.github/workflows/issue_pr_status.yml:272-288; .github/workflows/review_autofix.yml:774-785,895-915,5216-5236,5399-5419,6388-6407; scripts/review_rb_judge.sh:635-645`  
  **Severity** — Medium  
  **Category tag** — `consistency`  
  **Description** — Linked-issue resolution semantics diverge across the repo. `issue_pr_status.yml` and the deterministic-skip path in `review_autofix.yml` explicitly reject bare `issue #N` / `issues/N`, while `review_rb_judge.sh` and several other `review_autofix.yml` tails still accept them. That inconsistency is the shared root cause behind `BUG-001`, `BUG-002`, and `BUG-005`.  
  **Recommended fix** — Centralize one shared extractor, e.g. `scripts/issue_link_helpers.sh::extract_linked_issue_numbers <repo> <text> <mode>`, defaulting to GraphQL first plus strict closing-keyword/repo-path fallback, and delete the ad-hoc regex copies.

- **ID** — `SHELL-001`  
  **File path** — `scripts/review_conflict_resolve.sh:2315-2315`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — Targeted `shellcheck -x` reports `SC2086` on `git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}`. The current inputs are usually safe, but the command still relies on unquoted-expansion behavior that is easy to copy forward unsafely.  
  **Recommended fix** — Quote the expansions inside the URL, or build the remote string in a variable with `printf -v` before passing it to `git remote set-url`.

- **ID** — `SHELL-002`  
  **File path** — `scripts/review_run_reviewers.sh:187-218,249-257,269-276,1590-1594,2380-2390,2474-2477,2693-2694`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — `shellcheck -x scripts/review_run_reviewers.sh` reports multiple live warnings: unused locals/vars (`probe_prompt`, `RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE`, `RAW_REVIEWER_SYMBOL_DIFF_SUMMARY_FILE`, `REVIEWER_HEALTH_LAST_OPEN_UNTIL_EPOCH`, `attempt_number`, `REVIEWER_ATTEMPT_WD_REASON`, `REVIEWER_ATTEMPT_CMD_RC`) plus smart quotes in prompt text at `1593` (`SC1111`). None are immediately fatal, but they mask higher-value warnings in one of the repo’s largest shell scripts.  
  **Recommended fix** — Delete or justify the unused variables, add narrow `shellcheck` suppressions only where intentional, and replace the smart quotes with plain ASCII quotes.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 5 | BUG-001, BUG-002, BUG-005, EXPR-001, EXPR-002 |
| Medium | 9 | BUG-003, BUG-004, API-001, API-002, API-003, DUP-001, EXPR-003, EXPR-004, CONSIST-001 |
| Low | 7 | API-004, API-005, DUP-002, DUP-003, DEAD-001, SHELL-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2–3 | Medium |
| API call optimization | 4–5 | Medium |
| Code modularization | 10+ | Large |
| Expression size reduction | 3–4 | Medium |
| Medium/Low fixes | 6–8 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-06-04)

### Safety Tag Legend
`SAFE_TO_MERGE` means the consolidation can be applied directly without changing endpoint/filter/retry/concurrency semantics. `NEEDS_VERIFICATION` means the overlap is real, but static review did not fully prove the safe-merge preconditions. `RISKY_SKIP` means the overlap exists, but the call sits in a retry/race/stall-recovery or otherwise safety-sensitive path and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

- **ID** — `MERGE-001`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/clarify.yml:418-423`  
  **Current call count** — `2` when `SEMANTIC_CACHE_BACKEND != none`  
  **Proposed call count** — `1`  
  **Endpoint(s)** — `GET /repos/{repo}/issues/{issue_number}/comments` with `sort=created&direction=asc` (`per_page=50` once; then paginated `per_page=100`).  
  **Evidence** — the same step fetches comments once for bounded prompt context, then re-fetches the same comment thread for semantic-cache history; `ISSUE_COMMENTS_FILE` is later consumed as a plain JSON array at `.github/workflows/clarify.yml:521-522`.
  ```sh
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=50" > "${ISSUE_COMMENTS_FILE}"

  if [ "${SEMANTIC_CACHE_BACKEND}" != "none" ]; then
    if ! gh_retry gh api --paginate --slurp "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=100" \
  ```
  **Proposed fix** — in the `Fetch issue comments` step, fetch the full paginated comment array once into a temp file, write `ISSUE_COMMENTS_FILE` from the first 50 flattened elements, and derive `THREAD_HISTORY_FILE` from the same flattened array.  
  **Safety rationale** — same workflow step and no intervening mutation make the overlap real, but the merged version would change pagination and response-shape semantics, so the `SAFE_TO_MERGE` pagination precondition is not fully satisfied.  
  **Downstream signal** — Verify on issues with `0`, `<50`, `50+`, and `100+` comments that slicing the flattened paginated response reproduces the exact chronological JSON shape currently consumed from `ISSUE_COMMENTS_FILE`, and confirm `THREAD_HISTORY_FILE` ordering is unchanged.

- **ID** — `MERGE-002`  
  **Safety tag** — `RISKY_SKIP`  
  **File path and line ranges** — `scripts/orchestrate_poll_process.sh:9420-9421`, `scripts/orchestrate_poll_process.sh:11470-11471`, `scripts/orchestrate_poll_process.sh:15513-15514`  
  **Current call count** — `2` per path (`6` total across the three paths)  
  **Proposed call count** — `1` per path (`3` total)  
  **Endpoint(s)** — `GET /repos/{repo}/issues/{issue_number}` via `_safe_gh_jq` / `gh_retry`.  
  **Evidence** — each reissue path reads the same issue twice only to split `title` and `body`.
  ```sh
  orig_title="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.title // ""' || echo "")"
  orig_body="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.body // ""' || echo "")"
  ```
  ```sh
  IF_TITLE="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${if_issue}" --jq '.title' || echo "")"
  IF_BODY="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${if_issue}" --jq '.body' || echo "")"
  ```
  **Proposed fix** — add a local helper in `scripts/orchestrate_poll_process.sh` that fetches `{title, body}` once and unpacks both fields for these three callers.  
  **Safety rationale** — `RISKY_SKIP` because the calls are inside `scripts/orchestrate_poll_process.sh` stall-recovery/reissue paths, which this audit contract explicitly treats as manual-review-only.  
  **Downstream signal** — Do not auto-implement; manually test normal stall-reissue, no-op ancestry reissue, and implementation-failed reissue paths to prove one-call extraction preserves fail-open behavior, emitted comments, and grep-stable log output.

- **ID** — `MERGE-003`  
  **Safety tag** — `RISKY_SKIP`  
  **File path and line ranges** — `scripts/review_conflict_resolve.sh:133-150`  
  **Current call count** — `2`  
  **Proposed call count** — `1`  
  **Endpoint(s)** — `gh run list` for `${_poll_workflow}` in `${GITHUB_REPOSITORY}` (Actions workflow-run listing), filtered once for `in_progress` and once for `queued`.  
  **Evidence** — `_dispatch_integration_judge_now()` checks the same workflow twice, differing only by status filter.
  ```sh
  _active_count="$(GH_TOKEN="${GH_PAT}" gh run list \
      --workflow="${_poll_workflow}" \
      --repo "${GITHUB_REPOSITORY}" \
      --status in_progress \
      --limit 1 \
      --json databaseId \
      --jq 'length' 2>/dev/null || echo 0)"
  ...
  _active_count="$(GH_TOKEN="${GH_PAT}" gh run list \
      --workflow="${_poll_workflow}" \
      --repo "${GITHUB_REPOSITORY}" \
      --status queued \
      --limit 1 \
      --json databaseId \
      --jq 'length' 2>/dev/null || echo 0)"
  ```
  **Proposed fix** — fetch one small mixed-status snapshot (for example `--json databaseId,status --limit <small N>`) and branch locally on `queued` vs `in_progress` so the function keeps the same two skip messages.  
  **Safety rationale** — `RISKY_SKIP` because this is a queue/race-defense dedup gate immediately before `gh workflow run`, so changing its observation window is not safe to auto-merge.  
  **Downstream signal** — Do not auto-implement; manually replay both scenarios (queued poller already present, in-progress poller already present) and confirm a single listing call still suppresses dispatch without missing freshly queued runs or changing observable skip logs.

### Redundant Re-Fetch (REUSE-###)

- **ID** — `REUSE-001`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/implement.yml:100-100`, `.github/workflows/implement.yml:411-411`, `.github/workflows/implement.yml:1115-1116`  
  **Current call count** — `3` on the normal non-skipped path before the first issue write  
  **Proposed call count** — `1`  
  **Endpoint(s)** — `GET /repos/{repo}/issues/{issue_number}`.  
  **Evidence** — the workflow reads the same issue payload for precheck, checkout-ref resolution, and then a persisted metadata file; the file-backed step explicitly fetches only because earlier steps never filled `ISSUE_META_FILE`.
  ```sh
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '{state: (.state // "open"), labels: [.labels[].name]}')"
  if ! issue_meta_json="$(gh_api_with_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"; then
  if [ ! -s "${ISSUE_META_FILE}" ]; then
    gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"
  fi
  ```
  **Proposed fix** — move to one early full-payload fetch that writes `ISSUE_META_FILE`, derive the precheck state/labels from that file, and have `Resolve checkout ref` read the cached JSON before falling back to `gh_api_with_retry` only if the cache is missing/invalid.  
  **Safety rationale** — the overlap is strong, but it spans multiple workflow steps and currently mixes plain `gh api`, `gh_api_with_retry`, and `gh_retry`, so the `SAFE_TO_MERGE` error-handling requirement is not proven.  
  **Downstream signal** — Verify there is no issue mutation before `.github/workflows/implement.yml:1116`, then diff the early cached JSON against the later live response on both a normal implement run and an E2E-smoke run; only consolidate if payloads match and fallback/retry behavior stays equivalent.

- **ID** — `REUSE-002`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/orchestrate_clarify_respond.yml:68-83`, `.github/workflows/orchestrate_clarify_respond.yml:429-441`  
  **Current call count** — `4` on the orchestrator-managed path with a tracking parent (`2` child-issue GETs + `2` tracking-issue GETs)  
  **Proposed call count** — `2`  
  **Endpoint(s)** — `GET /repos/{repo}/issues/{ISSUE_NUMBER}` and `GET /repos/{repo}/issues/{TRACKING_NUM}`.  
  **Evidence** — `Check orchestrator metadata` already fetches the child issue and optional tracking issue, then `Fetch issue and tracking context` re-fetches both.
  ```sh
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  TRACKING_TITLE="$(gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.title // ""' 2>/dev/null || echo "")"
  ...
  ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  TRACKING_BODY="$(gh_retry gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.body // ""')"
  ```
  **Proposed fix** — in `Check orchestrator metadata`, switch the reads to `gh_retry gh api`, persist the child-issue JSON plus optional tracking-issue JSON/number to a temp file or env file, and have `Fetch issue and tracking context` consume that cache before any live fallback.  
  **Safety rationale** — same job and same issue scope, but this crosses workflow steps and the first step currently lacks `gh_retry`, so retry/failure semantics are not identical enough for `SAFE_TO_MERGE`.  
  **Downstream signal** — Verify no step before `.github/workflows/orchestrate_clarify_respond.yml:429-441` mutates the child or tracking issue body/title, then compare persisted first-step payloads against the later live responses on an orchestrator-managed issue both with and without a tracking parent.

### Dead Calls (DEAD-API-###)
No findings.

### Cross-References to Deep Audit Section
- `API-001`: `NEEDS_VERIFICATION` — same-step overlap is real, but replacing four paginated REST reads with one GraphQL shape changes transport, pagination, and failure surfaces.
- `API-002`: `NEEDS_VERIFICATION` — extending the existing `closingIssuesReferences` query is sound in principle, but first-usable-issue/body/label behavior must be matched exactly.
- `API-003`: `NEEDS_VERIFICATION` — batching fallback issue-label fetches should help, but validate-dispatch fail-open behavior and label-removal semantics still need proof.
- `API-004`: `RISKY_SKIP` — this is a spoof-defense path in `scripts/check_external_branch_advance.sh`; batching commit-attribution checks changes a security-sensitive decision point.
- `API-005`: `RISKY_SKIP` — the target code is itself retry/backoff logic, so auto-rewriting it risks changing permanent-failure handling and rate-limit logging.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 3 | MERGE-001, REUSE-001, REUSE-002 |
| RISKY_SKIP | 2 | MERGE-002, MERGE-003 |

### Implement-Stage Handoff
No SAFE_TO_MERGE findings in this pass.
