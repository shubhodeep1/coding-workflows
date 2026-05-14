## Executive Summary

- `review_autofix` is the dominant bottleneck: 89 runs consumed 81,704s, or 70.1% of all sampled runtime. Tiny PRs still paid for the full review stack; runs `25804342547` (450s) and `25805360210` (1580s) logged `files=1/2`, `additions=5/7`, but `small_diff=false` and `skip=false`. **Estimated impact:** save ~7-26 minutes on eligible small PRs, plus major model-cost reduction. **Confidence:** high.
- All 10 cancelled runs in the window were `review_autofix`, totaling 14,223s of runner time, with very late cancellations in runs `25780844046` (4150s) and `25779045838` (3242s). **Estimated impact:** recover ~4 hours of wasted runtime per similar window. **Confidence:** medium.
- The `Internal: AI Review Autofix Sweep` failure cluster was caused by a concrete CLI bug, not flaky infrastructure: 9 failed runs (`25776898919` … `25801406931`) died in `sweep / Enumerate open PRs and dispatch internal-review.yml` with `the '--slurp' option is not supported with '--jq' or '--template'`. A later success, `25837695872`, shows the fixed pipe-to-`jq` implementation. **Estimated impact:** remove a 36% sweep failure rate and restore missed review dispatches. **Confidence:** high.
- `Internal: AI Implement` failures `25785091932` and `25790944632` spent ~22 minutes each before the `Commit changes` guard blocked `65 staged deletions` over `BULK_DELETE_THRESHOLD=3`. **Estimated impact:** save ~22 minutes on blocked runs by failing fast or pre-authorizing narrow docs-cleanup cases. **Confidence:** high.
- CI is the second-largest time sink: 28 runs consumed 20,305s (17.4%), and multiple runs (`25805359821`, `25804430559`, `25800811829`, `25804343147`) show `lint` consuming ~12-13 minutes of ~12-13 minute CI runs. **Estimated impact:** ~4-6 minutes/run if the monolith can be parallelized cleanly. **Confidence:** medium.
- Node 20 deprecation is a near-term reliability risk, already visible in `implement` (`25785091932`), `review_autofix` (`25780853576`), and `orchestrate_poll` (`25837442493`). Affected actions include `astral-sh/setup-uv@v3` and, in slow review runs, `actions/cache/restore@v4` and `actions/cache/save@v4`. **Estimated impact:** avoid deadline-driven breakage before June 2, 2026. **Confidence:** high.

## Speed Optimizations

### Critical-path wins

1. **Make `review_autofix` actually take the small-diff fast path**
   - **Evidence:** `review_autofix` consumed 81,704s total. In run `25804342547`, the gate logged `AUTOFIX_GATE_DET_SKIP_EVAL pr=2567 files=1 additions=5 deletions=? ... small_diff=false skip=false` and the run still took 450s. In run `25805360210`, it logged `files=2 additions=7 deletions=? ... small_diff=false skip=false` and still took 1580s. The workflow already has a small-diff gate in `.github/workflows/review_autofix.yml`, but it requires both `pr_additions` and `pr_deletions` to be numeric.
   - **Root cause:** the fast-path predicate is present but not reliably getting numeric deletion totals, so trivial PRs fall through to the full review stack.
   - **Exact change:** fix `pr_deletions` propagation in the gate path and add a safe fallback/retry when `/pulls/{n}` metadata does not parse cleanly; then attach a reduced profile for `SMALL_DIFF=true` (for example: 1-2 reviewers, no reviewer pass 2, lower reasoning effort).
   - **Estimated time savings:** ~7-26 minutes on runs like `25804342547` and `25805360210`.
   - **Implementation risk:** medium; keep the existing force-review override and fail open to the full profile when diff metadata is incomplete.

2. **Fail fast on destructive-delete implement tasks**
   - **Evidence:** failed runs `25785091932` (1363s) and `25790944632` (1329s) both reached `implement / implement -> Commit changes` and then stopped with `Refusing to commit: 65 staged deletions exceeds BULK_DELETE_THRESHOLD=3 and ALLOW_BULK_DELETE is not 'true'.`
   - **Root cause:** a legitimate safety guard runs at the end of a long implementation path instead of near the start.
   - **Exact change:** run the same bulk-delete guard immediately after the edit plan / staged-diff becomes available; if the task is a doc/analysis cleanup and the candidate diff is limited to approved paths, require an explicit narrow override before spending model time.
   - **Estimated time savings:** ~22 minutes per blocked run.
   - **Implementation risk:** low; this preserves the guard and only moves it earlier.

3. **Cancel superseded `review_autofix` runs before expensive reviewer work**
   - **Evidence:** all 10 cancellations in the repo were `review_autofix`, totaling 14,223s. Worst cases: `25780844046` cancelled at 4150s and `25779045838` at 3242s.
   - **Root cause:** obsolete PR-review runs are being cancelled late, after expensive work has already started.
   - **Exact change:** tighten concurrency cancellation and add stale head/base checks before `codex-agent` starts and again between major reviewer/editor phases.
   - **Estimated time savings:** up to ~69 minutes on a single worst-case run; ~4 hours across this sampled window.
   - **Implementation risk:** low-medium; verify that branch-push review mode is excluded where needed.

4. **Break up the monolithic CI `lint` job**
   - **Evidence:** `ci` used 20,305s total. Recent run summaries show `lint` dominated `25805359821` (752s total, ~736s in `lint`), `25804430559` (737s total), `25800811829` (757s total), and `25804343147` (778s total).
   - **Root cause:** one long `lint` job is the CI critical path.
   - **Exact change:** split `lint` into independent jobs or at least independent steps that can fail fast and run in parallel (for example: Python quality, tests, workflow-reference validation).
   - **Estimated time savings:** **inference** ~4-6 minutes/run if the current work splits into at least two comparable parallel branches.
   - **Implementation risk:** medium; preserve the same checks and branch protection semantics.

### Micro-optimizations

5. **Trim fixed bootstrap overhead in `orchestrate_poll`**
   - **Evidence:** `orchestrate_poll` averaged 129.9s. In run `25837442493`, Codex install took 3s (`added 2 packages in 3s`), Semble install/build ran during the poll bootstrap, and the run started with `SEMBLE_AVAILABLE=false`, `SEMBLE_INDEX_AVAILABLE=false` before later enabling them.
   - **Root cause:** every poll cycle pays a repeated tool-install/setup cost.
   - **Exact change:** defer Semble install/index build until active issues exist and the poller will actually use retrieval/judge logic; keep the current fail-soft behavior.
   - **Estimated time savings:** ~10-15s/run.
   - **Implementation risk:** low.

## Cost Optimizations

> Exact token and dollar savings cannot be computed from this window because normalized prompt/completion token telemetry is mostly missing.

1. **Reduce review-model fan-out on low-risk PRs**
   - **Evidence:** slow run `25780853576` shows `REVIEWER_MODELS` with 6 reviewers (`minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`, `deepseek/deepseek-v4-pro`, `z-ai/glm-5`, `qwen/qwen3.6-plus`, `x-ai/grok-4.1-fast`), `MODEL_EDITOR: openai/gpt-5.4`, `REVIEWER_REASONING_EFFORT: xhigh`, `EDITOR_REASONING_EFFORT: xhigh`, `ENABLE_REVIEWER_TWO_PASS: true`, `XPOLL_SUMMARISER_MODEL: openai/gpt-5.4-mini`, `XPOLL_SUMMARISER_CALL_TIMEOUT_SECS: 2400`.
   - **Root cause:** the default review profile is expensive even when the diff is tiny and deterministic-skip should have triggered.
   - **Exact change:** once the small-diff gate is fixed, use a reduced review profile for low-risk PRs; keep the full six-reviewer/two-pass profile for force-review, large diffs, or conflicted/stalled PRs.
   - **Estimated savings:** **inference** >50% of reviewer-model invocations on tiny PRs, plus large wall-clock savings.
   - **Quality-risk notes:** medium; keep force-review markers and fail open to the full profile when risk signals are present.

2. **Cap Semble overflow on implement runs; keep reviewer-context narrow**
   - **Evidence:** there were 21 anchored runtime `SEMBLE_QUERY` lines in deep-dive logs, totaling 1,445,291 logged bytes. `implement` accounted for 14 `target=overflow` queries and 1,236,185 bytes; failed run `25790944632` alone emitted 8 queries and 913,031 bytes, while `25785091932` emitted 6 queries and 323,154 bytes. `review_autofix` added 7 `target=reviewer-context` queries totaling 209,106 bytes.
   - **Root cause:** Semble is adding large prompt context blocks without enough task-aware bounding. In `implement`, this looks more like prompt expansion than prompt reduction.
   - **Exact change:** cap total overflow bytes/files per run, dedupe repeated static docs (`README.md`, runbooks, old analysis docs), and skip overflow entirely for doc/analysis cleanup tasks that will trip the bulk-delete guard anyway. For `review_autofix`, keep reviewer-context tied to changed files and linked-issue context only.
   - **Estimated savings:** medium-high on `implement`; moderate on `review_autofix`. Exact token savings unavailable.
   - **Quality-risk notes:** medium; keep an operator override for cases where extra context is genuinely required.

3. **Stop paying for cancelled `review_autofix` work**
   - **Evidence:** 10 cancelled `review_autofix` runs consumed 14,223s; they are the only cancellations in the repo.
   - **Root cause:** superseded review jobs keep running after they have become stale.
   - **Exact change:** cancel earlier via concurrency/stale-base checks before reviewer fan-out and before editor/merge phases.
   - **Estimated savings:** high runner and model-cost recovery in busy PR windows.
   - **Quality-risk notes:** low, if cancellation is keyed to newer SHA or a newer run in the same concurrency group.

4. **Make prompt-cache effectiveness measurable before tuning further**
   - **Evidence:** 10 anchored `openrouter usage phase=review_autofix_cache_probe` lines were found across 5 `review_autofix` runs, and all showed `cache_enabled=true` but `prompt_tokens=na`, `completion_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`.
   - **Root cause:** cache probes exist, but the metrics required to quantify savings are not being emitted.
   - **Exact change:** log real prompt/completion/cache token fields on every provider call, not only probe calls.
   - **Estimated savings:** low immediate savings, high enabling value for future cost tuning.
   - **Quality-risk notes:** low.

**Semble assessment:** in this sampled window, `review_autofix` Semble use looks targeted but unproven (`reviewer-context`, ~29.9 KB/query on average); `implement` Semble use looks like noisy overflow expansion. No anchored runtime `SEMBLE_FALLBACK` events were observed.

## Reliability Improvements

1. **Verify the sweep fix is deployed on every triggering ref**
   - **Failure evidence:** 9 failed `Internal: AI Review Autofix Sweep` runs (`25776898919`, `25779749170`, `25782674865`, `25786171405`, `25789468330`, `25792813946`, `25795714053`, `25798300389`, `25801406931`) all failed in `sweep / Enumerate open PRs and dispatch internal-review.yml` with `the '--slurp' option is not supported with '--jq' or '--template'`.
   - **Root cause category:** shell/CLI incompatibility.
   - **Exact fix:** ensure the later fixed implementation seen in run `25837695872` (separate `gh api --paginate` piped into `jq -s 'add // []'`) is present on every branch/ref that can execute the sweep; add a tiny shell smoke test for that command shape.
   - **Expected reliability impact:** removes the main failure driver in the `review_autofix` family; the sweep-only workflow split was 9 failures in 25 runs (36%).
   - **Rollback / fail-open:** low risk; the fix already succeeded in production.

2. **Preflight the bulk-delete guard for approved cleanup tasks**
   - **Failure evidence:** `implement` failures `25785091932` and `25790944632` both died at `Commit changes` after ~22 minutes because `65 staged deletions` exceeded the threshold.
   - **Root cause category:** safety-policy mismatch.
   - **Exact fix:** run the same guard early, and for tightly-scoped analysis/docs cleanup tasks allow a narrow explicit override path instead of discovering the block at the very end.
   - **Expected reliability impact:** high for this task class; it prevents long doomed runs and clearer operator retries.
   - **Rollback / fail-open:** keep the current default guard behavior; only relax it on explicitly approved cleanup scopes.

3. **Upgrade Node-24-compatible actions now**
   - **Failure evidence:** deprecation warnings already appear in `implement` (`25785091932`), `review_autofix` (`25780853576`), and `orchestrate_poll` (`25837442493`, `25835868692`). Affected actions include `astral-sh/setup-uv@v3`, `actions/cache/restore@v4`, and `actions/cache/save@v4`.
   - **Root cause category:** platform/runtime deprecation.
   - **Exact fix:** bump action versions where available and validate with `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` before the forced switch on June 2, 2026.
   - **Expected reliability impact:** high; this is a repo-wide breakage risk.
   - **Rollback / fail-open:** temporary opt-out exists, but should be used only as an emergency bridge.

4. **Fix the nightly validation self-test fixtures without changing the good fail-open artifact path**
   - **Failure evidence:** run `25776273352` logged `validation-selftest: fixtures=3 passed=1 failed=2`, then still uploaded artifacts and updated `analysis/validation-selftest-status.json`.
   - **Root cause category:** test regression.
   - **Exact fix:** repair the two failing fixtures; keep artifact upload and status-file update exactly as they are.
   - **Expected reliability impact:** restores nightly regression signal without losing diagnostics.
   - **Rollback / fail-open:** already healthy; artifacting worked even on failure.

**Semble fallback note:** no anchored runtime `SEMBLE_FALLBACK` lines were emitted in this window, so I do not see evidence of a masked broken rollout or excessive fail-open behavior.

## AI Memory Health

- Deep-dive logs contained **56** structured `AI_MEMORY_TELEMETRY` records:
  - `record-run-event`: 34
  - `retrieve`: 10
  - `record-candidate`: 8
  - `processed-command-check`: 2
  - `processed-command-claim`: 2
- **Retrieve hit rate:** `2/10 = 20%`.
- **Average `estimated_tokens` on retrieves:** `8.4`.
- **Keyword method distribution:** `plain=2`, `none=8`, `llm=0`.
- **Zero-result retrieves:** `8/10`.
- **`fail_open: true` retrieves:** `0`.
- **`enabled: false` retrieves:** `0`.
- **Push retries >1:** one observed case, run `25793045391`, `record-run-event phase_started`, `push_attempts=2`.

| Workflow family | Retrieves | Hits | Hit rate | Avg estimated_tokens | keyword_method |
|---|---:|---:|---:|---:|---|
| `implement` | 2 | 2 | 100% | 42.0 | `plain` |
| `review_autofix` | 8 | 0 | 0% | 0.0 | `none` |

**Interpretation**
- `implement` memory retrieval is working: run `25790944632` logged `records_selected=2`, `estimated_tokens=56`, `keyword_method=plain`.
- `review_autofix` retrieval is not working well enough to matter: run `25780853576` logged `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`, and the same pattern repeats across other slow review runs.
- I did **not** find `finalize-task`, `promote`, or `compact` telemetry in the sampled deep dives, so I cannot assess those operations from this window.
- The telemetry does **not** expose an explicit retrieval-token budget, so I can report average `estimated_tokens`, but not “vs budget.”

**Recommendation**
- Make `review_autofix` retrieval query construction less empty: include PR number, linked issue, changed files, last failing job/step, or skip reason so the system does not fall back to `keyword_method=none`.
- Keep the current telemetry format; it is already useful and fail-open behavior looks healthy.

## GH API Call Audit

No HTTP 429s, secondary rate-limit events, or retry storms were observed in the sampled logs.

| Workflow / evidence | Observed pattern | Redundancy risk | Concrete change | Estimated reduction |
|---|---|---|---|---|
| `review_autofix` sweep, run `25837695872` | `gh api --paginate GET /pulls`, then per PR loops over `GET /actions/workflows/{wf}/runs` for `internal-review.yml` and `review_autofix.yml` | High when PR count rises: inner-loop run-state lookups | Reuse the repo’s existing cached actions-runs snapshot pattern (`ACTIONS_RUNS_CACHE_TTL_SECONDS=60`, `_load_actions_runs_cached`) or fetch one snapshot per workflow and filter client-side | **Scaling estimate:** from `2 x candidate PRs` calls to `2` calls per sweep; e.g. 40 → 2 if 20 candidate PRs |
| `Copilot code review`, runs `25800810017`, `25805362798` | `github.rest.pulls.get`, `github.paginate(github.rest.pulls.listFiles, ...)`, and `/actions/runs/{id}/artifacts` | Low-medium | Pass PR file manifests and artifact metadata across steps/jobs instead of re-reading when possible | ~1-2 calls/run |
| `orchestrate_poll` | Repo docs and scripts already centralize `/actions/runs` reads and batched GraphQL prefetch | Low | Keep this as the reference implementation and port sweep logic to it | Reliability/rate-limit benefit, not just call reduction |

**Cross-reference to repo rules**
- The repo already documents `ACTIONS_RUNS_CACHE_TTL_SECONDS=60` and central cached `/actions/runs` reads in `README.md` and `scripts/orchestrate_poll_process.sh`.
- The biggest GH API hygiene gap in this window is that the sweep path is still doing its own per-PR active-run checks instead of reusing that shared cache/prefetch design.

## Prompt Cache & Memory System

**What is working**
- `review_autofix.yml` explicitly pre-assembles a stable “static context” prefix so the provider can cache it across runs.
- `OPENROUTER_PROMPT_CACHE_DISABLED: false` appears in sampled `review_autofix` and `orchestrate_poll` runs.
- AI memory telemetry is structured and usable.

**What is not measurable yet**
- Prompt-cache probes exist, but all 10 anchored probe lines reported `cache_enabled=true` with every token/cache field as `na`.
- There are no normalized prompt/completion totals in the sampled workflow logs, so real cache hit rate and token savings are unknown.

**Fragmentation / inefficiency risks**
- **Inference:** `implement` prompt tails are probably highly unstable because Semble overflow added 1.24 MB of logged context across just two failed runs.
- `review_autofix` uses multi-model fan-out plus two-pass review, which naturally reduces cache reuse unless the shared prefix is kept extremely stable.
- AI memory is not offsetting prompt growth in `review_autofix`; its retrieve hit rate there was 0%.

**Concrete improvements**
1. Emit real prompt/completion/cache token fields on every provider call.
2. Keep the static prefix first and stable; append Semble blocks, diff material, and reviewer outputs after that boundary.
3. Deduplicate Semble content before injection, especially repeated repo docs and stale analysis files.
4. Skip or shrink reviewer-context retrieval when deterministic skip should already win.
5. Add a one-line cache summary to workflow outputs so cache regressions show up in run summaries.

**Expected impact**
- Moderate token and latency improvement in `review_autofix` and `implement`, but exact savings are not measurable from this window.

## Orchestrator Health

- The control loop itself looks healthy: `orchestrate_poll` had **28/28 successful runs**, average **129.9s**, p95 **159.9s**.
- Recent poll run `25837442493` found **3 active tracking issue(s)** and completed successfully, which suggests the poller is progressing work rather than spinning empty.
- Fail-soft behavior looks good:
  - poll bootstrap starts with `SEMBLE_AVAILABLE=false` / `SEMBLE_INDEX_AVAILABLE=false`, then enables them later in the run;
  - no anchored runtime `SEMBLE_FALLBACK` was observed;
  - promote run `25804134893` failed fast and clearly when `stable` was 17 commits ahead of `main`, and later `forward_merge_stable_to_main` / `promote_main_to_stable` runs succeeded.
- The main observability gap is exported state quality:
  - `clarify`: 72 runs, but only 2 `success` and 70 `other`
  - `plan`: 70 runs, but only 3 `success` and 67 `other`
  - `orchestrate_clarify_respond`: 70 runs, all `other`
- Because `other` collapses skipped/deferred/waiting/non-terminal outcomes, I cannot reliably quantify clarification loops, wave deferrals, or stuck states from this window alone.

**Recurring operational pain point**
- The orchestrator hands off into expensive PR-healing loops (`review_autofix`) where all repo cancellations occur.

**Smallest safe mitigations**
- Export explicit phase outcomes instead of broad `other`.
- Add per-run fields for wave number, deferral reason, stall-recovery count, and cancellation reason.
- Track these indicators:
  - cancelled `review_autofix` seconds
  - poller runner-wait seconds
  - active tracking issues per poll
  - count of `small_diff=false` on PRs under the configured thresholds

## Pipeline Flow Bottlenecks

| Pipeline segment | Dominant bottleneck type | Evidence | Highest-value fix |
|---|---|---|---|
| Clarify → Plan | Observability, not latency | `clarify` avg 3.9s, `plan` avg 24.7s, but most outcomes are `other` | Export explicit skipped/deferred/waiting states |
| Implement | Late failure / wasted compute | `25785091932` and `25790944632` ran 1329-1363s and then failed at `Commit changes` | Move bulk-delete guard to preflight |
| Review / Autofix | Compute + queue + cancellation | 81,704s total, p95 3237.6s, multiple hosted-runner waits, 10 cancellations totaling 14,223s | Fix small-diff gate, reduce reviewer fan-out on tiny PRs, cancel stale runs earlier |
| CI | Compute | `lint` dominates 737-778s CI runs | Split monolithic `lint` into parallelizable parts |
| Validate / nightly self-test | Reliability signal, not major latency | `validate` only 206s; nightly self-test failed once at 125s | Fix fixtures, keep artifact/status fail-open path |
| Poll / orchestration | Queue + bootstrap overhead | `orchestrate_poll` avg 129.9s; tool setup repeats every run | Defer optional bootstrap work until needed |
| Promote / forward-merge | Healthy fail-closed guard | failed promote was 30s with explicit remediation; next forward-merge/promote succeeded | Keep current guard behavior |

**Ordered by end-to-end impact**
1. Fix `review_autofix` small-diff gating and shrink the low-risk review profile.
2. Fail fast on bulk-delete implement tasks.
3. Cancel superseded review runs earlier.
4. Break up CI `lint`.
5. Reuse cached actions-runs snapshots in the sweep path.
6. Trim fixed poller bootstrap overhead.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix`: 89 runs, 81,704s total, p95 3237.6s
  - `ci`: 28 runs, 20,305s total, `lint` dominates
  - `orchestrate_poll`: steady 2-3 minute control-loop overhead
- **Top failure modes**
  - sweep CLI bug cluster: 9 failed sweep runs
  - late bulk-delete guard failures in `implement`: 2 long failures
  - nightly validation self-test fixture regression: 1 failure
  - promote failure was intentional fail-closed behavior, not a bad workflow
- **Highest-cost drivers**
  - six-reviewer, `xhigh` reasoning, two-pass `review_autofix`
  - long cancelled `review_autofix` runs
  - monolithic CI `lint`
  - oversized Semble overflow on `implement`
- **Top 3 prioritized actions**
  1. Fix `review_autofix` small-diff gating and add a reduced low-risk review profile.
  2. Verify/backport the sweep `gh api --paginate | jq -s` fix and replace per-PR active-run checks with cached `/actions/runs` snapshots.
  3. Move the `implement` bulk-delete guard to preflight and add a narrow override path for explicitly approved docs/analysis cleanup tasks.

## Metrics Appendix

### Overall repo metrics

| Repo | Runs | Success | Failure | Cancelled | Other | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 468 | 171 | 13 | 10 | 274 | 2.8% | 248.9 | 2.0 | 1790.0 |

> `Other` is the collector bucket for non-success/failure/cancelled outcomes and likely includes skipped/non-terminal runs.

### Workflow family metrics

| Workflow family | Runs | Success | Failure | Cancelled | Avg (s) | p50 (s) | p95 (s) | Total duration (s) | Runtime share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `review_autofix` | 89 | 70 | 9 | 10 | 918.0 | 92.0 | 3237.6 | 81,704 | 70.1% |
| `ci` | 28 | 28 | 0 | 0 | 725.2 | 730.0 | 774.5 | 20,305 | 17.4% |
| `implement` | 70 | 1 | 2 | 0 | 58.5 | 1.0 | 2.5 | 4,098 | 3.5% |
| `copilot_pull_request_reviewer` | 14 | 14 | 0 | 0 | 260.7 | 239.0 | 376.4 | 3,650 | 3.1% |
| `orchestrate_poll` | 28 | 28 | 0 | 0 | 129.9 | 121.5 | 159.9 | 3,638 | 3.1% |
| `plan` | 70 | 3 | 0 | 0 | 24.7 | 1.0 | 6.4 | 1,730 | 1.5% |
| `clarify` | 72 | 2 | 0 | 0 | 3.9 | 1.0 | 4.3 | 284 | 0.2% |

### `review_autofix` workflow split

| Workflow name | Runs | Success | Failure | Cancelled | Avg (s) | p50 (s) | p95 (s) | Total duration (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `Internal: AI Review & Autofix` | 50 | 41 | 0 | 9 | 1129.2 | 676.5 | 3413.0 | 56,461 |
| `Codex PR Self-Healing Semantic Agent` | 14 | 13 | 0 | 1 | 1784.1 | 1818.5 | 3551.6 | 24,978 |
| `Internal: AI Review Autofix Sweep` | 25 | 16 | 9 | 0 | 10.6 | 10.0 | 15.8 | 265 |

### Semble telemetry (anchored runtime lines only)

| Scope | Queries | Fallbacks | Logged bytes | Avg bytes/query | Avg latency |
|---|---:|---:|---:|---:|---:|
| All observed Semble runtime telemetry | 21 | 0 | 1,445,291 | 68,823 | 527.9 ms |
| `implement` / `target=overflow` | 14 | 0 | 1,236,185 | 88,299 | 558.6 ms |
| `review_autofix` / `target=reviewer-context` | 7 | 0 | 209,106 | 29,872 | 466.6 ms |

### AI memory telemetry

| Metric | Value |
|---|---:|
| Total telemetry records | 56 |
| `record-run-event` | 34 |
| `record-candidate` | 8 |
| `retrieve` | 10 |
| `processed-command-check` | 2 |
| `processed-command-claim` | 2 |
| Retrieve hit rate | 20.0% |
| Avg retrieve `estimated_tokens` | 8.4 |
| `fail_open: true` retrieves | 0 |
| `enabled: false` retrieves | 0 |
| Push attempts >1 | 1 |

| Workflow family | Retrieves | Hits | Hit rate | Avg `estimated_tokens` | `keyword_method` |
|---|---:|---:|---:|---:|---|
| `implement` | 2 | 2 | 100% | 42.0 | `plain` |
| `review_autofix` | 8 | 0 | 0% | 0.0 | `none` |

### Prompt cache metrics

| Metric | Value |
|---|---|
| Anchored prompt-cache probe lines | 10 |
| Runs with probes | 5 (`25784761455`, `25795934355`, `25793075911`, `25790519763`, `25793045391`) |
| `cache_enabled=true` | 10/10 |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | all `na` |
| `cache_creation_input_tokens` / `cache_read_input_tokens` | all `na` |
| Normalized token totals for workflow model calls | not available in sampled logs |

### GH API call summary

| Workflow / run evidence | Observed hotspot | Notes |
|---|---|---|
| `review_autofix` sweep (`25837695872`, plus similar recent sweep runs) | `GET /pulls`, then per-PR `GET /actions/workflows/{wf}/runs`, then dispatch `internal-review.yml` | Highest redundancy risk; best batching candidate |
| `Copilot code review` (`25800810017`, `25805362798`) | `pulls.get`, paginated `pulls.listFiles`, run-artifact lookup | One-shot, not urgent, but avoid duplicate reads if steps share data |
| `orchestrate_poll` | Shared `/actions/runs` cache and batched GraphQL already documented and implemented | This is the repo’s good pattern to reuse elsewhere |

### Token / model telemetry availability

| Metric | Availability in this window |
|---|---|
| Prompt token totals | not emitted |
| Completion token totals | not emitted |
| Total token totals | not emitted |
| Cache creation/read token totals | probes present, but all values `na` |
| Per-model costable usage totals | not derivable from sampled logs |

## Deep Audit — Workflows & Scripts (2026-05-14)

### Section 1: Bug & Correctness Sweep

- **ID**: BUG-001
  - **File path**: `scripts/tg_helpers.sh:296-356,365-427`
  - **Severity**: Medium
  - **Category tag**: `bug`
  - **Description**: `tg_cleanup_phase_msgs` and `tg_cleanup_msgs` page forward through `/issues/{n}/comments` and delete matching comments before incrementing `page`. Because GitHub comment pagination is page-indexed, deleting page-1 items can shift later matching comments onto already-consumed pages; those comments are then skipped when an issue has >100 comments or many TG marker comments.
  - **Recommended fix**: First collect all matching comment IDs across pages, then delete in a second pass; or re-read `page=1` until no matching markers remain. Keep the read side on `curl_gh_api` so failures stay rate-limit aware.

- **ID**: BUG-002
  - **File path**: `.github/workflows/review_autofix.yml:519-529,4420-4426,4542-4547,5294-5301; scripts/review_rb_judge.sh:242-245; .github/workflows/issue_pr_status.yml:200-210`
  - **Severity**: Medium
  - **Category tag**: `bug`
  - **Description**: `issue_pr_status.yml` explicitly tightened PR-text fallback parsing so bare prose mentions like `issue #N` or `issues/N` do **not** count as linked issues after the `#1469` false-match regression, but `review_autofix.yml` and `review_rb_judge.sh` still use the older broad regex. In those paths, an incidental prose reference can still trigger validate dispatches or phase-label mutations on unrelated issues.
  - **Recommended fix**: Replace the broad fallback regexes with one shared strict extractor that matches the `issue_pr_status.yml` policy (`close|fix|resolve` keywords or full repo-scoped issue URLs/paths). Only allow broader parsing behind an explicit opt-in flag and caller-specific comment.

### Section 2: GitHub API Call Redundancy Audit

_Note: I did not duplicate the already-documented `review_autofix_sweep.yml` per-PR `/actions/workflows/*/runs` hotspot from the current report._

- **ID**: BATCH-001
  - **File path**: `scripts/review_rb_judge.sh:235-273`
  - **Severity**: Medium
  - **Category tag**: `api-batching`
  - **Description**: `review_rb_judge.sh` first does 1 GraphQL call to fetch only linked-issue numbers, then loops with per-issue REST reads to recover `body` and `labels` for the first usable issue. Current call count is `1 + N` (`1` GraphQL + up to `N` `GET /issues/{n}` calls; e.g. 6 calls for 5 linked issues). The needed fields are available in GraphQL already.
  - **Recommended fix**: Extend the initial `closingIssuesReferences` query to request `body` and `labels(first: 50) { nodes { name } }`, or reuse the aliased-issue batching pattern from `scripts/orchestrate_poll_process.sh:_fetch_candidate_issue_details_graphql`. Proposed call count: `1`.

- **ID**: API-001
  - **File path**: `scripts/review_rb_judge.sh:210-212,243-245,654-655,723-734,782-785,993-1012`
  - **Severity**: Medium
  - **Category tag**: `api-redundancy`
  - **Description**: The same PR payload is re-fetched multiple times in one judge run: early merged guard, PR-text fallback, post-judge merged guard, merge branch poll, final-fix merge check, and merge-with-followup poll. Fixed pre-branch cost is already 2-3 `GET /pulls/{n}` reads before any action-specific polling starts, and action branches then do another 1-6 reads. This duplicates data that is already partially staged in `PR_META_FILE`.
  - **Recommended fix**: Introduce one cached `PR_LIVE_META_JSON` fetch near the top of the script, refresh it only inside the chosen action’s mergeability poll, and thread that JSON through the guard/fallback branches. Extend the existing `gh_pr_with_all_comments` / `PRELOADED_PR_META` pattern in `scripts/gh_helpers.sh`. Proposed fixed call count: `1` pre-branch `GET /pulls/{n}` instead of `2-3`.

### Section 3: Code Duplication & Modularization Opportunities

- **ID**: DUP-001
  - **File path**: `.github/workflows/review_autofix.yml:519-529,4420-4426,4542-4547,5294-5301; scripts/review_rb_judge.sh:242-245; .github/workflows/issue_pr_status.yml:200-210`
  - **Severity**: Medium
  - **Category tag**: `duplication`
  - **Description**: PR-text linked-issue extraction is duplicated across five call sites, and two different regex policies are now in use. This duplication is what let the stricter `issue_pr_status.yml` fix diverge from `review_autofix.yml` and `review_rb_judge.sh`.
  - **Recommended fix**: Move the logic into `scripts/gh_helpers.sh` or a new `scripts/issue_link_helpers.sh` helper, e.g. `extract_linked_issue_numbers_from_pr_text <repo> <text> [strict=true]`. Update callers in `issue_pr_status.yml`, `review_autofix.yml`, and `review_rb_judge.sh` to use the same helper and policy.

- **ID**: DUP-002
  - **File path**: `.github/workflows/test-and-mark-stable.yml:3369-3435,3545-3580,3608-3645,3667-3704,3779-3814,4015-4057`
  - **Severity**: Medium
  - **Category tag**: `duplication`
  - **Description**: `test-and-mark-stable.yml` repeats the same “dispatch workflow → wait for a new run ID → poll `/actions/runs/{id}` until completion” shell block at least six times. The copies already drift in deadline, accepted conclusions, and error wording, which raises maintenance and audit cost.
  - **Recommended fix**: Extract a shared helper into `scripts/gh_helpers.sh` or a new `scripts/watch_workflow_run.sh`, e.g. `gh_wait_for_dispatched_workflow_run <repo> <workflow_file> <pre_run_id> <deadline_secs> <poll_secs> <accepted_conclusions_csv>`. Update the `workflow-log-analysis`, `validation-refresh`, `update_workflows`, `memory-maintenance`, `orchestrate`, and `internal-validate` smoke steps to call it.

### Section 4: Expression Size Limit Risk Assessment

- **ID**: EXPR-001
  - **File path**: `.github/workflows/orchestrate_clarify_respond.yml:840-1122`
  - **Severity**: Medium
  - **Category tag**: `expression-limit`
  - **Description**: The `Parse and post answer` step has an estimated interpolated `run:` body size of **15,140** characters, leaving about **5,860** characters of headroom against GitHub’s 21,000-character expression limit. The block mixes loop-guard logic, memory writes, label edits, and comment posting in one interpolated shell body. [NEEDS VERIFICATION]
  - **Recommended fix**: Extract the loop-guard / post-answer logic to a dedicated script under `scripts/` and keep the workflow step to env wiring plus one script call.

- **ID**: EXPR-002
  - **File path**: `.github/workflows/review_autofix.yml:893-1125`
  - **Severity**: Medium
  - **Category tag**: `expression-limit`
  - **Description**: The `Stage workflow support files` step is estimated at **15,230** interpolated characters, leaving about **5,770** characters of headroom. It is a long bootstrap block with many `${{ }}` references and file-copy branches, so ordinary rollout edits can push it into the hard limit. [NEEDS VERIFICATION]
  - **Recommended fix**: Move the bootstrap logic into a shared script or composite action, ideally one that `validate.yml` can also reuse.

- **ID**: EXPR-003
  - **File path**: `.github/workflows/review_autofix.yml:1418-1806`
  - **Severity**: High
  - **Category tag**: `expression-limit`
  - **Description**: The `Collect PR metadata` step is estimated at **21,048** interpolated characters, about **48** characters over the nominal 21,000-character ceiling. This is an additional high-risk block beyond the older `review_autofix` expression-limit incidents already noted elsewhere in repo analysis. [NEEDS VERIFICATION]
  - **Recommended fix**: Split the step into smaller phases (`collect_pr_payload`, `collect_comments`, `normalize_context`) or move the whole collector into `scripts/collect_pr_review_context.sh`.

- **ID**: EXPR-004
  - **File path**: `.github/workflows/test-and-mark-stable.yml:1204-1587`
  - **Severity**: High
  - **Category tag**: `expression-limit`
  - **Description**: The `Phase 4: Wait for review & autofix to complete` step is estimated at **23,499** interpolated characters, leaving about **-2,499** characters of headroom. The polling loop, live-log probing, and retry logic are all embedded directly in one interpolated `run:` body. [NEEDS VERIFICATION]
  - **Recommended fix**: Extract the wait loop into a script (or composite action) and pass repo / PR / timeout values as env vars or arguments.

- **ID**: EXPR-005
  - **File path**: `.github/workflows/test-and-mark-stable.yml:1674-2078`
  - **Severity**: High
  - **Category tag**: `expression-limit`
  - **Description**: The `Phase 4b: Verify editor restored canary (pytest + retry)` step is estimated at **21,288** interpolated characters, leaving about **-288** characters of headroom. The step includes install fallback, retry polling, fetch helpers, and pytest classification in one interpolated shell block. [NEEDS VERIFICATION]
  - **Recommended fix**: Move the verify-and-retry flow into a script such as `scripts/verify_e2e_canary.sh`, keeping the workflow step as a thin wrapper.

- **ID**: EXPR-006
  - **File path**: `.github/workflows/validate.yml:205-577`
  - **Severity**: High
  - **Category tag**: `expression-limit`
  - **Description**: The `Fetch workflow support files` step is estimated at **20,816** interpolated characters, leaving only about **184** characters of headroom. This is a near-limit bootstrap block, so even small support-file or env additions can make the workflow unloadable. [NEEDS VERIFICATION]
  - **Recommended fix**: Extract support-file bootstrap to a shared script/composite action and split optional feature fetches (Semble, Serena, self-heal) into separate smaller steps.

_No workflow file exceeds 800 KB. The largest current workflow is `.github/workflows/review_autofix.yml` at 322,997 characters._

### Section 5: Cross-Cutting Concerns

- **ID**: CONSIST-001
  - **File path**: `scripts/tg_helpers.sh:175-205,246-276,346-350,417-421`
  - **Severity**: Low
  - **Category tag**: `consistency`
  - **Description**: `tg_helpers.sh` uses `curl_gh_api` for GitHub **reads**, but its paired POST/PATCH/DELETE writes use raw `curl`. That bypasses the repo’s standard retry/backoff/rate-limit alert path in `scripts/gh_helpers.sh`, so write-side failures are quieter and behave differently from read-side failures.
  - **Recommended fix**: Route GitHub writes through `curl_gh_api` too, or add a small `_curl_gh_json <method> <url> <jq_body>` wrapper in `scripts/tg_helpers.sh` that delegates to `curl_gh_api`.

- **ID**: DEAD-001
  - **File path**: `scripts/ai_context_utils.py:80-207,325-603`
  - **Severity**: Low
  - **Category tag**: `dead-code`
  - **Description**: Multiple top-level envelope / attachment helpers (`build_issue_envelope`, `build_pull_request_envelope`, `build_pull_request_reviews_envelope`, `build_review_comment_local_context`, `build_attachment_bundle_section`, `ingest_attachments`, `build_prompt_parts`, `append_structured_context_to_file`) have no in-repo callers; a repo-wide symbol search only returns their definitions. That leaves a sizeable dormant surface in a production repo.
  - **Recommended fix**: Either wire these helpers into an actual prompt-building path with tests, or remove/archive them until the attachment-context feature is live.

_No actionable TODO/FIXME/HACK markers surfaced in scope; the earlier `XXXXXX` grep hits were `mktemp` templates, not debt markers._

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 4 | EXPR-003, EXPR-004, EXPR-005, EXPR-006 |
| Medium | 8 | BUG-001, BUG-002, BATCH-001, API-001, DUP-001, DUP-002, EXPR-001, EXPR-002 |
| Low | 2 | CONSIST-001, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 2 | Medium |
| Code modularization | 5 | Medium |
| Expression size reduction | 5 | Large |
| Medium/Low fixes | 4 | Medium |
