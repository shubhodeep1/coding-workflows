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

## Deep Audit — Workflows & Scripts (2026-06-27)

### Section 1: Bug & Correctness Sweep

#### BUG-001
- **ID:** `BUG-001`
- **File path:** `.github/workflows/implement.yml:3564-3598`
- **Severity:** High
- **Category tag:** `bug`
- **Description:** The `SVB_REASON` / `ai:scope-blocked` branch creates the repo label, edits the issue, and posts the blocking comment with best-effort `2>/dev/null || true` calls only. In the same block, the human-facing comment says the issue “is now labeled `ai:scope-blocked`,” but there is no post-write verification that the label actually exists on the issue. This is a correctness gap because the safer `ai:destructive-blocked` path in the same workflow already does a read-back verification at `.github/workflows/implement.yml:3648-3700`. If `gh label create` or `gh issue edit` fails, the job still exits red while operators are told redispatch is blocked when it may not be.
- **Recommended fix:** Reuse the destructive-block pattern for scope blocks: ensure the label exists, apply it with `gh_retry`, then verify with `gh issue view --json labels` before claiming the latch is active. The cleanest fix is a shared helper such as `scripts/label_helpers.sh::latch_issue_label <repo> <issue> <label> [remove_label]` and using it for both `ai:scope-blocked` and `ai:destructive-blocked`.

#### BUG-002
- **ID:** `BUG-002`
- **File path:** `.github/workflows/test-and-mark-stable.yml:2934-2944`
- **Severity:** Medium
- **Category tag:** `bug`
- **Description:** The cancel-on-close wait loop turns transient GitHub API misses into a hard 600-second timeout. Each iteration fetches the same run twice (`.status` then `.conclusion`), and both commands fall back to `""` on failure. The loop exits only when `EXISTING_STATUS=completed`, so any transient fetch error resets status to empty and keeps the loop spinning until the deadline. Because status and conclusion come from separate responses, the loop can also observe inconsistent state.
- **Recommended fix:** Fetch `repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}` once per iteration, parse both fields from the same payload, and only overwrite the last-known-good values on a successful fetch. On fetch failure, retry within the iteration or emit an explicit API-error outcome instead of silently converting the state to empty strings.

### Section 2: GitHub API Call Redundancy Audit

#### API-001
- **ID:** `API-001`
- **File path:** `.github/workflows/review_autofix.yml:852-887`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** In the PR-body/title fallback path, `issue_nodes_json` is initialized with `labels: null`, then the workflow enters a `while` loop and runs `gh issue view "${issue_number}" --json labels` for each candidate issue whose labels are unknown. That is a per-issue REST fetch inside the loop, even though the workflow only needs a boolean “has `ai:orchestrator-validate-required` label” answer for each candidate.
- **Current call count:** `N` extra REST calls for `N` fallback-linked issues.
- **Proposed call count:** `1` aliased GraphQL query for all candidate issue numbers.
- **Batching pattern to extend:** `scripts/orchestrate_poll_process.sh::_fetch_candidate_issue_details_graphql`
- **Recommended fix:** Batch-hydrate the fallback issue metadata once, up front, by extending the existing GraphQL batching helper to request each issue’s label names and feed that JSON into the existing loop.

#### API-002
- **ID:** `API-002`
- **File path:** `.github/workflows/test-and-mark-stable.yml:2934-2944`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** The cancel-on-close poll loop calls the same endpoint twice per iteration: once for `.status` and once for `.conclusion`. Over the 600-second budget and 5-second sleep interval, that doubles the steady-state API traffic for no additional information.
- **Current call count:** `2` calls per iteration, up to about `240` calls over the full wait budget.
- **Proposed call count:** `1` call per iteration, up to about `120` calls.
- **Batching pattern to extend:** single-fetch parsing in `scripts/gh_helpers.sh::_safe_gh_jq`
- **Recommended fix:** Capture the run JSON once per iteration and parse both fields locally.

#### API-003
- **ID:** `API-003`
- **File path:** `scripts/review_rb_judge.sh:735-786`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** `review_rb_judge.sh` already does one GraphQL query to get `closingIssuesReferences.nodes[].number`, but then loops over those numbers and performs `_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}"` calls until it finds the first linked issue body. The script only uses the first issue’s body and labels, so the second round-trip layer is redundant.
- **Current call count:** `1 + N` calls (`1` GraphQL query plus up to `N` REST issue fetches).
- **Proposed call count:** `1` GraphQL query.
- **Batching pattern to extend:** `scripts/orchestrate_poll_process.sh::_fetch_candidate_issue_details_graphql`
- **Recommended fix:** Extend the initial GraphQL query to request each linked issue’s `body` and `labels { nodes { name } }`, then stop after the first populated node without issuing per-issue REST GETs.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **ID:** `DUP-001`
- **File path:** `scripts/review_apply_fixes.sh:577-586`  
  `scripts/review_conflict_prepare.sh:493-502`  
  `scripts/review_run_reviewers.sh:1060-1069`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** `append_semble_query_section()` appears three times with the same signature and body: `-s` guard, `head -c "${max_bytes}"`, and trailing newline emission. This is exact helper duplication in review-side prompt assembly code.
- **Shared module / signature:** `scripts/semble_helpers.sh::append_semble_query_section <label> <path> [max_bytes]`
- **Callers:** `scripts/review_apply_fixes.sh`, `scripts/review_conflict_prepare.sh`, `scripts/review_run_reviewers.sh`
- **Recommended fix:** Add the helper to the existing `scripts/semble_helpers.sh`, source it in the three callers, and delete the inline copies.

#### DUP-002
- **ID:** `DUP-002`
- **File path:** `scripts/review_run_reviewers.sh:78-108`  
  `scripts/review_conflict_resolve.sh:127-187`  
  `scripts/review_rb_judge.sh:115-129`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** Review runtime plumbing is duplicated across multiple entrypoints. `resolve_ledger_substate_helper()` appears in both `review_run_reviewers.sh` and `review_conflict_resolve.sh`, while `read_codex_stall_guard_state()` appears in all three files. This is control-path logic for stall detection and ledger emission; drift here will be subtle and hard to test.
- **Shared module / signature:** new `scripts/review_runtime_helpers.sh` with `resolve_ledger_substate_helper [support_scripts_dir]` and `read_codex_stall_guard_state <status_file>`
- **Callers:** `scripts/review_run_reviewers.sh`, `scripts/review_conflict_resolve.sh`, `scripts/review_rb_judge.sh`
- **Recommended fix:** Move both helpers into one sourced runtime helper module and update the three scripts to import it.

#### DUP-003
- **ID:** `DUP-003`
- **File path:** `scripts/label_helpers.sh:120-154`  
  `scripts/orchestrate_poll_process.sh:2133-2188`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** The repo has two different `ensure_label_exists()` implementations with the same intent but drifted semantics. `scripts/label_helpers.sh` accepts an explicit repo, uses hard-coded label maps, and returns `1` on genuine failures; `scripts/orchestrate_poll_process.sh` hard-codes `GITHUB_REPOSITORY`, consults `.github/ai/label_contract.v1.json`, caches ensured labels, and still returns `0` after warning on failure. Same name plus different failure behavior is a maintenance trap.
- **Shared module / signature:** make `scripts/label_helpers.sh::ensure_label_exists <label> [repo]` canonical, with optional contract lookup/cache behavior added there
- **Callers:** `scripts/orchestrate_poll_process.sh` and any workflow/script currently re-implementing label creation
- **Recommended fix:** Consolidate on one helper in `scripts/label_helpers.sh`, fold the orchestrator-only contract/cache features into it, and remove the local copy from `scripts/orchestrate_poll_process.sh`.

#### DUP-004
- **ID:** `DUP-004`
- **File path:** `.github/workflows/mark-stable.yml:314-345`  
  `.github/workflows/test-and-mark-stable.yml:3446-3477`  
  `.github/workflows/mark-stable.yml:468-489`  
  `.github/workflows/test-and-mark-stable.yml:4748-4769`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** Two release workflows carry exact duplicate shell blocks: the “Script-workflow cross-reference” block and the “Tag version and update stable pointer” block. This is near-duplicate workflow structure, so release-policy changes must be hand-applied in both places.
- **Shared module / signature:** `python3 scripts/check_workflow_script_refs.py` for cross-reference validation, plus new `scripts/publish_release_tags.sh <version> [remote]`
- **Callers:** `.github/workflows/mark-stable.yml`, `.github/workflows/test-and-mark-stable.yml`
- **Recommended fix:** Replace the inline blocks with shared script calls so both workflows reuse one implementation.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001
- **ID:** `EXPR-001`
- **File path:** `.github/workflows/memory_maintenance.yml:45-391`
- **Severity:** Medium
- **Category tag:** `expression-limit`
- **Description:** The repository-learnings extraction step is a large inline `run:` block with `${{ }}` interpolations plus substantial shell and two embedded Python heredocs. Measured expression body size is about `15,152` characters, leaving roughly `5,848` characters before GitHub’s `21,000`-character hard failure limit. This repo has already hit the expression ceiling elsewhere, so this step is already in the warning zone.
- **Recommended fix:** Extract the learnings-extraction logic to `scripts/memory_extract_repo_learnings.sh` and move the embedded Python into dedicated scripts/modules. That keeps the workflow expression small and makes future prompt/logic growth safer.

No workflow file exceeded `800 KB` in this scan, and no other `run:`/`if:` expression crossed the `15,000`-character warning threshold.

### Section 5: Cross-Cutting Concerns

#### SHELL-001
- **ID:** `SHELL-001`
- **File path:** `scripts/codex_thread_reuse.sh:492-557,806-856`
- **Severity:** Low
- **Category tag:** `shellcheck`
- **Description:** The script uses `cmd` as an array in `codex_thread_reuse_direct_run()` (`local -a cmd=()` and `${cmd[@]}`) and then reuses `cmd` as a scalar in `codex_thread_reuse_main()` (`local cmd="${1:-}"`). This is the ShellCheck collision behind SC2178/SC2128: the same identifier carries array and scalar meanings in one file, making `${cmd}` / `${cmd[@]}` handling fragile.
- **Recommended fix:** Rename the scalar in `codex_thread_reuse_main()` to `subcommand` and keep `cmd` reserved for the array command-builder path.

#### CONSIST-001
- **ID:** `CONSIST-001`
- **File path:** `.github/workflows/review_autofix.yml:950-966`  
  `scripts/label_helpers.sh:120-154`
- **Severity:** Low
- **Category tag:** `consistency`
- **Description:** `review_autofix.yml` defines an inline `gh_retry()` and `ensure_label_exists()` for the deterministic-skip-merge path instead of using the repo’s canonical helpers. The inline version retries blindly and swallows label-create failures with `|| true`, while `scripts/label_helpers.sh` preserves warning behavior and central label metadata. That gives one workflow different retry/label semantics from the rest of the repo.
- **Recommended fix:** Move this block into a repo script or source a minimal helper shim that delegates to the canonical `gh_retry` and `ensure_label_exists` implementations.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 6 | BUG-002, API-001, API-003, DUP-002, DUP-003, EXPR-001 |
| Low | 5 | API-002, DUP-001, DUP-004, SHELL-001, CONSIST-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2-3 | Medium |
| API call optimization | 3-4 | Medium |
| Code modularization | 10+ | Large |
| Expression size reduction | 2-3 | Medium |
| Medium/Low fixes | 2 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-06-27)

### Safety Tag Legend
`SAFE_TO_MERGE` means the duplicate call can be removed now without changing endpoint scope, filters, retry/error semantics, cache contracts, or concurrency behavior. `NEEDS_VERIFICATION` means the overlap is real but freshness, job-boundary, or fail-closed semantics still need a human check. `RISKY_SKIP` means the redundancy sits in a retry/poll/race-defense path and must not be auto-implemented without manual review.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — `RISKY_SKIP`
- **File path and line ranges:** `.github/workflows/test-and-mark-stable.yml:1022-1024`, `.github/workflows/test-and-mark-stable.yml:1051-1056`
- **Current call count:** `2 × stability_attempts + 1` calls per execution of this block (minimum `3`)
- **Proposed call count:** `2 × stability_attempts` if the final successful stability sample is reused for state/merged checks
- **Endpoint(s):** `GET /repos/{owner}/{repo}/pulls/{pull_number}`
- **Evidence:**
  ```sh
  # .github/workflows/test-and-mark-stable.yml:1022-1025
  HEAD_A=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")
  sleep 3
  HEAD_B=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")
  ```
  ```sh
  # .github/workflows/test-and-mark-stable.yml:1051-1056
  PR_META=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" 2>/dev/null || echo "")
  PR_STATE=$(printf '%s' "${PR_META}" | jq -r '.state // ""' 2>/dev/null || echo "")
  PR_MERGED=$(printf '%s' "${PR_META}" | jq -r '.merged // false' 2>/dev/null || echo "false")
  ```
  The third fetch hits the same PR endpoint immediately after the two SHA-stability reads and consumes overlapping data from the final read.
- **Proposed fix:** If a maintainer accepts the race tradeoff, change the second stability read to capture full PR JSON, derive `HEAD_B`, `PR_STATE`, and `PR_MERGED` from that same response, and drop the separate `PR_META` fetch.
- **Safety rationale:** `RISKY_SKIP` because the surrounding comments at `.github/workflows/test-and-mark-stable.yml:1013-1019` and `:1032-1049` explicitly document this block as upstream-race defense.
- **Downstream signal:** Do not auto-implement; manual review must decide whether the post-loop closed/merged check can safely use the final stability sample without reopening the documented race with auto-merge or late PR closure.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — `NEEDS_VERIFICATION`
- **File path and line ranges:** `.github/workflows/review_autofix.yml:1441-1448`, `scripts/review_collect_pr_metadata.sh:209-234`, `.github/workflows/review_autofix.yml:4756-4765`, `scripts/review_enable_auto_merge.sh:127-139`, `scripts/review_enable_auto_merge.sh:192-214`
- **Current call count:** `2` PR-metadata GETs on the PR-backed auto-merge path
- **Proposed call count:** `1` on cache hit (`2` only if a verified cache-miss/live-fallback path is retained)
- **Endpoint(s):** `GET /repos/{owner}/{repo}/pulls/{pull_number}`
- **Evidence:**
  ```sh
  # .github/workflows/review_autofix.yml:1446-1447
  echo "PR_PAYLOAD_FILE=${RUNTIME_DIR}/pr_payload.json"
  echo "PR_META_FILE=${RUNTIME_DIR}/pr_meta.json"
  ```
  ```sh
  # scripts/review_collect_pr_metadata.sh:209-234
  gh_retry "${PR_PAYLOAD_FILE}" api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"
  jq '{
    title: (.title // ""),
    body: (.body // ""),
    baseRefName: (.base.ref // ""),
    headRefName: (.head.ref // ""),
    headRepoFullName: (.head.repo.full_name // "")
  }' "${PR_PAYLOAD_FILE}" > "${PR_META_FILE}"
  ```
  ```sh
  # scripts/review_enable_auto_merge.sh:127-136,192
  if ! _ORCH_PR_META_JSON="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" 2>"${_orch_pr_meta_err_file}")"; then
  ...
  _orch_pr_head_ref="$(printf '%s' "${_ORCH_PR_META_JSON}" | jq -r '.head.ref // ""' 2>/dev/null || echo "")"
  _orch_pr_body="$(printf '%s' "${_ORCH_PR_META_JSON}" | jq -r '.body // ""' 2>/dev/null || echo "")"
  ```
  The helper re-fetches the same PR resource even though the job already created `PR_META_FILE` with the two fields it later reads (`headRefName`, `body`).
- **Proposed fix:** Extend `scripts/review_enable_auto_merge.sh` to accept `PR_META_FILE` as an input, read `headRefName` and `body` from that file first, and keep the current live `gh_retry gh api` fetch only as a cache-miss/parse-failure fallback.
- **Safety rationale:** `NEEDS_VERIFICATION` because the helper is intentionally fail-closed and runs late in a long job with deferred push behavior, so cached metadata might not be an acceptable freshness substitute.
- **Downstream signal:** Verify all three before changing this path: (1) `PR_META_FILE` always survives until the auto-merge step, (2) forward-merge/orchestrator suppression only depends on `headRefName` and `body`, and (3) skipping the live fetch on cache hit does not weaken the existing fail-closed behavior during transient API failures.

#### REUSE-002 — `NEEDS_VERIFICATION`
- **File path and line ranges:** `.github/workflows/review_autofix.yml:202-211`, `.github/workflows/review_autofix.yml:289-304`, `.github/workflows/review_autofix.yml:1722-1727`, `scripts/review_collect_pr_metadata.sh:209-234`, `.github/workflows/review_autofix.yml:1826-1827`
- **Current call count:** `2` PR-metadata GETs on each PR-backed `review_autofix` run that reaches `codex-agent`
- **Proposed call count:** `1` on snapshot hit (`2` only if downstream live fallback is retained)
- **Endpoint(s):** `GET /repos/{owner}/{repo}/pulls/{pull_number}`
- **Evidence:**
  ```sh
  # .github/workflows/review_autofix.yml:289-304
  if _pr_gate="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" \
    --jq '{state: (.state // ""), merged: (.merged // false), head_ref: (.head.ref // ""), labels: ((.labels // []) | map(.name)), additions: (.additions // 0), deletions: (.deletions // 0), title: (.title // ""), body: (.body // "")}' \
    2>/dev/null)"; then
  ```
  ```yaml
  # .github/workflows/review_autofix.yml:202-211
  outputs:
    head_ref: ${{ steps.evaluate.outputs.head_ref }}
    post_merge_pr_text_json: ${{ steps.evaluate.outputs.post_merge_pr_text_json }}
    post_merge_linked_issues_json: ${{ steps.evaluate.outputs.post_merge_linked_issues_json }}
  ```
  ```sh
  # scripts/review_collect_pr_metadata.sh:209-234
  gh_retry "${PR_PAYLOAD_FILE}" api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"
  ...
  ```
  ```sh
  # .github/workflows/review_autofix.yml:1826-1827
  PR_TITLE=$(jq -r '.title // ""' "${PR_PAYLOAD_FILE}")
  PR_BODY=$(jq -r '.body // ""' "${PR_PAYLOAD_FILE}")
  ```
  The gate job already fetches `state`, `merged`, `head_ref`, `labels`, `additions`, `deletions`, `title`, and `body`, but only `head_ref` and post-merge caches are exported, so `codex-agent` refetches the same PR again to rebuild files.
- **Proposed fix:** Extend the gate handoff with a compact PR snapshot (for example `title`, `body`, `base.ref`, `head.ref`, `head.repo.full_name`, and any still-needed gate fields), then teach `review_collect_pr_metadata.sh` to materialize `PR_META_FILE`/`PR_PAYLOAD_FILE` from that snapshot before falling back to a live fetch.
- **Safety rationale:** `NEEDS_VERIFICATION` because this reuse crosses a job boundary, and PR title/body/base/head may legitimately change between `gate` and `codex-agent`.
- **Downstream signal:** Verify on real PR-backed runs that (1) gate-time and codex-agent-time values for `title`, `body`, `base.ref`, `head.ref`, and `head.repo.full_name` are acceptably stable, and (2) the proposed snapshot fits step-output or artifact size limits before removing the second fetch.

### Dead Calls (DEAD-API-###)
No findings.

### Cross-References to Deep Audit Section
- API-001: `NEEDS_VERIFICATION` — batching the fallback issue-label lookups is plausible, but it changes per-issue fail-open behavior inside a linked-issue inference path.
- API-002: `RISKY_SKIP` — the duplicate run-state fetch is inside a bounded poll/wait control path, so consolidation changes race and polling semantics.
- API-003: `NEEDS_VERIFICATION` — folding first-issue body/labels into the existing GraphQL query is attractive, but partial GraphQL results and “first populated issue” selection need proof.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | REUSE-001, REUSE-002 |
| RISKY_SKIP | 1 | MERGE-001 |

### Implement-Stage Handoff
No SAFE_TO_MERGE findings in this pass.
