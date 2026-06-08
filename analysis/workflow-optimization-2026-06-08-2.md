## Executive Summary

- **Fix the `orchestrate_poll_process` regression first.** `ci` failed **16/20** times, and **15** of those failures were the same `lint / Orchestrate poll process unit tests` failure; the same regression also broke `test_and_mark_stable` run **27112686470**. Fast-failing that suite earlier would have saved about **24,770s (~6.9h)** across the 15 repeated CI failures in this window. **Estimated impact:** very high. **Confidence:** high.
- **`review_autofix` is the dominant AI cost center.** In log-parsed runs it consumed **354,256,628 / 356,921,169 measured Codex tokens (99.3%)**. Tail runs were extreme: run **27112656269** took **7,989s** and used **98,861,184** tokens; run **27110905305** took **3,940s** and used **57,673,076** tokens. **Estimated impact:** very high if reviewer fan-out/reasoning is right-sized. **Confidence:** high.
- **A non-blocking stable-gate audit is still blocking wall-clock.** In `test_and_mark_stable` run **27112686470**, `workflow-log-analysis-test` watched child run **27112704280** until **03:30:58Z** even though it only emitted a warning and `validate-scripts` had already failed at **02:54:48Z**. That added about **36.2 minutes** of unnecessary tail time. **Estimated impact:** high. **Confidence:** high.
- **Prompt-size pressure is real; policy pressure is not.** Repo-wide `break_glass_count` was **0**, but `context_budget_warn_count` was **42** (**30** in `review_autofix`, **12** in `workflow_log_analysis`). Sampled review runs logged warnings at **216,970**, **221,873**, **223,657**, and **225,488** prompt tokens. **Estimated impact:** high on both speed and cost if warnings trigger a cheaper path. **Confidence:** high.
- **AI memory retrieval is currently not helping sampled review runs.** Deep-dive logs contained **8** `retrieve` events; **0/8** selected any records, average `estimated_tokens` was **0**, and all used `keyword_method="llm"`. **Estimated impact:** medium. **Confidence:** high.
- **Semble is not the cost problem.** Strict non-analysis deep-dive verification found **11** real `SEMBLE_QUERY` events totaling **135,179 bytes**, mostly ~15 KB `target=reviewer-context` fetches in `review_autofix`; by contrast the expensive review runs were burning tens of millions of model tokens. **Estimated impact:** medium if used to trim prompts further; low if disabled. **Confidence:** high.

## Speed Optimizations

### Critical-path wins

1. **Split the failing `orchestrate_poll_process` tests into an early fast-fail job**
   - **Evidence:** `ci` had **20** runs, **16** failures, **p50 1671s**, **p95 1722.1s**. In failing run **27131537810** (`errors/.../ci/27131537810/step-001-lint.log`), the job reached `284 passed, 1 failed, 285 total` and then emitted repeated `Integration fingerprint verification FAILED — resolver output regressed merged sub-issue intent`. The same underlying test failure appeared in `test_and_mark_stable` run **27112686470** (`step-009-validate-scripts.log`), where `FAIL  test_implementation_failed_reissue_preserves_dependency_gates_and_pending_defs` ended with `284 passed, 1 failed, 285 total`.
   - **Root cause:** A known regression is detected late in a long `lint`/unit-test sequence instead of at the front of the pipeline.
   - **Exact change:** Add a dedicated early CI job that runs `tests/test_orchestrate_poll_process.py` (or the narrower fingerprint/reissue subset) before the long lint/test bundle. Reuse that same targeted job in `test_and_mark_stable` so the stable gate fails before running long watcher steps.
   - **Estimated time savings:** About **1,651s/run (~27.5 min)** on each repeated CI failure; **24,770s (~6.9h)** across the 15 repeated CI failures in this window.
   - **Implementation risk:** **Low.** This is a test-ordering/job-graph change, not a behavior change.

2. **Stop waiting to terminal state on the non-blocking `workflow-log-analysis` smoke dispatch**
   - **Evidence:** In `test_and_mark_stable` run **27112686470**, `step-002-workflow-log-analysis-test.log` shows `Watching run #27112704280`, then at **03:30:58Z** `status=completed conclusion=failure`, followed by `workflow-log-analysis run #27112704280 concluded failure; continuing because this audit is non-blocking`. But `validate-scripts` had already failed at **02:54:48Z** in `step-009-validate-scripts.log`.
   - **Root cause:** The workflow is semantically fail-open but operationally still blocks a runner while polling the child workflow to completion.
   - **Exact change:** Keep the dispatch, record the child run ID if available, and exit once registration succeeds. Report the child status later via a summary comment/artifact/next scheduled analysis instead of holding the stable-release job open.
   - **Estimated time savings:** In run **27112686470**, about **2,170s (~36.2 min)** of tail time after the real blocker had already occurred.
   - **Implementation risk:** **Low.** The step already treats child failure as warning-only.

3. **Right-size `review_autofix` before the second pass turns into the critical path**
   - **Evidence:** `review_autofix` completed-run **p50** was **494s** but **p95** was **3346.5s**. Log-parsed spend was **354,256,628** tokens across **174** Codex calls. Extreme runs:
     - **27112656269**: **7,989s**, **98,861,184** tokens, **48** calls, **6** context warnings.
     - **27110905305**: **3,940s**, **57,673,076** tokens, **30** calls, **6** context warnings.
     - **27116473444**: **3,134s**, **49,430,592** tokens, **24** calls, **6** context warnings.
     Workflow defaults in `.github/workflows/review_autofix.yml` are **6 reviewer models**, `REVIEWER_REASONING_EFFORT=xhigh`, `ENABLE_REVIEWER_TWO_PASS=true`; `scripts/review_run_reviewers.sh` still runs pass 1 at **xhigh** and leaves pass 2 effectively **xhigh** by default.
   - **Root cause:** Full six-reviewer, two-pass, xhigh review is applied even when the prompt is already oversized.
   - **Exact change:** Enable `REVIEWER_RISK_TIER_ENABLED`, define lite/trivial reviewer subsets, and on first `CONTEXT_BUDGET_WARN` switch to a reduced path: fewer reviewers, no extra overflow expansion, and `REVIEWER_PASS2_REASONING_SMALL=high` for non-critical diffs.
   - **Estimated time savings:** **10-40 minutes** on tail review runs; biggest wins are on the 24-call/48-call outliers.
   - **Implementation risk:** **Medium.** Keep the current full path for critical globs already encoded in `REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX`.

### Micro-optimizations

4. **Lazy-install Codex and Semble in `orchestrate_poll` only when actually needed**
   - **Evidence:** Recent poller run **27135212721** succeeded in **147s** with **0 Codex calls** and **0 Semble queries**. Its explicit setup steps still consumed:
     - `Checkout repository`: **16.4s**
     - `Install Codex CLI`: **3.5s**
     - `setup-uv`: **1.6s**
     - `Install semble`: **9.4s**
     - Actual `Process each tracking issue`: **45.1s**
     In `.github/workflows/orchestrate_poll.yml`, these installs run whenever `steps.find_tracking.outputs.has_work == 'true'`.
   - **Root cause:** Fixed tool bootstrap runs on every poll cycle with work, even on deterministic/no-AI cycles.
   - **Exact change:** Gate Codex/Semble install behind a derived `needs_codex` / `needs_semble` flag after scanning issue state, or move installs into the specific stall/clarify/conflict branches that invoke them.
   - **Estimated time savings:** **15-20s per poll run** immediately; roughly **8-11%** of the family p50.
   - **Implementation risk:** **Low-medium.** Requires careful gating so recovery paths still have tools when needed.

**Cross-cutting note:** runner queueing is visible in successful runs across `ci` (**27134096577**, **27131950306**), `plan` (**27134456768**), `implement` (**27134812498**), `review_autofix` (**27134094946**), `orchestrate_poll` (**27135212721**), and multiple `test_and_mark_stable` jobs. Since new infrastructure is out of scope, the safest latency reduction is to remove non-critical long-lived jobs and unnecessary setup from the queue.

## Cost Optimizations

1. **Turn on review risk-tiering; stop paying six-reviewer/two-pass/xhigh on every PR**
   - **Evidence:** `review_autofix` accounts for **99.3%** of measured Codex tokens in the window (**354.3M / 356.9M**). In sampled logs, `REVIEWER_RISK_TIER` reported `reviewers=6 enabled=false`, and `Two-pass review enabled.` was active while `REVIEWER_REASONING_EFFORT: xhigh`.
   - **Root cause:** The expensive path is the default path.
   - **Exact change:** Enable `REVIEWER_RISK_TIER_ENABLED=1`, keep full review for critical files matching the existing always-full regex, and define smaller reviewer sets for trivial/lite diffs.
   - **Estimated savings:** A **30%** cut on current measured `review_autofix` spend would save about **106,276,988** tokens in a window like this; **50%** would save about **177,128,314** tokens.
   - **Quality-risk notes:** Low if the full path remains mandatory for `scripts/`, `.github/workflows/`, `prompts/`, `workflow-templates/`, and `ai-memory/`.

2. **Use `CONTEXT_BUDGET_WARN` as a hard cost circuit breaker, not a passive warning**
   - **Evidence:** Repo aggregate `context_budget_warn_count` was **42**; outside the self-referential `workflow_log_analysis` run, all sampled warnings were in `review_autofix`. Specific runs:
     - **27110905305**: warnings at **216,970** and **220,663** prompt tokens.
     - **27116473444**: warnings at **221,873** and **223,657** prompt tokens.
     - **27112656269**: warnings at **225,488** prompt tokens.
     All three runs had **6** warnings each and zero `BREAK_GLASS`.
   - **Root cause:** The system keeps building larger prompts even after it has crossed the configured warning threshold (`CONTEXT_BUDGET_WARN_RATIO=0.7`).
   - **Exact change:** On first warning, freeze prompt growth: summarize pass-1 findings once into a bounded block, cap overflow retrieval, and downgrade pass 2 to `high` or single-pass for non-critical diffs.
   - **Estimated savings:** **High** on tail runs; on the largest 24-call/48-call runs, eliminating an extra full review pass could cut call count roughly in half (**inference**).
   - **Quality-risk notes:** Use fail-open escalation back to full review for critical paths or if summarization fails.

3. **Keep Semble enabled; cut reviewer context, not retrieval bytes**
   - **Evidence:** Repo aggregate shows **17** Semble queries / **214,335** bytes. Strict non-analysis deep-dive verification found **11** real queries / **135,179** bytes: **7** `target=reviewer-context` and **4** `target=overflow`, all in `review_autofix`. Example: run **27110905305** logged `SEMBLE_QUERY target=reviewer-context ... bytes=15735 ms=587` while the run still consumed **57.7M** model tokens.
   - **Root cause:** Semble fetches are small and targeted; the expensive part is downstream model fan-out.
   - **Exact change:** Keep `SEMBLE_ENABLED=true`; if anything, lean on it earlier for overflow selection and trim raw prompt expansion around it.
   - **Estimated savings:** Disabling Semble is unlikely to save meaningful cost and may increase prompt size. The cost win is in surrounding prompt architecture, not the Semble bytes themselves.
   - **Quality-risk notes:** Low. Current evidence says Semble is helping more than hurting.

4. **Do not spend optimization effort on Serena until telemetry proves it is replacing work**
   - **Evidence:** Repo aggregate recorded only **2** Serena queries, **0** response bytes, **0** tool calls, and **0** fallbacks/probe events. I did not verify any non-analysis runtime Serena lines in deep-dive logs; recent `review_autofix` log summaries (**27134094946**, **27131948445**) showed `SERENA_ENABLED: false`.
   - **Root cause:** Serena is not yet showing measurable substitution value in the sampled production paths.
   - **Exact change:** Before expanding Serena use, emit `response_bytes`, `tool_calls`, and target-level success metrics on real runs; otherwise it is impossible to prove savings.
   - **Estimated savings:** Unknown today; current evidence does not justify rollout work.
   - **Quality-risk notes:** Low; this is a measurement-first recommendation.

5. **Improve prompt-prefix stability before drawing conclusions from `cache_hit_rate`**
   - **Evidence:** Repo aggregate `cache_hit_rate` was **0.235294**, but only **2** `or_calls` and **250** total `or_*` tokens were recorded. `review_autofix` pass 2 appends a per-run cross-pollination summary (`scripts/review_run_reviewers.sh:3190-3323`), which is highly dynamic.
   - **Root cause:** Dynamic reviewer ledgers and overflow blocks likely fragment prompt cache prefixes (**inference**).
   - **Exact change:** Pull static instructions/checklists into a stable preassembled file, canonicalize block ordering, and append dynamic cross-pollination/memory blocks last.
   - **Estimated savings:** Medium upside, but current telemetry is too sparse to quantify confidently.
   - **Quality-risk notes:** Low.

## Reliability Improvements

1. **Repair the `implementation_failed` reissue / merged-sub-issue-intent regression**
   - **Failure evidence:** `ci` had **15** failures at `lint / Orchestrate poll process unit tests`, and `test_and_mark_stable` run **27112686470** failed the same logic in `validate-scripts / Unit tests`. That is **16 of 18** observed failures in the window.
   - **Root cause category:** Orchestrator state/regression bug.
   - **Exact fix:** Repair the `scripts/orchestrate_poll_process.sh` behavior that changed merged sub-issue intent / dependency-gate preservation until `test_implementation_failed_reissue_preserves_dependency_gates_and_pending_defs` and the integration fingerprint checks pass again. Keep a dedicated fast contract job in front of broader CI.
   - **Expected reliability impact:** Very high; this single fix should remove the dominant failure mode.
   - **Rollback / fail-open:** If the precise fix is risky, revert the offending state-transition change first, then re-land behind a targeted test gate.

2. **Treat `CONTEXT_BUDGET_WARN` as a reliability alarm for prompt-size risk**
   - **Failure evidence:** Repo aggregate had **42** warnings and **0** `BREAK_GLASS` events. The warnings are concentrated in `review_autofix` (**30**) plus the self-referential `workflow_log_analysis` run (**12**). Sampled review runs crossed **216k-225k** prompt tokens.
   - **Root cause category:** Prompt-size blow-up, not policy/rubric pressure.
   - **Exact fix:** When `CONTEXT_BUDGET_WARN` fires, switch to bounded review mode rather than continuing to expand context. Emit a run summary note so operators can see when the reduced path was used.
   - **Expected reliability impact:** Medium-high; reduces risk of empty-output retries, latency blowups, and cancellation exposure.
   - **Rollback / fail-open:** Keep full review for critical-file diffs or if the bounded path fails to build.

3. **Reclassify CI `SEMBLE_FALLBACK` noise so real MCP regressions stay visible**
   - **Failure evidence:** In failing CI run **27131537810**, `step-001-lint.log` logged **5** `SEMBLE_FALLBACK target=overflow` events, all caused by temp paths like `/tmp/.../missing_semble`. Repo aggregate shows **75** CI fallbacks; strict deep-dive verification found **60** unique non-analysis fallback events across inspected CI logs, all on `target=overflow`. Success run **27134096577** also carried Semble fallback counts, so these fallbacks are not correlated with pipeline failure.
   - **Root cause category:** Test-fixture fail-open behavior surfacing in production telemetry.
   - **Exact fix:** Tag fixture fallbacks explicitly (for example, by recognizing `missing_semble` temp paths) and exclude them from rollout-health alerts while preserving them in test logs.
   - **Expected reliability impact:** Medium on signal quality; it will reduce false MCP outage alarms.
   - **Rollback / fail-open:** Keep raw counts in debug output so contract tests still validate fallback behavior.

4. **Keep `workflow_log_analysis` fail-open, but preserve partial output sooner**
   - **Failure evidence:** Run **27112704280** failed in `deep-audit` after **3,564s**. The direct step log ended with `The runner has received a shutdown signal` and `The operation was canceled`; `api-redundancy` was skipped because `deep-audit` did not complete.
   - **Root cause category:** Long monolithic observability job vulnerable to runner cancellation.
   - **Exact fix:** Preserve a partial artifact/report before the expensive deep-audit Codex pass and allow downstream lightweight analysis to continue off `analyze-commit-notify` output when deep-audit is interrupted.
   - **Expected reliability impact:** Medium; reduces observability blind spots without changing product behavior.
   - **Rollback / fail-open:** Existing fallback report behavior can remain the default if partial chaining is unstable.

5. **Clean up the non-fatal `git-submodule` warning in `implement`**
   - **Failure evidence:** Recent successful `implement` run **27134812498** logged `fatal: /usr/lib/git-core/git-submodule cannot be used without a working tree.` and `The process '/usr/bin/git' failed with exit code 1`.
   - **Root cause category:** Tooling precondition mismatch.
   - **Exact fix:** Guard the submodule call behind a working-tree check or skip it when checkout runs with `submodules: false`.
   - **Expected reliability impact:** Low, but it removes warning fatigue and makes real failures easier to spot.
   - **Rollback / fail-open:** Safe to no-op the submodule probe if the repo does not use submodules.

**Semble / Serena rollout health summary:** inspected CI Semble fallbacks are healthy fixture fail-open behavior, not a broken rollout; `SERENA_FALLBACK` remained **0** and `SERENA_PROBE` stayed **0/0/0**, so there is no evidence of Serena availability failure in this window.

## AI Memory Health

Deep-dive logs under `errors/`, `slow/`, and `recent/` (excluding `workflow_log_analysis` contamination) produced these memory findings:

| Metric | Value |
|---|---:|
| Unique `AI_MEMORY_TELEMETRY` events | 35 |
| `record-run-event` | 20 |
| `retrieve` | 8 |
| `record-candidate` | 7 |
| `retrieve` hit rate (`records_selected > 0`) | 0 / 8 = **0%** |
| Avg `estimated_tokens` on retrieve | **0** |
| `keyword_method` distribution | `llm`: 8, `plain`: 0, `none`: 0 |
| `fail_open: true` retrieves | 0 |
| `enabled: false` retrieves | 0 |
| High push-retry outliers | run **27103488076** had `push_attempts=3` |

- **Retrieve coverage:** all **8** retrieve events were in `review_autofix` runs (**27129171047**, **27103488076**, **27116473444**, **27110905305**, **27116465738**, **27112662126**, **27129334186**, **27112656269**), and all returned `records_selected=0`.
- **Budget comparison:** sampled retrieve events emitted `estimated_tokens`, but no explicit per-retrieve budget field, so budget utilization could not be compared directly.
- **Write health:** `orchestrate_poll` run **27135212721** successfully recorded both `poll_started` and `poll_completed` with `push_attempts=1`.
- **Not observed in deep-dive logs:** `finalize-task`, `promote`, `compact`, `processed-command-claim`, and `processed-command-complete`.

**Recommendation:** review-path retrieval should either (a) add a cheap plain-keyword fallback seeded from PR title + touched paths after an `llm` miss, or (b) be temporarily skipped when the last N runs have a 0% hit rate. Today it adds complexity without adding context.

## GH API Call Audit

**Caveat:** the current window did **not** surface direct per-run GH API call counters or any 429 / secondary-rate-limit events, so this audit is based on workflow/code inspection plus log evidence.

| Workflow / step | Evidence | High-redundancy pattern | Concrete change | Estimated call reduction |
|---|---|---|---|---|
| `test_and_mark_stable.yml` dispatch/watch blocks | Repeated `PRE -> dispatch -> NEW_ID -> status poll` logic at `.github/workflows/test-and-mark-stable.yml:3523-3543`, `3699-3719`, `3780-3800`, `3852-3872`, `4213-4233`; run **27112686470** spent ~36 extra minutes waiting on a non-blocking child workflow | Tight polling of workflow-run lists/status in long watcher loops | For non-blocking watchers, stop after dispatch registration. For blocking watchers, use a shared helper with exponential poll-backoff after registration | **High** on long watches; **inference:** 50-80% fewer poll calls on hour-long waits |
| `internal-review.yml` `resolve-claude-branch-pr` | `.github/workflows/internal-review.yml:101-116` does an open-PR lookup, then a repo default-branch lookup if event data is empty | Up to 2 REST calls on every push before real work starts | Trust `github.event.repository.default_branch` whenever present; only fall back to `repos/${REPOSITORY}` when absent. Optionally combine into one GraphQL query | Small per run, broad-volume over many pushes |
| `review_autofix.yml` post-merge validate dispatch | `.github/workflows/review_autofix.yml:778-805` falls back from GraphQL to PR-text parsing, then may call `gh issue view` once per linked issue to recover labels | Per-issue label lookup in a loop on fallback path | When fallback issue numbers are discovered, batch-fetch issue labels in one GraphQL query instead of N `gh issue view` calls | Up to **N-1** calls saved on multi-issue PRs |
| `scripts/orchestrate_poll_process.sh` | `CLAUDE.md:419-446` requires batching/caching; `scripts/orchestrate_poll_process.sh` already uses `_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`, `ACTIVE_WORKFLOW_ISSUES`, and `STALL_MANAGED_LINKED_PR_CACHE` | This is the good pattern to preserve | Keep extending existing GraphQL helpers and cycle-local caches instead of adding inner-loop REST calls | Prevents future regressions |

**Bottom line:** API hygiene is already strong in `orchestrate_poll_process.sh`; the biggest remaining wins are in watcher polling behavior and a few fallback paths. No direct rate-limit pain was observed, so the goal here is reducing shared-resource load before it becomes visible.

## Prompt Cache & Memory System

- **Prompt-cache telemetry is sparse.** Repo aggregate `cache_hit_rate` was **0.235294**, but only **2** `or_calls` and **250** `or_total_tokens` were recorded across **123** log-parsed runs. That is not enough to treat current prompt-cache performance as representative.
- **Do not confuse GitHub dependency caches with AI prompt caching.** Recent runs like **27134812498** and **27132232853** logged `setup-uv...` cache hits; those are useful, but they do not prove AI prompt cache effectiveness.
- **The likely cache-fragmentation source is `review_autofix`.** `scripts/review_run_reviewers.sh:3190-3248` builds a dynamic cross-pollination summary from pass-1 outputs, and `:3320-3323` appends it to the pass-2 prompt. Combined with actual `CONTEXT_BUDGET_WARN` events above **216k** prompt tokens, this strongly suggests unstable prefixes are eroding reuse (**inference**).
- **Concrete improvements:**
  1. Preassemble the static reviewer instructions/checklists once, the same way `plan.yml` already uses `scripts/build_static_context.sh`.
  2. Keep stable blocks first and move volatile cross-pollination, memory, and overflow blocks to the end.
  3. Canonicalize ordering of files/chunks before prompt assembly so semantically identical runs share prefixes.
  4. When `CONTEXT_BUDGET_WARN` fires, cap or summarize overflow context instead of appending more raw material.

**Estimated impact:** medium on tokens and latency once telemetry coverage improves; low risk because this is prompt assembly hygiene, not logic change.

## Orchestrator Health

- **Healthy behavior observed:** `orchestrate_poll` went **34/34** in the window. Recent run **27135212721** successfully reconciled `issue=3232` from `old=pending` to `new=done`, and emitted one `STALL_REANCHOR_FALLBACK issue=3232 source=branch_name resolved=2026-06-08T11:40:28Z`. That looks like a healthy rare fallback, not a masked outage.
- **Brittle area:** the repeated CI/stable failures around `implementation_failed` reissue and merged-sub-issue intent show that the orchestrator state machine is currently fragile in exactly the paths that recover from failed implementation work.
- **Safety vs throughput tradeoff:** `.github/workflows/orchestrate_poll.yml:54-59` enforces a single repo-level poller concurrency group. That is the right safety default, but it will serialize bursts of tracking work (**inference**).
- **Useful observables to track next:**
  - Count of `Integration fingerprint verification FAILED`
  - Count of `STALL_REANCHOR_FALLBACK`
  - Time from poll job start to first `Processing tracking issue`
  - Fraction of poll cycles with `0` Codex calls / `0` Semble queries but full tool bootstrap
  - Count of `PLAN_FAILURE_CONTEXT` lines in `plan`
- **Smallest safe mitigations:** fix the reissue regression first, then make tool setup lazy in poller paths so orchestration cycles spend time on issue work rather than bootstrap.

## Pipeline Flow Bottlenecks

1. **Validate / CI late-fail bottleneck**
   - `ci` is the clearest end-to-end bottleneck: failures happen after ~24-29 minutes of work, at the end of a long `lint` sequence.
   - Type: **compute wasted before failure**.

2. **Review / autofix AI-compute bottleneck**
   - `review_autofix` is the dominant model-compute stage by both cost and tail latency.
   - Type: **large-prompt AI compute + fan-out**.

3. **Stable-release watch bottleneck**
   - `test_and_mark_stable` is carrying a long-lived watcher for a child workflow it explicitly treats as non-blocking.
   - Type: **API polling + idle runner occupancy**.

4. **Poller bootstrap bottleneck**
   - `orchestrate_poll` spends a large share of its runtime on checkout/tool setup even when no AI tools are used.
   - Type: **fixed startup overhead**.

5. **Cross-workflow queueing**
   - Runner waits were visible in `ci`, `plan`, `implement`, `review_autofix`, `orchestrate_poll`, and many `test_and_mark_stable` jobs.
   - Type: **queueing overhead**. Since adding infrastructure is out of scope, the right fix is to reduce unnecessary concurrent work and long watcher jobs.

**Ordered by end-to-end impact:**  
`ci fast-fail` → `stable async non-blocking audit` → `review_autofix right-sizing` → `poller lazy tooling` → `watch-loop GH API backoff`.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` AI compute (`p95 3346.5s`, **354.3M** measured tokens)
  - `ci` late-failing orchestrate-poll regression (`16/20` failures, `p50 1671s`)
  - `test_and_mark_stable` non-blocking audit watcher extending runtime
  - `orchestrate_poll` fixed bootstrap overhead on short cycles

- **Top failure modes**
  - Repeated `Integration fingerprint verification FAILED` / `test_implementation_failed_reissue_preserves_dependency_gates_and_pending_defs`
  - One `workflow_log_analysis` `deep-audit` runner shutdown/cancel
  - One isolated `Install actionlint` failure in CI run **27121786890**

- **Highest-cost drivers**
  - Six-reviewer, two-pass, xhigh `review_autofix`
  - Oversized review prompts triggering `CONTEXT_BUDGET_WARN`
  - Review prompts with dynamic cross-pollination summaries and overflow context
  - Not Semble; verified Semble bytes are small relative to model spend

- **Top 3 prioritized actions**
  1. **Fix and front-load the orchestrate-poll regression suite** so CI and stable release fail fast.
  2. **Enable review risk-tiering + warning-triggered downsizing** in `review_autofix`.
  3. **Make non-blocking `workflow-log-analysis` dispatch asynchronous** in stable release and reduce watcher polling.

## Metrics Appendix

**Method note:** I read `workflow-log-output/summary.json` first for window sanity, then used `analysis/analysis_context.json` for broader aggregates (it expands coverage with `log_summary` rows), and used deep-dive logs under `errors/`, `slow/`, and `recent/` for run-specific evidence. I did **not** treat model-generated report text inside `workflow_log_analysis` logs as primary evidence.

### Repo summary

| Repo | Total runs | Success | Failure | Cancelled | Other | Failure rate | p50 s | p95 s | Codex tokens | Codex calls | OR tokens | OR calls | `cache_hit_rate` | `context_budget_warn_count` | `break_glass_count` | Telemetry runs | `wall_clock_p50_ms` | `wall_clock_p99_ms` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1,000 | 232 | 18 | 9 | 741 | 1.8% | 1.0 | 1695.6 | 356,921,169 | 211 | 250 | 2 | 23.5% | 42 | 0 | 123 | 2,000 | 6,960,170 |

### Workflow-family summary

| Family | Runs | S / F / C | p50 / p95 s | Telemetry runs | Codex tokens | Codex calls | Semble q / bytes / fb | Serena q / resp bytes / tool calls / fb | `cache_hit_rate` | `context_budget_warn_count` | `break_glass_count` | `wall_clock_p50_ms` / `wall_clock_p99_ms` |
|---|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---|
| `ci` | 20 | 4 / 16 / 0 | 1671.0 / 1722.1 | 15 | 0 | 0 | 0 / 0 / 75 | 0 / 0 / 0 / 0 | — | 0 | 0 | 1,660,000 / 1,739,920 |
| `review_autofix` | 87 | 78 / 0 / 7 | 494.0 / 3346.5 | 17 | 354,256,628 | 174 | 15 / 182,865 / 2 | 0 / 0 / 0 / 0 | — | 30 | 0 | 2,647,000 / 7,952,840 |
| `implement` | 190 | 10 / 0 / 2 | 1.0 / 228.0 | 17 | 2,652,382 | 26 | 0 / 0 / 0 | 0 / 0 / 0 / 0 | 23.5% | 0 | 0 | 1,000 / 432,080 |
| `plan` | 190 | 10 / 0 / 0 | 1.0 / 16.6 | 17 | 4,054 | 6 | 0 / 0 / 0 | 0 / 0 / 0 / 0 | — | 0 | 0 | 1,000 / 386,720 |
| `orchestrate_poll` | 34 | 34 / 0 / 0 | 177.5 / 259.1 | 5 | 0 | 0 | 0 / 0 / 0 | 0 / 0 / 0 / 0 | — | 0 | 0 | 182,000 / 218,760 |
| `workflow_log_analysis` | 1 | 0 / 1 / 0 | 3564.0 / 3564.0 | 1 | 8,105 | 5 | 2 / 31,470 / 39 | 2 / 0 / 0 / 0 | — | 12 | 0 | 3,564,000 / 3,564,000 |
| `test_and_mark_stable` | 1 | 0 / 1 / 0 | 3660.0 / 3660.0 | 1 | 0 | 0 | 0 / 0 / 0 | 0 / 0 / 0 / 0 | — | 0 | 0 | 3,660,000 / 3,660,000 |

### Notable runs

| Run ID | Family | Conclusion | Duration s | Key evidence |
|---|---|---|---:|---|
| 27112656269 | `review_autofix` | success | 7,989 | 98,861,184 tokens, 48 Codex calls, 6 context warnings, 2 Semble queries |
| 27110905305 | `review_autofix` | success | 3,940 | 57,673,076 tokens, 30 Codex calls, warnings at 216,970 and 220,663 prompt tokens, Semble reviewer-context 15,735 bytes |
| 27116473444 | `review_autofix` | success | 3,134 | 49,430,592 tokens, 24 Codex calls, warnings at 221,873 and 223,657 prompt tokens |
| 27131537810 | `ci` | failure | 1,660 | Repeated integration fingerprint failures late in lint; 284 passed, 1 failed, 285 total; 5 fixture Semble fallbacks |
| 27112686470 | `test_and_mark_stable` | failure | 3,660 | `validate-scripts / Unit tests` failed on `test_implementation_failed_reissue_preserves_dependency_gates_and_pending_defs`; non-blocking audit watcher still ran to completion |
| 27112704280 | `workflow_log_analysis` | failure | 3,564 | `deep-audit` ended with runner shutdown / canceled; `api-redundancy` skipped |
| 27135212721 | `orchestrate_poll` | success | 147 | 0 Codex calls, 0 Semble queries, successful `poll_started`/`poll_completed` memory writes, one `STALL_REANCHOR_FALLBACK` |

### AI memory summary

| Metric | Value |
|---|---:|
| Unique deep-dive memory telemetry events | 35 |
| `record-run-event` | 20 |
| `retrieve` | 8 |
| `record-candidate` | 7 |
| Retrieve hit rate | 0% |
| Avg `estimated_tokens` on retrieve | 0 |
| `keyword_method=llm` | 8 |
| `keyword_method=plain` | 0 |
| `keyword_method=none` | 0 |
| `fail_open:true` seen | 0 |
| `enabled:false` seen | 0 |
| Push retry outliers | 1 (`27103488076`, `push_attempts=3`) |

### GH API audit summary

| Area | Evidence | Risk today | Suggested reduction |
|---|---|---|---|
| Stable-release watcher loops | `test-and-mark-stable.yml:3523-3543`, `3699-4233`; run 27112686470 watched a non-blocking child for ~60 min | Shared GH API/runners consumed by polling | Async exit for non-blocking watches; exponential backoff for blocking ones |
| Push PR resolution | `internal-review.yml:101-116` | Low per run, broad by volume | Skip repo default-branch GET when event data already has it |
| Review fallback label lookups | `review_autofix.yml:778-805` | Moderate on multi-issue PRs | Batch fallback issue-label lookups with GraphQL |
| Orchestrator poll process | `CLAUDE.md:419-446`, `orchestrate_poll_process.sh` batching helpers/caches | Good hygiene already present | Preserve existing GraphQL/cache pattern; do not add inner-loop REST calls |

**Rate-limit note:** no explicit 429 / secondary-rate-limit / retry-storm evidence was surfaced in the sampled logs.

### MCP telemetry summary

| System | Scope | Collector aggregate | Strict non-analysis deep-dive verification | Notes |
|---|---|---:|---:|---|
| Semble queries | Repo | 17 queries / 214,335 bytes | 11 queries / 135,179 bytes | Verified queries were all in `review_autofix`; mostly `target=reviewer-context` plus some `overflow` |
| Semble fallbacks | Repo | 116 | 60 unique verified events | Verified non-analysis fallbacks were all CI fixture `target=overflow` fallbacks using `missing_semble`; `workflow_log_analysis` also reports fallbacks but its log text is contaminated |
| Serena queries | Repo | 2 / 0 response bytes / 0 tool calls | 0 verified non-analysis runtime lines | No evidence of meaningful substitution value yet |
| Serena fallbacks | Repo | 0 | 0 | None observed |
| Serena probes | Repo | ok=0 / failed=0 / skipped=0 | 0 | No probe telemetry emitted |

### MCP availability / probe rows

| MCP server | Target | `probe_ok` | `probe_failed` | `probe_skipped` | Notes |
|---|---|---:|---:|---:|---|
| Serena | all observed targets | 0 | 0 | 0 | No probe telemetry surfaced in this window |

### Other MCP servers observed

| Prefix | Run ID | Workflow family | Details |
|---|---:|---|---|
| `STALL_REANCHOR_FALLBACK` | 27135212721 | `orchestrate_poll` | `issue=3232 source=branch_name resolved=2026-06-08T11:40:28Z` |


## Deep Audit — Workflows & Scripts (2026-06-08)

### Section 1: Bug & Correctness Sweep

- **ID** — `SEC-001`
  - **File path** — `scripts/run_validation_repo_checks.sh:21-29`
  - **Severity** — Medium
  - **Category tag** — `security`
  - **Description** — When positional arguments are supplied, the script replaces the built-in `CHECK_COMMANDS` list with `("$@")` and then executes each entry through `timeout ... /bin/sh -c "${check_cmd}"`. That reintroduces shell parsing for every override string, so metacharacters change what actually runs instead of preserving argv boundaries. `README.md:1691,1711-1716` documents this script as the seeded repo-check entrypoint for validation harnesses, so this is on a workflow-facing path.
  - **Recommended fix** — Stop treating overrides as shell text. Either: (1) accept named check IDs that map to the built-in commands, or (2) accept one argv vector and execute it directly with `timeout "${CHECK_TIMEOUT_SECS}" "$@"`. If a string interface must remain, reject shell interpreters/metacharacters before dispatch, following the dangerous-command screening pattern already used in `scripts/review_synthesise_smoke.sh:252-270`.

- **ID** — `BUG-001`
  - **File path** — `.github/workflows/review_autofix.yml:785-827,4634-4654,4817-4837,5871-5890; scripts/review_rb_judge.sh:727-736,1844-1858; .github/workflows/issue_pr_status.yml:272-287`
  - **Severity** — High
  - **Category tag** — `bug`
  - **Description** — `issue_pr_status.yml` explicitly tightened fallback parsing so only closing-keyword links or repo-scoped issue URLs/paths are actionable, and it documents that bare prose mentions like `issue #N` / `issues/N` caused incorrect automated transitions (`issue_pr_status.yml:272-287`). `review_autofix.yml` and `review_rb_judge.sh` still use the older broad regex that matches `issues/123` and `issue #123`. In `review_autofix.yml`, those matches drive validate dispatch + `ai:orchestrator-validate-required` removal (`785-827`), `ai:ready-to-merge` labeling (`4634-4654`), and `ai:review-blocked` labeling (`4817-4837`, `5871-5890`). That leaves the review path able to mutate unrelated issues even after `issue_pr_status` was hardened against the same failure mode.
  - **Recommended fix** — Extract one shared fallback parser and reuse it everywhere. The best starting point is the stricter body-text fallback already in `scripts/review_collect_pr_metadata.sh:159-194`; generalize it into a helper that returns deduplicated issue numbers using the `issue_pr_status` semantics, write that once into `LINKED_ISSUES_JSON`, and delete the four inline regex copies in `review_autofix.yml` plus the copy in `review_rb_judge.sh`.

- **ID** — `BUG-002`
  - **File path** — `scripts/review_rb_judge.sh:740-764,896-916,2124-2133,2374-2391`
  - **Severity** — Medium
  - **Category tag** — `bug`
  - **Description** — `review_rb_judge.sh` records `FIRST_ISSUE` and `FIRST_ISSUE_LABELS_JSON` from the first linked issue it sees, captures the first non-empty body it encounters, and then breaks (`740-764`). That same issue/body is later used as the judge’s “original requirement” (`896-916`) and as the source of `ai:orchestrator-managed` propagation for both merge-with-followup and close-and-reissue flows (`2124-2133`, `2374-2391`). On PRs linked to multiple issues, the effective parent is therefore “first one returned” rather than an explicit canonical parent. [NEEDS VERIFICATION]
  - **Recommended fix** — Resolve the canonical parent during the early metadata pass instead of inside `review_rb_judge.sh`. Extend `scripts/review_collect_pr_metadata.sh:124-152` to emit `PRIMARY_LINKED_ISSUE_NUMBER`, `PRIMARY_LINKED_ISSUE_BODY`, and `PRIMARY_LINKED_ISSUE_LABELS_JSON` using deterministic precedence (for example: exact closing-keyword issue first, else sole linked issue, else issue already carrying orchestrator-managed/tracking metadata), and consume those values in `review_rb_judge.sh`.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`
  - **File path** — `scripts/gh_helpers.sh:916-932`
  - **Severity** — Medium
  - **Category tag** — `api-batching`
  - **Description** — `_gh_issue_timeline_with_cross_refs_rest()` fetches the issue timeline once, extracts unique same-repo PR URLs, then calls `gh_retry gh api "${pr_url}"` inside a `while read` loop for every PR URL. **Current call count:** `1 + N` per issue (`1` timeline call + `N` PR REST calls). **Proposed call count:** `2` total for same-repo PRs, or `1 + ceil(N/25)` if aliased GraphQL batching is chunked. This is a per-iteration API pattern in a shared helper, so every caller that falls back to REST inherits the fan-out.
  - **Recommended fix** — Extend the existing alias-batching pattern from `scripts/orchestrate_poll_process.sh:10585-10728` (`_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql`) into `scripts/gh_helpers.sh`, batch PR numbers in one GraphQL request, and keep the current REST path only as fail-open fallback.

- **ID** — `API-002`
  - **File path** — `scripts/review_collect_pr_metadata.sh:124-136,161-191`
  - **Severity** — Medium
  - **Category tag** — `api-batching`
  - **Description** — The script first queries `closingIssuesReferences` once via GraphQL (`124-136`). When that returns empty, the fallback path parses PR body text and then loops over each matched issue number, calling `gh_retry ... api "repos/${REPOSITORY}/issues/${_fb_num}"` one by one (`161-191`). `_FALLBACK_MAX_ISSUES=20` caps worst-case fan-out, but **current call count** on the fallback path is still `1 + N` (`1` GraphQL miss + up to `20` issue GETs). **Proposed call count:** `2` (`1` GraphQL miss + `1` batched issue-details fetch).
  - **Recommended fix** — Add a batched issue-summary helper to `scripts/gh_helpers.sh` (same alias style as `_fetch_candidate_issue_details_graphql`) that returns `{number,title,body}` for a list of issue numbers. Then replace the per-issue loop in `review_collect_pr_metadata.sh` with one batch call and build the linked-issue context from that response.

- **ID** — `API-003`
  - **File path** — `scripts/check_external_branch_advance.sh:175-185`
  - **Severity** — Low
  - **Category tag** — `api-batching`
  - **Description** — The identity-verification path performs one `gh_retry gh api "repos/${REPOSITORY}/commits/${sha}"` call per SHA in `self_subject_shas`. The inline comment says the set is “usually tiny,” but **current call count** is still `N` commit API calls for `N` SHAs. **Proposed call count:** `1` aliased GraphQL batch for the whole SHA set. [NEEDS VERIFICATION]
  - **Recommended fix** — If this path starts seeing larger advance sets, add a batched commit-attribution helper in `scripts/gh_helpers.sh` using the same alias pattern already used in `scripts/orchestrate_poll_process.sh`, and have `check_external_branch_advance.sh` submit the full SHA list at once.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`
  - **File path** — `.github/workflows/comprehensive-test-and-release.yml:72-97,318-343`
  - **Severity** — Low
  - **Category tag** — `duplication`
  - **Description** — `gh_api_safe()` is defined twice in the same workflow with near-identical bodies: same temp-file capture, same “rate limit” grep, same 30→60→120 backoff logic, and same error reporting. This is literal duplication inside one file.
  - **Recommended fix** — Remove the local copies and use one shared implementation. The simplest path is to source `scripts/gh_helpers.sh` and call `gh_retry gh api ...` directly; if a compatibility wrapper is desired, put `gh_api_safe() { gh_retry gh api "$@"; }` in a shared helper and have both `list_dispatch_runs()` call sites use it.

- **ID** — `DUP-002`
  - **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/mark-stable.yml:401-427,550-576; .github/workflows/orchestrate_poll.yml:84-118; .github/workflows/test-and-mark-stable.yml:475-489,1281-1303,4845-4871`
  - **Severity** — Medium
  - **Category tag** — `duplication`
  - **Description** — Rate-limit-aware GH wrappers are reimplemented across at least seven workflow blocks. They are not fully identical: some return empty strings on failure (`test-and-mark-stable`), some hard-fail, and `orchestrate_poll` also trips a circuit-breaker file. That means the same GitHub API behavior is being maintained in multiple copies with already-divergent semantics.
  - **Recommended fix** — Centralize this into one module. `scripts/gh_helpers.sh` already owns `gh_retry()` and `_safe_gh_jq()` (`391-446`, `532-617`); either source that where checkout is already present, or extract a tiny bootstrap companion that exposes `gh_retry "$@"`, `_safe_gh_jq <endpoint> [--jq ...]`, and shared rate-limit waiting. Update the listed callers to source the shared helper instead of keeping bespoke wrappers.

- **ID** — `DUP-003`
  - **File path** — `.github/workflows/clarify.yml:216-314; .github/workflows/plan.yml:267-364; .github/workflows/implement.yml:797-1043; .github/workflows/orchestrate.yml:341-453; .github/workflows/orchestrate_poll.yml:295-431; .github/workflows/orchestrate_clarify_respond.yml:260-376; .github/workflows/review_autofix.yml:1257-1270; scripts/stage_workflow_support.sh:4-220`
  - **Severity** — Medium
  - **Category tag** — `duplication`
  - **Description** — Six workflows still inline large “Stage workflow support files” blocks that all perform overlapping work: script staging, branch→main fallback, overlay loading, model-catalog staging, schema staging, and consumer `.gitignore` generation. `review_autofix.yml` has already externalized this concern into `scripts/stage_workflow_support.sh`, so the remaining inline copies are now a clear drift hazard and a major source of YAML bulk.
  - **Recommended fix** — Generalize the existing `stage_review_runtime_support()` implementation in `scripts/stage_workflow_support.sh` into a mode-driven shared entrypoint (for example, `stage_workflow_runtime_support <mode>` or the same env-driven function with per-mode lists such as `REQUIRED_BOOTSTRAP_SCRIPTS`, `OPTIONAL_BOOTSTRAP_SCRIPTS`, and `PROMPT_FILES`). Then update `clarify.yml`, `plan.yml`, `implement.yml`, `orchestrate.yml`, `orchestrate_poll.yml`, and `orchestrate_clarify_respond.yml` to call it the way `review_autofix.yml` already does.

### Section 4: Expression Size Limit Risk Assessment

Static counts below are body-size estimates for interpolated `run:` blocks; runner-side serialization can differ slightly.

- **ID** — `EXPR-001`
  - **File path** — `.github/workflows/implement.yml:3456-3832`
  - **Severity** — High
  - **Category tag** — `expression-limit`
  - **Description** — The `Commit changes` step’s interpolated `run:` body is approximately **21,133 characters**, leaving roughly **-133 characters** of headroom versus GitHub’s **21,000-character** expression ceiling. This is already at or just beyond the hard limit on a raw-body basis. [NEEDS VERIFICATION]
  - **Recommended fix** — Extract the entire step into an external script such as `scripts/implement_commit_changes.sh` and leave only env wiring / output capture in YAML. This repo already uses that pattern successfully in helpers like `scripts/review_collect_pr_metadata.sh` and `scripts/stage_workflow_support.sh`.

- **ID** — `EXPR-002`
  - **File path** — `.github/workflows/review_autofix.yml:1697-2043`
  - **Severity** — Medium
  - **Category tag** — `expression-limit`
  - **Description** — The `Collect PR check-run failures (CI/lint autofix context)` step is approximately **17,876 characters**, leaving only about **3,124 characters** of headroom. The combination of a long shell polling loop plus an embedded Python heredoc makes this block likely to cross the threshold with routine maintenance.
  - **Recommended fix** — Move the embedded writer/log-tail logic into `scripts/review_collect_check_run_failures.py` (or a paired shell wrapper) and keep the workflow step as a short launcher that passes `HEAD_SHA`, `SELF_RUN_ID`, and output paths.

- **ID** — `EXPR-003`
  - **File path** — `.github/workflows/review_autofix.yml:4971-5185`
  - **Severity** — Medium
  - **Category tag** — `expression-limit`
  - **Description** — The `Enable auto-merge on PR` step is approximately **15,878 characters**, leaving about **5,122 characters** of headroom. It packs e2e-label suppression, forward-merge special handling, orchestrator integration suppression, PR metadata fetches, and final merge enablement into one interpolated shell body.
  - **Recommended fix** — Extract this flow to `scripts/review_enable_automerge.sh`, or split it into smaller guarded steps (`fetch PR metadata`, `e2e guard`, `integration guard`, `enable merge`) so no single interpolated `run:` block carries all branches.

- **ID** — `EXPR-004`
  - **File path** — `.github/workflows/orchestrate_clarify_respond.yml:889-1172`
  - **Severity** — Medium
  - **Category tag** — `expression-limit`
  - **Description** — The `Parse and post answer` step is approximately **15,141 characters**, leaving about **5,859 characters** of headroom. The block combines memory claim handling, loop-guard state, escalation comment generation, Telegram notification, and processed-command completion in one interpolated step.
  - **Recommended fix** — Move the body into a script such as `scripts/orchestrate_clarify_post_answer.sh`, or split the current logic into separate steps for memory claim, loop guard, escalation, and final answer posting.

No workflow exceeded the **800 KB** warning threshold. Largest files in this repo are `.github/workflows/review_autofix.yml` (**370,941** bytes), `.github/workflows/test-and-mark-stable.yml` (**295,404** bytes), and `.github/workflows/implement.yml` (**287,349** bytes).

### Section 5: Cross-Cutting Concerns

No `TODO` / `FIXME` / `HACK` / `XXX` markers were present under `.github/workflows/` or `scripts/` in this audit pass.

- **ID** — `DEAD-001`
  - **File path** — `scripts/review_run_reviewers.sh:254-258,337-343,2443-2457`
  - **Severity** — Low
  - **Category tag** — `dead-code`
  - **Description** — Several variables are assigned but never consumed: `probe_prompt` is declared and never read, `RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE` and `RAW_REVIEWER_SYMBOL_DIFF_SUMMARY_FILE` are assigned once and never referenced again, and `REVIEWER_HEALTH_LAST_OPEN_UNTIL_EPOCH` is initialized and populated from parser output but never used after line 2457. In a very large reviewer script, these dead assignments make it harder to tell which artifacts and health fields actually affect runtime behavior.
  - **Recommended fix** — Remove the unused variables, or wire them into existing outputs if they were intended to matter. If `open_until_epoch` is supposed to be observable, emit it through the existing reviewer health logging path instead of parsing and discarding it.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, EXPR-001 |
| Medium | 9 | SEC-001, BUG-002, API-001, API-002, DUP-002, DUP-003, EXPR-002, EXPR-003, EXPR-004 |
| Low | 3 | API-003, DUP-001, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3-4 | Medium |
| API call optimization | 3-5 | Medium |
| Code modularization | 8-10 | Large |
| Expression size reduction | 4-6 | Large |
| Medium/Low fixes | 2-4 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-06-08)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlapping calls are in the same local code path with matching scope/filters/error semantics and can be consolidated directly. `NEEDS_VERIFICATION` means overlap is likely but a human must verify runtime contracts or step/job interactions first. `RISKY_SKIP` means the call sits in polling/retry/race-defense/rate-limit-sensitive logic, so it should not be auto-consolidated even if it looks redundant.

### Consolidation Candidates (MERGE-###)

- **ID** — `MERGE-001`
  - **Safety tag** — `SAFE_TO_MERGE`
  - **File path and line ranges** — `.github/workflows/review_autofix.yml:769-830`, `.github/workflows/review_autofix.yml:4500-4545`, `.github/workflows/review_autofix.yml:4632-4641`, `.github/workflows/review_autofix.yml:4815-4824`, `.github/workflows/review_autofix.yml:5869-5878`, `scripts/review_collect_pr_metadata.sh:124-156`
  - **Current call count** — 2 GraphQL calls in the common PR path (`scripts/review_collect_pr_metadata.sh` early fetch + `post-merge-validate-dispatch` fetch); 3 in the rare cache-miss path because `Cache linked issues references` can re-fetch too.
  - **Proposed call count** — 1 GraphQL call in the common PR path; 2 only when the late cache variable is genuinely absent.
  - **Endpoint(s)** — GraphQL `repository.pullRequest(number).closingIssuesReferences`
  - **Evidence**
    ```sh
    # early metadata fetch already writes LINKED_ISSUES_JSON
    scripts/review_collect_pr_metadata.sh:131-151
    if gh_retry "${_linked_tmp}" api graphql ... \
      -f query='... pullRequest(number:$number){closingIssuesReferences(first:50){nodes{number title body}}} ...'
    ...
    printf 'LINKED_ISSUES_JSON=%s\n' "${_linked_numbers}" >> "${GITHUB_ENV}"
    ```
    ```sh
    # later step explicitly says to reuse that cache
    .github/workflows/review_autofix.yml:4511-4514
    # Reuse LINKED_ISSUES_JSON from the early "Collect PR metadata" step
    # which already fetched closingIssuesReferences via GraphQL (with
    # title+body).
    ```
    ```sh
    # but post-merge dispatch still re-fetches the same linked issues
    .github/workflows/review_autofix.yml:778-783
    issue_nodes_json="$(gh api graphql \
      ... pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number labels(first: 100) { nodes { name } } } } } ...
    ```
  - **Proposed fix** — Extend `scripts/review_collect_pr_metadata.sh` to export a second cache variable/file carrying `[{number,labels}]` for `closingIssuesReferences`, then update `.github/workflows/review_autofix.yml` `post-merge-validate-dispatch` to consume that cache before issuing its own GraphQL fetch.
  - **Safety rationale** — Same workflow invocation, same PR number, no intervening mutation before the dispatch step that could change `closingIssuesReferences`; extending the existing early fetch preserves error behavior because the late step already treats missing data fail-open.
  - **Downstream signal** — Add labels to the early `review_collect_pr_metadata.sh` GraphQL payload and have `post-merge-validate-dispatch` read the cached result instead of re-querying `closingIssuesReferences`.

- **ID** — `MERGE-002`
  - **Safety tag** — `SAFE_TO_MERGE`
  - **File path and line ranges** — `.github/workflows/review_autofix.yml:4632-4641`, `.github/workflows/review_autofix.yml:4815-4824`, `.github/workflows/review_autofix.yml:5869-5878`, `.github/workflows/review_autofix.yml:1630-1635`, `scripts/review_collect_pr_metadata.sh:104-121`
  - **Current call count** — up to 4 `/repos/${REPOSITORY}/pulls/${PR_NUMBER}` reads across one run: initial metadata fetch plus up to 3 fallback re-reads.
  - **Proposed call count** — 1 in these paths.
  - **Endpoint(s)** — REST `GET /repos/{owner}/{repo}/pulls/{pull_number}`
  - **Evidence**
    ```sh
    # early metadata step already writes PR_META_FILE from PR_PAYLOAD_FILE
    scripts/review_collect_pr_metadata.sh:104-121
    gh_retry "${PR_PAYLOAD_FILE}" api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"
    ...
    jq '{ title: (.title // ""), body: (.body // ""), ... }' "${PR_PAYLOAD_FILE}" > "${PR_META_FILE}"
    ```
    ```sh
    # late steps already read PR_META_FILE first
    .github/workflows/review_autofix.yml:4636-4638
    PR_DATA="$(jq -r '[.title // "", .body // ""] | join(" ")' "${PR_META_FILE}" 2>/dev/null || echo "")"
    if [ -z "$(printf '%s' "${PR_DATA}" | tr -d '[:space:]')" ]; then
      PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' ...)"
    fi
    ```
    Same fallback pattern repeats at `4819-4821` and `5873-5875`.
  - **Proposed fix** — Remove the three fallback `/pulls/${PR_NUMBER}` reads and trust `PR_META_FILE`/`PR_PAYLOAD_FILE` from `Collect PR metadata`; if the file is missing or invalid, fail open exactly as those steps already do when `PR_DATA` stays empty.
  - **Safety rationale** — Same run, same PR, same data shape, and the early metadata step is a hard prerequisite of the consuming job path; no separate retry/backoff contract is being crossed.
  - **Downstream signal** — Stop re-fetching PR title/body in the three late fallback blocks and use the already-populated `PR_META_FILE` / `PR_PAYLOAD_FILE` only.

### Redundant Re-Fetch (REUSE-###)

- **ID** — `REUSE-001`
  - **Safety tag** — `SAFE_TO_MERGE`
  - **File path and line ranges** — `.github/workflows/issue_pr_status.yml:181-186`, `.github/workflows/issue_pr_status.yml:272-287`
  - **Current call count** — 1 fallback PR refetch when GraphQL returns no linked issues and `${PR_TITLE} ${PR_BODY}` is blank/whitespace.
  - **Proposed call count** — 0 fallback refetches in that path.
  - **Endpoint(s)** — REST `GET /repos/{owner}/{repo}/pulls/{pull_number}`
  - **Evidence**
    ```yaml
    .github/workflows/issue_pr_status.yml:181-186
    PR_NUMBER: ${{ github.event.pull_request.number }}
    PR_HEAD_REF: ${{ github.event.pull_request.head.ref }}
    PR_TITLE: ${{ github.event.pull_request.title }}
    PR_BODY: ${{ github.event.pull_request.body || '' }}
    ```
    ```sh
    .github/workflows/issue_pr_status.yml:282-285
    PR_DATA="${PR_TITLE:-} ${PR_BODY:-}"
    if [ -z "$(printf '%s' "${PR_DATA}" | tr -d '[:space:]')" ]; then
      PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' ...)"
    fi
    ```
  - **Proposed fix** — Use the event payload as authoritative in this `pull_request.closed` workflow and drop the fallback `/pulls/${PR_NUMBER}` fetch in the linked-issue text parser.
  - **Safety rationale** — The workflow is triggered directly from `pull_request` and already receives title/body in event context; removing the fallback does not change filters, auth, or retry semantics for any remaining API call.
  - **Downstream signal** — Delete the fallback `/pulls/${PR_NUMBER}` read in `issue_pr_status.yml` and parse linked-issue text from `PR_TITLE`/`PR_BODY` only.

- **ID** — `REUSE-002`
  - **Safety tag** — `SAFE_TO_MERGE`
  - **File path and line ranges** — `.github/workflows/internal-review.yml:89-91`, `.github/workflows/internal-review.yml:114-117`
  - **Current call count** — up to 2 calls on push: open-PR lookup plus repo default-branch lookup.
  - **Proposed call count** — 1 call when `github.event.repository.default_branch` is present.
  - **Endpoint(s)** — REST `GET /repos/{owner}/{repo}/pulls?...`, REST `GET /repos/{owner}/{repo}`
  - **Evidence**
    ```yaml
    .github/workflows/internal-review.yml:89-91
    HEAD_SHA: ${{ github.sha }}
    EVENT_DEFAULT_BRANCH: ${{ github.event.repository.default_branch || '' }}
    ```
    ```sh
    .github/workflows/internal-review.yml:114-117
    base_ref="${EVENT_DEFAULT_BRANCH:-}"
    if [ -z "${base_ref}" ]; then
      base_ref="$(gh api "repos/${REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo 'main')"
    fi
    ```
  - **Proposed fix** — Keep the existing fallback, but make implementers treat the repo GET as unreachable on normal GitHub-hosted push events unless they can prove `github.event.repository.default_branch` can be absent in the wrapper’s real callers.
  - **Safety rationale** — The event payload already carries the target field and the fallback is already conditional, so the only safe change is to rely on existing event data where present.
  - **Downstream signal** — Confirm `github.event.repository.default_branch` is always populated for the push events that hit `internal-review.yml`; if yes, remove the repo default-branch GET fallback.

- **ID** — `REUSE-003`
  - **Safety tag** — `NEEDS_VERIFICATION`
  - **File path and line ranges** — `.github/workflows/review_autofix.yml:215-222`, `.github/workflows/review_autofix.yml:2068-2104`
  - **Current call count** — 1 linked-issue title fetch on smoke-detection fallback.
  - **Proposed call count** — 0 if the caller contract guarantees smoke PRs always carry `[E2E Smoke Test]` in `inputs.pr_title`/`inputs.pr_body`.
  - **Endpoint(s)** — REST `GET /repos/{owner}/{repo}/issues/{issue_number}`
  - **Evidence**
    ```yaml
    .github/workflows/review_autofix.yml:215-222
    PR_TITLE: ${{ inputs.pr_title }}
    PR_BODY: ${{ inputs.pr_body }}
    ```
    ```sh
    .github/workflows/review_autofix.yml:2079-2100
    if echo "${PR_TITLE}" | grep -qi '\[E2E Smoke Test\]' \
       || echo "${PR_BODY}" | grep -qi '\[E2E Smoke Test\]'; then
      IS_SMOKE=true
    fi
    ...
    ISSUE_TITLE=$(_safe_gh_jq "repos/${{ github.repository }}/issues/${ISSUE_NUM}" --jq '.title // ""' || echo "")
    ```
  - **Proposed fix** — Verify whether all workflow callers pass through PR title/body reliably for every path that can hit `Detect smoke test and tune LLM settings`; if they do, remove the linked-issue-title API fallback and rely on PR metadata only.
  - **Safety rationale** — Static reading shows likely redundancy, but `workflow_call` and `workflow_dispatch` callers may omit or sanitize `pr_title`/`pr_body`, so the equivalence is not fully proven.
  - **Downstream signal** — Verify every caller of `review_autofix.yml` always supplies accurate `pr_title`/`pr_body`; only then remove the `issues/${ISSUE_NUM}` title lookup.

- **ID** — `REUSE-004`
  - **Safety tag** — `SAFE_TO_MERGE`
  - **File path and line ranges** — `.github/workflows/implement.yml:413-430`, `.github/workflows/implement.yml:1241-1257`, `.github/workflows/implement.yml:1392-1398`, `.github/workflows/implement.yml:5165-5173`
  - **Current call count** — 1 issue fetch in `Resolve checkout ref`, plus 0-2 later fallback issue-label re-fetches when the cache file exists but `jq` parsing fails.
  - **Proposed call count** — keep the initial fetch; reduce later fallbacks to 0 in the normal path.
  - **Endpoint(s)** — REST `GET /repos/{owner}/{repo}/issues/{issue_number}`
  - **Evidence**
    ```sh
    .github/workflows/implement.yml:415-430
    issue_meta_json="$(gh_api_with_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
    ...
    printf '%s\n' "${issue_meta_json}" > "${ISSUE_META_FILE}"
    ```
    ```sh
    .github/workflows/implement.yml:1250-1252
    if [ ! -s "${ISSUE_META_FILE}" ]; then
      gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"
    fi
    ```
    ```sh
    .github/workflows/implement.yml:1392-1398
    if [ -s "${ISSUE_META_FILE:-}" ]; then
      ISSUE_LABELS_JSON="$(jq -c '[.labels[].name]' "${ISSUE_META_FILE}" ... || true)"
    fi
    if [ -z "${ISSUE_LABELS_JSON}" ]; then
      ISSUE_LABELS_JSON="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '[.labels[].name]')"
    fi
    ```
  - **Proposed fix** — Treat `Resolve checkout ref`’s `ISSUE_META_FILE` as the single source of truth in the successful path and keep re-fetch only on true cache miss, not for later non-critical consumers that can fail open.
  - **Safety rationale** — The file is written before later consumers and already contains labels/body/title/state; later re-fetches are only defensive fallbacks, so common-path consolidation is safe.
  - **Downstream signal** — Reuse `ISSUE_META_FILE` everywhere after `Resolve checkout ref`; only preserve fresh issue GETs for genuine cache-miss branches.

- **ID** — `REUSE-005`
  - **Safety tag** — `NEEDS_VERIFICATION`
  - **File path and line ranges** — `.github/workflows/orchestrate_clarify_respond.yml:68-83`, `.github/workflows/orchestrate_clarify_respond.yml:437-449`
  - **Current call count** — 3 issue reads in the common orchestrator path: child issue once in `Check orchestrator metadata`, tracking issue title once for smoke suppression, child issue again in `Fetch issue and tracking context`, and possibly tracking issue body again.
  - **Proposed call count** — 2 if the first step persists the child payload and tracking title/body for reuse.
  - **Endpoint(s)** — REST `GET /repos/{owner}/{repo}/issues/{issue_number}`
  - **Evidence**
    ```sh
    .github/workflows/orchestrate_clarify_respond.yml:68-70
    ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
    ISSUE_BODY="..."
    ISSUE_TITLE="..."
    ```
    ```sh
    .github/workflows/orchestrate_clarify_respond.yml:437-448
    ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
    ...
    TRACKING_BODY="$(gh_retry gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.body // ""')"
    ```
  - **Proposed fix** — Persist `ISSUE_PAYLOAD` from the first step into a runtime file/env and reuse it in `Fetch issue and tracking context`; optionally fetch tracking title/body together once if both are needed.
  - **Safety rationale** — Likely same-scope duplication, but these are separate workflow steps and the first step also controls smoke-alert suppression; verify no intervening mutation depends on a refreshed issue body before consolidating.
  - **Downstream signal** — Verify no step between `Check orchestrator metadata` and `Fetch issue and tracking context` can mutate the child/tracking issue; if not, cache and reuse the first issue payload.

### Dead Calls (DEAD-API-###)

- **ID** — `DEAD-API-001`
  - **Safety tag** — `SAFE_TO_MERGE`
  - **File path and line ranges** — `scripts/review_collect_pr_metadata.sh:110-111`, `scripts/review_collect_pr_metadata.sh:240-347`, `.github/workflows/review_autofix.yml:4667-4695`
  - **Current call count** — 1 paginated `GET /repos/{repo}/pulls/{pr}/reviews` per review run.
  - **Proposed call count** — 0 for runs where review-blocked break-glass scanning is disabled and no downstream consumer needs top-level review objects; otherwise unchanged.
  - **Endpoint(s)** — REST `GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews`
  - **Evidence**
    ```sh
    scripts/review_collect_pr_metadata.sh:110-111
    gh_retry "${reviews_raw}" api --paginate "repos/${REPOSITORY}/pulls/${PR_NUMBER}/reviews"
    jq -s 'add // []' "${reviews_raw}" > "${PR_REVIEWS_FILE}"
    ```
    ```py
    scripts/review_collect_pr_metadata.sh:294-307
    for review in reviews:
        entries.append({ "kind": "review", ... "state": ..., "body": ... })
    ```
    ```sh
    .github/workflows/review_autofix.yml:4667-4669
    if [ ! -s "${PR_ISSUE_COMMENTS_FILE:-}" ] && [ ! -s "${PR_REVIEWS_FILE:-}" ]; then
      echo "Review-blocked break-glass scan inputs missing; continuing with override disabled."
    ```
    Static grep in this repo shows `PR_REVIEWS_FILE` is otherwise only used to build `PR_ALL_COMMENTS_CONTEXT_FILE` and in this optional break-glass scanner.
  - **Proposed fix** — Gate the `/pulls/${PR_NUMBER}/reviews` fetch behind the feature that consumes it (`REVIEW_BREAK_GLASS_ENABLED`) or extend `review_collect_pr_metadata.sh` with an opt-in flag so `PR_REVIEWS_FILE` is skipped on ordinary runs.
  - **Safety rationale** — The fetched reviews are not on the mandatory reviewer/editor path in this repo; they are only used for an optional advisory/break-glass context, so gating the fetch behind that feature preserves common-path behavior.
  - **Downstream signal** — Add a flag to `review_collect_pr_metadata.sh` to skip `PR_REVIEWS_FILE` population unless break-glass/comment-context consumers are enabled, and verify no external caller depends on that file unconditionally.

### Cross-References to Deep Audit Section

- `API-001`: `NEEDS_VERIFICATION` — batching is directionally correct, but it sits on a REST fallback helper and must preserve the existing fail-open parity path.
- `API-002`: `NEEDS_VERIFICATION` — agree with batched fallback issue fetch; verify the helper returns exactly the body/title shape the prompt builder expects.
- `API-003`: `RISKY_SKIP` — commit attribution lookup is inside a rare identity-verification path explicitly described as acceptable due to tiny cardinality, so it should not be auto-batched.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 6 | MERGE-001, MERGE-002, REUSE-001, REUSE-002, REUSE-004, DEAD-API-001 |
| NEEDS_VERIFICATION | 3 | REUSE-003, REUSE-005, API-001, API-002 |
| RISKY_SKIP | 1 | API-003 |

### Implement-Stage Handoff

- `MERGE-001`
- `MERGE-002`
- `REUSE-004`
- `REUSE-001`
- `REUSE-002`
- `DEAD-API-001`
