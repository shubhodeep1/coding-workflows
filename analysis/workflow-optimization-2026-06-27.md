## Executive Summary

- **Fix the CI actionlint regression first.** `ci` failed 7/12 times (58.3%) in this window, and every inspected failure (`28274939719`, `28277123171`, `28278953505`, `28280508343`) died in `lint / Actionlint — reusable workflows and consumer templates` on `.github/workflows/workflow-log-analysis.yml` with `context "env" is not allowed here` at lines `62` and `192`. **Estimated impact:** recover most current CI failures immediately. **Confidence:** high.
- **`workflow_log_analysis` is failing hard on what the collector tests define as soft-fail conditions.** The family failed `26/26` runs; 11 deep-dive error rows were `partial_data:missing_log_archive` 404s for `actions/runs/{id}/logs`, while `tests/test_collect_workflow_logs.py` explicitly expects 404/410 archives to classify as `partial_data:missing_log_archive`, retry once, and cache the result. **Estimated impact:** move this family from 0% success to near-healthy for missing-archive cases. **Confidence:** high.
- **`review_autofix` is the dominant latency bottleneck.** Family metrics: `72` runs, `p95=3539.4s`, `avg=1270.6s`; top slow runs were `28251534442` (`4195s`), `28275433276` (`3989s`), `28278017231` (`3607s`), `28259440385` (`3546s`). Sampled `review_codex-agent` logs consumed `3246.6s`, `3973.9s`, and `3554.6s` by themselves. **Estimated impact:** 15–40 minute p95 reduction if prompt growth and stale-run waste are addressed. **Confidence:** medium.
- **Cancelled `review_autofix` runs are burning real wall time.** There were `12` cancelled runs totaling `18,937s` (`5.26h`), averaging `1578s`; the worst were `28259428677` (`3534s`), `28252342304` (`3247s`), and `28274217765` (`3236s`). **Estimated impact:** large speed/cost win from earlier supersession checks and faster cancellation. **Confidence:** medium.
- **Implement dominates measured AI cost.** `implement` used `2,652,382 / 2,674,669` measured Codex tokens (`99.17%`) across `26` calls, about `102,015` tokens/call; `review_autofix` also recorded `101 or_calls` with zero prompt/completion token telemetry, so some model cost is currently invisible. **Estimated impact:** every 10% implement-token reduction saves ~`265k` tokens per 1000-run window. **Confidence:** high.
- **AI memory is healthy; Serena is effectively inactive.** Deep-dive AI memory retrievals hit `9/9` times, averaged `635` estimated tokens against a `1400` budget, and used `keyword_method="llm"` every time. Semble recorded `28` queries and `0` fallbacks; Serena recorded `0` queries, `0` fallbacks, and `0` probes. **Estimated impact:** keep AI memory/Semble, improve telemetry around availability and cache hits. **Confidence:** high.

## Speed Optimizations

1. **Trim the `review_autofix` critical path inside `review_codex-agent`** (**critical-path win**).
   - **Evidence:** `review_autofix` had `p95=3539.4s`; slow runs `28251534442`, `28275433276`, `28278017231`, and `28259440385` all exceeded `3546s`. In those runs, `step-001-review_codex-agent.log` alone took `3246.6s`, `3973.9s`, and `3554.6s`. Prompt growth is visible in-line: run `28275433276` logged `summariser (pass1) prompt_bytes=39948`, `summariser (review) prompt_bytes=61452`, then `stage=consolidator ... input_bytes=259186`; run `28259440385` showed `21839`, `38222`, and `226175` bytes respectively.
   - **Root cause:** review context is growing through multiple summariser/review/consolidation phases, and Semble overflow lookups show the bundle is spilling past the initial targeted context.
   - **Exact change:** dedupe reviewer inputs before summarisation, skip overflow file fetches unless the file is changed or directly implicated, and gate the second summariser/consolidation pass on actual disagreement/unresolved conflicts rather than always running the full path.
   - **Estimated savings:** ~5–15 minutes on current `review_autofix` p95 runs.
   - **Implementation risk:** medium; quality should be spot-checked on multi-file and disputed-review cases.

2. **Abort stale or superseded `review_autofix` runs much earlier** (**critical-path win for wasted work and queue pressure**).
   - **Evidence:** `12` cancelled `review_autofix` runs consumed `18,937s` total (`5.26h`), average `1578s`, max `3534s` (`28259428677`). Three cancelled runs are among the top 15 slowest review runs.
   - **Root cause:** **inference**: review jobs are continuing long after the PR/head state makes them obsolete.
   - **Exact change:** add cheap head-SHA / PR-state checks before the expensive reviewer phase and again between major phases (before/after summariser and before consolidation); if superseded or closed, exit cleanly with a neutral/skipped result.
   - **Estimated savings:** ~26 minutes on the average cancelled review run; also reduces runner contention for subsequent runs.
   - **Implementation risk:** low if the checks fail open.

3. **Reduce queue exposure by preventing no-op workflow fan-out before dispatch** (**critical-path win on tail latency**).
   - **Evidence:** runner-wait lines appeared in `10/10` deep-dive `review_autofix` runs and `4/4` deep-dive CI failures. Run `28279316884` spent about `3279s` in the job system log before the agent step even started. `orchestrate_poll` runs `28281510055`, `28280671637`, and `28282341733` also logged `Job is waiting for a hosted runner to come online.` Meanwhile, `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` produced `788` `other/skipped` runs combined.
   - **Root cause:** too many workflows are being dispatched only to skip later, increasing runner acquisition pressure.
   - **Exact change:** move “should this run at all?” gating into the caller/orchestrator before dispatching downstream workflows; keep concurrency cancellation, but make the no-op decision earlier.
   - **Estimated savings:** ~3–55 minutes on queue-tailed runs, plus less cross-workflow interference.
   - **Implementation risk:** medium; caller-side gating needs careful equivalence testing.

4. **Deduplicate support-source checkout across clarify/plan/orchestrate** (**micro-optimization**).
   - **Evidence:** the same “resolve `SCRIPT_REF` → checkout `.codex-workflow-src` → fallback checkout” pattern is repeated in `.github/workflows/orchestrate.yml:157-189`, `plan.yml:218-257`, and `clarify.yml:165-204`.
   - **Root cause:** each active workflow redoes the same remote support-source checkout.
   - **Exact change:** resolve the support ref once per top-level pipeline and pass it forward as an input/output, or stage the support source once and reuse it across called workflows in the same run.
   - **Estimated savings:** low single-digit seconds per active workflow.
   - **Implementation risk:** low.

## Cost Optimizations

1. **Attack `implement` token volume first.**
   - **Evidence:** `implement` used `2,652,382` measured Codex tokens across `26` calls (`~102,015` tokens/call), which is `99.17%` of all measured Codex spend in the window. The success-path outlier `28280902629` ran `1672s` even though setup caches hit.
   - **Root cause:** **inference**: the implement prompt/context bundle is the main measured token sink.
   - **Exact change:** feed implement from a compact plan/delta artifact first, include unchanged helper files only on demand, and prefer targeted retrieval over broad prompt expansion.
   - **Estimated savings:** every 10% reduction saves about `265k` tokens per 1000-run window; a 20% target saves about `530k`.
   - **Quality-risk notes:** medium; guard with regression tests on multi-file changes.

2. **Shrink `review_autofix` prompt expansion before it reaches large summariser/consolidator inputs.**
   - **Evidence:** run `28275433276` grew from `39948` and `61452` prompt bytes to a `259186`-byte consolidator input; `28262319625` hit `205440`; `28259440385` hit `226175`. Deep-dive Semble usage in review logged `8 reviewer-context` queries and `8 overflow` queries; overflow files included `README.md`, `scripts/ai_memory.py`, `scripts/ai_memory_lib.py`, `scripts/render_prompt.py`, and reviewer test/helpers.
   - **Root cause:** repeated review/summarise passes are expanding context, and overflow retrieval is compensating for that pressure.
   - **Exact change:** dedupe summaries, cap overflow to the top 1–2 ranked files, and only run high-reasoning consolidation when multiple reviewers or unresolved conflicts justify it.
   - **Estimated savings:** direct measured Codex savings are modest (`20,260` tokens total in the family), but likely larger on the `101 or_calls` currently missing token accounting.
   - **Quality-risk notes:** medium; keep the full path for disputed or safety-critical reviews.

3. **Complete non-Codex model cost telemetry before changing model mix.**
   - **Evidence:** repo totals show `101 or_calls`, but `or_prompt_tokens=0`, `or_completion_tokens=0`, `or_total_tokens=0`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, and `cache_hit_rate=null`. That makes review-side model cost mostly invisible.
   - **Root cause:** instrumentation gap, not necessarily zero cost.
   - **Exact change:** emit prompt/completion/cache metrics for every `or_call`, and compute a non-null `cache_hit_rate`.
   - **Estimated savings:** no direct savings by itself, but it is the prerequisite for correctly tuning review-side model selection and cache reuse.
   - **Quality-risk notes:** none.

4. **Keep Semble; reduce overflow triggers.**
   - **Evidence:** aggregate telemetry and actual log lines matched exactly at `28` Semble queries and `0` fallbacks. Average bytes/query were ~`10.1KB` overall, ~`11.8KB` in `review_autofix`, and ~`7.9KB` in `implement`. In the deep-dive sample, `reviewer-context` queries accounted for `119,140` bytes across `8` calls; `overflow` queries accounted for `69,926` bytes across `8` calls and only `3` runs.
   - **Root cause:** Semble itself looks efficient; overflow is the expensive symptom.
   - **Exact change:** keep `target=reviewer-context` retrieval, but demote `target=overflow` to a last resort after stricter ranking/deduplication.
   - **Estimated savings:** cuts logged Semble bytes and, more importantly, downstream prompt growth.
   - **Quality-risk notes:** low.

5. **Do not spend time on Serena optimization yet.**
   - **Evidence:** repo totals show `serena_query_calls=0`, `serena_fallbacks=0`, `serena_probe_ok=0`, `serena_probe_failed=0`, `serena_probe_skipped=0`, and no trustworthy runtime Serena query/fallback/probe lines were found.
   - **Root cause:** Serena is inactive in this window, not obviously malfunctioning.
   - **Exact change:** either leave it disabled or add explicit availability/probe telemetry before re-enabling.
   - **Estimated savings:** none in the current state.
   - **Quality-risk notes:** none.

## Reliability Improvements

1. **Repair the CI actionlint failure on invalid `env` context usage.**
   - **Failure evidence:** runs `28274939719`, `28277123171`, `28278953505`, and `28280508343` all failed in `lint / Actionlint — reusable workflows and consumer templates` with `.github/workflows/workflow-log-analysis.yml:62` and `:192` reporting `context "env" is not allowed here. available contexts are "github", "inputs", "needs", "vars"`.
   - **Root cause category:** GitHub Actions expression/context misuse.
   - **Exact fix:** replace workflow/job-level `if:` or expression references from `env.*` to `vars.*` or `needs.<job>.outputs.*`, depending on where the value is produced.
   - **Expected reliability impact:** should eliminate the dominant CI failure mode in this window (`7/12` CI failures).
   - **Rollback/fail-open:** low risk; actionlint remains the guardrail.

2. **Stop treating missing log archives as fatal `workflow_log_analysis` failures.**
   - **Failure evidence:** `workflow_log_analysis` failed `26/26` runs. The deep-dive error set contains `11` `partial_data:missing_log_archive` 404s (e.g. run `28281469012`). The collector tests named in `tests/test_collect_workflow_logs.py` explicitly expect 404/410 archive fetches to become cached `partial_data:missing_log_archive` soft-fails with one retry.
   - **Root cause category:** soft-fail classification is correct in the collector, but the workflow/reporting layer is still surfacing it as a hard failure.
   - **Exact fix:** catch classified `partial_data:missing_log_archive` exceptions in the analysis workflow, emit a degraded report with warnings, and reserve non-zero exit codes for unclassified collector failures.
   - **Expected reliability impact:** near-total recovery for this workflow family when archive absence is the only problem.
   - **Rollback/fail-open:** keep current hard-fail behavior only for unexpected exceptions.

3. **Fix the review consolidator’s trusted-directory failure instead of masking it forever.**
   - **Failure evidence:** runs `28251534442`, `28262319625`, `28275433276`, `28278017231`, `28256623501`, and `28259440385` all logged `stage=consolidator ... exit_code=1 ... failopen=1` with `stderr=Not inside a trusted directory and --skip-git-repo-check was not specified.`
   - **Root cause category:** environment/setup mismatch.
   - **Exact fix:** run the consolidator from the checked-out repo root and/or add the workspace to Git safe directories; if the git check is not needed, explicitly pass the equivalent skip flag.
   - **Expected reliability impact:** removes a repeated masked failure path and makes review outcomes more deterministic.
   - **Rollback/fail-open:** preserve current fail-open behavior until the fix is validated.

4. **Make cancellation/supersession checks happen before expensive review work.**
   - **Failure evidence:** `review_autofix` had `12` cancellations totaling `5.26h`; the longest cancelled runs were `3534s`, `3247s`, and `3236s`.
   - **Root cause category:** **inference**: obsolete work is not being stopped soon enough.
   - **Exact fix:** check PR open/head state before starting review, before long summariser phases, and before posting/applying results.
   - **Expected reliability impact:** fewer long cancellations, fewer reruns, and less runner contention.
   - **Rollback/fail-open:** fail open if state checks themselves error.

5. **Clarify break-glass and context-budget telemetry semantics.**
   - **Failure evidence:** aggregate telemetry shows `break_glass_count=0` and `context_budget_warn_count=0` repo-wide, but deep-dive review logs repeatedly echo `REVIEW_BREAK_GLASS_ENABLED: false` and `CONTEXT_BUDGET_WARN_RATIO: 0.7` (for example in run `28278017231`).
   - **Root cause category:** observability ambiguity, not an operational event.
   - **Exact fix:** emit explicit “configured threshold” vs “warning fired” events so dashboards do not confuse env/config echoes with live incidents.
   - **Expected reliability impact:** cleaner triage; no direct runtime change.
   - **Rollback/fail-open:** none needed.

**Fallback/probe status:** Semble recorded `0` fallbacks and `0` runtime fallbacks in this window; Serena recorded `0` queries, `0` fallbacks, and `0` probes. That looks like healthy Semble behavior plus an inactive Serena rollout, not a broken fallback storm. The smallest safe mitigation is to add an explicit “unavailable but fail-open” counter when `SEMBLE_AVAILABLE=false` (seen in `orchestrate_poll` run `28282341733`).

## AI Memory Health

- Deep-dive logs contained `42` `AI_MEMORY_TELEMETRY` rows:
  - `record-run-event`: `18`
  - `retrieve`: `9`
  - `record-candidate`: `8`
  - `write_lessons_learned`: `7`
- Retrieval quality was strong:
  - **Hit rate:** `9/9` (`100%`) returned `records_selected > 0`
  - **Average estimated tokens:** `635`
  - **Average token budget:** `1400`
  - **Keyword method:** `llm` in `9/9`
  - **Zero-record retrieves:** `0`
  - **`fail_open: true` retrieves:** `0`
  - **`enabled: false` retrieves:** `0`
  - **High push retries:** none; max observed `push_attempts=1`
- Representative evidence: run `28259440385` logged `{"op":"retrieve","records_selected":15,"estimated_tokens":635,"token_budget":1400,"keyword_method":"llm"}` before review context assembly.
- The main weakness is write-back, not retrieval:
  - `write_lessons_learned` failed open with `ok:false` in `2/7` sampled writes, specifically in runs `28275433276` and `28256623501`.
- No `finalize-task`, `promote`, `compact`, or processed-command telemetry appeared in the sampled deep-dive logs. That may be expected for these flows, but it is worth verifying that those operations are either intentionally absent or emitted elsewhere.

## GH API Call Audit

1. **`workflow_log_analysis` is re-hitting missing archives that should stay soft-failed.**
   - **Evidence:** `11` deep-dive error rows were `partial_data:missing_log_archive` 404s for `repos/.../actions/runs/{id}/logs`; the family failed `26/26` runs.
   - **High-redundancy pattern:** each analysis invocation is still paying for known-missing log-archive lookups.
   - **Concrete change:** keep the existing per-run soft-fail classification, but persist a negative cache of missing archive IDs for the current analysis window and do not fail the workflow on those cases.
   - **Estimated call-count reduction:** up to one `actions/runs/{id}/logs` call per already-known-missing run per analysis invocation.
   - **Rate-limit risk reduction:** medium.
   - **Repo-specific hygiene cross-check:** this aligns with the collector tests that assert one retry and cached soft-fail behavior for 404/410 archive fetches.

2. **`orchestrate_poll` is scanning for the same tracking issue every cycle.**
   - **Evidence:** runs `28281510055` and `28282341733` both found `1 active tracking issue(s)` via `gh issue list --label "ai:orchestrator-tracking"`; the family ran `33` times in the window.
   - **High-redundancy pattern:** label scan on every poll even in steady state.
   - **Concrete change:** cache the active tracking issue number in workflow state/output and relist only periodically or on cache miss.
   - **Estimated call-count reduction:** from at least one list call per poll run to a small number of relists plus direct issue gets.
   - **Rate-limit risk reduction:** low to medium; no 429/secondary limit was observed.

3. **Review-side GH API hygiene already exists, but call counts are not visible.**
   - **Evidence:** repo scripts already wrap many calls in `gh_retry` (`scripts/review_rb_judge.sh`, `scripts/review_apply_fixes.sh`, `scripts/review_conflict_prepare.sh`, `scripts/post_review_comment.sh`), and comments explicitly avoid retrying known non-transient cases like some 422s.
   - **Missed opportunity:** telemetry does not expose per-step call counts, so high-volume loops cannot be ranked from this window.
   - **Concrete change:** add lightweight per-step GH API counters to review/orchestrator scripts before changing behavior.
   - **Estimated call-count reduction:** unknown until instrumented.
   - **Rate-limit risk reduction:** mostly observability today; no secondary-rate-limit evidence was found.

## Prompt Cache & Memory System

- **Prompt-cache telemetry is effectively absent.** Repo totals show `cache_hit_rate=null`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, and there were `0` runs with non-null `cache_hit_rate` in `workflow_log_report.json`.
- **Workflow/build caches are working, but that is different from prompt caching.**
  - `plan` run `28280678475` hit both `setup-uv...` and `codex-v0.114.0-v2`.
  - `implement` run `28280902629` hit `setup-uv...`.
  - Those hits confirm build cache reuse, not prompt-cache reuse.
- **Cache fragmentation risk is high** (**inference**):
  - review prompts vary widely in size (`18KB` → `61KB` summariser prompts, `205KB` → `259KB` consolidator inputs),
  - overflow pulls bring in variable file sets (`README.md`, helper scripts, tests),
  - and there is no visible stable-prefix cache metric to prove reuse.
- **Concrete improvements:**
  1. Keep static instructions and policy blocks stable and front-loaded.
  2. Append dynamic file/context blocks after the stable prefix.
  3. Sort/normalize file ordering before rendering prompts.
  4. Treat Semble overflow as an exception path, not a normal prompt ingredient.
- **Expected impact:** lower prompt variance, better future cacheability, and smaller prompts.
- **Memory retrieval effectiveness is strong:** `9/9` retrieve hits, all within budget, all `llm`-keyword based.
- **Context-budget signals:** counted `context_budget_warn_count` stayed `0`, but the configuration threshold (`CONTEXT_BUDGET_WARN_RATIO: 0.7`) is echoed in review logs and overflow retrieval is happening. I would add explicit “budget used / budget available” telemetry whenever overflow is triggered.

## Orchestrator Health

- **`orchestrate_poll` itself is healthy but slow by design.**
  - Family metrics: `33` runs, `32` success, `1` cancelled, `p50=217s`, `p95=261.8s`.
  - Evidence-grade summaries for runs `28281510055`, `28280671637`, and `28282341733` all point to runner wait and steady-state polling around a single active tracking issue.
- **The larger orchestrator issue is control-plane churn.**
  - `clarify`: `205` runs, only `5` success, `200` other
  - `plan`: `199` runs, only `5` success, `194` other
  - `implement`: `199` runs, only `5` success, `194` other
  - `orchestrate_clarify_respond`: `200` runs, `200` other
- **Recent examples:** `28282407625` (`clarify`, skipped, `1s`), `28282407633` (`plan`, skipped, `1s`), `28282407635` (`implement`, skipped, `1s`), `28282407608` (`orchestrate_clarify_respond`, skipped, `8s`).
- **Inference:** the orchestrator is dispatching downstream workflows speculatively, and many immediately self-skip.
- **Smallest safe mitigations:**
  1. make the skip/no-op decision in the parent flow before dispatch,
  2. carry the active tracking issue ID/state forward instead of rediscovering it every poll,
  3. monitor `other_count / success_count` by family as a health KPI.
- **Track these indicators going forward:**
  - `review_autofix` cancelled wall time
  - runner-wait affected runs
  - poll cycles per active tracking issue
  - ratio of dispatched internal workflows to actually-executed ones

## Pipeline Flow Bottlenecks

1. **Review/autofix is the dominant end-to-end bottleneck.**
   - `review_autofix` owns the slowest cluster of runs (`4195s`, `3989s`, `3607s`, `3546s`).
   - In deep-dive coverage (`11` runs with telemetry), `wall_clock_p50_ms=3,480,000` and `wall_clock_p99_ms=4,174,400`.
   - Bottleneck type: **compute + queueing**.

2. **Implement is the dominant measured token sink when it actually runs.**
   - Family-level `p95=11s` is misleading because `194/199` runs were `other/skipped`.
   - The active success path is the right lens: run `28280902629` took `1672s` and implement consumed `99.17%` of measured Codex tokens.
   - Bottleneck type: **model compute / context volume**.

3. **Plan has a smaller but still meaningful active-path tail.**
   - Again, `p95=11s` is compressed by `194` skipped runs.
   - Active success outliers include `28280678475` at `648s`, `28275065459` at `426s`, and `28270210254` at `398s`.
   - Bottleneck type: **model compute**.

4. **Poll/orchestrator loops are mostly queueing + control-plane overhead.**
   - `orchestrate_poll` spends ~`217–233s` to find one active tracking issue and wait for a runner.
   - Bottleneck type: **queueing + repeated GH issue discovery**.

5. **CI and log-analysis failures are small in duration but high in disruption.**
   - CI lint failures happen in ~`72–78s`, but they block merges.
   - `workflow_log_analysis` fails almost instantly (`p50=0s`) and adds noisy red runs with no useful output.
   - Bottleneck type: **reliability gate / control-plane failure**.

6. **Retry/merge-conflict overhead is not the main problem in this window.**
   - No strong evidence of live retry storms, 429s, or secondary rate limits.
   - The dominant issues were queueing, context expansion, and hard-failing soft errors.

**Recommended fix order by end-to-end impact:**
1. Fix CI actionlint + `workflow_log_analysis` hard-fail behavior.
2. Cut `review_autofix` work size and stop stale runs early.
3. Trim implement context/tokens.
4. Reduce orchestrator no-op fan-out and poll rediscovery.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` long-tail latency (`p95=3539.4s`; top run `4195s`)
  - `implement` active-path cost/latency (`1672s` outlier; `2,652,382` Codex tokens)
  - `orchestrate_poll` queueing/poll overhead (`p50=217s` around one active tracking issue)

- **Top failure modes**
  - CI actionlint failure on invalid `env` context usage in `.github/workflows/workflow-log-analysis.yml`
  - `workflow_log_analysis` hard-failing on `partial_data:missing_log_archive` 404s despite collector soft-fail semantics
  - masked `review_autofix` consolidator failure (`Not inside a trusted directory`)

- **Highest-cost drivers**
  - `implement`: `2,652,382` measured Codex tokens (`99.17%` of measured total)
  - `review_autofix`: `101 or_calls` with missing prompt/completion token telemetry
  - Semble: `283,782` query bytes total, mostly `review_autofix` (`189,066`) and `implement` (`94,716`)

- **Top 3 prioritized actions**
  1. Fix the actionlint/env-context regression and restore CI reliability.
  2. Make `workflow_log_analysis` publish partial results instead of failing on missing archives.
  3. Reduce `review_autofix` tail latency by trimming overflow/context growth and aborting stale runs early.

## Metrics Appendix

### Overall repo totals

| repo | runs | success | failure | cancelled | other | success% | failure% | p50_s | p95_s | avg_s | codex_calls | codex_tokens | or_calls | cache_hit_rate | break_glass_count | context_budget_warn_count | wall_clock_p50_ms | wall_clock_p99_ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 165 | 33 | 14 | 788 | 16.5% | 3.3% | 1.0 | 334.0 | 124.151 | 39 | 2,674,669 | 101 | null | 0 | 0 | 1000 | 3,958,440 |

**Notes:** `wall_clock_*` coverage came from `109` runs with log telemetry; `cache_hit_rate` was null everywhere.

### Workflow family metrics

| family | runs | success | failure | cancelled | other | success% | failure% | p50s | p95s | avg_s | codex_calls | tokens | semble_calls | semble_bytes | wall_p50_ms | wall_p99_ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 12 | 4 | 7 | 1 | 0 | 33.3% | 58.3% | 77.5 | 1788.95 | 772.8 | 0 | 0 | 0 | 0 | 75000 | 77970 |
| workflow_log_analysis | 26 | 0 | 26 | 0 | 0 | 0.0% | 100.0% | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 |  |  |
| review_autofix | 72 | 60 | 0 | 12 | 0 | 83.3% | 0.0% | 40.5 | 3539.4 | 1270.6 | 10 | 20260 | 16 | 189066 | 3480000 | 4174400 |
| plan | 199 | 5 | 0 | 0 | 194 | 2.5% | 0.0% | 1.0 | 11.0 | 14.6 | 3 | 2027 | 0 | 0 | 1000 | 507860 |
| implement | 199 | 5 | 0 | 0 | 194 | 2.5% | 0.0% | 1.0 | 11.0 | 25.6 | 26 | 2652382 | 12 | 94716 | 1000 | 1306580 |
| clarify | 205 | 5 | 0 | 0 | 200 | 2.4% | 0.0% | 1.0 | 11.0 | 6.2 | 0 | 0 | 0 | 0 | 1000 | 10000 |
| orchestrate_poll | 33 | 32 | 0 | 1 | 0 | 97.0% | 0.0% | 217.0 | 261.8 | 213.0 | 0 | 0 | 0 | 0 | 228000 | 232900 |
| orchestrate_clarify_respond | 200 | 0 | 0 | 0 | 200 | 0.0% | 0.0% | 1.0 | 10.0 | 3.4 | 0 | 0 | 0 | 0 | 1000 | 10000 |
| copilot_pull_request_reviewer | 17 | 17 | 0 | 0 | 0 | 100.0% | 0.0% | 278.0 | 408.6 | 274.2 | 0 | 0 | 0 | 0 | 467000 | 467000 |
| validation_refresh | 1 | 1 | 0 | 0 | 0 | 100.0% | 0.0% | 859.0 | 859.0 | 859.0 | 0 | 0 | 0 | 0 |  |  |

### AI memory summary

| metric | value |
|---|---:|
| telemetry rows | 42 |
| retrieve ops | 9 |
| retrieve hit rate | 100% (9/9) |
| avg estimated tokens per retrieve | 635 |
| avg token budget per retrieve | 1400 |
| keyword method | llm (9/9) |
| zero-record retrieves | 0 |
| retrieve `fail_open:true` | 0 |
| retrieve `enabled:false` | 0 |
| max push attempts observed | 1 |
| `write_lessons_learned` fail-open + `ok:false` | 2 / 7 |

### GH API summary

| workflow/family | evidence | hotspot | rate-limit note |
|---|---|---|---|
| workflow_log_analysis | 11 observed missing archive 404s in deep-dive errors; family had 26/26 failures | `actions/runs/{id}/logs` | 404 soft-fail should be cached per tests; workflow still fails |
| orchestrate_poll | 33 runs total; log_summary shows `gh issue list` with 1 active tracking issue in runs `28281510055` and `28282341733` | `gh issue list --label ai:orchestrator-tracking` | no 429/secondary rate limit observed |
| review scripts | repo contains `gh_retry` wrappers in `review_rb_judge.sh`, `review_apply_fixes.sh`, `review_conflict_prepare.sh` | multiple `gh api` endpoints | call counts not emitted in telemetry |

### Semble / Serena / MCP summary

| server | scope/target | query_calls | fallback_calls | probe_ok | probe_failed | probe_skipped | response_bytes | notes |
|---|---|---:|---:|---:|---:|---:|---:|---|
| Semble | repo total | 28 | 0 | n/a | n/a | n/a | n/a | 283,782 query bytes total; run `28282341733` logged `SEMBLE_AVAILABLE:false` and `SEMBLE_INDEX_AVAILABLE:false` |
| Serena | repo total | 0 | 0 | 0 | 0 | 0 | 0 | no trustworthy runtime query/fallback/probe lines; aggregate telemetry zeros |
| Other MCP servers observed | run `28281473792` | 0 | 0 | n/a | n/a | n/a | 0 | `github-mcp-server` and `playwright` connected with `invocations=0` in Copilot review summary |

### Deep-dive Semble target breakdown

| target | query_calls | query_bytes | unique_runs | notes |
|---|---:|---:|---:|---|
| reviewer-context | 8 | 119140 | 8 | targeted retrieval appears useful and fallback-free |
| overflow | 8 | 69926 | 3 | pressure signal; files included `README.md`, reviewer helpers/tests, and AI-memory/render scripts |

### Prompt/cache telemetry

| scope | cache_hit_rate | or_cache_read_tokens | or_cache_write_tokens | note |
|---|---:|---:|---:|---|
| repo total | null | 0 | 0 | prompt-cache effectiveness cannot be evaluated from emitted telemetry |
| all runs with non-null `cache_hit_rate` | 0 |  |  | build caches hit in some runs, but prompt-cache fields stayed absent |

### Cancellation and queue-pressure highlights

| metric | value |
|---|---:|
| cancelled `review_autofix` runs | 12 |
| cancelled `review_autofix` wall time | 18,937s (5.26h) |
| avg cancelled `review_autofix` duration | 1,578s |
| max cancelled `review_autofix` duration | 3,534s (`28259428677`) |
| deep-dive `review_autofix` runs with runner-wait lines | 10 / 10 |
| deep-dive CI runs with runner-wait lines | 4 / 4 |

### MCP availability rows

| server | target/scope | probe_ok | probe_failed | probe_skipped | note |
|---|---|---:|---:|---:|---|
| Serena | all observed runs | 0 | 0 | 0 | inactive in this window |
| Semble | all observed runs | n/a | n/a | n/a | no probe counters emitted; availability only surfaced via env/log flags in `28282341733` |
