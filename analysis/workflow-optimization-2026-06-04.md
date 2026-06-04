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
