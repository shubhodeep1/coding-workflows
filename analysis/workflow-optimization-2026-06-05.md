## Executive Summary

- Window is sufficient overall (`insufficient_data=false`), but validate diagnosis is partially blind: at least **9 `validate` failures and 1 `implement` failure** were collector-side `partial_data:missing_log_archive ... HTTP 404` soft-fails, so the observed **`validate` 12/13 failure rate** should not be treated as workflow-logic proof yet. **Estimated impact:** medium on debugging speed. **Confidence:** high.
- `review_autofix` is the biggest latency problem. Five cancelled runs in `shubhodeep1/coding-workflows` — **26934703864, 26944643043, 26960113331, 26968187150, 26977120613** — consumed **22.5 of the family’s 24.2 cancelled runner-hours**, with `workspace_safety_check.sh` missing within ~1-2 minutes of `Editor prompt bytes`, but `phase_failed` not recorded until **184.5-209.5 minutes later**. **Estimated impact:** ~3-3.5 hours saved per bad run if made terminal immediately. **Confidence:** high.
- CI failures are highly concentrated. **5 of 6 CI failures** — **26934022980, 26944606902, 26946788598, 26956143718, 26968085383** — failed in `lint / Review autofix review-pipeline plumbing contract test` with the same `attempt_prompt_file` assertion after **215-225s**. **Estimated impact:** raise CI success from **25/31** toward **30/31** and remove ~**18.9 minutes** of repeat failed CI time in this window. **Confidence:** high.
- The release gate failed deterministically and late. `Test & Mark Stable Release` run **26942520644** failed after **3702s** in `validate-scripts / Script-workflow cross-reference` with `FAIL: scripts/render_prompt.py referenced in workflows but does not exist`, while parallel heavy jobs still ran. **Estimated impact:** avoid another ~**61.7-minute** release-gate failure and substantial wasted parallel compute. **Confidence:** high.
- The largest measured token sink was the analysis workflow itself. `workflow_log_analysis` run **26942555350** logged `AI_MEMORY_TELEMETRY` with `op=summarize_unselected_runs`, `summarized=95`, `tokens_used=149183`, and spent **1580.8s** in `analyze-commit-notify`. **Estimated impact:** 50-80% token/time reduction for that workflow if scope is narrowed. **Confidence:** high.
- Prompt-cache tuning is blocked by telemetry blindness. Repo aggregate shows **114** log-parsed runs, but `cache_hit_rate=null`, all `or_*` fields are `0`, and repo-wide `break_glass_count=0`, `context_budget_warn_count=0`. **Estimated impact:** medium once instrumented; current optimization confidence is low. **Confidence:** high.

## Speed Optimizations

1. **Fail fast on stuck `review_autofix` editor paths** *(critical-path win)*  
   - **Evidence:** `review_autofix` cancelled runs **26934703864, 26944643043, 26960113331, 26968187150, 26977120613** logged `Editor prompt bytes` of **254,394 / 257,854 / 268,338 / 282,160 / 290,232**, then hit `bash: .../scripts/workspace_safety_check.sh: No such file or directory`, but only recorded `phase_failed` **184.5 / 200.1 / 191.5 / 209.5 / 198.8 minutes** later. Run **26968187150** also logged `Editor aborted — PR #3082 is closed (attempt 1)` at **19:59:11Z**, yet the run was only cancelled at **23:14:21Z**.  
   - **Root cause:** terminal-state handling/cleanup bug after editor prompt emission; likely a dead path rather than pure prompt-size slowness.  
   - **Exact change:** make “missing workspace helper” and “PR closed” terminal outcomes in the review/editor path: record `phase_failed` immediately, kill Codex/heartbeat/orphan subprocesses immediately, and exit the job; add a post-editor idle watchdog keyed to “no phase transition for N minutes after `Editor prompt bytes`”.  
   - **Estimated time savings:** **~184-210 minutes per bad run**; the top 5 cancelled runs alone account for **22.5 runner-hours**.  
   - **Implementation risk:** low-medium; if fail-closed is too strict, prefer a clear fail-open skip over a multi-hour hang.

2. **Backport/front-load the CI plumbing contract check** *(critical-path win)*  
   - **Evidence:** CI failures **26934022980, 26944606902, 26946788598, 26956143718, 26968085383** all failed in `lint / Review autofix review-pipeline plumbing contract test` with `AssertionError: codex stdin must be fed from the per-attempt prompt file...` after **215-225s**.  
   - **Root cause:** branch/ref drift between the failing CI target and the contract expectation. Current repo main already shows the per-attempt prompt logic in `scripts/review_apply_fixes.sh:1422-1434`, so the failing ref likely did not carry that version.  
   - **Exact change:** ensure the tested ref/stable branch carries the current per-attempt prompt logic, and move this contract test to the very top of `lint` so regressions fail before other lint/setup work.  
   - **Estimated time savings:** about **3.5 minutes per failing CI run**, plus fewer reruns; **~18.9 minutes** of failed CI time were spent on this one regression in the current window.  
   - **Implementation risk:** low.

3. **Gate `Test & Mark Stable Release` behind a cheap script cross-reference preflight** *(critical-path win on release path)*  
   - **Evidence:** `test_and_mark_stable` run **26942520644** failed in `validate-scripts / Script-workflow cross-reference` after **1456.6s**, but parallel jobs still consumed **1572.2s** (`orphan-workflows-test`), **2623.0s** (`e2e-smoke-test`), and **3654.8s** (`workflow-log-analysis-test`).  
   - **Root cause:** a deterministic config/reference error was discovered too late, after expensive jobs were already running.  
   - **Exact change:** make cross-reference validation a first preflight job and add it as a `needs:` dependency for the long E2E / analysis jobs; if current main already contains the allowlist fix (`test-and-mark-stable.yml:3355-3363`, `mark-stable.yml:295-303`), backport that exact change to the tested release ref.  
   - **Estimated time savings:** **~26-61 minutes** on equivalent failures, depending on how early the gate trips relative to the long jobs.  
   - **Implementation risk:** low.

4. **Trim `workflow_log_analysis` scope** *(non-critical-path but large single-run win)*  
   - **Evidence:** `workflow_log_analysis` run **26942555350** lasted **3619s**; major steps were `analyze-commit-notify` **1580.8s**, `deep-audit` **1355.3s**, `api-redundancy` **589.2s**, `collect-logs` **64.1s**.  
   - **Root cause:** the workflow is doing a broad deep audit plus summarizing **95** unselected runs every time.  
   - **Exact change:** target only failures/slow runs/recent runs plus a much smaller sample of unselected runs without deep-dive coverage; cache prior summaries by `run_id` and skip unchanged successful sweeps/poller runs.  
   - **Estimated time savings:** likely **15-30 minutes** per analysis run.  
   - **Implementation risk:** low, if failures/slow outliers remain mandatory.

5. **Suppress obvious no-op orchestrator triggers earlier** *(micro-optimization)*  
   - **Evidence:** `clarify` had **190 skipped** runs, `plan` **183**, `implement` **179**, `orchestrate_clarify_respond` **185**. Their skipped runs averaged only **1.41s / 1.43s / 1.61s / 1.49s**; recent skipped runs **26987348056**, **26987348025**, **26987348062** were all false-condition no-ops.  
   - **Root cause:** workflows are starting on comments/state changes that can be rejected before useful work begins.  
   - **Exact change:** move the `<!-- ORCHESTRATOR_STATE_V2 ... -->` and non-command gating earlier, ideally before runner-heavy work.  
   - **Estimated time savings:** only about **18.2 minutes total** across this whole window, so this is a clear after-the-big-rocks item.  
   - **Implementation risk:** low.

## Cost Optimizations

1. **Cut `summarize_unselected_runs` first**
   - **Evidence:** `workflow_log_analysis` run **26942555350** logged `AI_MEMORY_TELEMETRY: {"op":"summarize_unselected_runs", ... "targeted":100, "summarized":95, "tokens_used":149183, "model":"openai/gpt-5.4-mini"}`. Repo aggregate `codex_tokens_used` was only **10,130** across the full window, so this single explicit line is the clearest large token spend.  
   - **Root cause:** broad summarization of mostly non-selected runs.  
   - **Exact change:** only summarize runs that are neither in `errors/slow/recent` nor already summarized recently; cap the targeted count; bias toward failures and long-tail runs.  
   - **Estimated savings:** **tens of thousands of tokens per analysis run**; halving that workload would likely save **70k+** tokens/run.  
   - **Quality-risk notes:** low, as long as deep-dive failures/slow outliers remain mandatory.

2. **Stop paying for dead `review_autofix` runs**
   - **Evidence:** `review_autofix` had **15 cancelled** runs totaling **24.15 runner-hours**; the top 5 outliers consumed **22.5 hours** by themselves. The stalled runs had similar Semble usage (~**15KB** logged bytes/query) and similar prompt sizes to the one successful comparator **26930968999**, which completed **4.7 minutes** after a **252,909-byte** editor prompt.  
   - **Root cause:** dead-path runtime waste, not measured model/token inefficiency.  
   - **Exact change:** same as the speed fix — make missing-helper and PR-closed paths terminal immediately.  
   - **Estimated savings:** high runner-cost savings; token savings are **not measurable** in this window because prompt/model telemetry is blind.  
   - **Quality-risk notes:** low if the workflow exits cleanly instead of hanging.

3. **Eliminate avoidable reruns and deterministic failures**
   - **Evidence:** repeated CI plumbing failures consumed **~18.9 minutes** across 5 runs; release-gate run **26942520644** consumed **3702s** before failing on a deterministic cross-reference issue.  
   - **Root cause:** branch drift and config drift, not model quality.  
   - **Exact change:** backport the current `attempt_prompt_file` logic and keep optional-script allowlists synchronized across release/test refs.  
   - **Estimated savings:** **~80 minutes** of avoidable workflow time per recurrence set seen here, before counting human rerun/debug time.  
   - **Quality-risk notes:** low.

4. **Keep Semble; fix the accounting noise instead of disabling it**
   - **Evidence:** repo aggregate shows **12 `SEMBLE_QUERY` calls / 137,800 logged bytes / 70 fallbacks**. Deep logs confirm the real runtime split is:  
     - `review_autofix`: **7** real `SEMBLE_QUERY target=reviewer-context` lines totaling **106,378** logged bytes (~**15.2KB/query**) across runs **26930968999, 26934703864, 26944643043, 26952637519, 26960113331, 26968187150, 26977120613**.  
     - non-analysis runtime fallbacks: **25** real `SEMBLE_FALLBACK target=overflow` lines, all `reason=...missing_semble`, across CI runs **26944606902, 26946788598, 26956143718, 26968085383** and stable run **26942520644**.  
     - the remaining **45** fallback counts come from `workflow_log_analysis` self-reference, not operational rollout failure.  
   - **Root cause:** test-fixture and self-referential fallback lines are mixed into operational totals.  
   - **Exact change:** split operational vs fixture/self-analysis fallback counters; keep `reviewer-context` Semble enabled.  
   - **Estimated savings:** small direct dollar savings, but medium improvement in decision quality; avoids the wrong optimization (turning Semble off).  
   - **Quality-risk notes:** disabling Semble based on the current raw fallback count would likely hurt context quality while barely changing spend.

5. **Do not change model family or reasoning effort yet; instrument first**
   - **Evidence:** deep `review_autofix` logs show `MODEL_EDITOR: openai/gpt-5.4`, `EDITOR_REASONING_EFFORT: xhigh`, `REVIEWER_REASONING_EFFORT: xhigh`, but repo-wide `cache_hit_rate=null`, all `or_*` fields are `0`, and there were **no runtime `CONTEXT_BUDGET_WARN`** lines. Serena contributed nothing: repo summary `serena_* = 0`, deep logs repeatedly `SERENA_ENABLED: false`.  
   - **Root cause:** missing prompt/cache/token telemetry, not proven model inefficiency.  
   - **Exact change:** emit per-phase prompt/completion/cache counters before making model or reasoning downgrades.  
   - **Estimated savings:** unknown from this window.  
   - **Quality-risk notes:** high if changed blindly; the measured pain is elsewhere.

## Reliability Improvements

1. **Backport/remove the CI plumbing regression**
   - **Failure evidence:** CI runs **26934022980, 26944606902, 26946788598, 26956143718, 26968085383** all failed in `lint / Review autofix review-pipeline plumbing contract test` with the same `attempt_prompt_file` assertion. The only other CI failure, **26953240776**, was separate: `YAML lint` failed in **14s** with a `yamllint` `TypeError`.  
   - **Root cause category:** branch drift / regression in tested ref.  
   - **Exact fix:** ensure the failing branch/stable ref uses the current `scripts/review_apply_fixes.sh:1422-1434` per-attempt prompt-file logic, keep the contract test strict, and then investigate the one-off `yamllint` failure separately.  
   - **Expected reliability impact:** CI failure rate could drop from **19.35%** to roughly **3.2%** if the repeated plumbing regression is removed.  
   - **Rollback/fail-open considerations:** low; this is a correctness regression, not an optional feature.

2. **Make `review_autofix` terminal when helper/bootstrap or PR-state preconditions break**
   - **Failure evidence:** the long cancelled runs above all saw `workspace_safety_check.sh` missing shortly after editor prompt emission; **26968187150** also detected a closed PR but remained alive for hours.  
   - **Root cause category:** terminal-state handling / cleanup / fail-open behavior.  
   - **Exact fix:** if required helper state is missing, emit a structured warning/error and exit the phase immediately; if the PR is closed, stop the run immediately after recording the terminal state.  
   - **Expected reliability impact:** should remove the dominant long-tail cancellation class in `review_autofix`.  
   - **Rollback/fail-open considerations:** if hard failure is too risky, fall open into a clearly marked “skipped due missing helper” outcome — but do not hang.

3. **Keep optional script references consistent across release/test refs**
   - **Failure evidence:** `test_and_mark_stable` run **26942520644** failed on `scripts/render_prompt.py` absence. Current repo main already documents the intended behavior in `mark-stable.yml:295-303`, `test-and-mark-stable.yml:3355-3363`, and `validate.yml:318-330`, where `render_prompt.py` is treated as optional/bootstrap-staged.  
   - **Root cause category:** workflow/script reference drift across refs.  
   - **Exact fix:** centralize the optional-script list or reuse the same helper/snippet across release/test/stable refs so the cross-reference gate and runtime bootstrap agree.  
   - **Expected reliability impact:** removes a deterministic release-gate failure mode.  
   - **Rollback/fail-open considerations:** low; this is backward-compatible and already reflected in mainline code.

4. **Fix the log-archive observability gap**
   - **Failure evidence:** `analysis_context.errors` contains **10** `partial_data:missing_log_archive` soft-fails: **9** `validate` runs and **1** `implement` run. That leaves much of the `validate` family unclassified.  
   - **Root cause category:** collector/log-retention timing gap, not proven workflow failure.  
   - **Exact fix:** add a delayed second archive fetch before finalizing 404 soft-fail, and/or persist a minimal job summary artifact for `validate`/`implement` even when full logs are unavailable.  
   - **Expected reliability impact:** better failure attribution and fewer blind reruns.  
   - **Rollback/fail-open considerations:** keep the current soft-fail classification; just improve the evidence captured.

5. **Reclassify Semble fixture fallbacks as healthy fail-open**
   - **Failure evidence:** the **25** runtime-confirmed fallbacks were all `target=overflow`, all `reason=...missing_semble`, all in CI/stable test contexts. There were **no** runtime `SERENA_FALLBACK` or `SERENA_PROBE` lines, and repo-wide `break_glass_count=0`, `context_budget_warn_count=0`.  
   - **Root cause category:** test-fixture noise in operational counters.  
   - **Exact fix:** tag or filter `target=overflow` + `missing_semble` fixture fallbacks out of operational reliability dashboards.  
   - **Expected reliability impact:** cleaner alerting; prevents false “MCP rollout broken” diagnoses.  
   - **Rollback/fail-open considerations:** low.

## AI Memory Health

- I found **28** structured `AI_MEMORY_TELEMETRY` events in deep-dive logs, all in **7 `review_autofix` runs**: **26930968999, 26934703864, 26944643043, 26952637519, 26960113331, 26968187150, 26977120613**.
- Operation mix:
  - `record-run-event`: **14**
  - `retrieve`: **7**
  - `record-candidate`: **7**
- `retrieve` effectiveness is currently poor:
  - **Hit rate:** **0%** (`0/7` had `records_selected > 0`)
  - **Average `estimated_tokens`:** **0.0**
  - **`keyword_method`:** `llm` in **7/7**
  - **`enabled`:** `true` in **7/7**
- Health signals:
  - No `enabled: false` retrieves observed.
  - No `fail_open: true` retrieves observed.
  - All push-capable events had `push_attempts=1`; no high retry counts.
  - `record-candidate` writes succeeded in all 7 runs, so write-path health looks okay.
- Interpretation: the memory system is **recording**, but not **retrieving useful context** in this window.
- Recommendation: tighten retrieval keys around PR/issue/head SHA, or short-circuit retrieval when no prior records exist for that scope. Right now it adds complexity without shortening prompts or improving outcomes.

## GH API Call Audit

No deep-dive run showed `429` or secondary-rate-limit events, so this is a **preventive** API audit, not an emergency one.

1. **`test-and-mark-stable.yml` is the heaviest API surface**
   - **Evidence:** code inventory shows **102** GitHub API call sites. A concrete hotspot is the PR-stability loop in `.github/workflows/test-and-mark-stable.yml:1041-1043`, which fetches `GET /pulls/{PR}` twice per attempt (`HEAD_A`, sleep, `HEAD_B`), then fetches it again at `1070` as `PR_META`.  
   - **Redundancy:** a stable-on-first-try path uses **3 PR fetches** where **2** are enough; worst case is **11** fetches across 5 attempts plus `PR_META`.  
   - **Exact change:** carry `HEAD_B` forward as the next `HEAD_A`, and reuse the final PR payload as `PR_META`.  
   - **Estimated call reduction:** about **33%** on the stable-fast path, up to **45%** in the worst-case loop.  
   - **Rate-limit risk reduction:** medium.

2. **`review_autofix.yml` check-run polling can multiply requests on long waits**
   - **Evidence:** `.github/workflows/review_autofix.yml:2273-2357` (`Collect PR check-run failures`) polls `GET /commits/{sha}/check-runs?per_page=100` in a loop, defaulting to **20s** intervals up to **300s**. `README.md` also warns that this “one logical snapshot” may fan out into multiple underlying API requests because of pagination/retries.  
   - **Redundancy:** unchanged pending-state snapshots cause repeated full check-run fetches.  
   - **Exact change:** after the first 1-2 polls, back off more aggressively when the pending set is unchanged, or stop early once the check-run state is stable enough for the editor context you actually need.  
   - **Estimated call reduction:** roughly **40-60%** on long waits.  
   - **Rate-limit risk reduction:** medium-high on busy PRs.

3. **`plan.yml` has comment-lifecycle churn**
   - **Evidence:** code inventory shows **28** API call sites. A concrete pattern is one progress-comment create (`827-829`), up to two patches (`1023-1025`, `1052-1054`), multiple deletes in mutually exclusive paths (`1171`, `1400`, `1423`), then follow-up comment posts (`1193`, `1404`, `1468`, `1483`).  
   - **Redundancy:** more comment state transitions than necessary for a single run.  
   - **Exact change:** use one progress comment state machine and only patch on real state transitions; keep cleanup in one final path.  
   - **Estimated call reduction:** **2-4 calls per plan run**.  
   - **Rate-limit risk reduction:** low-medium.

4. **`review_autofix_sweep.yml` is already doing the right batching**
   - **Evidence:** `.github/workflows/review_autofix_sweep.yml:104-148` snapshots active review runs **once per workflow** and reuses them locally. Recent runs **26982377351, 26984445346, 26985562831, 26986578845, 26987657647** all skipped the same PR because an active run already existed.  
   - **Audit note:** this already matches the repo’s own API-hygiene guidance in `README.md` H6/H5 to prefer batched calls and cycle-local caches.  
   - **Smallest safe improvement:** if the same `head_ref` has been skipped for `active_run` across consecutive sweeps, back off the sweep cadence for that branch.  
   - **Estimated call reduction:** modest — roughly the whole sweep’s API footprint when the run can be skipped entirely.

## Prompt Cache & Memory System

- **Prompt-cache telemetry is effectively absent.** Across **114** runs with log telemetry, repo aggregate shows `cache_hit_rate=null`, `or_prompt_tokens=0`, `or_completion_tokens=0`, `or_total_tokens=0`, `or_cache_write_tokens=0`, `or_cache_read_tokens=0`.  
- **The only visible cache hit in deep evidence was not prompt cache.** `orchestrate_poll` run **26985020033** logged a dependency-cache hit on `setup-uv...`; that does **not** say anything about prompt caching.
- **There were no runtime prompt-pressure alerts.** Repo-wide `break_glass_count=0` and `context_budget_warn_count=0`; I found no runtime `BREAK_GLASS` or `CONTEXT_BUDGET_WARN` lines.  
- **Prompt growth still looks real.** Sampled `review_autofix` editor prompts were **252,909-290,232 bytes**.  
  - **Inference:** likely cache-fragmentation sources are volatile run-local data such as temp paths (`/tmp/codex-pr-...` appears repeatedly in logs), per-run check-run context, and dynamic status/comment text around prompt assembly.  
- **Semble looks bounded, not bloated.** The 7 real `reviewer-context` queries averaged about **15.2KB/query**, much smaller than the **253-290KB** editor prompts. That suggests Semble is not the main prompt-size driver.
- **Memory retrieval is not helping cache pressure yet.** AI memory retrieve hit rate was **0%**.

**Concrete improvements**
1. Emit real prompt-cache counters and per-phase token usage from `review_autofix`, `plan`, and `implement`.  
   - **Impact:** unlocks measurable token/latency tuning.  
   - **Risk:** low.

2. Reuse the static-prefix pattern already present in `plan.yml` (`Pre-assemble static context`) for `review_autofix`.  
   - **Impact:** medium probable token/cache benefit.  
   - **Risk:** low.  
   - **Inference:** moving volatile data to the prompt tail should improve cache stability.

3. Make AI memory retrieval conditional on prior records for the same PR/issue/head SHA.  
   - **Impact:** low-medium latency simplification until hit rate improves.  
   - **Risk:** low.

## Orchestrator Health

- The orchestrator control plane looks mostly healthy in this window:
  - `clarify`: **197** runs, **0** failures
  - `plan`: **189** runs, **0** failures
  - `orchestrate_clarify_respond`: **186** runs, **0** failures
  - `orchestrate_poll`: **21/21** success
- The main orchestrator inefficiency is **trigger churn**, not logic failure. Skipped no-op runs averaged only **~1.4-1.6s**, and recent runs **26987348056** (`plan`), **26987348025** (`clarify`), and **26987348062** (`orchestrate_clarify_respond`) were clean false-condition exits.
- The bigger downstream problem is that once work is triggered, `review_autofix` can sit for hours in a bad terminal state. That makes orchestrator deferrals look normal while the real PR path is stuck.
- Repeated sweep evidence shows deferred-but-active behavior is common: recent sweep runs **26982377351, 26984445346, 26985562831, 26986578845, 26987657647** all skipped the same PR because an active review run already existed.
- I did **not** see a recurring wave-state corruption or clarification-loop explosion in the available deep dives; keep conclusions bounded because direct wave-progression telemetry was sparse.

**Smallest safe mitigations**
- Filter `<!-- ORCHESTRATOR_STATE_V2 ... -->` comments earlier so obvious no-op workflows do not start.
- Track a per-`head_ref` “consecutive `AUTOFIX_SWEEP_SKIP ... reason=active_run`” counter.
- Alert on `review_autofix` runs that exceed **30 minutes** without `phase_completed`/`phase_failed`.

**Observable indicators to track**
- no-op skip ratio by family
- `review_autofix` runs >30 min
- consecutive active-run sweep skips per head ref
- missing-log-archive count
- AI memory retrieve hit rate

## Pipeline Flow Bottlenecks

| Phase | Evidence | Bottleneck type | Priority fix |
|---|---|---:|---|
| Clarify | `clarify` **190 skipped** runs, avg skipped duration **1.41s** | trigger churn | low |
| Plan | active successful runs **26934079662 = 600s**, **26976217485 = 524s**; family has **183 skipped** runs | compute + comment/API overhead | medium |
| Implement | active successful runs **26934436094 = 553s**, **26976654429 = 655s**; one `implement` failure lacked log archive | compute + observability gap | medium |
| Review/autofix | successful runs like **26952058478 = 1462s**, **26964811866 = 1300s**; cancelled outliers **14449-20891s** | compute + hang/retry dead path | **highest** |
| CI | successful runs **1441-1504s**; 5 repeated plumbing failures around **220-232s** | compute + deterministic regression | **high** |
| Validate | family shows **12/13 failures**, but at least **9** are missing-log-archive soft-fails | observability gap | high for diagnosis |
| Orchestrate poll/sweep | `orchestrate_poll` p50 **174s**; run **26985020033** spent most of **185s** in runner wait/cleanup; sweeps are **7-9s** total with ~**2s** useful work | queueing/cleanup overhead | low-medium |
| Workflow log analysis | run **26942555350 = 3619s**; `analyze-commit-notify` **1580.8s** | auxiliary compute + token spend | medium |

**Ordered end-to-end fixes**
1. Kill the `review_autofix` dead-path hang.  
2. Backport/fix the CI plumbing regression and keep that contract test first.  
3. Make release/stable preflight checks gate the long E2E/analysis jobs.  
4. Narrow `workflow_log_analysis` summarization scope.  
5. Only then tune no-op orchestrator trigger churn.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` cancellations/hangs: **15** cancelled runs, **24.15 runner-hours** total.
  - CI runtime: successful CI runs cluster around **24-26 minutes**.
  - Auxiliary heavy workflows: `test_and_mark_stable` **3702s**, `workflow_log_analysis` **3619s**.

- **Top failure modes**
  - Repeated CI plumbing regression in `lint / Review autofix review-pipeline plumbing contract test`.
  - Missing log archives obscuring `validate` and one `implement` failure.
  - Release-gate script/workflow drift around `scripts/render_prompt.py`.
  - One separate `yamllint` TypeError failure (**26953240776**).

- **Highest-cost drivers**
  - `workflow_log_analysis` summarization (`149,183` explicitly logged tokens in run **26942555350**).
  - `review_autofix` cancellation waste (**24.15 runner-hours**).
  - Repeat CI failures and release-gate reruns.

- **Top 3 prioritized actions**
  1. Make `review_autofix` terminal on missing helper / closed PR instead of hanging for hours.
  2. Ensure all tested refs carry the current per-attempt prompt-file fix and keep that contract test first in CI.
  3. Improve observability and release gating together: backport optional-script allowlist parity and add delayed log-archive refetch/minimal summary artifacts for `validate`/`implement`.

## Metrics Appendix

### Repository summary

| Repository | Runs | Success | Failure | Cancelled | Other | Failure rate | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 221 | 20 | 18 | 741 | 2.0% | 194.9 | 1.0 | 985.0 |

### Key workflow-family metrics

| Family | Runs | Success | Failure | Cancelled | Other | Failure rate | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 80 | 62 | 0 | 15 | 3 | 0.0% | 76.0 | 14449.15 |
| ci | 31 | 25 | 6 | 0 | 0 | 19.4% | 1448.0 | 1555.5 |
| plan | 189 | 6 | 0 | 0 | 183 | 0.0% | 1.0 | 6.0 |
| implement | 189 | 6 | 1 | 3 | 179 | 0.5% | 1.0 | 10.0 |
| clarify | 197 | 7 | 0 | 0 | 190 | 0.0% | 1.0 | 4.8 |
| orchestrate_poll | 21 | 21 | 0 | 0 | 0 | 0.0% | 174.0 | 285.0 |
| orchestrate_clarify_respond | 186 | 1 | 0 | 0 | 185 | 0.0% | 1.0 | 2.0 |
| validate | 13 | 1 | 12 | 0 | 0 | 92.3%* | 0.0 | 70.0 |
| test_and_mark_stable | 1 | 0 | 1 | 0 | 0 | 100.0% | 3702.0 | 3702.0 |
| workflow_log_analysis | 1 | 1 | 0 | 0 | 0 | 0.0% | 3619.0 | 3619.0 |

\*At least 9 of the 12 `validate` failures were missing-log-archive soft-fails, so this rate is not clean root-cause evidence.

### Cost / telemetry summary

| Scope | Runs w/ log telemetry | Codex calls | Codex tokens | `or_prompt` | `or_completion` | `or_total` | `or_cache_write` | `or_cache_read` | `cache_hit_rate` | `wall_clock_p50_ms` | `wall_clock_p99_ms` | `BREAK_GLASS` | `CONTEXT_BUDGET_WARN` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|
| Repo aggregate | 114 | 5 | 10130 | 0 | 0 | 0 | 0 | 0 | null | 1000 | 15906710 | 0 | 0 |
| review_autofix | 13 | 2 | 4052 | 0 | 0 | 0 | 0 | 0 | null | 8734000 | 20303960 | 0 | 0 |
| ci | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | null | 222000 | 231840 | 0 | 0 |
| workflow_log_analysis | 1 | 3 | 6078 | 0 | 0 | 0 | 0 | 0 | null | 3619000 | 3619000 | 0 | 0 |
| test_and_mark_stable | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | null | 3702000 | 3702000 | 0 | 0 |

### Explicit extra token signal not captured by `or_*`

| Run ID | Workflow | Signal | Value |
|---|---|---|---:|
| 26942555350 | workflow_log_analysis | `summarize_unselected_runs.tokens_used` | 149183 |

### Semble / Serena / other MCP summary

| Server | Scope | Query calls | Logged bytes | Fallbacks | Response bytes | Tool calls | Query ms | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---|
| Semble | Repo aggregate | 12 | 137800 | 70 | — | — | — | Aggregate is inflated by `workflow_log_analysis` self-reference |
| Semble | Runtime-confirmed non-analysis | 7 | 106378 | 25 | — | — | ~2921 total | 7 real `reviewer-context` queries; 25 real fixture fallbacks |
| Serena | Repo aggregate | 0 | 0 | 0 | 0 | 0 | 0 | No runtime activity |
| Other MCP servers observed | Repo aggregate | 0 | 0 | 0 | 0 | 0 | 0 | None |

### Per-target MCP availability / probe rows

| Server | Target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---:|---:|---:|---|
| Serena | all targets | 0 | 0 | 0 | No runtime `SERENA_PROBE`; deep `review_autofix` logs show `SERENA_ENABLED: false` |
| Semble | reviewer-context | 0 | 0 | 0 | No Semble probe telemetry emitted in this schema/window |
| Semble | overflow | 0 | 0 | 0 | Fallback-only test target; not a probe path |

### AI memory summary

| Workflow family | Runs with memory telemetry | Total events | `retrieve` | Hit rate | Avg `estimated_tokens` | `keyword_method` | `record-candidate` | `record-run-event` | `enabled=false` | `fail_open=true` | Push attempts >1 |
|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|
| review_autofix | 7 | 28 | 7 | 0% | 0.0 | `llm` only | 7 | 14 | 0 | 0 | 0 |

### Review/autofix stall sample

| Run ID | Outcome | Editor prompt bytes | Semble bytes | Editor → terminal | Terminal event |
|---|---|---:|---:|---:|---|
| 26930968999 | success | 252909 | 15711 | 4.7 min | `phase_completed` |
| 26934703864 | cancelled | 254394 | 15067 | 184.5 min | `phase_failed` |
| 26944643043 | cancelled | 257854 | 15067 | 200.1 min | `phase_failed` |
| 26960113331 | cancelled | 268338 | 15067 | 191.5 min | `phase_failed` |
| 26968187150 | cancelled | 282160 | 15067 | 209.5 min | `phase_failed` |
| 26977120613 | cancelled | 290232 | 15332 | 198.8 min | `phase_failed` |

### GH API inventory summary

| Workflow / file | Approx API call sites | Concrete hotspot | Suggested reduction |
|---|---:|---|---|
| `.github/workflows/test-and-mark-stable.yml` | 102 | repeated `GET /pulls/{PR}` in stability loop (`1041-1043`, `1070`) | reuse payloads; ~33-45% cut in that block |
| `.github/workflows/review_autofix.yml` | 63 | check-run polling loop (`2273-2357`) | back off unchanged polls; ~40-60% on long waits |
| `.github/workflows/implement.yml` | 58 | broad issue/PR/comment API surface | secondary audit after the top two items |
| `.github/workflows/plan.yml` | 28 | progress comment create/patch/delete churn | 2-4 calls/run |
| `.github/workflows/clarify.yml` | 14 | repeated issue/comment operations | low-priority consolidation |
| `.github/workflows/review_autofix_sweep.yml` | 3 | already batched active-run snapshots | keep as-is; only add branch cooldown |

If you want, I can turn this into a commit-ready markdown report file under `analysis/workflow-optimization-2026-06-05.md`.
