## Executive Summary

- `review_autofix` is the main critical-path bottleneck: family p95 is `3470.1s`, and the longest runs were `28201392068` (`3920s`), `28212502752` (`3892s`), `28181478326` (`3652s`), `28165184542` (`3518s`), `28177807382` (`3466s`), `28189723820` (`3451s`), `28209731379` (`3437s`), and `28181492218` (`3404s`). Estimated impact: `10–30 min/run` faster PR cycles if optimized here first. Confidence: **high**.
- `review_autofix` also wastes substantial stale work: `13` cancelled runs consumed `21,806s` (~`6.1h`) in this window, while PR-backed runs are intentionally `cancel-in-progress: false` in `.github/workflows/internal-review.yml`. Estimated impact: save ~`6h` runner time/window and return the freshest review `15–30 min` sooner during push bursts *(inference)*. Confidence: **high**.
- Two `review_autofix` failures (`28147004758`, `28147019610`) were caused by checkout post-job cleanup inheriting `GIT_DIR`/`GIT_WORK_TREE`; both logs end with `git submodule ... cannot be used without a working tree.` Estimated impact: remove `2/99` review failures in this sample. Confidence: **high**.
- CI reliability is dominated by one deterministic lint defect: runs `28206317872`, `28211580640`, `28215501481`, and `28216181255` all failed `CI → lint → Python lint (ruff)` on `scripts/slop_scan_local.py:425` with `E731`. Estimated impact: eliminate the current `4/18` CI failures and avoid reruns of a workflow whose p50 is `1691.5s` (~`28.2 min`). Confidence: **high**.
- Semble is not the problem: only `9` real `SEMBLE_QUERY` events were found, totaling `100,724` bytes and `4.635s`; `0` Semble fallbacks were observed. Serena produced `0` query/fallback/probe events and appears disabled, not degraded. Estimated impact: prevents low-value tuning work. Confidence: **high**.
- Prompt-cache / OpenRouter cost telemetry is effectively blind: deep-dive telemetry shows `76` OR calls (wider `analysis_context`: `100`), but all OR token/cache fields are `0` and `cache_hit_rate=null`. Estimated impact: unlocking this telemetry is prerequisite to credible cost tuning. Confidence: **high**.
- Comment-trigger fan-out is noisy but secondary: `clarify` + `plan` + `implement` + `orchestrate_clarify_respond` produced `689` skipped runs, but only `2,185s` total runtime. Estimated impact: small speed gain, medium observability gain. Confidence: **high**.

## Speed Optimizations

1. **Critical-path: add a superseded-head soft exit before expensive PR review work**
   - **Evidence:** `review_autofix` p95 is `3470.1s`; `13` cancelled runs consumed `21,806s`; the slow folders are centered on `step-001-{review_}codex-agent.log` for runs like `28165184542`, `28177807382`, `28181492218`, `28189723820`, `28201392068`, and `28212502752`. `.github/workflows/internal-review.yml:48-50` leaves PR-backed runs `cancel-in-progress: false`, and `.github/workflows/review_autofix.yml:1093-1117` keeps queued PR runs alive by design.
   - **Root cause:** stale PR-head work is allowed to continue too far before the workflow discovers it has been superseded.
   - **Exact change:** keep the current no-self-cancel design, but add an early live PR-head SHA check in the `gate` phase and again before reviewer pass 1; if the live PR head no longer matches the run head, soft-exit `0` with an explicit stale marker.
   - **Estimated savings:** up to the `21,806s` of cancelled review time seen here; more practically, `15–30 min` fresher latest-review latency during synchronize bursts *(inference)*.
   - **Implementation risk:** **low-medium**; fail open on API errors.

2. **Critical-path: enable low-risk review tiers and lower reasoning on non-risky paths**
   - **Evidence:** recent plan run `28216501931` used `MODEL_EDITOR: openai/gpt-5.4` and `MODEL_REASONING_EFFORT: xhigh` for `552s`. `review_autofix.yml` defaults to six reviewer models plus `EDITOR_REASONING_EFFORT: xhigh`, while `REVIEWER_RISK_TIER_ENABLED` is off.
   - **Root cause:** full reviewer breadth and highest reasoning are applied by default, even when the workflow already has trivial/lite tier knobs.
   - **Exact change:** turn on `REVIEWER_RISK_TIER_ENABLED`, populate `REVIEWER_TIER_TRIVIAL_MODELS` / `REVIEWER_TIER_LITE_MODELS`, and lower plan reasoning one notch for non-workflow/non-`scripts/` work. Keep the existing always-full regex for `.github/workflows/`, `scripts/`, `prompts/`, etc.
   - **Estimated savings:** `10–20 min` on low-risk `review_autofix` runs and `1–2 min` on plan runs *(inference)*.
   - **Implementation risk:** **medium**; use a repo variable rollout.

3. **Critical-path: trim `post-merge-validate-dispatch` work in review/autofix**
   - **Evidence:** evidence-grade `log_summary` for run `28216179473` says `review post-merge-validate-dispatch` dominated the `336s` run. In `.github/workflows/review_autofix.yml:824-887`, that step can fall back to GraphQL plus per-issue `gh issue view` calls.
   - **Root cause:** linked-issue label data is not always carried into the dispatch step, forcing per-issue lookups in a late hot path.
   - **Exact change:** always populate `POST_MERGE_LINKED_ISSUES_JSON` with labels so the dispatch step can decide from one payload and skip the `gh issue view` loop.
   - **Estimated savings:** tens of seconds to a few minutes on merged-PR review runs with linked issues.
   - **Implementation risk:** **low**.

4. **Micro-optimization: reduce skip-heavy `issue_comment` fan-out**
   - **Evidence:** `clarify` had `184` runs with `176` skipped, `plan` `176/168`, `implement` `176/169`, `orchestrate_clarify_respond` `176/176`. Combined skipped runtime was only `2,185s`, but this was `689` workflow runs. Wrapper files `.github/workflows/internal-clarify.yml`, `internal-orchestrate-clarify-respond.yml`, `internal-plan.yml`, and `internal-implement.yml` all trigger on `issue_comment.created`.
   - **Root cause:** routing is done after workflow dispatch instead of before it.
   - **Exact change:** move the obvious marker checks into the wrapper layer, or use one comment-router workflow that dispatches the correct reusable workflow.
   - **Estimated savings:** ~`36 min` total runtime per `952`-run window, plus cleaner run history and less operator noise.
   - **Implementation risk:** **low**.

## Cost Optimizations

1. **Turn on low-risk review tiering before adding more model work**
   - **Evidence:** `review_autofix` is the longest family (`p95=3470.1s`), and `review_autofix.yml` runs a six-model reviewer panel with `xhigh` editor reasoning by default. Wider telemetry only proves `codex_tokens_used=20,262` across `14` calls; OR token totals are missing.
   - **Root cause:** the expensive path is the default path.
   - **Exact change:** enable the existing risk-tier controls and reduce reviewer breadth / reasoning on trivial and lite diffs, while preserving full scrutiny for workflow, automation, and prompt files.
   - **Estimated savings:** `25–40%` LLM spend on low-risk review runs *(inference; OR token telemetry is currently blind)*.
   - **Quality-risk notes:** **medium**; protect high-risk paths with the existing regex.

2. **Stop paying for stale/cancelled review work**
   - **Evidence:** `13` cancelled `review_autofix` runs consumed `21,806s`; the four biggest cancellations were `28165172221` (`3507s`), `28189741718` (`3415s`), `28195801261` (`3274s`), and `28173616084` (`3114s`).
   - **Root cause:** PR-backed review runs keep running after newer synchronize events arrive.
   - **Exact change:** add superseded-head soft exits early, not just late stale-base checks.
   - **Estimated savings:** up to a full review run’s model spend on each long stale cancellation *(inference)*; hard lower bound is `6.1h` runner time in this sample.
   - **Quality-risk notes:** **low** if the stale check fails open.

3. **Repair OR token/cache telemetry, then stabilize prompt prefixes**
   - **Evidence:** deep-dive summary shows `or_calls=76`, wider `analysis_context` shows `or_calls=100`, but `or_prompt_tokens=0`, `or_total_tokens=0`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, and `cache_hit_rate=null` in both scopes. Meanwhile `plan.yml:856-863` and `review_autofix.yml:1680-1692` already try to build cacheable static prefixes.
   - **Root cause:** telemetry emission/parser gap; possible prompt-prefix instability is currently unmeasurable.
   - **Exact change:** plumb provider token/cache fields into the logs parsed by `cost_audit.py`, then keep volatile per-run text below the stable prefix.
   - **Estimated savings:** unquantified today; likely `5–15%` repeated-run prompt savings once measurable *(inference)*.
   - **Quality-risk notes:** **low**.

4. **Keep Semble; it is targeted. Serena is currently a non-factor.**
   - **Evidence:** real `SEMBLE_QUERY` telemetry found `6` `reviewer-context` calls (`81,565` bytes) and `3` `overflow` calls (`19,159` bytes) for only `4.635s` total; all `overflow` calls targeted `tests/test_implement_post_codex_recovery.py`. No `SEMBLE_FALLBACK` events were observed. Serena had `0` real query/fallback/probe events, and sampled review logs showed `SERENA_ENABLED:false`.
   - **Root cause:** Semble is already being used as narrow retrieval, not as noisy bulk context.
   - **Exact change:** do **not** remove Semble for cost reasons; instead trim comment/API churn (`clarify.yml:1163-1167`, `plan.yml:848-850`, `1132-1163`) and finish OR cache telemetry.
   - **Estimated savings:** small direct spend reduction from comment/API churn; high avoided-risk from not removing a cheap context helper.
   - **Quality-risk notes:** **low** for keeping Semble; Serena should stay off until explicitly rolled out.

Also worth noting: avoidable reruns from the Ruff defect and the `GIT_WORK_TREE` bug are the clearest non-model cost savings available immediately.

## Reliability Improvements

1. **Scope `GIT_DIR` / `GIT_WORK_TREE` to shell steps only**
   - **Failure evidence:** `review_autofix` runs `28147004758` (`234s`) and `28147019610` (`187s`) failed after job cleanup with `fatal: /usr/lib/git-core/git-submodule cannot be used without a working tree.` The logs also show `GIT_DIR=/home/runner/work/coding-workflows/coding-workflows/.git` and `GIT_WORK_TREE=/home/runner/work/_temp/workspaces/...`. The workflow currently exports both through `GITHUB_ENV` at `.github/workflows/review_autofix.yml:1563-1567`.
   - **Root cause category:** environment leakage into action post-steps.
   - **Exact fix:** keep `BASH_ENV` global, but move `GIT_DIR`/`GIT_WORK_TREE` into `workspace-shell.env` so only shell `run:` steps inherit them; alternatively unset them before actions’ post-job cleanup.
   - **Expected reliability impact:** removes the two observed review failures and prevents similar checkout cleanup breakage.
   - **Rollback / fail-open:** trivial; shell steps still get the split-worktree context.

2. **Fix the deterministic Ruff violation in `scripts/slop_scan_local.py`**
   - **Failure evidence:** runs `28206317872`, `28211580640`, `28215501481`, and `28216181255` all failed `CI → lint → Python lint (ruff)` with `E731` at `scripts/slop_scan_local.py:425`.
   - **Root cause category:** deterministic source defect.
   - **Exact fix:** replace the assigned `lambda` with a named `def`.
   - **Expected reliability impact:** should eliminate the current `4/18` CI failures in this sample.
   - **Rollback / fail-open:** none needed.

3. **Add superseded-head exits to reduce stale review cancellations**
   - **Failure evidence:** `review_autofix` had `13` cancellations; `.github/workflows/review_autofix.yml:1093-1117` intentionally queues PR-backed runs instead of cancelling them. Existing stale-base guards at `.github/workflows/review_autofix.yml:2627-2665`, `3060-3095`, and `4783-4809` are helpful but late.
   - **Root cause category:** concurrency/update race.
   - **Exact fix:** add a live head-SHA mismatch check before reviewer pass 1 and before editor work, with soft exit `0` and a clear stale marker.
   - **Expected reliability impact:** fewer stale comments, fewer human reruns, less cancellation churn.
   - **Rollback / fail-open:** fail open on API errors.

4. **Reduce `validation_refresh` starvation**
   - **Failure evidence:** run `28215278870` (`1483s`) reported `processed=13`, `green=9`, `red=1`, `skipped=3`, and skipped `shubhodeep1/bitsafe.io`, `shubhodeep1/hylifegroup.com`, and `shubhodeep1/radateeree-resort.com` because `remaining=811s/679s/662s` fell below `worst_case_single=900s`.
   - **Root cause category:** serialized budget exhaustion.
   - **Exact fix:** reorder targets by recent-change likelihood / historical runtime, and do a lighter first-pass discovery before reserving the full 900-second slot.
   - **Expected reliability impact:** more consistent repo coverage per refresh cycle.
   - **Rollback / fail-open:** keep current ordering as fallback.

Other reliability signals were healthy:
- `break_glass_count=0` and `context_budget_warn_count=0` in both deep-dive and wider telemetry.
- No real `SEMBLE_FALLBACK`, `SERENA_FALLBACK`, or `SERENA_PROBE` events were observed.
- Serena looks **disabled**, not **broken**.

## AI Memory Health

- AI memory telemetry was present in `11` runs: plan `28216501931` and review/autofix runs `28147004758`, `28147019610`, `28165184542`, `28177807382`, `28181478326`, `28181492218`, `28189723820`, `28201392068`, `28209731379`, and `28212502752`.
- Deduped event count: `41`.
  - `record-run-event`: `22`
  - `record-candidate`: `7`
  - `retrieve`: `9`
  - `processed-command-check`: `1`
  - `processed-command-claim`: `1`
  - `processed-command-complete`: `1`
- Retrieve health was strong:
  - hit rate: `100%` (`9/9` had `records_selected > 0`)
  - average `estimated_tokens`: `413`
  - average `token_budget`: `1377.8`
  - `keyword_method`: `llm=9`, `plain=0`, `none=0`
  - `records_selected=0`: `0`
  - `fail_open:true`: `0`
  - `enabled:false`: `0`
- One push-retry outlier appeared: run `28147019610` logged `record-run-event` with `push_attempts=2`.
- No `finalize-task`, `promote`, or `compact` operations were observed in this sample. If those ops are expected in production, expand telemetry coverage so they appear in future bundles.

## GH API Call Audit

Exact per-run API call counts are **not** emitted today, so this audit is based on workflow code plus evidence-grade run summaries.

| Workflow / step | Evidence | Current pattern | Recommended change | Estimated call reduction |
|---|---|---|---|---|
| `plan` | `plan.yml:395`, `434`, `500-508`, `666-668`, `848-850`, `1132-1163`; run `28216501931` took `552s` and the plan step dominated | 1 issue fetch, 1 paginated comments fetch, 1 paginated timeline fetch, progress-comment create + patches, per-comment deletes | Reuse one comments snapshot throughout the run; patch progress only on state transitions; avoid deleting every historic clarification comment | `3–6` calls per successful plan run, plus `N` DELETEs |
| `clarify` | `clarify.yml:430-435`, `464-470`, `484-485`, `1125-1126`, `1163-1167` | Optional double comment fetch when semantic cache is on; two POST comments on clear path | Reuse the full thread response for both bounded context and cache canonicalization; collapse clear-text + `/answer` into one comment that starts with `/answer` | `1–2` calls per successful clarify run |
| `review_autofix` `post-merge-validate-dispatch` | `review_autofix.yml:824-887`; run `28216179473` was dominated by this step | GraphQL/REST fallback plus per-linked-issue `gh issue view` lookups and edits | Always pass label arrays in `POST_MERGE_LINKED_ISSUES_JSON`; decide from one payload and skip `gh issue view` loop | Up to `1` lookup per linked issue |
| `orchestrate_poll` `Find active tracking issues` | `orchestrate_poll.yml:145-160`; run `28216359646` found `1 active tracking issue` | One batched `gh issue list --json number,title --limit 20` call | No change; this is the right pattern | None |

Best practice already present in this repo: batched `gh issue list --json` in `orchestrate_poll.yml` and GraphQL linked-issue aggregation in `review_autofix.yml`. Extend those patterns to late-stage label checks and comment handling.

## Prompt Cache & Memory System

- Good news: both `plan` and `review_autofix` already try to build stable prompt prefixes:
  - `plan.yml:856-863` pre-assembles static context.
  - `review_autofix.yml:1680-1692` pre-assembles unattended instructions / AGENTS / trimmed README.
- Bad news: prompt-cache effectiveness is not observable right now.
  - Deep-dive summary: `or_calls=76`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, `cache_hit_rate=null`.
  - Wider `analysis_context`: `or_calls=100`, same zero cache fields, same null hit rate.
- The only cache hit clearly visible in logs was package/tooling cache, not prompt cache: plan run `28216501931` logged `Cache hit occurred on key setup-uv... not saving cache.`
- There were **no** `CONTEXT_BUDGET_WARN` or `BREAK_GLASS` events. Run `28216179473` exposed `CONTEXT_BUDGET_WARN_RATIO: 0.7`, but no actual warn lines fired.
- Memory retrieval itself looks healthy (see previous section), so the weak point is observability, not retrieval quality.
- Recommended changes:
  1. plumb OR token/cache fields into emitted telemetry;
  2. keep dynamic progress text, IDs, and volatile status strings below the stable prefix where possible *(inference)*;
  3. preserve Semble’s targeted retrieval, since its footprint is small and focused.
- Estimated impact: once measurable, likely `5–15%` repeated-run token/latency savings *(inference)* plus earlier warning of prompt growth.

## Orchestrator Health

- `orchestrate_poll` looked healthy in this sample: `41/41` success, `p50=201s`, `p95=244s`. Evidence-grade summary for run `28216359646` shows a single batched query finding `1 active tracking issue`.
- `clarify` has a sensible orchestrator fast path: `clarify.yml:395-398` defers orchestrator-managed issues, and `clarify.yml:472-485` relabels to `ai:planning` and auto-posts `/answer`.
- The main orchestration pain point is **coverage/noise**, not hard failure:
  - `orchestrate_clarify_respond` had `176` runs and `176` skips.
  - No successful `orchestrate_clarify_respond` run was captured, so the unattended clarification auto-answer path lacks fresh smoke evidence.
- `validation_refresh` is the main orchestrator-scale bottleneck: run `28215278870` spent `1483s` and skipped three target repos because of budget exhaustion.
- No stuck-state or fallback-storm signature was visible:
  - `break_glass_count=0`
  - `context_budget_warn_count=0`
  - no Semble fallback lines
  - no Serena runtime/probe failures
- Smallest safe mitigations:
  1. add one smoke test that exercises a full successful `orchestrate_clarify_respond` path each release;
  2. track `validation_refresh skipped_budget count`;
  3. watch `review_autofix cancelled count` as the best stale-work signal.
- Observable indicators teams should track:
  - active tracking issue count
  - `orchestrate_poll` p95 duration
  - `validation_refresh` skipped-budget count
  - `review_autofix` cancelled count
  - `cache_hit_rate` once fixed
  - `context_budget_warn_count`

## Pipeline Flow Bottlenecks

| Stage | Evidence | Dominant bottleneck | Recommendation |
|---|---|---|---|
| Clarify | `184` runs, `176` skipped, `p50=1s`, `p95=11s` | Event-routing noise, not compute | Consolidate wrapper routing later; not first priority |
| Plan | `176` runs, `8` real successes; successful runs `377–605s`; run `28216501931` took `552s` with `gpt-5.4` `xhigh` | Model compute + GH comment orchestration | Lower reasoning on low-risk cases and trim progress-comment churn |
| Implement | `176` runs, `7` real successes; successful runs include `1325s`, `1155s`, `862s`, `680s`, `580s` | AI compute, but less evidence than review | Keep as second-order target after review/autofix |
| Review / Autofix | `99` runs, `p50=245s`, `p95=3470.1s`, `13` cancellations; top 8 longest runs all here | Dominant compute path (`codex-agent`) plus stale queued work | Add superseded-head exits and enable low-risk review tiering |
| Validate / Orchestrate | CI `p50=1691.5s`, `p95=1790.2s`; `validation_refresh` run `28215278870` took `1483s`; `orchestrate_poll` much smaller | CI compute and refresh-budget exhaustion | Fix deterministic CI failure; rebalance refresh ordering/budget |

Breakdown by bottleneck type:
- **Queueing:** minor in sampled logs; e.g. slow cancelled run `28165172221` showed only brief runner wait.
- **Compute:** `review_autofix` and `ci` dominate.
- **Retry:** `plan.yml` can do up to 3 Codex attempts with `10s`/`20s` backoff; this is not the main sampled cost, but it lengthens bad runs.
- **Merge/conflict overhead:** present in `review_autofix`, but the sampled hot late stage was `post-merge-validate-dispatch`, not conflict healing.

Ordered by end-to-end impact:
1. stale-review soft exits
2. low-risk review tiering / reasoning reduction
3. fix deterministic CI lint failure
4. trim review post-merge validate-dispatch lookups
5. rebalance validation refresh budget
6. clean up comment-trigger fan-out

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix`: `99` runs, `p95=3470.1s`, `13` cancellations
  - `ci`: `18` runs, `p50=1691.5s`, `p95=1790.2s`
  - `validation_refresh`: run `28215278870` at `1483s`
- **Top failure modes**
  - Ruff `E731` at `scripts/slop_scan_local.py:425` (`28206317872`, `28211580640`, `28215501481`, `28216181255`)
  - checkout post-job cleanup failure from leaked `GIT_DIR`/`GIT_WORK_TREE` (`28147004758`, `28147019610`)
  - stale/cancelled `review_autofix` work (`13` cancellations)
- **Highest-cost drivers**
  - six-model `review_autofix` path with `xhigh` editor reasoning
  - `xhigh` plan run `28216501931`
  - missing OR token/cache telemetry, which hides real prompt/model spend
- **Top 3 prioritized actions**
  1. scope `GIT_DIR`/`GIT_WORK_TREE` to shell steps only in `review_autofix`
  2. add superseded-head exits and enable low-risk review tiering
  3. fix the Ruff `E731` defect and repair OR token/cache telemetry emission

## Metrics Appendix

### Window summary

| Scope | Total runs | Success | Failure | Cancelled | Skipped/other | Started success rate | Avg duration (s) | p50 (s) | p95 (s) | Total runtime (h) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| All sampled runs | 952 | 242 | 6 | 14 | 690 | 92.4% | 195.9 | 2.0 | 1716.0 | 51.8 |

### Workflow-family metrics

| Family | Total | Success | Failure | Cancelled | Other/skipped | p50 (s) | p95 (s) | Avg (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 99 | 83 | 2 | 13 | 1 | 245.0 | 3470.1 | 1343.7 |
| ci | 18 | 13 | 4 | 1 | 0 | 1691.5 | 1790.2 | 1326.5 |
| orchestrate_poll | 41 | 41 | 0 | 0 | 0 | 201.0 | 244.0 | 205.3 |
| validation_refresh | 1 | 1 | 0 | 0 | 0 | 1483.0 | 1483.0 | 1483.0 |
| copilot_pull_request_reviewer | 22 | 22 | 0 | 0 | 0 | 247.5 | 382.6 | 263.4 |
| plan | 176 | 8 | 0 | 0 | 168 | 1.0 | 14.0 | 25.3 |
| implement | 176 | 7 | 0 | 0 | 169 | 1.0 | 11.0 | 35.5 |
| clarify | 184 | 8 | 0 | 0 | 176 | 1.0 | 11.0 | 7.9 |
| orchestrate_clarify_respond | 176 | 0 | 0 | 0 | 176 | 1.0 | 10.0 | 3.2 |
| issue_pr_status | 11 | 11 | 0 | 0 | 0 | 69.0 | 80.0 | 56.4 |
| integration_pr_readiness | 16 | 16 | 0 | 0 | 0 | 8.5 | 11.5 | 8.7 |
| lint_pr_body_auto_close | 16 | 16 | 0 | 0 | 0 | 8.5 | 10.0 | 8.2 |
| cancel_on_pr_close | 11 | 11 | 0 | 0 | 0 | 12.0 | 15.5 | 11.5 |
| forward_merge_stable_to_main | 2 | 2 | 0 | 0 | 0 | 31.5 | 31.9 | 31.5 |
| nightly_validation_selftest | 1 | 1 | 0 | 0 | 0 | 122.0 | 122.0 | 122.0 |
| promote_main_to_stable | 1 | 1 | 0 | 0 | 0 | 31.0 | 31.0 | 31.0 |
| workspace_cache_maintenance | 1 | 1 | 0 | 0 | 0 | 7.0 | 7.0 | 7.0 |

### Telemetry coverage and cost metrics

| Telemetry scope | Runs with log telemetry | Codex tokens | Codex calls | OR calls | OR prompt tokens | OR total tokens | OR cache read | OR cache write | `cache_hit_rate` | `wall_clock_p50_ms` | `wall_clock_p99_ms` | Semble calls | Semble bytes | Serena calls | Serena response bytes | `break_glass_count` | `context_budget_warn_count` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Deep-dive `summary.json` bundle | 31 | 20262 | 14 | 76 | 0 | 0 | 0 | 0 | null | 71000 | 3911600 | 9 | 100724 | 0 | 0 | 0 | 0 |
| Wider provided `analysis_context` | 116 | 20262 | 14 | 100 | 0 | 0 | 0 | 0 | null | 2000 | 3860800 | 15 | 157824 | 0 | 0 | 0 | 0 |

Note: the deep-dive bundle is biased toward `errors/`, `slow/`, and `recent/`; the wider `analysis_context` includes more unselected runs, which explains the lower wall-clock p50 there.

### Semble / Serena telemetry

| Server | Target | Calls | Bytes / response bytes | Total ms | Avg ms | Fallbacks | Tool calls |
|---|---|---:|---:|---:|---:|---:|---:|
| Semble | reviewer-context | 6 | 81565 | 3103 | 517.2 | 0 | n/a |
| Semble | overflow | 3 | 19159 | 1532 | 510.7 | 0 | n/a |
| Serena | all targets | 0 | 0 | 0 | 0.0 | 0 | 0 |

### Per-target MCP availability

| Server | Target | `probe_ok` | `probe_failed` | `probe_skipped` | Notes |
|---|---|---:|---:|---:|---|
| Serena | all targets | 0 | 0 | 0 | No real `SERENA_PROBE` lines observed; sampled review logs showed `SERENA_ENABLED:false` |
| Semble | n/a | n/a | n/a | n/a | No probe telemetry family emitted for Semble in this bundle |

**Other MCP servers observed:** none.

### AI memory metrics

| Metric | Value |
|---|---|
| Runs with AI memory telemetry | 11 |
| Deduped telemetry events | 41 |
| `retrieve` ops | 9 |
| Retrieve hit rate | 100% |
| Avg `estimated_tokens` | 413 |
| Avg `token_budget` | 1377.8 |
| `keyword_method` distribution | `llm=9`, `plain=0`, `none=0` |
| `records_selected=0` | 0 |
| `fail_open:true` | 0 |
| `enabled:false` | 0 |
| Push retries > 1 | 1 (`28147019610`) |

### GH API hotspot summary

| Workflow / step | Evidence | Observed pattern | Estimated avoidable calls |
|---|---|---|---|
| `plan` main path | `plan.yml:395`, `434`, `500-508`, `666-668`, `848-850`, `1132-1163`; run `28216501931` | repeated issue/comment/timeline reads, progress comment create+patch, per-comment deletes | `3–6` per successful plan run + `N` deletes |
| `clarify` clear path | `clarify.yml:430-435`, `1163-1167` | optional duplicate comment fetch + two POST comments | `1–2` per successful clarify run |
| `review_autofix` post-merge validate dispatch | `review_autofix.yml:824-887`; run `28216179473` | fallback GraphQL/REST plus per-linked-issue lookups | up to `1` per linked issue |
| `orchestrate_poll` tracking scan | `orchestrate_poll.yml:155-160`; run `28216359646` | already batched | none |

### Skip fan-out metrics

| Family | Skipped count | Skipped total seconds | Avg skipped runtime (s) |
|---|---:|---:|---:|
| clarify | 176 | 589 | 3.35 |
| plan | 168 | 473 | 2.82 |
| implement | 169 | 561 | 3.32 |
| orchestrate_clarify_respond | 176 | 562 | 3.19 |

### Cancelled review waste

| Metric | Value |
|---|---:|
| Cancelled `review_autofix` runs | 13 |
| Total cancelled runtime | 21806 s |
| Avg cancelled runtime | 1677.4 s |
| Largest cancellations | `28165172221` 3507s; `28189741718` 3415s; `28195801261` 3274s; `28173616084` 3114s |

## Deep Audit — Workflows & Scripts (2026-06-26)

### Section 1: Bug & Correctness Sweep

I audited all `.github/workflows/*.yml` and `scripts/*.sh|*.py`; to avoid duplicating the current in-progress report, I omitted the already-documented stale-review, review-tiering, post-merge validation, `GIT_WORK_TREE`, Ruff, validation-refresh, and OpenRouter-telemetry items.

#### BUG-001
- **File path**: `scripts/tg_helpers.sh:167-205,240-276`
- **Severity**: Medium
- **Category tag**: `bug`
- **Description**: `tg_store_msg_id()` and `tg_store_phase_msg_id()` both implement a read-comment → mutate-body → `PATCH` flow. If two notifications for the same issue/phase run concurrently, both can read the same old marker comment, append different Telegram IDs locally, and then race to overwrite the same GitHub comment; the last `PATCH` wins and silently drops the earlier ID. The cleanup paths later trust those stored IDs (`scripts/tg_helpers.sh:330-368,399-439`), so a lost update can strand Telegram messages and/or leave stale tracking comments behind.
- **Recommended fix**: Stop coalescing multiple IDs into one mutable marker comment. The lowest-risk change is one tracking comment per message ID/phase ID (the cleanup loops already scan all matching comments), or add an optimistic re-read/retry loop before `PATCH` so conflicting writes are retried instead of overwritten.

#### SEC-001
- **File path**: `scripts/issue_attachment_bundle.py:129-171`
- **Severity**: Medium
- **Category tag**: `security`
- **Description**: `_download()` allows both `http` and `https`, does not reject private/loopback hostnames before `urlopen`, and never validates the final redirect target after redirects complete. The safer attachment fetch path in `scripts/ai_context_utils.py:361-409` already rejects non-HTTPS URLs, private/loopback hosts, and redirect-to-private targets. This bundler is only syntax-checked in CI today (`.github/workflows/ci.yml:127-147`), so I did not prove a live workflow execution path. [NEEDS VERIFICATION]
- **Recommended fix**: Replace `_download()` with a thin wrapper around `ai_context_utils.fetch_attachment()`, or port the exact hostname and post-redirect checks from `scripts/ai_context_utils.py:361-409` before any bytes are downloaded.

### Section 2: GitHub API Call Redundancy Audit

#### API-001
- **File path**: `scripts/review_rb_judge.sh:735-787`
- **Severity**: Medium
- **Category tag**: `api-batching`
- **Description**: The judge first does one GraphQL call that fetches only closing-issue numbers (`735-740`), then loops over those numbers and calls `_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}"` until it finds a non-empty body while also harvesting labels (`772-786`). That is an N+1 hydration pattern in a latency-sensitive review-blocked path.
- **Current call count**: 1 GraphQL call + up to `N` REST issue GETs (`N` = linked issues examined, capped at 50 by the upstream GraphQL query).
- **Proposed call count**: 1 GraphQL call.
- **Batching pattern to extend**: Promote the alias-based GraphQL hydration pattern already implemented in `scripts/review_collect_pr_metadata.sh:70-172` into a shared helper (ideally under `scripts/gh_helpers.sh`) so the judge can reuse it.
- **Recommended fix**: Extend the initial GraphQL query to fetch `number`, `body`, and label names for each closing issue, then choose `FIRST_ISSUE`, `FIRST_ISSUE_BODY`, and `FIRST_ISSUE_LABELS_JSON` locally with no per-issue REST loop.

#### API-002
- **File path**: `scripts/pr_checks_lib.sh:67-119,143-178`
- **Severity**: Medium
- **Category tag**: `api-redundancy`
- **Description**: `_pr_required_check_names_for_base()` explicitly issues one branch-protection GET per invocation (`67-71`), and `_pr_checks_completed()` calls it every time a base ref is supplied (`174-178`). In one `scripts/orchestrate_poll_process.sh` shell, the same helper is invoked at `7196`, `15095`, `15152`, `15419`, and `15514` against the same repo/base branch, so identical protection data can be re-fetched up to five times in one poll cycle; `scripts/review_rb_judge.sh:2037-2039` does the same in its own process.
- **Current call count**: 1 protection GET per `_pr_checks_completed(..., base_ref)` call; currently up to 5 identical GETs in one orchestrator cycle, plus 1 more in the standalone judge.
- **Proposed call count**: 1 GET per distinct `repo/base_ref` per process.
- **Batching pattern to extend**: Extend the within-process state reuse already inherent in `scripts/orchestrate_poll_process.sh` by memoizing resolved required-check CSVs inside `pr_checks_lib.sh`.
- **Recommended fix**: Add an associative-array or temp-file cache keyed by `${repo}:${base_ref}` inside `pr_checks_lib.sh`, and return the cached branch-protection contexts on subsequent calls in the same process.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **File path**: `.github/workflows/review_autofix.yml:4364-4398,4555-4588,5427-5445`
- **Severity**: Low
- **Category tag**: `duplication`
- **Description**: `review_autofix.yml` carries three inline late-stage fallback implementations of `ensure_label_exists()` / `set_issue_phase_label_resilient()`: one for `ai:ready-to-merge`, one for `ai:review-blocked` after judge exhaustion, and one for workflow-failure labeling. The bodies are nearly identical but already diverge in label color/description and warning text, which raises drift risk in the most failure-prone part of the workflow.
- **Recommended fix**: Extract one shared fallback helper into `scripts/label_helpers.sh` (or a dedicated `scripts/review_label_fallback.sh`) with a signature like `set_issue_phase_label_resilient <issue_number> <target_label> <repo> [color] [description]`, and update all three late-stage callers to use it.

#### DUP-002
- **File path**: `.github/workflows/review_autofix.yml:4401-4423,4591-4613,5450-5471`
- **Severity**: Low
- **Category tag**: `duplication`
- **Description**: Three separate late-stage branches re-implement the same linked-issue resolution ladder: use `LINKED_ISSUES_JSON`, else `LINKED_ISSUE_FALLBACK_NUMBERS_JSON`, else mine `PR_META_FILE` / PR title+body for repo-scoped issue refs. Because the resolver is copied rather than shared, any future parsing or fallback fix must be kept in sync across ready-to-merge, review-blocked, and workflow-failure branches.
- **Recommended fix**: Move this logic into `scripts/review_collect_pr_metadata.sh` or a new `scripts/linked_issue_helpers.sh` helper with an interface like `resolve_linked_issue_numbers <repo> <pr_number> <pr_meta_file>`, and have all three branches consume the same function.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001
- **File path**: `.github/workflows/memory_maintenance.yml:45-391`
- **Severity**: Medium
- **Category tag**: `expression-limit`
- **Description**: The `Run repository learnings extraction` step embeds a long shell wrapper plus two inline Python heredocs and several `${{ github.* }}` interpolations in one `run:` block. A raw step-template scan puts this block at about `15,168` characters from `run:` line 45 through line 391, leaving roughly `5,832` characters of headroom before GitHub’s `21,000`-character expression ceiling. That is above the requested `15,000`-character medium-risk threshold and close enough to prior repo history to be fragile.
- **Recommended fix**: Extract the two inline Python programs into `scripts/` (for example `scripts/memory_extract_sources.py` and `scripts/memory_extract_learnings.py`) and keep the workflow step as a short shell wrapper that passes file paths and env vars.

No other interpolated `run:` block in the audited workflows exceeded `15,000` characters, and no workflow file exceeded the `800 KB` warning threshold; the largest current workflow is `.github/workflows/review_autofix.yml` at `346,366` characters.

### Section 5: Cross-Cutting Concerns

#### CONSIST-001
- **File path**: `scripts/tg_helpers.sh:169-205,241-276,332-368,401-439`
- **Severity**: Low
- **Category tag**: `consistency`
- **Description**: `tg_helpers.sh` uses `curl_gh_api` for its read paths (`169-172`, `241-244`, `332-335`, `401-404`) but drops to raw `curl` for the corresponding POST/PATCH/DELETE writes (`175-179`, `194-198`, `201-205`, `246-250`, `266-270`, `272-276`, `364-368`, `435-439`). That bypasses the rate-limit/header-aware retry logic in `scripts/gh_helpers.sh:630-679`, and the trailing `|| true` makes transient write failures invisible.
- **Recommended fix**: Route all GitHub REST writes in this helper through `curl_gh_api` (or a thin `gh_helpers.sh` wrapper for issue-comment create/update/delete) so reads and writes share the same retry and rate-limit handling.

#### DEBT-001
- **File path**: `scripts/comprehensive_test_and_release_gh_api.sh:3-68`
- **Severity**: Medium
- **Category tag**: `tech-debt`
- **Description**: This script reimplements GitHub API retry as `gh_api_safe*`, but it only treats stderr containing `rate limit` as retriable and otherwise returns failure immediately. That duplicates a weaker version of functionality already centralized in `scripts/gh_helpers.sh:391-679` (permanent-failure classification, exponential backoff, reset-aware waits, JSON-safe wrappers). It is sourced in `.github/workflows/comprehensive-test-and-release.yml:56,287`, `.github/workflows/test-and-mark-stable.yml:495,616,822,1298,2535`, and `scripts/dispatch_and_watch_workflow_run.sh:5-7`, so GitHub failures are handled inconsistently across release/test flows.
- **Recommended fix**: Retire `gh_api_safe*` in favor of sourcing `scripts/gh_helpers.sh` and keeping only truly workflow-specific convenience wrappers (for example, a thin `list_dispatch_runs()` around `gh_retry gh api ...`).

No `TODO`, `FIXME`, or `HACK` markers were present under `.github/workflows` or `scripts`. Targeted shellcheck only surfaced low-signal SC2034/SC1007 notes, not a stronger standalone shellcheck defect beyond the findings above.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 6 | BUG-001, SEC-001, API-001, API-002, EXPR-001, DEBT-001 |
| Low | 3 | DUP-001, DUP-002, CONSIST-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 2-3 | Medium |
| Code modularization | 2-4 | Medium |
| Expression size reduction | 3 | Medium |
| Medium/Low fixes | 4-6 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-06-26)

### Safety Tag Legend
`SAFE_TO_MERGE` means the consolidation is statically proven to preserve endpoint scope, filters, retry/error behavior, and local execution boundaries; `NEEDS_VERIFICATION` means the overlap is real but at least one pagination/live-data/error-path assumption still needs a human check; `RISKY_SKIP` means the redundancy sits in a retry/recovery/pagination/race-defense path and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — RISKY_SKIP
- **File paths / lines:** `scripts/orchestrate_poll_process.sh:9769-9770`, `scripts/orchestrate_poll_process.sh:11846-11847`, `scripts/orchestrate_poll_process.sh:16035-16036`
- **Current call count:** 6 REST GETs across the three sites (2 per site)
- **Proposed call count:** 3 REST GETs across the three sites (1 per site)
- **Endpoint(s):** `GET /repos/{repo}/issues/{issue_number}`
- **Evidence:** each reissue branch fetches the same issue twice back-to-back, once for `.title` and once for `.body`.
```sh
orig_title="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.title // ""' || echo "")"
orig_body="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.body // ""' || echo "")"

orig_title="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.title' || echo "")"
orig_body="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.body // ""' || echo "")"

IF_TITLE="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${if_issue}" --jq '.title' || echo "")"
IF_BODY="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${if_issue}" --jq '.body' || echo "")"
```
- **Proposed fix:** add a tiny local helper in `scripts/orchestrate_poll_process.sh` that fetches the issue JSON once and extracts both title/body locally, then update all three reissue sites to consume that one payload.
- **Safety rationale:** this is inside `scripts/orchestrate_poll_process.sh` reissue/stall-recovery code, which the policy explicitly treats as `RISKY_SKIP`; it also changes today's independent fail-open behavior where title and body can succeed/fail separately.
- **Downstream signal:** Do not auto-implement; manual review must confirm that collapsing each title/body pair into one fetch preserves reissue behavior, fail-open semantics, and existing stall-recovery log output.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — NEEDS_VERIFICATION
- **File paths / lines:** `.github/workflows/clarify.yml:430-435` (both comment fetches), `.github/workflows/clarify.yml:528-537` (bounded comments consumer)
- **Current call count:** 2 GETs when `SEMANTIC_CACHE_BACKEND != none`
- **Proposed call count:** 1 GET when `SEMANTIC_CACHE_BACKEND != none`
- **Endpoint(s):** `GET /repos/{repo}/issues/{issue_number}/comments`
- **Evidence:** clarify first fetches the first 50 comments for prompt context, then immediately re-fetches the full comment history for semantic-cache canonicalization in the same step.
```sh
# Keep clarify prompt context bounded to preserve historical behavior.
gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=50" > "${ISSUE_COMMENTS_FILE}"

# Build full thread history only when semantic cache is enabled.
if ! gh_retry gh api --paginate --slurp "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=100" \
  | jq -r 'add // [] | .[] | "[" + (.created_at // "") + "] @" + (.user.login // "unknown") + ":\n" + (.body // "") + "\n"' > "${THREAD_HISTORY_FILE}"; then
```

```sh
echo "ISSUE COMMENTS"
# Comments are already in chronological order (direction=asc fetch).
jq -r '.[] | "[" + (.created_at // "") + "] @" + (.user.login // "unknown") + ":\n" + (.body // "") + "\n"' "${ISSUE_COMMENTS_FILE}"
```
- **Proposed fix:** in `Fetch issue comments`, when semantic cache is enabled, do one paginated fetch, flatten it once, write the first 50 chronological entries into `ISSUE_COMMENTS_FILE`, and write the full flattened history into `THREAD_HISTORY_FILE`; keep the current single 50-comment fetch on the no-semantic-cache path.
- **Safety rationale:** both calls are in the same workflow step with no intervening mutation, but the current bounded prompt context depends on exact pagination/window semantics (`per_page=50` vs `--paginate --slurp per_page=100`), so this is not safe without verification.
- **Downstream signal:** Verify on a test issue with more than 50 comments and `SEMANTIC_CACHE_BACKEND != none` that one paginated fetch reproduces the exact current `ISSUE_CONTEXT_FILE` first-50 comment window and `THREAD_HISTORY_FILE`, including the fail-open path when the full-history fetch errors.

#### REUSE-002 — NEEDS_VERIFICATION
- **File paths / lines:** `.github/workflows/issue_pr_status.yml:403-406` (earlier per-issue fallback fetch), `.github/workflows/issue_pr_status.yml:584-599` (later merged-alert re-fetch)
- **Current call count:** on the incomplete-classification path, up to `K` fallback issue GETs in the classifier and then up to `K` more issue GETs in merged-alert suppression (`K` = linked issues retried later)
- **Proposed call count:** `K + U`, where only unresolved `U <= K` issue numbers are retried in the alert step
- **Endpoint(s):** `GET /repos/{repo}/issues/{issue_number}`
- **Evidence:** when orchestrator classification falls back to per-issue REST and one lookup fails, the later Telegram step re-reads every linked issue instead of only retrying the unresolved ones.
```sh
while IFS= read -r _orch_num; do
  [ -n "${_orch_num}" ] || continue
  [[ "${_orch_num}" =~ ^[0-9]+$ ]] || continue
  _orch_meta="$(gh_retry gh api "repos/${REPOSITORY}/issues/${_orch_num}" --jq '{labels:[.labels[].name], body:(.body // "")}' 2>/dev/null || echo '')"
```

```sh
elif [ "${ORCHESTRATOR_CLASSIFICATION_COMPLETE:-false}" != "true" ] && [ -n "${LINKED_ISSUE_NUMBERS:-}" ]; then
  echo "::warning::Reused orchestrator classification is incomplete; falling back to per-issue body lookup for PR merged alert suppression."
  while IFS= read -r issue_number; do
    [ -n "${issue_number}" ] || continue
    ISSUE_IS_MANAGED="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '
      ((.labels // []) | map(.name) | index("ai:orchestrator-tracking")) == null
      and (
        ((.labels // []) | map(.name) | index("ai:orchestrator-managed")) != null
        or ((.body // "") | contains("Managed by: AI Orchestrator"))
      )
    ' || echo "")"
```
- **Proposed fix:** in `Update linked issue labels when PR closes`, export an unresolved-issue list (for example `UNRESOLVED_ORCHESTRATOR_ISSUES`) alongside `MANAGED_ISSUES`/`TRACKING_ISSUES`; in `Telegram PR merged notification`, retry only those unresolved issue numbers instead of looping all `LINKED_ISSUE_NUMBERS`.
- **Safety rationale:** the later step only needs the same `labels/body` classification data, and the intervening logic does not mutate those fields, but this is a cross-step retry-after-failure path, so alert-suppression behavior must be verified before narrowing the retry set.
- **Downstream signal:** Force one `_orch_meta` lookup failure in a PR with multiple linked issues, then verify that retrying only unresolved issue numbers still suppresses the merged alert when the failed issue is orchestrator-managed and still sends the alert when unresolved issues are standalone.

#### REUSE-003 — RISKY_SKIP
- **File paths / lines:** `scripts/orchestrate_poll_process.sh:11131-11132`, `scripts/orchestrate_poll_process.sh:11163-11168`, `scripts/orchestrate_poll_process.sh:11564-11567`, `scripts/orchestrate_poll_process.sh:12042-12049`
- **Current call count:** +1 extra REST comments GET per standalone `auto_respond_clarify` action on top of the loop’s already-loaded comment snapshot
- **Proposed call count:** +0 extra GETs; reuse the already-loaded `comments_json`
- **Endpoint(s):** current extra call hits `GET /repos/{repo}/issues/{issue_number}/comments`; reusable upstream data already comes from `_fetch_candidate_issue_details_graphql` or the loop’s fallback `GET /repos/{repo}/issues/{issue_number}/comments?sort=created&direction=desc&per_page=100`
- **Evidence:** the standalone stall loop already loads `comments_json` for each candidate, then `extract_recommended_answers()` re-fetches recent comments again before posting `/answer`.
```sh
_candidate_details_json="$(_fetch_candidate_issue_details_graphql "$(printf '%s' "${candidates}" | jq -c '[.[].number]')")"

if printf '%s' "${_candidate_details_json}" | jq -e --arg n "${issue_num}" 'has($n)' >/dev/null 2>&1; then
  labels_json="$(printf '%s' "${_candidate_details_json}" | jq -c --arg n "${issue_num}" '.[$n].labels // []')"
  comments_json="$(printf '%s' "${_candidate_details_json}" | jq -c --arg n "${issue_num}" '.[$n].comments // []')"
else
  labels_json="$(get_issue_labels_json "${issue_num}")"
  comments_json="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments?sort=created&direction=desc&per_page=100" | jq -s 'add // []' 2>/dev/null || echo '[]')"
fi
```

```sh
case "${action}" in
  auto_respond_clarify)
    rec_answers="$(extract_recommended_answers "${issue_num}")"
```

```sh
extract_recommended_answers() {
  local issue_num="$1"
  local comments_json
  comments_json="$(gh_retry gh api \
    "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments?sort=created&direction=desc&per_page=50" \
    2>/dev/null || echo "[]")"
```
- **Proposed fix:** extend `extract_recommended_answers` to accept optional preloaded `comments_json`; call it from the standalone loop with the already-loaded `comments_json`, and keep the current live fetch only as the helper’s fallback when no preloaded payload is supplied. This reuses the existing `_fetch_candidate_issue_details_graphql` comment payload shape.
- **Safety rationale:** this lives inside `scripts/orchestrate_poll_process.sh` standalone stall recovery, which the policy marks `RISKY_SKIP`; it also changes a race-defense path that currently re-reads a newest-first live window before auto-answering.
- **Downstream signal:** Do not auto-implement; manual review must confirm that the cached comment window (`comments(last:100)` or the existing REST fallback) always exposes the latest clarification-question marker and that no stall-recovery log keys or fail-open behaviors change.

### Dead Calls (DEAD-API-###)
No findings.

### Cross-References to Deep Audit Section
- API-001: NEEDS_VERIFICATION — agreed; batching the linked-issue body/label hydration into the first GraphQL query is directionally correct, but `FIRST_ISSUE`, `FIRST_ISSUE_BODY`, and `FIRST_ISSUE_LABELS_JSON` selection semantics should be spot-checked against the current first-non-empty-body loop.
- API-002: NEEDS_VERIFICATION — agreed; process-local branch-protection memoization looks valid, but confirm no caller depends on a second live read after a first failure or after mid-process branch-protection edits.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | REUSE-001, REUSE-002 |
| RISKY_SKIP | 2 | MERGE-001, REUSE-003 |

### Implement-Stage Handoff
No SAFE_TO_MERGE findings in this pass.
