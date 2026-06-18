## Executive Summary

- **Largest PR-path latency is review fan-out, not orchestration.** `review_autofix` runs `27737077550` and `27735564120` spent `2160.9s` and `2054.3s` in `Run reviewer models`, then another `301.9s` and `256.6s` in the editor step. **Impact:** likely `15–20 min` off long review runs by reducing panel width / pass count on non-high-risk diffs. **Confidence:** high.
- **One implement run drove essentially all measured Codex spend.** Run `27735290498` (`Internal: AI Implement`) used `1,326,191` Codex tokens on `openai/gpt-5.4` with `reasoning effort: xhigh`; repo aggregate Codex telemetry matches that same total. **Impact:** `30–60%` token reduction on similar outliers by lowering default implement reasoning and reserving `xhigh` for repair/high-risk scopes. **Confidence:** medium.
- **Cost telemetry is not yet trustworthy enough for cache tuning.** Repo totals show `or_calls=51`, `or_total_tokens=0`, `or_cache_write_tokens=0`, `or_cache_read_tokens=0`, and `cache_hit_rate=null`. In run `27737077550`, the 12 distinct `INFO: openrouter usage` lines in `step-045` are repeated in `step-001`, which likely doubles recorded call counts; the same run also records duplicated `SEMBLE_QUERY` lines. **Impact:** high on budgeting accuracy; fix telemetry before making fine-grained cache decisions. **Confidence:** high.
- **Force-tick dedup is failing open often enough to matter.** Implement run `27735290498` logged `memory force-tick-get/put failed to clone ai-memory (fail-open)` and still dispatched `internal-orchestrate-poll.yml`; review runs `27735420388`, `27735415903`, and `27735400652` reported the same pattern in `log_summary`. **Impact:** fewer duplicate poller wakeups/cancellations and cleaner orchestrator state. **Confidence:** high.
- **Semble looks healthy in production, noisy only in tests.** Distinct deep-dive Semble queries were small and targeted (`14642–15063` bytes for `reviewer-context`; `6734–7999` bytes for overflow), with **no runtime fallbacks**. The repo’s `20` Semble fallbacks all came from CI contract-test runs and were explicitly tagged `context=contract-test`. **Impact:** keep Semble enabled; just filter test noise from operational dashboards. **Confidence:** high.
- **No hard failures were observed; this window is an optimization problem, not a break/fix problem.** Repo totals: `260` runs, `0` failures, `8` cancellations, `143` other/skip-like outcomes. **Impact:** prioritize safe latency/cost wins over behavior changes. **Confidence:** high.

## Speed Optimizations

1. **Enable reviewer risk tiers and shrink pass-2 on medium-risk PRs** *(critical-path win)*  
   - **Evidence:** `review_autofix` run `27737077550` spent `2160.9s` in `step-045-codex-agent_Run_reviewer_models`; `27735564120` spent `2054.3s` in `step-046-review_codex-agent_Run_reviewer_models`. In `27737077550`, `step-045` shows 12 distinct reviewer calls (6 `pass1`, 6 `review`) across six models.  
   - **Root cause:** full six-model, two-pass review path is active on long runs; `REVIEWER_RISK_TIER_ENABLED` exists in `.github/workflows/review_autofix.yml` but defaults off, and `ENABLE_REVIEWER_TWO_PASS=true`.  
   - **Exact change:** turn on `REVIEWER_RISK_TIER_ENABLED=1`, populate `REVIEWER_TIER_LITE_MODELS` with 2–3 fast models, and skip pass-2 unless the diff crosses existing size/risk guards (`REVIEWER_PASS2_DIFF_LARGE_LOC`, `REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX`).  
   - **Estimated savings:** `15–20 min` on long `review_autofix` runs; likely smaller but still material on moderate PRs.  
   - **Implementation risk:** medium; keep the full panel for workflow/script/db/risky paths.

2. **Lower default implement reasoning from `xhigh` to `high` for first-pass coding** *(critical-path + cost win)*  
   - **Evidence:** implement run `27735290498` lasted `608.9s` in `step-001-implement_implement.log`, logged `model: openai/gpt-5.4` and `reasoning effort: xhigh`, and consumed `1,326,191` Codex tokens.  
   - **Root cause:** the default implement path uses the most expensive reasoning tier even when the task is not obviously repair-grade.  
   - **Exact change:** set `THINKING_LEVEL_IMPLEMENT=high`, keep `THINKING_LEVEL_IMPLEMENT_REPAIR=xhigh`, and reserve `xhigh` for repair/reissue/workflow-sensitive scopes.  
   - **Estimated savings:** `2–4 min` and `300k–700k` tokens on similar outliers.  
   - **Implementation risk:** medium; quality-check on workflow/script-heavy issues before broad rollout.

3. **Gate the unconditional free-disk step on actual disk headroom** *(critical-path win)*  
   - **Evidence:** `Free disk space` took `88.0s` in run `27737077550` and `114.6s` in run `27735564120`. No disk-pressure failure was observed in this repo during the sampled window.  
   - **Root cause:** `jlumbroso/free-disk-space@v1.3.1` runs on every full `codex-agent` review job.  
   - **Exact change:** add a cheap precheck (`df -BG /`) and skip the free-disk action when free space is already above a safe threshold; keep current behavior for large repos / low-headroom runners.  
   - **Estimated savings:** `1.5–2 min` per full `review_autofix` run in small-to-medium repos.  
   - **Implementation risk:** low if the threshold is conservative.

4. **Shorten check-run waiting when only one pending check remains** *(critical-path win on affected runs)*  
   - **Evidence:** run `27735564120` spent `141.5s` in `Collect PR check-run failures`; the step logged waits of `20s`, `40s`, and `80s` for one pending check-run.  
   - **Root cause:** review waits synchronously for CI context before building reviewer/editor prompts.  
   - **Exact change:** reduce `CHECK_RUNS_WAIT_TIMEOUT_SECS` for `review_autofix` (for example `300 → 120`) or exit after the first unchanged snapshot when only one non-self check remains pending.  
   - **Estimated savings:** up to `1–2 min` on affected review runs.  
   - **Implementation risk:** medium; some CI-failure context may land one iteration later instead of the current one.

5. **Suppress extra poller wakeups when a scheduled poller is already in flight** *(flow optimization)*  
   - **Evidence:** implement run `27735290498` force-dispatched `internal-orchestrate-poll.yml`; successful `orchestrate_poll` runs `27735455671`, `27735533234`, and `27735612179` still took `258–287s`, mostly in runner wait/poll time, and the family had `6` cancellations.  
   - **Root cause:** force-tick fail-open can wake a second poller near the 5-minute cron boundary.  
   - **Exact change:** before dispatching from `orchestrate_force_tick.sh`, check for an existing queued/running poller and skip if one exists.  
   - **Estimated savings:** `2–4 min` on orchestration-triggered paths plus less queue churn.  
   - **Implementation risk:** low-medium; keep cron as the fail-open fallback.

## Cost Optimizations

1. **Re-tier the implement model/reasoning first; it is the biggest measured spend source**  
   - **Evidence:** run `27735290498` accounts for the repo’s full measured `codex_tokens_used=1,326,191`; it ran `openai/gpt-5.4` at `xhigh`.  
   - **Root cause:** expensive model + expensive reasoning on the default implement path.  
   - **Exact change:** default implement to `high`; keep `xhigh` only for `repair`, reissues, or workflow/script-sensitive scopes; optionally route diagnose-only work to a cheaper model via `MODEL_DIAGNOSE`.  
   - **Estimated savings:** `30–60%` of implement tokens on similar outliers.  
   - **Quality-risk notes:** medium; protect high-risk scopes with existing overrides.

2. **Reduce reviewer fan-out before touching Semble or Serena**  
   - **Evidence:** recorded `review_autofix` OpenRouter activity is concentrated in three slow runs; run `27737077550` shows 12 distinct reviewer calls in `step-045`, and `27735564120` shows the same 12-call shape.  
   - **Root cause:** six reviewer models across two passes.  
   - **Exact change:** enable `REVIEWER_RISK_TIER_ENABLED`, define lite/trivial reviewer sets, and disable pass-2 for non-risky diffs.  
   - **Estimated savings:** likely `40–70%` of reviewer-model spend on medium-risk PRs; also major latency reduction.  
   - **Quality-risk notes:** medium; keep full review for paths already covered by `REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX`.

3. **Fix OpenRouter/cache telemetry before doing cache optimization work**  
   - **Evidence:** repo totals show `or_calls=51` but `or_prompt_tokens=0`, `or_completion_tokens=0`, `or_total_tokens=0`, `or_cache_write_tokens=0`, `or_cache_read_tokens=0`, and `cache_hit_rate=null`. In run `27737077550`, every `INFO: openrouter usage` line reported `prompt_tokens=na`, `completion_tokens=na`, `total_tokens=na`, and cache fields `na`; those same lines were also duplicated into `step-001-codex-agent.log`.  
   - **Root cause:** usage normalization is present for reviewer runs but still yields `na`; editor/implement paths do not emit comparable usage lines; the auditor counts duplicated wrapper-log lines.  
   - **Exact change:**  
     - dedupe `cost_audit.py` by structured payload / step precedence, or ignore umbrella `step-001-*` telemetry when step-specific copies exist;  
     - emit normalized usage lines from editor, implement, and validate paths too.  
   - **Estimated savings:** indirect but high-value; today the cache system cannot be tuned credibly because measured hits/misses are missing.  
   - **Quality-risk notes:** none.

4. **Semble is not the main cost problem; keep it, but measure it correctly**  
   - **Evidence:** distinct deep-dive Semble queries were small: `reviewer-context` at `14642`, `14907`, and `15063` bytes; overflow at `6734` and `7999` bytes. No runtime fallbacks were observed.  
   - **Assessment:** Semble appears to be replacing broader prompt expansion with bounded context blocks, especially in reviewer-context. That is a good trade.  
   - **Exact change:** keep Semble enabled; filter `context=contract-test` fallbacks out of prod dashboards; correct duplicate-count inflation in the auditor.  
   - **Estimated savings:** small direct token savings per run, but likely positive already; not a rollback candidate.  
   - **Quality-risk notes:** low.

5. **Serena is currently neutral, not a savings lever**  
   - **Evidence:** repo totals show `serena_query_calls=0`, `serena_query_response_bytes=0`, `serena_query_tool_calls=0`, `serena_fallbacks=0`, and no real `SERENA_PROBE` lines were present; review summaries repeatedly showed `SERENA_ENABLED: false`.  
   - **Assessment:** Serena is neither replacing downstream tool/model work nor adding noisy bytes in this window.  
   - **Exact change:** none for cost right now; do not optimize a disabled path.  
   - **Estimated savings:** none in current window.  
   - **Quality-risk notes:** none.

**Existing saver worth keeping:** deterministic review skip is already working. Review runs `27735401131` and `27735383542` skipped on `docs_only`, and `27735399509` skipped on `small_diff`, all without AI execution.

**Context-budget note:** no real `CONTEXT_BUDGET_WARN` telemetry lines were observed. Repeated `CONTEXT_BUDGET_WARN_RATIO: 0.7` mentions in summaries are configuration, not warning events.

## Reliability Improvements

1. **Harden force-tick cooldown claiming so fail-open does not trigger duplicate pollers**  
   - **Failure evidence:** run `27735290498` logged `memory force-tick-get failed to clone ai-memory (fail-open)`, `memory force-tick-put failed to clone ai-memory (fail-open)`, and `force-tick memory claim failed; dispatching without persisted cooldown claim.` Review summaries for runs `27735420388`, `27735415903`, and `27735400652` show the same warning pattern.  
   - **Root cause category:** state-persistence / cooldown-dedup fail-open.  
   - **Exact fix:** if `memory_force_tick_put` fails, fall back to a cheap local lock or an in-flight poller check before dispatching; keep cron as the ultimate fallback.  
   - **Expected reliability impact:** fewer duplicate poller starts and fewer cancel/queue collisions.  
   - **Rollback / fail-open:** keep the current dispatch-after-warning path behind one final guard if the new lock/check fails.

2. **Unify review continuation into one concurrency domain**  
   - **Failure evidence:** PR `#3387` produced paired long runs `27737069779` (`Internal: AI Review & Autofix`, `cancelled`, `2753s`) and `27737077550` (`Codex PR Self-Healing Semantic Agent`, `success`, `2752s`). The parent run recorded the gate evaluation; the child run did the heavy reviewer/editor work.  
   - **Root cause category:** workflow retrigger / concurrency split between caller and direct-dispatch successor.  
   - **Exact fix:** use one PR-scoped successor entrypoint and one shared PR-scoped concurrency key for continuation dispatches; avoid direct child dispatch that leaves the parent run alive in parallel.  
   - **Expected reliability impact:** fewer mirrored/cancelled review runs and clearer operator state.  
   - **Rollback / fail-open:** keep the current synchronize-event fallback if successor dispatch fails.

3. **Treat Semble contract-test fallbacks as healthy fail-open behavior, not rollout breakage**  
   - **Failure evidence:** all `20` fallback events came from CI runs `27668672694`, `27734718464`, `27734736098`, and `27735486277`, each tagged `context=contract-test` with missing temp binary paths and `ms=0`.  
   - **Root cause category:** contract-test harness behavior.  
   - **Exact fix:** filter `context=contract-test` fallbacks out of production reliability alerts and dashboards.  
   - **Expected reliability impact:** better signal/noise; avoids false incident handling.  
   - **Rollback / fail-open:** none needed; runtime Semble already had `0` production fallbacks in the measured window.

4. **Resolve or suppress the repeated Copilot content-exclusion 404**  
   - **Failure evidence:** Copilot review runs `27738643101` and `27735566148` both logged `content exclusion policy fetch failed ... status=404 ... proceeding without exclusions`.  
   - **Root cause category:** external lookup misconfiguration / stale endpoint.  
   - **Exact fix:** correct the lookup target, or cache `404 = no exclusions configured` for the repo so it does not warn every run.  
   - **Expected reliability impact:** lower warning noise and less operator confusion.  
   - **Rollback / fail-open:** current behavior is already fail-open and safe.

**Break-glass / prompt-pressure note:** no real `BREAK_GLASS` or `CONTEXT_BUDGET_WARN` events were observed in the collected logs. There is no evidence in this window of policy/rubric pressure or prompt-size emergency behavior.

**Serena note:** no `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines were observed. That looks like a disabled rollout, not a broken one.

## AI Memory Health

Unique deep-dive `retrieve` operations were sparse but usable:

- **Unique retrieve hit rate:** `1 / 3` (`33%`).  
  - Hit: implement run `27735290498`, role `implementation`, `records_selected=1`, `estimated_tokens=28`, `keyword_method=plain`.  
  - Misses: review runs `27737077550` and `27735564120`, role `reviewer`, `records_selected=0`, `estimated_tokens=0`, `keyword_method=llm`.
- **Estimated token use vs budget:** average `estimated_tokens` across unique retrieves was `9.3`. Only run `27735290498` exposed a human-readable budget line: `token_budget: 1600`, `estimated_tokens_used: 28`, `records_selected: 1` (~`1.8%` of budget). Reviewer retrieves did not emit a structured `token_budget` field, so cross-run budget comparison is incomplete.
- **Keyword-method distribution:** `llm=2`, `plain=1`, `none=0`. The `llm` path currently produced `0/2` hits in review; the lone `plain` retrieval hit in implement.
- **Zero-record retrieves:** both long review runs (`27737077550`, `27735564120`) returned zero records and wrote fallback `AI MEMORY CONTEXT` status output.
- **`fail_open: true` retrieve entries:** none observed on `retrieve` itself.
- **`enabled: false` retrieve entries:** none observed.
- **Write-path friction:** `push_attempts=2` appeared on implement run `27735290498` (`record-run-event`) and review run `27734697477` (`record-candidate`), but there was no evidence of repeated high-retry storms.
- **Separate memory issue outside retrieve:** force-tick `get/put` fail-open happened in implement run `27735290498` and three review summaries, which hurts orchestrator dedup more than retrieval quality.

**Recommendation:** keep implement retrieval as-is, but improve reviewer retrieval recall. The current evidence says reviewer memory search is doing LLM keywording and still returning zero records on the longest runs; the smallest safe fix is to seed reviewer retrieval with stable file-path / issue / PR signals from reviewer consensus and changed-file context, then emit structured `token_budget` on every retrieve so usefulness can be tracked.

## GH API Call Audit

Repo policy is explicit here: `codex.md` / `CLAUDE.md` say “**Cycle-local caches are first-class**” and adding per-iteration `gh api` inside a loop is a **review-blocker**. The current window mostly follows that rule in orchestrator code, but `review_autofix` still has redundant PR reads.

1. **`review_autofix` repeats PR-state fetches across gate, collector, reviewer preflight, and editor preflight**  
   - **Evidence:**  
     - gate fetches `repos/{repo}/pulls/{PR_NUMBER}` in `.github/workflows/review_autofix.yml`;  
     - `scripts/review_collect_pr_metadata.sh` fetches the same PR payload again;  
     - `scripts/review_run_reviewers.sh` does another PR-open preflight;  
     - `scripts/review_apply_fixes.sh` checks PR state again before editor output is applied.  
     - Run `27737077550` gate logs explicitly show `/pulls/{PR_NUMBER}`, `/commits/{PR_HEAD_SHA}`, and `/pulls/{PR_NUMBER}/files` activity.  
   - **High-redundancy pattern:** same PR payload/state is fetched multiple times within one review cycle.  
   - **Concrete change:** write one canonical `pr_payload.json` / `pr_state.json` in the runtime dir and have downstream scripts read it unless they truly need a refresh.  
   - **Estimated reduction:** about `3–5` REST calls per full `review_autofix` run.  
   - **Rate-limit impact:** modest per run, meaningful at scale, and directly aligned with repo policy.

2. **Check-run polling is the biggest API loop in the review path**  
   - **Evidence:** README documents that each poll iteration performs one paginated check-runs snapshot; run `27735564120` visibly waited `20s + 40s + 80s` for one pending check.  
   - **High-volume pattern:** repeated paginated check-run snapshots in a single run.  
   - **Concrete change:** stop earlier on unchanged snapshots or reduce timeout for review-only runs.  
   - **Estimated reduction:** `2–3` paginated check-run snapshot attempts on affected runs.  
   - **Rate-limit impact:** medium; this is the main review-path loop that can grow under CI churn.

3. **Orchestrator batching/caching looks healthy; preserve it**  
   - **Evidence:** local docs point to `_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`, and caches like `ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`, `_candidate_details_json`. No `429` or secondary-rate-limit signals were observed in this window.  
   - **Assessment:** this is the correct pattern already.  
   - **Concrete change:** none operational; just keep extending batch helpers rather than adding per-issue REST calls.  
   - **Estimated reduction:** N/A; this is a “don’t regress” item.

4. **Autofix sweep dedup is already good and should be reused elsewhere**  
   - **Evidence:** run `27737055255` (`Internal: AI Review Autofix Sweep`) logged `AUTOFIX_SWEEP_SKIP pr=#3387 reason=active_run`.  
   - **Assessment:** active-run detection is already preventing useless review dispatches.  
   - **Concrete change:** reuse the same active-run check for force-tick/manual poll wakeups where possible.  
   - **Estimated reduction:** small direct API savings, better workflow-churn control.

## Prompt Cache & Memory System

- **Prompt-cache coverage is effectively missing in this window.** Repo totals show `cache_hit_rate=null`, `or_cache_write_tokens=0`, and `or_cache_read_tokens=0`. In run `27737077550`, reviewer usage lines reported `cache_enabled=true` but every usage field was `na`; editor and implement paths emitted no comparable usage lines at all.
- **Static-prefix work is already present and should be preserved.** `review_autofix.yml` has a `Pre-assemble static context (cacheable across runs)` step. That is the right foundation.
- **Likely fragmentation causes (inference):** dynamic PR metadata, comments, linked-issue context, check-run snapshots, AI-memory context, and reviewer outputs are all volatile. If any of that drifts into the cacheable prefix, hit rate will fragment quickly.
- **Concrete improvements:**  
  1. emit normalized OpenRouter usage from editor / implement / validate, not only reviewer code paths;  
  2. dedupe wrapper-log telemetry in `cost_audit.py`;  
  3. keep volatile sections strictly after the pre-assembled static prefix;  
  4. if prompt-budget protection matters, emit real `CONTEXT_BUDGET_WARN` lines from prompt builders, because only the ratio setting (`0.7`) is visible today.
- **Estimated impact:** unquantifiable until instrumentation is fixed, but this is prerequisite work for any real cache optimization.
- **Reliability note:** no true cache fail-open or cache-invalidation incidents were observed; the bigger problem is observability.

On the memory side, retrieval is useful in implement and ineffective in review. That argues for improving reviewer retrieval relevance before expanding memory scope.

## Orchestrator Health

- **Poller health is acceptable but noisy.** `orchestrate_poll` had `24` successes and `6` cancellations, with `p50=152.5s` and `p95=266.25s`. The longest recent successes (`27735455671`, `27735533234`, `27735612179`) were dominated by runner wait plus the `poll` step, not logic failure.
- **Force-tick is the main orchestration weak point.** The fail-open cooldown-claim behavior in `27735290498` and three review summaries means the poller can be woken redundantly when ai-memory persistence is unhealthy.
- **Clarify/respond wrappers are mostly routing noise.** `orchestrate_clarify_respond` ran `38` times and every run ended skip-like; `clarify` ran `44` times with only `5` successes. This is not a large compute cost, but it adds workflow churn and queue pressure.
- **No evidence of stuck terminal states or break-glass escapes.** There were no real `BREAK_GLASS` events, no Serena probe failures, and no hard orchestrator failures in the sampled window.
- **Smallest safe mitigations:**  
  - harden force-tick dedup first;  
  - reuse active-run detection before manual poll wakes;  
  - keep poller batching/caching behavior as-is;  
  - only collapse wrapper workflows if queue pressure becomes materially worse.

**Track these indicators:** `orchestrate_poll` cancellation count, poller `p95` duration, number of force-tick fail-open warnings, skip-like wrapper count, and paired long review continuation runs.

## Pipeline Flow Bottlenecks

1. **Review/autofix is the dominant end-to-end bottleneck.**  
   - Compute: reviewer fan-out (`2054–2161s`) and editor (`257–302s`).  
   - Fixed overhead: free-disk cleanup (`88–115s`).  
   - Retry/wait overhead: check-run polling (`141.5s` in `27735564120`).  
   - Merge/conflict overhead was **not** a top issue in this window.

2. **Implement is the dominant token-cost bottleneck.**  
   - One run (`27735290498`) consumed all measured Codex tokens and then triggered force-tick fail-open behavior.  
   - This is the main model-selection / reasoning-level lever.

3. **Orchestration cost is more queueing than compute.**  
   - Successful poller runs were mostly `258–287s`; summaries call out runner pickup wait.  
   - This is a control-plane/dedup problem, not a model-runtime problem.

4. **Clarify/plan/respond wrappers are high-count but low-compute noise.**  
   - `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` produced `143` other/skip-like outcomes collectively across the repo window; their combined skip-like wall time in those four families was only about `439s`.  
   - They are not the primary cost driver, but they do add Actions/UI churn.

5. **Validation refresh is long but off the PR critical path.**  
   - `validation_refresh` run `27734824872` took `1134s`, but it is a cross-repo batch maintenance path, not the main PR loop.  
   - Optimize it separately from PR turnaround.

**Ordered fixes by end-to-end impact:**  
`review_autofix reviewer fan-out` → `implement reasoning` → `free-disk gating` → `check-run wait cap` → `force-tick/poller dedup`.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix`: `35` runs, `p50=15s`, `p95=2742.2s`; long runs dominated by reviewer fan-out and editor time.
  - `implement`: `38` runs, but only `5` real executions; one run (`27735290498`) drove all measured Codex token spend.
  - `orchestrate_poll`: `30` runs, `24` success / `6` cancelled; successful runs still spend `~2.5–4.5 min` mostly in queue/poll time.

- **Top failure modes**
  - No hard failures in the window.
  - Recurring fail-open `force-tick` memory claims.
  - Mirrored long review continuation run pair (`27737069779` + `27737077550`) causing cancel/duplicate churn.
  - CI Semble fallbacks are test-only noise, not production breakage.

- **Highest-cost drivers**
  - Codex spend: implement run `27735290498` (`1,326,191` tokens).
  - Reviewer-model runtime: long review runs `27735564120` and `27737077550`.
  - Recorded OpenRouter/Semble counts in `review_autofix` are likely upper bounds because wrapper logs duplicate step-level telemetry.

- **Top 3 prioritized actions**
  1. Enable reviewer risk tiers / reduce pass-2 on non-high-risk PRs.
  2. Lower default implement reasoning from `xhigh` to `high`, keeping `xhigh` for repair/high-risk paths.
  3. Harden force-tick dedup and fix telemetry inflation / missing cache metrics before deeper cost tuning.

## Metrics Appendix

### Repo summary

| Repo | Runs | Success | Failure | Cancelled | Other | p50 dur | p95 dur | Codex tokens | Codex calls | Recorded OR calls | Recorded OR tokens | Cache hit rate | Semble query calls | Semble bytes | Semble fallbacks | Serena queries | Serena probes (ok/failed/skipped) | BREAK_GLASS | CONTEXT_BUDGET_WARN | Wall p50 | Wall p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 260 | 109 | 0 | 8 | 143 | 8s | 420s | 1,326,191 | 13 | 51* | 0† | n/a | 8* | 95,628* | 20 | 0 | 0 / 0 / 0 | 0 | 0 | 10,000ms | 2,750,180ms |

\* Upper bound: some `review_autofix` wrapper logs duplicate step-level telemetry.  
† Recorded as zero because usage fields were not emitted/populated, not because OpenRouter usage was absent.

### Workflow family highlights

| Workflow family | Runs | Success | Cancelled | Other | p50 dur | p95 dur | Key telemetry |
|---|---:|---:|---:|---:|---:|---:|---|
| review_autofix | 35 | 33 | 2 | 0 | 15s | 2742.2s | recorded `or_calls=51*`, Semble `7* / 87,629* bytes`, wall p99 `2,753,000ms` |
| implement | 38 | 5 | 0 | 33 | 1s | 437.0s | Codex `1,326,191` tokens, Semble `1 / 7,999 bytes`, wall p99 `617,000ms` |
| ci | 4 | 4 | 0 | 0 | 1711s | 1737.1s | Semble fallbacks `20`, all `context=contract-test` |
| copilot_pull_request_reviewer | 8 | 8 | 0 | 0 | 172.5s | 351.8s | prompt sizes observed `12,202–12,426` tokens in summaries |
| orchestrate_poll | 30 | 24 | 6 | 0 | 152.5s | 266.3s | wall p99 `287,000ms`; runner-wait-heavy |
| clarify | 44 | 5 | 0 | 39 | 1.5s | 107.5s | mostly skipped gating runs |
| plan | 38 | 5 | 0 | 33 | 1s | 309.2s | mostly skipped gating runs |

### Skip-heavy workflow overhead

| Family | Total runs | Skip-like runs | Skip-like wall time |
|---|---:|---:|---:|
| clarify | 44 | 39 | 149s |
| plan | 38 | 33 | 83s |
| implement | 38 | 33 | 99s |
| orchestrate_clarify_respond | 38 | 38 | 108s |

### Unique AI memory retrieve operations observed

| Run ID | Workflow family | Role | records_selected | estimated_tokens | token_budget | keyword_method | Result |
|---|---|---|---:|---:|---:|---|---|
| 27735290498 | implement | implementation | 1 | 28 | 1600 | plain | hit |
| 27737077550 | review_autofix | reviewer | 0 | 0 | n/a | llm | miss |
| 27735564120 | review_autofix | reviewer | 0 | 0 | n/a | llm | miss |

### GH API hotspot summary

| Workflow / step | Evidence | Current pattern | Estimated reducible calls |
|---|---|---|---:|
| review_autofix / gate + collector + reviewer/editor preflights | `.github/workflows/review_autofix.yml`, `scripts/review_collect_pr_metadata.sh`, `scripts/review_run_reviewers.sh`, `scripts/review_apply_fixes.sh`; run `27737077550` gate logs | repeated PR-state / PR-payload fetches within one review cycle | 3–5 per full run |
| review_autofix / check-run collection | run `27735564120` waited `20s + 40s + 80s` for one pending check; README documents one paginated snapshot per poll iteration | repeated check-run snapshots in one run | 2–3 snapshot attempts on affected runs |
| orchestrate_poll / issue processing | local policy docs + existing GraphQL batch helpers/caches | batched/cached already; no 429/rate-limit evidence | keep as-is |

### Semble / Serena telemetry

| System | Recorded query calls | Recorded bytes | Recorded fallbacks | Runtime fallbacks | Notes |
|---|---:|---:|---:|---:|---|
| Semble | 8* | 95,628* | 20 | 0 | production queries were small; fallbacks all CI contract-test |
| Serena | 0 | 0 | 0 | 0 | no live telemetry; review summaries showed `SERENA_ENABLED: false` |

### Observed distinct deep-dive Semble targets

| Target | Distinct queries observed | Distinct bytes observed | Fallbacks observed | Notes |
|---|---:|---:|---:|---|
| reviewer-context | 3 | 44,612 | 0 | review runs `27734697477`, `27735564120`, `27737077550` |
| overflow | 2 | 14,733 | 20 | overflow queries in implement/review; fallbacks only from CI contract tests |

### MCP availability / probe rows

| Server | Target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---:|---:|---:|---|
| Serena | none observed | 0 | 0 | 0 | no real `SERENA_PROBE` lines in the window |
| Other MCP (non-telemetry) | github-mcp-server | n/a | n/a | n/a | Copilot runs `27738643101` and `27735566148` connected with `invocations=0` |
| Other MCP (non-telemetry) | playwright | n/a | n/a | n/a | Copilot runs `27738643101` and `27735566148` connected with `invocations=0` |

### Other MCP servers observed

| Server | Where observed | Activity |
|---|---|---|
| github-mcp-server | `copilot_pull_request_reviewer` runs `27738643101`, `27735566148` | connected, `invocations=0` |
| playwright | `copilot_pull_request_reviewer` runs `27738643101`, `27735566148` | connected, `invocations=0` |
