## Executive Summary

- **Fix the `implement.yml` checkout-ref log contract drift first.** Every `ci` run in this window failed (`29/29`, family failure rate `100%`, p95 `666.6s`). Sample run `25981394151` failed in `lint / Validation self-test unit tests` with `AssertionError: implement.yml missing resolved-ref log output`; `tests/test_workflow_checkout_integration_ref_audit.py:72,88` expects `Resolved ref:` while `.github/workflows/implement.yml:588` still prints `Resolved fallback ref:`. **Estimated impact:** eliminate about **5.1 runner-hours** of failed CI in this window; likely reduce downstream autofix churn (**inference**). **Confidence:** high.
- **`review_autofix` is the main speed and cost hotspot.** It accounts for about **57,808s / 60.1%** of observed runtime. In slow runs `25980458060` and `25980645146`, reviewer models consumed `1278.9s` / `1126.9s` and the editor consumed `696.3s` / `653.0s`; config still runs **6 reviewer models**, **two-pass review**, and **xhigh** reasoning by default. **Estimated impact:** **20–35% latency reduction** and **25–40% model-cost reduction** if the full path is reserved for large/high-risk diffs. **Confidence:** high.
- **The review consolidator is wasting long-tail time.** Runs `25980645146` and `25980458060` each spent **300s** in `stage=consolidator` with `output_bytes=0` and `failopen=1`, on inputs of `124,870` and `128,524` bytes. **Estimated impact:** save **up to 300s per affected run** and reduce masked degraded reviews. **Confidence:** high.
- **The release-test failure was a downstream cancellation, not a functional validation failure.** `test_and_mark_stable` run `25980238302` failed after **4053s** because dispatched `validation-refresh` run `25980248328` ended `conclusion=cancelled`. Current repo state appears to already address this via branch-scoped concurrency in `.github/workflows/validation-refresh.yml:36-48` and per-run smoke branches in `.github/workflows/test-and-mark-stable.yml:3545-3551`. **Estimated impact:** remove a **67.6-minute false-negative** release blocker once confirmed on live runs. **Confidence:** medium-high.
- **Observability is the biggest tuning blocker.** `OPENROUTER_PROMPT_CACHE_DISABLED=false` is present in sampled `review_autofix` runs (`25980645146`, `25980458060`), but no raw `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens` lines were emitted outside generated analysis logs. AI memory retrieval also hit **0/8** times. **Estimated impact:** no immediate runtime gain, but this is the prerequisite for confident cache/model tuning. **Confidence:** high.
- **`workflow_log_analysis` is a measurable but secondary cost center.** Run `25980248446` took **3985s**; its `Summarize unselected runs (gpt-5.4-mini)` step spent **185,780 tokens** to summarize **77** unselected runs out of **100** targeted. **Estimated impact:** **25–60% mini-token savings** and ~**1–2 minutes** if fanout is trimmed. **Confidence:** high.

## Speed Optimizations

1. **Critical path: fix the deterministic CI contract drift and fail faster on future recurrences.**
   - **Evidence:** `ci` family = `29` runs, `29` failures, avg `634.5s`, p95 `666.6s`. Run `25981394151` failed in `lint / Validation self-test unit tests` with `AssertionError: implement.yml missing resolved-ref log output`. The test expects `echo "Resolved ref: ..."` in `tests/test_workflow_checkout_integration_ref_audit.py:72,88`; current workflow still logs `Resolved fallback ref: ...` at `.github/workflows/implement.yml:588`.
   - **Root cause:** workflow/test contract drift.
   - **Exact change:** change `.github/workflows/implement.yml:588` to the canonical `Resolved ref:` string expected by the audit; optionally split that audit into its own earliest CI shard so the next drift fails in seconds, not ~10–11 minutes.
   - **Estimated time savings:** immediate fix removes ~`18,400s` of failed CI in this window; early-shard placement would save most of the current `628–678s` burned per recurrence.
   - **Implementation risk:** low.

2. **Critical path: stop paying for the full 6-model, 2-pass, xhigh review path on routine PRs.**
   - **Evidence:** `review_autofix` has `93` runs, p50 `489s`, p95 `1993s`, avg `621.6s`. Slow runs:
     - `25980458060`: reviewers `1278.9s` (58.3% of run), editor `696.3s` (31.7%).
     - `25980645146`: reviewers `1126.9s` (57.2%), editor `653.0s` (33.2%).
     - `25980129050`: reviewers `924.3s`, editor `562.0s`.
     Config: `.github/workflows/review_autofix.yml:96-102` defines 6 reviewer models; `:107` reviewer reasoning = `xhigh`; `:129` editor reasoning = `xhigh`; `:140` two-pass enabled. `scripts/review_run_reviewers.sh:1653-1693` shows pass 1 runs at `xhigh`, and pass-2 small/large defaults are effectively a no-op at `xhigh`.
   - **Root cause:** the expensive path is the default, not the exception.
   - **Exact change:** use the existing knobs instead of introducing new logic: lower `REVIEWER_PASS2_REASONING_SMALL`, optionally disable pass 2 on small diffs, and keep the full 6-model set only for large/high-risk diffs.
   - **Estimated time savings:** ~**20–35%** on `review_autofix` runs overall; roughly **2–3 minutes** on a median run and **6–12 minutes** on the 30+ minute outliers.
   - **Implementation risk:** medium; watch review quality on a canary subset.

3. **Critical path: downshift or gate the consolidator before it burns 300s and fail-opens.**
   - **Evidence:** `25980645146` logged `stage=consolidator ... input_bytes=124870 output_bytes=0 wall_secs=300 ... failopen=1`; `25980458060` logged the same pattern with `input_bytes=128524`. Current defaults are `.github/workflows/review_autofix.yml:752-756`: `REVIEW_CONSOLIDATOR_MODEL=openai/gpt-5.4`, `REVIEW_CONSOLIDATOR_REASONING=xhigh`, `REVIEW_CONSOLIDATOR_TIMEOUT_SECS=300`.
   - **Root cause:** expensive full-model synthesis on very large reviewer bundles, despite a downstream fail-open path.
   - **Exact change:** lower consolidator reasoning for routine bundles, or route small/medium bundles to a cheaper model; keep the existing fail-open parser/editor path.
   - **Estimated time savings:** up to **300s per affected run**; biggest gain is in the p95 tail.
   - **Implementation risk:** low-medium, because downstream already tolerates empty consolidator output.

4. **Micro-optimizations: make `review_autofix` housekeeping conditional.**
   - **Evidence:** non-LLM overhead is smaller than reviewer/editor time, but still measurable:
     - free disk: `99.0s` (`25980129050`), `53.8s` (`25980458060`), `32.5s` (`25980645146`);
     - check-run collection: `30.8s` (`25980458060`);
     - re-trigger dispatch step: `19.1s` (`25980645146`).
   - **Root cause:** unconditional cleanup/polling/dedup work on runs where the repo state may not need it.
   - **Exact change:** guard free-disk cleanup on actual low-disk thresholds; skip check-run waiting when there are no non-self in-flight checks; keep retrigger dedup but avoid extra sleeps when the successor run already exists.
   - **Estimated time savings:** **30–100s** on affected runs.
   - **Implementation risk:** low.

5. **Secondary: trim `workflow_log_analysis` fanout.**
   - **Evidence:** run `25980248446` took `3985s`; its main analysis step lasted `2292.7s`, and the unselected-run summarizer step lasted `169.1s` while summarizing `77` runs.
   - **Root cause:** analysis prompt volume is being widened aggressively.
   - **Exact change:** lower `WORKFLOW_LOG_SUMMARY_MAX_RUNS` from `100` to a smaller target (for example, 40–60), or prioritize only failed/slow/rare-family runs that lack deep-dive logs.
   - **Estimated time savings:** about **1–2 minutes** directly in the summarizer step, plus possible downstream Codex-prompt shrinkage (**inference**).
   - **Implementation risk:** low.

## Cost Optimizations

> Dollar estimates are directional because raw prompt/completion token totals were not emitted in this window.

1. **Make the `review_autofix` model stack conditional instead of default.**
   - **Evidence:** the workflow currently runs 6 reviewer models (`.github/workflows/review_autofix.yml:96-102`), two-pass review (`:140`), pass-1 `xhigh` review (`scripts/review_run_reviewers.sh:1653-1662`), pass-2 defaults that are effectively also `xhigh`, plus an `xhigh` editor (`.github/workflows/review_autofix.yml:129`). That is the dominant runtime family (`60.1%` of total observed runtime).
   - **Root cause:** overprovisioned reviewer breadth/reasoning on routine work.
   - **Exact change:** shrink the default reviewer set or reserve the full set for large/risky diffs; use the existing small/large diff gate to lower pass-2 reasoning on small diffs.
   - **Estimated savings:** roughly **25–40% of `review_autofix` model spend**.
   - **Quality risk:** medium; mitigate with a canary and compare accepted findings.

2. **Stop paying full-model cost for a consolidator that often produces no output.**
   - **Evidence:** two slow runs spent the full `300s` timeout on `openai/gpt-5.4` consolidator calls and then fail-opened with `output_bytes=0`.
   - **Root cause:** full-model synthesis is being used where the pipeline already has a graceful degraded path.
   - **Exact change:** route routine consolidations to a smaller model or lower reasoning; keep the current `gpt-5.4` path only for very large/high-conflict bundles.
   - **Estimated savings:** one expensive model call per affected run, plus reduced runner cost from the 300s tail.
   - **Quality risk:** low-medium.

3. **Trim analysis-side mini-model spend.**
   - **Evidence:** `workflow_log_analysis` run `25980248446` logged `AI_MEMORY_TELEMETRY` for `summarize_unselected_runs`: `targeted=100`, `summarized=77`, `tokens_used=185780`, `model=openai/gpt-5.4-mini`.
   - **Root cause:** wide coverage expansion without a tighter priority filter.
   - **Exact change:** lower `WORKFLOW_LOG_SUMMARY_MAX_RUNS`, and prioritize runs with no deep dive only when they are failed, slow, or family outliers.
   - **Estimated savings:** **25–60%** of this step’s mini-model spend in similar windows.
   - **Quality risk:** low, because the step is already fail-open and supplemental.

4. **Reconsider self-triggered autofix verification reruns where quality does not require them.** **(Inference, code-backed)**
   - **Evidence:** `.github/workflows/review_autofix.yml:328-333` and `:4886-4933` explicitly document that `AUTOFIX_SKIP_SELF_TRIGGERED=false` restores continuous reruns and that the old skip behavior was a “LLM-cost-saving” path; recent data also shows `10` cancelled `review_autofix` runs, with sampled cancellations still burning `386s`, `676s`, `700s`, `1088s`, and `1359s` before ending.
   - **Root cause:** follow-up verification passes on bot-generated `[ai-autofix]` commits.
   - **Exact change:** canary `AUTOFIX_SKIP_SELF_TRIGGERED=true` for low-risk/non-orchestrator PRs, or only after CI is already green.
   - **Estimated savings:** potentially large; the workflow’s own comments describe this as roughly a **~2× LLM spend** lever per autofix iteration.
   - **Quality risk:** medium-high; use selectively, not repo-wide by default.

**Semble / Serena assessment**
- **Semble:** operationally looks efficient, not noisy. I found **11 deduped `SEMBLE_QUERY` events** totaling **136,569 bytes** with average latency **485ms/query**; `reviewer-context` queries were **8/11** and averaged **14.7KB** each. Relative to reviewer steps lasting `924–1279s`, this is small overhead and plausibly beneficial for prompt-size control (**inference**, because prompt-token telemetry is missing).
- **Serena:** no operational `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines were observed outside generated analysis logs. In this window, Serena is not replacing downstream tool/model work, but it is also not adding cost/noise.

## Reliability Improvements

1. **Remove the `implement.yml`/audit drift that is causing every CI failure.**
   - **Failure evidence:** all `29` `ci` runs failed at the same step; sampled runs include `25981394151`, `25979656195`, `25980223741`, `25980368054`.
   - **Root cause category:** configuration/test contract drift.
   - **Exact fix:** align `.github/workflows/implement.yml:588` with the canonical `Resolved ref:` log string expected by `tests/test_workflow_checkout_integration_ref_audit.py:72,88`; ideally centralize the contract in one shared helper/string.
   - **Expected reliability impact:** remove the current **100% CI family failure rate**.
   - **Rollback / fail-open:** low-risk; if desired, accept both strings temporarily during rollout.

2. **Validate the branch-scoped `validation-refresh` concurrency fix on live release smoke runs.**
   - **Failure evidence:** `test_and_mark_stable` run `25980238302` watched downstream `validation-refresh` run `25980248328`, which ended `status=completed conclusion=cancelled`; the parent then failed after `4053s`.
   - **Root cause category:** likely concurrency-group collision between smoke and normal `validation-refresh` runs (**inference**).
   - **Exact fix:** the current repo state already appears correct: `.github/workflows/validation-refresh.yml:36-48` keys concurrency by `branch_name`, and `.github/workflows/test-and-mark-stable.yml:3545-3551` dispatches a unique `ai/validation-refresh-smoke-${GITHUB_RUN_ID}` branch. Confirm that this exact version is what release smoke uses, and add a regression assertion around the derived concurrency key.
   - **Expected reliability impact:** prevents long false-negative release failures.
   - **Rollback / fail-open:** low; production validation runs still share one default branch-scoped group.

3. **Reduce avoidable `review_autofix` reruns/cancellations.** **(Inference)**
   - **Failure evidence:** family totals show `10` cancelled `review_autofix` runs out of `93`; recent examples still consumed `1359s` (`25978250378`), `1088s` (`25979656234`), `700s` (`25980411883`), `676s` (`25980412623`), `386s` (`25977935010`) before cancellation.
   - **Root cause category:** overlapping successor/verification runs after autofix pushes.
   - **Exact fix:** use the existing repo-variable lever `AUTOFIX_SKIP_SELF_TRIGGERED` more selectively, or strengthen post-push peer detection before launching the successor run.
   - **Expected reliability impact:** lower rerun-rate and less Actions/UI noise; some portion of the 10 cancellations should disappear.
   - **Rollback / fail-open:** simple repo-variable rollback.

4. **Make consolidator fail-open cheaper and more explicit.**
   - **Failure evidence:** runs `25980645146` and `25980458060` each spent `300s` in a consolidator call that produced `0` bytes and then `failopen=1`.
   - **Root cause category:** timeout/empty-output in a non-critical synthesis stage.
   - **Exact fix:** keep the fail-open path, but lower the timeout/reasoning or use a smaller model for routine bundles; emit an explicit degraded-state metric/comment whenever `failopen=1` occurs.
   - **Expected reliability impact:** fewer masked degraded reviews and less long-tail stall time.
   - **Rollback / fail-open:** current safe behavior remains intact.

**Fallback/probe note**
- I found **5 raw `SEMBLE_FALLBACK` lines** (2 unique target/file combos) and all of them were in `test_and_mark_stable` run `25980238302`, step `validate-scripts`, with `reason=...missing_semble` and `ms=0`. This is healthy contract-test fail-open behavior, not evidence of a broken production rollout.
- I found **no operational `SERENA_FALLBACK` or `SERENA_PROBE` lines**, so there is no Serena availability/regression pattern to mitigate in this window.

## AI Memory Health

| Metric | Observed value | Notes |
|---|---:|---|
| Operational `AI_MEMORY_TELEMETRY` events | 32 | Found across 8 slow `review_autofix` deep dives: `25978047174`, `25979101486`, `25979105933`, `25979383060`, `25979830085`, `25980129050`, `25980458060`, `25980645146` |
| `record-run-event` ops | 16 | phase start/completion writes are happening |
| `record-candidate` ops | 8 | candidates are being written |
| `retrieve` ops | 8 | all reviewer retrieves |
| Retrieve hit rate | 0 / 8 = **0%** | every retrieve returned `records_selected=0` |
| Avg `estimated_tokens` per retrieve | 0 | `budget_tokens` was not emitted |
| `keyword_method` distribution | `none`: 8 / 8 | no `plain` or `llm` retrievals observed |
| `enabled:false` retrieves | 0 | retrieval is turned on |
| `fail_open:true` retrieves | 0 | no retrieval fail-open events observed |
| Push attempts | `1`: 23, `2`: 1 | only run `25980458060` needed `push_attempts=2` |

**Assessment**
- The memory system is **writing**, but it is not yet **helping retrieval**. In this sample, reviewer retrieval returned zero records every time.
- That means current memory adds a small write path without any observed reviewer-context payoff.
- Coverage is incomplete: recent non-deep-dive evidence includes `review_autofix` run `25980612326` (`AI_MEMORY_TELEMETRY not present in provided logs`) and `copilot_pull_request_reviewer` run `25981386870` (`AI_MEMORY_TELEMETRY: n/a`), so emission is not yet uniformly visible across the broader workflow set.

**Recommended next steps**
1. Make retrieval stop defaulting to `keyword_method=none`; seed deterministic keywords from PR title, changed files, failed check names, and workflow family.
2. Log `budget_tokens` on every retrieve so over-budget suppression can be distinguished from genuine “no relevant memory”.
3. Verify memory telemetry emission outside slow `review_autofix` deep dives.

## GH API Call Audit

> Aggregate GitHub API call counts were not emitted in this bundle, so the audit below is bounded to directly observed call patterns.

| Pattern | Evidence | Audit | Concrete change | Expected reduction |
|---|---|---|---|---|
| `review_autofix` check-run polling | `.github/workflows/review_autofix.yml:1835-1905`; run `25980458060` spent `30.8s` in `Collect PR check-run failures` | This is the clearest remaining API hotspot. Each poll iteration uses `gh api --paginate --slurp` and can multiply via pagination/retries. | Snapshot once when no non-self checks are in flight; otherwise reduce poll iterations and skip waiting on clearly irrelevant checks. | Likely **1–3 logical fetches/run** in many cases (**inference**), plus lower rate-limit exposure. |
| `issue_pr_status` linked-issue lookup | `.github/workflows/issue_pr_status.yml:188-193` | Good hygiene: single GraphQL call for closing issues. | Keep as-is. | None needed. |
| `issue_pr_status` orchestrator issue classification | `.github/workflows/issue_pr_status.yml:280-330` | Good hygiene: one batched GraphQL call, REST only on batch failure. | Keep as-is; this matches the repo’s own batching guidance. | Already optimized. |
| `orchestrate_poll` label/state lookups | `scripts/orchestrate_poll_process.sh:50-57`, `1505-1582`, `1591-1595` | Good hygiene: process-lifetime label cache + batched GraphQL label fetch + merged state/labels REST read. | Keep as-is. | Already optimized. |
| Copilot artifact cleanup | Recent run `25981528568` listed artifacts once, then looped DELETE per artifact | Minor loop; likely small call count, but still per-artifact. | Delete only named/expected artifacts or skip cleanup when artifact count is zero/one. | Small; likely **1–3 API calls/run**. |

**Rate-limit posture**
- The repo already has solid retry infrastructure in `scripts/gh_helpers.sh`.
- I saw **no direct 429 or secondary-rate-limit incidents** in the sampled operational logs/log summaries, so this is not a retry-logic problem; it is a **logical call-count** problem concentrated mainly in `review_autofix`.

## Prompt Cache & Memory System

- **Prompt cache is intended to be on, but it is effectively unobservable.** Sampled `review_autofix` runs `25980645146` and `25980458060` logged `OPENROUTER_PROMPT_CACHE_DISABLED=false`, yet no raw cache create/read counters were emitted.
- **The repo is already trying to preserve static context**, but the evidence trail stops before cache-hit confirmation. Without actual `cache_creation_input_tokens` / `cache_read_input_tokens`, it is impossible to tell whether prompt-prefix stabilization is working.
- **Memory retrieval is ineffective right now.** The AI memory section above shows `0/8` retrieval hits, so the “memory” side of the prompt/cache system is not yet buying down prompt size or latency.
- **Semble looks like the one active retrieval helper that is paying its way.** Operational `SEMBLE_QUERY` volume was small (`11` queries, `136,569` bytes total, ~`485ms` average), which is tiny compared with multi-minute reviewer/editor calls.
- **Serena is not active in this repo window.** No operational Serena telemetry means no current cache/context benefit and no current Serena noise.

**Likely cache-fragmentation causes** **(inference)**
- highly dynamic PR metadata and check-run snapshots,
- pass-1 consensus ledgers feeding pass 2,
- large diff-context inserts,
- large consolidator inputs (`124–129KB`),
- and any volatile blocks placed too early in the prompt prefix.

**Recommended improvements**
1. Emit real prompt/cache counters per reviewer, summarizer, editor, and consolidator call.
2. Keep stable instructions/repo docs in a canonical prefix and append volatile artifacts last.
3. Reuse the same preassembled static prefix across pass 1, pass 2, and editor where possible.
4. When `SEMBLE_AVAILABLE=false` (seen in `orchestrate_poll` run `25981084870` log summary), short-circuit Semble setup/query earlier so unavailable retrieval does not still perturb prompt assembly or startup noise.

## Orchestrator Health

- **Healthy overall:** no `orchestrate` or `orchestrate_poll` failures were recorded in this window. `orchestrate_poll` had `18/18` successes; `clarify` and `orchestrate_clarify_respond` were mostly skipped fast, not stuck.
- **Clarification loops do not look pathological:** `clarify` had `203` skipped runs out of `210`; `orchestrate_clarify_respond` had `199` skipped out of `200`; p95 for both families was about `2s`. Recent runs `25981529172` and `25981529169` show clean `if` gating rather than loop churn.
- **The poller is the main orchestrator pain point:** `orchestrate_poll` p50 was `131.5s`, p95 `359.0s`, with outlier `25978632116` at `739s`. Recent run `25981084870` took `292s`, and its log summary says the `poll` step dominated while `SEMBLE_AVAILABLE=false`.
- **No direct evidence of conflict-heal storms or terminal-state thrash** was visible in this window. The code already has guardrails such as `INTEGRATION_CONFLICT_LIFETIME_MAX` in `scripts/orchestrate_poll_process.sh`, but I did not see those caps firing operationally.

**Smallest safe mitigations**
1. Emit per-poll metrics: issues scanned, judge invocations, stall recoveries, and `SEMBLE_AVAILABLE` status.
2. Alert when `orchestrate_poll` p95 exceeds ~`300s` for several consecutive windows.
3. Skip Semble work earlier when unavailable; the current fail-open behavior is healthy, but it should be cheaper and noiseless.

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence | Recommendation |
|---|---|---|---|
| Clarify / Orchestrate-clarify-respond | Control-plane noise, not compute | `203` skipped `clarify` runs and `199` skipped `orchestrate_clarify_respond` runs; p95 ~`2s` | Do not prioritize for speed work. |
| Plan / Implement | Moderate compute when active, but hidden by many skips | `plan` family avg `8.7s` because `194/200` are skipped, but active run `25978770473` took `625s`; `implement` avg `12.2s` because `194/201` are skipped, but active run `25979048822` took `1160s` | Optimize only after `review_autofix` and CI. |
| CI gate | Repeated failure/rework | `29/29` CI failures, `628–678s` each, same failure point | Fix the checkout-ref contract drift immediately. |
| Review / Autofix | **Primary compute bottleneck** | `93` runs, p50 `489s`, p95 `1993s`, ~`60.1%` of runtime; reviewer+editor dominate the slowest runs | Make reviewer/editor/consolidator paths conditional by diff risk/size. |
| Validate / Orchestrate poll | Secondary compute/wait bottleneck | `orchestrate_poll` p95 `359s`; outlier `739s`; recent `292s` run dominated by poll step | Track poller dwell and availability dependencies. |
| Release side-chains (`test_and_mark_stable`, `workflow_log_analysis`) | Long dispatch/watch chains | `test_and_mark_stable` failure `25980238302` took `4053s`; `workflow_log_analysis` run `25980248446` took `3985s` | Keep these after CI + review/autofix in priority; validate the concurrency fix and trim analysis fanout. |

**Queueing / retry / merge-overhead note**
- Multiple recent runs logged “waiting for a hosted runner,” but **queue/wait metrics were not surfaced in `analysis_context.json`**, so queueing could not be ranked quantitatively here.
- Retry overhead was minor in sampled healthy runs (for example, `forward_merge_stable_to_main` run `25981592618` retried push/fetch but still finished in `18s`).
- Merge/conflict-heal overhead did not stand out in this window.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` runtime and model cost (`93` runs, p50 `489s`, p95 `1993s`, ~`60.1%` of total runtime).
  - Deterministic `ci` failures (`29/29` failed).
  - Long-tail release/analysis workflows (`test_and_mark_stable` `4053s`, `workflow_log_analysis` `3985s`).

- **Top failure modes**
  - Contract drift between `implement.yml` and checkout-ref audit test.
  - One downstream `validation-refresh` cancellation causing a release-test false negative.
  - `review_autofix` long-tail cancellations and consolidator fail-open stalls.

- **Highest-cost drivers**
  - 6-model, 2-pass, `xhigh` `review_autofix` reviewer/editor stack.
  - Full-model consolidator on large bundles.
  - Workflow-log-analysis coverage-widening summaries (`185,780` mini tokens in one run).

- **Top 3 prioritized actions**
  1. Fix `.github/workflows/implement.yml:588` to match the canonical `Resolved ref:` contract and run that audit earlier in CI.
  2. Use the existing `review_autofix` knobs to reduce reviewer/editor/consolidator cost on small/routine diffs.
  3. Confirm that the branch-scoped `validation-refresh` concurrency fix is what release smoke now dispatches, and verify the cancellation disappears in the next stable-mark run.

## Metrics Appendix

### Overall run metrics

| Scope | Runs | Success | Failure | Cancelled | Skipped | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 164 | 30 | 13 | 793 | 96.1 | 1.0 | 648.0 |
| `ci` | 29 | 0 | 29 | 0 | 0 | 634.5 | 648.0 | 666.6 |
| `review_autofix` | 93 | 80 | 0 | 10 | 3 | 621.6 | 489.0 | 1993.0 |
| `plan` | 200 | 6 | 0 | 0 | 194 | 8.7 | 1.0 | 2.2 |
| `implement` | 201 | 5 | 0 | 2 | 194 | 12.2 | 1.0 | 2.0 |
| `orchestrate_poll` | 18 | 18 | 0 | 0 | 0 | 174.8 | 131.5 | 359.0 |
| `workflow_log_analysis` | 1 | 1 | 0 | 0 | 0 | 3985.0 | 3985.0 | 3985.0 |
| `test_and_mark_stable` | 1 | 0 | 1 | 0 | 0 | 4053.0 | 4053.0 | 4053.0 |

### Runtime concentration (derived from family averages × run counts)

| Family | Total runtime s | Share of observed runtime |
|---|---:|---:|
| `review_autofix` | 57,808 | 60.1% |
| `ci` | 18,400 | 19.1% |
| `test_and_mark_stable` | 4,053 | 4.2% |
| `workflow_log_analysis` | 3,985 | 4.1% |
| `orchestrate_poll` | 3,146 | 3.3% |

### Token / cache metrics

| Metric | Value | Notes |
|---|---:|---|
| Raw prompt tokens | unavailable | No operational `prompt_tokens` lines observed outside generated analysis logs |
| Raw completion tokens | unavailable | Same gap |
| Raw total tokens | unavailable | Same gap |
| Prompt-cache create tokens | unavailable | No operational `cache_creation_input_tokens` lines observed |
| Prompt-cache read tokens | unavailable | No operational `cache_read_input_tokens` lines observed |
| `workflow_log_analysis` unselected-run summarizer tokens | 185,780 | Run `25980248446`, step `Summarize unselected runs (gpt-5.4-mini)` |
| Summaries produced | 77 / 100 targeted | Same run; `23` skipped for empty logs |
| Sampled cache-disabled flag | `false` | Runs `25980645146`, `25980458060` logged `OPENROUTER_PROMPT_CACHE_DISABLED=false` |

### GH API summaries

| Pattern | Direct evidence | Aggregate count |
|---|---|---:|
| Review check-run polling | `.github/workflows/review_autofix.yml:1835-1905`; run `25980458060` step time `30.8s` | unavailable |
| Issue/PR linked-issue GraphQL batch | `.github/workflows/issue_pr_status.yml:188-193` | n/a |
| Orchestrator issue-classification GraphQL batch + REST fallback | `.github/workflows/issue_pr_status.yml:280-330` | n/a |
| Orchestrate-poll label cache + batched GraphQL labels + merged state/labels REST read | `scripts/orchestrate_poll_process.sh:50-57,1505-1595` | n/a |
| Copilot artifact cleanup list + per-artifact DELETE loop | Recent run `25981528568` | unavailable |
| Observed 429 / secondary-rate-limit incidents | none in sampled operational logs | 0 observed |

### Semble / Serena / MCP telemetry

| System | Query count | Fallback count | Probe count | Logged bytes | Response bytes | Avg latency | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| Semble | 11 | 5 raw lines | n/a | 136,569 | n/a | 485ms/query | Queries: `reviewer-context` 8, `overflow` 3 |
| Serena | 0 | 0 | 0 | 0 | 0 | n/a | No operational Serena lines observed |
| Other MCP servers observed | 0 | 0 | 0 | 0 | 0 | n/a | None observed |

### Per-target MCP availability

| Server / target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---:|---:|---:|---|
| Semble / availability | 0 | 0 | 0 | No `SEMBLE_PROBE` format observed; one log summary (`25981084870`) reported `SEMBLE_AVAILABLE=false` |
| Serena / all targets | 0 | 0 | 0 | No operational `SERENA_PROBE` lines observed |
| Other MCP / all targets | 0 | 0 | 0 | None observed |

### AI memory metrics

| Metric | Value | Notes |
|---|---:|---|
| Operational memory telemetry events | 32 | 8 slow `review_autofix` runs only |
| `retrieve` hit rate | 0 / 8 (0%) | All reviewer retrievals returned 0 records |
| Avg `estimated_tokens` per retrieve | 0 | `budget_tokens` not emitted |
| `keyword_method` | `none` 8 / 8 | No `plain` or `llm` retrievals observed |
| Retrieve `enabled:false` | 0 | Retrieval was enabled |
| Retrieve `fail_open:true` | 0 | No retrieval fail-open events |
| Push attempts histogram | `1`: 23, `2`: 1 | One retry case in run `25980458060` |
| Other AI-memory-prefixed op | `summarize_unselected_runs` | Run `25980248446` used 185,780 mini tokens for coverage widening |

## Deep Audit — Workflows & Scripts (2026-05-17)

### Section 1: Bug & Correctness Sweep

Repo-wide syntax checks were clean (`yamllint` on workflows, `python -m py_compile` on `scripts/*.py`); the items below are logic/runtime defects.

- **BUG-001**
  - **File path:** `.github/workflows/plan.yml:393-399,482-487,604-625,646-684,729-748`
  - **Severity:** High
  - **Category tag:** `bug`
  - **Description:** `Fetch issue comments` writes raw `gh api --paginate` output directly to `ISSUE_COMMENTS_FILE`. On page 2+, that file becomes concatenated JSON arrays, but later steps treat it as one JSON array (`jq '[ .[] | ... ]'`, `.[]`, `sort_by(...)`). Long issue threads can therefore mis-parse, skip stale-comment detection, delete the wrong clarification comments, or fail plan-context assembly.
  - **Recommended fix:** Merge pages before writing, e.g. `gh_retry gh api --paginate ... | jq -s 'add // []' > "${ISSUE_COMMENTS_FILE}"`, matching `.github/workflows/implement.yml:1014-1015`; alternatively wrap it in a helper built on `scripts/gh_helpers.sh:549-615`.

- **BUG-002**
  - **File path:** `.github/workflows/plan.yml:450-473`
  - **Severity:** Medium
  - **Category tag:** `bug`
  - **Description:** `LINKED_PR_COUNT` is computed with `gh api --paginate ... --jq '[...] | length'`, but `--jq` runs once per page. When the issue timeline spans multiple pages, the variable can contain multiple integers (for example `0\n1`), and the later `[ "${LINKED_PR_COUNT}" -gt 0 ]` numeric test becomes invalid or false-negative. That can let planning continue even when an open linked PR already exists.
  - **Recommended fix:** Slurp pages before counting (`... --paginate | jq -s 'add // [] | map(...) | length'`), or replace the REST timeline count with a single batched GraphQL path modeled on `scripts/orchestrate_poll_process.sh:6512-6568`.

- **BUG-003**
  - **File path:** `scripts/validate_process.sh:2702-2718`
  - **Severity:** Medium
  - **Category tag:** `bug`
  - **Description:** Prior validation-failure context is intended to be capped to the latest 3 comments, but `gh api --paginate --jq '[...] | .[-3:] | .[].body'` applies `.[-3:]` per page, not across the merged comment history. Multi-page tracking issues can therefore leak more than 3 old failures into the next-cycle prompt.
  - **Recommended fix:** Slurp pages before slicing (`... --paginate | jq -s 'add // [] | map(select(...)) | .[-3:] | .[].body'`), or switch to a bounded GraphQL `comments(last: 3)` fetch.

### Section 2: GitHub API Call Redundancy Audit

- **API-001**
  - **File path:** `.github/workflows/review_autofix.yml:1529-1536; scripts/gh_helpers.sh:735-900`
  - **Severity:** High
  - **Category tag:** `api-redundancy`
  - **Description:** `Collect PR metadata` performs 4 logical fetches on the hot `review_autofix` path: PR payload, issue comments, reviews, and review comments. The repo already has a GraphQL-first batching helper (`gh_pr_with_all_comments`) for this shape, but this workflow still uses separate REST calls.
  - **Recommended fix:** Extend `gh_pr_with_all_comments` to also emit normalized review metadata (`state`, `body`, timestamps), then replace lines 1529-1536 with one helper call that writes the needed JSON files.
  - **Current call count:** 4 logical calls minimum, plus pagination/retry expansion.
  - **Proposed call count:** 1 logical helper call in the common case, with existing REST fallback only on pagination boundaries.
  - **Batching pattern to extend:** `scripts/gh_helpers.sh:735-900` (`gh_pr_with_all_comments`).

- **API-002**
  - **File path:** `.github/workflows/clarify.yml:387-402`
  - **Severity:** Medium
  - **Category tag:** `api-redundancy`
  - **Description:** When semantic cache is enabled, clarify fetches the same issue comments twice: once for `ISSUE_COMMENTS_FILE` (`per_page=50`) and again as full paginated history for `THREAD_HISTORY_FILE`. The second call already contains the first call’s data, so this is a duplicate logical fetch unless the exact server-side 50-comment truncation is treated as a contract. [NEEDS VERIFICATION]
  - **Recommended fix:** Fetch the full comment array once, store it, derive `ISSUE_COMMENTS_FILE` locally with `jq '.[0:50]'`, and render `THREAD_HISTORY_FILE` from that same JSON.
  - **Current call count:** 2 logical calls.
  - **Proposed call count:** 1 logical call.
  - **Batching pattern to extend:** No GraphQL batch is needed here; extend the single-fetch JSON-file pattern in `scripts/gh_helpers.sh:549-615`.

I did not raise `issue_pr_status.yml` or `scripts/orchestrate_poll_process.sh` here because they already use the repo’s batched GraphQL patterns called out in `CLAUDE.md` §15.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001**
  - **File path:** `.github/workflows/mark-stable.yml:316-509; .github/workflows/test-and-mark-stable.yml:4629-4800; scripts/mark-stable.sh:1-111`
  - **Severity:** High
  - **Category tag:** `duplication`
  - **Description:** The release path is duplicated across `mark-stable.yml` and `test-and-mark-stable.yml` and has already drifted: `mark-stable.yml` wraps consumer `repository_dispatch` calls in `_gh_retry`, while `test-and-mark-stable.yml` uses raw `gh api` in the equivalent loop.
  - **Recommended fix:** Move shared release logic into a single owner such as new `scripts/release_helpers.sh` with functions like `verify_ci_passed <repo> <sha> <branch>`, `publish_release <version> <source_branch> <notes_file>`, and `dispatch_consumer_updates <consumer_file> <version>`. Update callers in both workflows and `scripts/mark-stable.sh`.

- **DUP-002**
  - **File path:** `.github/workflows/test-and-mark-stable.yml:3330-3437,3538-3581,3596-3646,3661-3705,4011-4058`
  - **Severity:** Medium
  - **Category tag:** `duplication`
  - **Description:** `test-and-mark-stable.yml` repeats the same “capture PRE run id → dispatch workflow → poll for new run id → watch until completion” shell block at least 5 times, with only workflow name, timeout, inputs, and accepted conclusions changing.
  - **Recommended fix:** Extract a shared watcher like `scripts/dispatch_and_watch_workflow.sh` with signature `dispatch_and_watch_workflow <repo> <workflow_file> <deadline_secs> [--field key=value ...] [--accept success,skipped] [--nonblocking]`. Update the workflow-log-analysis, validation-refresh, update_workflows, memory-maintenance, and internal-validate callers.

- **DUP-003**
  - **File path:** `scripts/install_semble.sh:10-44,62-99; scripts/setup_serena.sh:23-90,127-148`
  - **Severity:** Low
  - **Category tag:** `duplication`
  - **Description:** The Semble and Serena installers duplicate the same bootstrap helpers (`log`, `write_github_env`, `append_github_path`, version-pin matching). The two scripts are already diverging in small behavior details.
  - **Recommended fix:** Extract common bootstrap code to `scripts/tool_bootstrap_helpers.sh` with helpers such as `tool_log <tool> <msg>`, `write_github_env <key> <value>`, `append_github_path <dir>`, and `version_matches_pin <actual_version> <expected_regex>`. Update `install_semble.sh` and `setup_serena.sh` to source it.

### Section 4: Expression Size Limit Risk Assessment

- **EXPR-001**
  - **File path:** `.github/workflows/test-and-mark-stable.yml:1203-1586`
  - **Severity:** High
  - **Category tag:** `expression-limit`
  - **Description:** The Phase 4 wait-review `run:` body is about **19,899 chars** and contains `${{ }}` interpolation, leaving only about **1,101 chars** of headroom before GitHub’s **21,000-char** expression ceiling.
  - **Recommended fix:** Extract this poll/watch loop to `scripts/test_and_mark_stable_wait_review.sh` and pass only the small env surface it needs.

- **EXPR-002**
  - **File path:** `.github/workflows/review_autofix.yml:1445-1834`
  - **Severity:** Medium
  - **Category tag:** `expression-limit`
  - **Description:** `Collect PR metadata` is about **17,408 chars**, leaving about **3,592 chars** of headroom. This is already the next large inline block on the review hot path, and it is likely to keep growing.
  - **Recommended fix:** Extract to `scripts/review_collect_pr_metadata.sh`; that also pairs naturally with `API-001`.

- **EXPR-003**
  - **File path:** `.github/workflows/validate.yml:204-577`
  - **Severity:** Medium
  - **Category tag:** `expression-limit`
  - **Description:** `Fetch workflow support files` is about **17,416 chars**, leaving about **3,584 chars** of headroom. It is a large inline bootstrap block with many future-growth points.
  - **Recommended fix:** Extract it to `scripts/validate_fetch_support_files.sh` and keep the workflow step as a thin wrapper.

- **EXPR-004**
  - **File path:** `.github/workflows/test-and-mark-stable.yml:1673-2077`
  - **Severity:** Medium
  - **Category tag:** `expression-limit`
  - **Description:** Phase 4b canary verification is about **17,408 chars**, leaving about **3,592 chars** of headroom. It is another large interpolated inline script in the same workflow that already has one near-limit block.
  - **Recommended fix:** Move the canary-fetch / pytest / retry logic into `scripts/test_and_mark_stable_verify_canary.sh`.

No workflow file exceeds the **800 KB** early-warning threshold; the largest is `.github/workflows/review_autofix.yml` at **334,445 bytes**.

### Section 5: Cross-Cutting Concerns

- **CONSIST-001**
  - **File path:** `.github/workflows/issue_pr_status.yml:235-249; scripts/label_helpers.sh:146-194`
  - **Severity:** Low
  - **Category tag:** `consistency`
  - **Description:** `issue_pr_status.yml` sources `scripts/label_helpers.sh` and then redefines a fallback `set_issue_phase_label_resilient` with weaker semantics. The inline fallback only POST-adds the target label, while the canonical helper fetches current labels and replaces the whole phase set via PUT, so the two paths can diverge and leave contradictory phase labels.
  - **Recommended fix:** Keep one implementation only: either fail fast if `set_issue_phase_label_resilient` is missing after sourcing, or move any required fallback behavior into `scripts/label_helpers.sh`.

- **SHELL-001**
  - **File path:** `scripts/orchestrate_poll_process.sh:11424-11427`
  - **Severity:** Low
  - **Category tag:** `shellcheck`
  - **Description:** `_sorted_issue_nums="$(printf '%s\n' ${ISSUE_NUMS} | sort -un)"` and `for inum in ${_sorted_issue_nums}; do` expand unquoted word lists. Unexpected whitespace or glob characters in `ISSUE_NUMS` can split or expand before sorting.
  - **Recommended fix:** Normalize explicitly with quoting or arrays, e.g. build an array first and iterate `"${array[@]}"`.

- **SHELL-002**
  - **File path:** `scripts/review_commit_changes.sh:482-490; scripts/review_conflict_resolve.sh:1459-1461`
  - **Severity:** Low
  - **Category tag:** `shellcheck`
  - **Description:** Both scripts set the authenticated remote with an unquoted credential-bearing URL: `git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}`. This is an avoidable SC2086-style expansion point and keeps the token in argv form longer than necessary.
  - **Recommended fix:** Build a quoted variable first (`remote_url=...`; `git remote set-url origin "${remote_url}"`) or switch to an auth mechanism that does not embed the token in the command line.

- **DEBT-001**
  - **File path:** `.github/workflows/test-and-mark-stable.yml:242-248,3145-3148,3256-3259,4705-4708,4816-4819`
  - **Severity:** Low
  - **Category tag:** `tech-debt`
  - **Description:** The temporary `git-checkout-diag` anchor is still wired into 4 jobs, and its own comments say it should be removed after the checkout exit-128 root cause is identified. It now represents long-lived debug scaffolding in the release smoke workflow.
  - **Recommended fix:** Gate it behind an explicit debug input, or remove it once the checkout post-step investigation is closed.

Repo-wide grep found no `TODO`/`FIXME`/`HACK` markers in workflows/scripts; the explicit remaining debt marker is the temporary checkout diagnostic above.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 4 | BUG-001, API-001, DUP-001, EXPR-001 |
| Medium | 7 | BUG-002, BUG-003, API-002, DUP-002, EXPR-002, EXPR-003, EXPR-004 |
| Low | 5 | DUP-003, CONSIST-001, SHELL-001, SHELL-002, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 1 | Small |
| API call optimization | 3 | Medium |
| Code modularization | 6-7 | Large |
| Expression size reduction | 5-7 | Large |
| Medium/Low fixes | 7-8 | Medium |
