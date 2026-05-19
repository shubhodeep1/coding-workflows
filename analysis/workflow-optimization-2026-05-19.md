## Executive Summary

- The repo-wide `p50=2s` is misleading because `681/1000` runs were skipped/other; the real critical path is `review_autofix` (`130` runs, `avg=1001.2s`, `p95=3017.8s`). In slow run `26080864487` (`Internal: AI Review & Autofix`), `review / codex-agent` took `4083s`, with `Run reviewer models` at `1509.7s`, `Collect PR check-run failures` at `1200.6s`, and `Apply fixes with editor model` at `1126.2s`. **Estimated impact:** cut slow PR latency by `10–30+ min`. **Confidence:** high.
- The biggest low-risk speed win is the check-run wait loop in `.github/workflows/review_autofix.yml:151-170,1887-2012`. Run `26080864487` logged `60` wait iterations and timed out after `1200s`; other successful runs `26044573064`, `26046485259`, and `26048685679` hit the same timeout path at `120s`. **Estimated impact:** save up to `18 min` on worst-case reviews and materially reduce GH API churn. **Confidence:** high.
- All sampled `ci` failures were deterministic and concentrated in one step: `lint / Orchestrate poll process unit tests` on runs `26039161815`, `26040817342`, `26042958244`, `26045097971`, and `26073356397`. That cluster alone burned `3269s` of wall time, and `ci` failure rate is `8.1%` (`5/62`). **Estimated impact:** remove the current red-build cluster and recover ~`55 min` of wasted CI time in-window. **Confidence:** high.
- The deterministic small-diff skip in `review_autofix` is likely broken (**inference**). Runs `26044573064`, `26046485259`, `26048685679`, and `26080864487` logged `additions=3/5/5/8` with `deletions=?`, `small_diff=false`, `skip=false`; the parsing logic in `.github/workflows/review_autofix.yml:257-258,412-415` plus a local reproduction of the same TSV/read pattern shows field shifting when labels are empty. **Estimated impact:** skip whole reviewer/editor cycles on eligible tiny PRs. **Confidence:** medium.
- Semble looks net-positive, but overflow retrieval is noisy. In the 5 slow `review_autofix` deep dives, normalized Semble telemetry showed `13` queries / `117,564` bytes / about `6.5s` total query time; `66,784` bytes were core `reviewer-context`, but `50,780` bytes (`43.2%`) were overflow. Serena did not participate operationally; sampled review runs logged `SERENA_ENABLED: false`, and no operational `SERENA_QUERY/FALLBACK/PROBE` lines were observed. **Estimated impact:** medium cost win by tightening overflow only. **Confidence:** high.
- AI memory and prompt-cache plumbing exist but are not yet proving value. Strict parsing found deduped AI memory retrieval hit rate at `0/5`; prompt cache is enabled (`OPENROUTER_PROMPT_CACHE_DISABLED: false`), but no trustworthy runtime cache-hit counters were emitted. **Estimated impact:** medium cost/relevance gain once retrieval and cache observability are fixed. **Confidence:** high.

## Speed Optimizations

1. **Critical path: enforce the short/backoff check-run wait path everywhere**
   - **Evidence:** Run `26080864487` (`review / codex-agent / Collect PR check-run failures (CI/lint autofix context)`) spent `1200.6s` polling and ended with `CHECK_RUNS_WAIT_TIMEOUT reached after 1200s`; the same step logged `60` wait messages. Runs `26044573064`, `26046485259`, and `26048685679` hit the same timeout branch after `120s`. Source: `.github/workflows/review_autofix.yml:151-170,1887-2012`.
   - **Root cause:** commit-level `check-runs` polling waits on long or unrelated checks before reviewers/editor can proceed.
   - **Exact change:** make the observed shorter setting (`120s` timeout / `30s` interval, seen in runtime logs such as `26083592225`) the enforced default across branches; add exponential backoff; stop waiting once only unrelated long-running checks remain; keep the existing fail-open snapshot behavior.
   - **Estimated time savings:** up to `1080s` on a worst-case run like `26080864487`; typically `90–120s` on the shorter-timeout runs.
   - **Implementation risk:** low.

2. **Critical path: fix the deterministic small-diff skip parser (**inference**)**
   - **Evidence:** `AUTOFIX_GATE_DET_SKIP_EVAL` logged `additions=3 deletions=?` on run `26044573064`, `additions=5 deletions=?` on `26046485259` and `26048685679`, and `additions=8 deletions=?` on `26080864487`, while thresholds were `max_add=10 max_del=10` and `small_diff=false`. Source logic is `.github/workflows/review_autofix.yml:257-258,412-415`. Local reproduction of the same `@tsv` + `read` pattern yielded `pr_labels=8`, `pr_additions=0`, `pr_deletions=` when labels were empty.
   - **Root cause:** TSV parsing appears to shift fields when the labels column is empty.
   - **Exact change:** parse the PR payload as JSON fields instead of `@tsv` + shell `read`; keep the existing `force-review` override.
   - **Estimated time savings:** avoid an entire `review_autofix` cycle on eligible tiny PRs — often `12–68 min` based on observed run durations.
   - **Implementation risk:** low-medium, because the hypothesis is strong but still an **inference** until fixed in production.

3. **Critical path: make reviewer/editor effort adaptive by diff size and risk**
   - **Evidence:** Slow run `26080864487` spent `1509.7s` in `Run reviewer models`; run `26048685679` repeatedly logged `REVIEWERS_SUCCESSFUL: 6`. The environment in sampled review runs shows `6` reviewer models, `ENABLE_REVIEWER_TWO_PASS: true`, `REVIEWER_REASONING_EFFORT: xhigh`, and `EDITOR_REASONING_EFFORT: xhigh`. By contrast, `Internal: AI Review Autofix Sweep` averaged only `8.8s` over `19` runs, so the latency is in model work, not wrapper overhead.
   - **Root cause:** full 6-reviewer, two-pass, xhigh reasoning is being used even on low-change PRs.
   - **Exact change:** keep the current full panel for risky diffs, conflict resolution, or failing check-run context; for PRs under a small file/LOC threshold, cap pass-2 to `2–3` reviewers and lower pass-2/editor reasoning to `medium` or `high`.
   - **Estimated time savings:** roughly `5–12 min` per small/medium review run.
   - **Implementation risk:** medium.

4. **Critical path: preflight consolidator inputs and bypass zero-output paths**
   - **Evidence:** `stage=consolidator` in runs `26044573064`, `26046485259`, and `26047394132` consumed the full `300s` with `output_bytes=0 failopen=1`; run `26080864487` failed open immediately because `review-consolidator.txt` was missing, followed by `stage=parser event=no_issue_markers failopen=1`. Source metrics emission is in `.github/workflows/review_autofix.yml:5944-6015`.
   - **Root cause:** expensive consolidator calls are still attempted when required prompt assets are missing or when the input bundle is unlikely to produce useful output.
   - **Exact change:** hard-preflight `review-consolidator.txt`; skip consolidator when missing; add an input-size/timeout guard that drops straight to parser fallback when the reviewer bundle is too large or previous attempts returned `output_bytes=0`.
   - **Estimated time savings:** up to `300s` on affected runs.
   - **Implementation risk:** low.

5. **Failure-fast, not green-path: shard or reorder `test_orchestrate_poll_process.py`**
   - **Evidence:** All 5 failing CI runs died in `ci / lint / Orchestrate poll process unit tests`; first failure appeared after about `377.6–650.4s`, and totals were `192 passed, 1 failed, 193 total` or `197 passed, 1 failed, 198 total`. Source invocation is `.github/workflows/ci.yml:133-136`.
   - **Root cause:** a single long, high-churn test file is deep in the CI job, so regressions surface late.
   - **Exact change:** split the high-risk contract tests into smaller entry points or move the most failure-prone subsets to an earlier/parallel CI step.
   - **Estimated time savings:** `4–10 min` on failing CI runs; little green-path impact unless parallelized.
   - **Implementation risk:** low-medium.

6. **Micro-optimization: remove the extra default-branch API call from the fast skip path**
   - **Evidence:** Recent run `26083764871` finished in `6s`, and its `resolve-claude-branch-pr` step dominated runtime while making two `gh api` calls: open PR lookup plus repo default branch lookup. Source: `.github/workflows/internal-review.yml:98-101`.
   - **Root cause:** the quick path fetches repo metadata even though `github.event.repository.default_branch` is already in the event payload.
   - **Exact change:** use the event payload default branch and keep only the open-PR lookup.
   - **Estimated time savings:** `1–3s` per quick-path skip run.
   - **Implementation risk:** low.

## Cost Optimizations

> **Note:** operational `prompt_tokens` / `completion_tokens` / cache-read counters were not emitted in sampled review/CI logs, so savings below use observed model calls, logged bytes, and runner-minute proxies.

1. **Fix the small-diff deterministic skip bug (**inference**)**
   - **Evidence:** same as Speed item 2; runs `26044573064`, `26046485259`, `26048685679`, and `26080864487` appear eligible for the `10/10` size gate but still ran full review.
   - **Root cause:** broken size-gate parsing prevents cheap PRs from taking the no-review path.
   - **Exact change:** JSON-parse PR fields; preserve `force-review` escape hatch.
   - **Estimated savings:** one full reviewer/editor/consolidator cycle per eligible small PR; this is the highest token/dollar opportunity in the sampled window.
   - **Quality-risk notes:** low, because the workflow already has explicit override markers and a doc-only fallback.

2. **Tier reviewer count and reasoning effort**
   - **Evidence:** sampled slow review runs use `6` reviewer models, `ENABLE_REVIEWER_TWO_PASS: true`, and `xhigh` reasoning; run `26080864487` spent `1509.7s` in reviewer models before editor time.
   - **Root cause:** expensive model settings are applied broadly, not only on high-risk diffs.
   - **Exact change:** define small/medium/large review tiers; for small tiers, use fewer reviewers and lower pass-2/editor reasoning; keep full settings for merge-conflict, stale-base, or failing-check contexts.
   - **Estimated savings:** high; likely the largest recurring model-cost lever after the skip bug.
   - **Quality-risk notes:** medium; mitigate by keeping full-panel rules for risky branches and when reviewer disagreement is high.

3. **Stop paying for 300-second consolidator calls that fail open**
   - **Evidence:** runs `26044573064`, `26046485259`, and `26047394132` each spent `300s` in `stage=consolidator` with `output_bytes=0 failopen=1`; run `26080864487` showed `missing=review-consolidator.txt`.
   - **Root cause:** the expensive consolidator path is attempted even when inputs are missing or already degraded.
   - **Exact change:** preflight prompt availability; bypass consolidator on missing prompt or empty reviewer bundle; add a hard cap that falls through directly to parser fallback.
   - **Estimated savings:** medium-high; eliminates repeated `openai/gpt-5.4` work that returns nothing.
   - **Quality-risk notes:** low, because the parser fail-open path already exists.

4. **Keep Semble, but tighten overflow retrieval**
   - **Evidence:** normalized Semble review telemetry across 5 slow `review_autofix` runs showed `13` query events totaling `117,564` bytes. `reviewer-context` accounted for `66,784` bytes, while overflow accounted for `50,780` bytes (`43.2%`), with the biggest overflow target being `tests/test_orchestrate_poll_process.py` (`19,369` bytes). Average Semble query time was only about `0.5s`; total added query time was ~`6.5s`.
   - **Root cause:** Semble is doing useful bounded retrieval, but overflow is still pulling large low-value context.
   - **Exact change:** keep `reviewer-context` queries; only allow overflow for changed files or files referenced by multiple reviewers/check-run failures; cap overflow bytes per file more aggressively.
   - **Estimated savings:** medium token-context savings with negligible latency downside.
   - **Quality-risk notes:** low-medium. Semble appears to be reducing prompt expansion overall; the noisy part is overflow, not the core query path. Serena is not replacing any downstream work yet — no operational `SERENA_QUERY/FALLBACK/PROBE` events were seen, and sampled runs logged `SERENA_ENABLED: false`.

5. **Instrument prompt-cache effectiveness before tuning model prompts further**
   - **Evidence:** `.github/workflows/review_autofix.yml:1435-1446` explicitly pre-assembles a stable static prefix “so the LLM provider can cache this stable prefix across runs,” and slow review runs log `OPENROUTER_PROMPT_CACHE_DISABLED: false`. But no trustworthy runtime `cache_creation_input_tokens` or `cache_read_input_tokens` counters were emitted in sampled operational logs.
   - **Root cause:** good cache-aware prompt structure exists, but effectiveness is opaque.
   - **Exact change:** emit cache create/read counters plus a stable prefix hash; keep volatile artifacts (check-run snapshots, overflow file pulls, reviewer bundles) strictly after the cached prefix.
   - **Estimated savings:** unknown but plausibly medium on repeated review runs.
   - **Quality-risk notes:** low.

6. **Cut avoidable rerun waste in `review_autofix`**
   - **Evidence:** `23` cancelled `review_autofix` runs consumed `30,809s` total (`avg=1339.5s`, `17.7%` of family runs, `23.7%` of family wall time).
   - **Root cause:** superseded/cancelled review work is reaching expensive stages before stopping; exact initiator is not visible in the sampled logs.
   - **Exact change:** add an early superseded-run check before reviewer/editor stages and emit a cancellation-cause marker when a run is terminated externally.
   - **Estimated savings:** medium in both runner minutes and some model spend.
   - **Quality-risk notes:** low if manual rerun and fail-open behavior remain intact.

7. **Low-priority cost note: `workflow_log_analysis` summarization is real but rare**
   - **Evidence:** run `26039903791` used `144,087` `gpt-5.4-mini` tokens and run `26045043369` used `183,891` for `summarize_unselected_runs` (`327,978` combined).
   - **Root cause:** the audit workflow deliberately expands coverage to up to `100` unselected runs.
   - **Exact change:** only trim `WORKFLOW_LOG_SUMMARY_MAX_RUNS` if audit frequency rises or token budget becomes tight.
   - **Estimated savings:** low in the current window because the workflow ran only twice.
   - **Quality-risk notes:** medium if coverage is reduced too aggressively.

## Reliability Improvements

1. **Repair the deterministic CI regression cluster**
   - **Failure evidence:** runs `26039161815`, `26040817342`, `26042958244`, and `26045097971` all failed `test_verify_integration_fingerprints_baseline_regressions`; run `26073356397` failed `test_resolver_tooling_refresh_skips_files_ahead_of_default_branch`. All 5 failures occurred in `ci / lint / Orchestrate poll process unit tests`.
   - **Root cause category:** code/test regression, not infra.
   - **Exact fix:** fix the underlying orchestrate-poll logic/fixtures covered by `tests/test_orchestrate_poll_process.py` (notably the baseline-regression and resolver-tooling-refresh contracts), then keep the test coverage intact. Source locations already under test include `tests/test_orchestrate_poll_process.py:6433-6448`, `8463-8495`, and `8747-8788`.
   - **Expected reliability impact:** should remove the entire observed `ci` failure cluster (`5/62`, `8.1%`).
   - **Rollback/fail-open:** if a hotfix is needed, revert the offending behavior change; do not remove the whole suite. `orchestrate_poll` workflows themselves were `24/24` successful in-window, so the suite is still valuable.

2. **Harden the consolidator fail-open path**
   - **Failure evidence:** runs `26044573064`, `26046485259`, and `26047394132` spent the full `300s` and failed open with zero consolidator output; run `26080864487` failed open because the consolidator prompt file was missing, and parser fallback found no issue markers.
   - **Root cause category:** missing asset / model-timeout degraded mode.
   - **Exact fix:** validate prompt assets before invocation; if missing, skip directly to parser fallback with a distinct reason code; if output is zero, mark the run degraded and bypass any second attempt.
   - **Expected reliability impact:** fewer silent degraded reviews and fewer long “succeeded but low-signal” runs.
   - **Rollback/fail-open:** keep the current parser fail-open behavior as the safety net.

3. **Instrument cancellation cause before changing concurrency semantics**
   - **Failure evidence:** `23` `review_autofix` runs were cancelled, consuming `30,809s`. At the same time, `.github/workflows/review_autofix.yml:730-754` explicitly sets `cancel-in-progress` only for no-PR `claude/**` review-comment mode, not normal PR-backed review runs.
   - **Root cause category:** external cancellation / concurrency churn (**inference**).
   - **Exact fix:** emit a lightweight cancellation-cause marker from the wrapper workflow and add an early “am I superseded?” exit before reviewer/editor stages.
   - **Expected reliability impact:** lower rerun churn and clearer diagnosis of why PR-backed review runs are being terminated.
   - **Rollback/fail-open:** low-risk; diagnostics and early exit can fail open to today’s behavior.

4. **Treat Semble fallbacks as expected test-fixture behavior, not rollout breakage**
   - **Failure evidence:** exactly `10` operational `SEMBLE_FALLBACK` lines were found, all in successful `test_and_mark_stable` runs `26039859964` and `26045005085`, all in `validate-scripts`, all `target=overflow`, all with `reason=missing_semble`, all `ms=0`.
   - **Root cause category:** healthy fixture fail-open.
   - **Exact fix:** suppress or label these as expected in test telemetry; do not page on them.
   - **Expected reliability impact:** better alert fidelity.
   - **Rollback/fail-open:** none needed; this is already safe behavior.

5. **Do not treat Serena as broken in this window**
   - **Failure evidence:** no operational `SERENA_FALLBACK` or `SERENA_PROBE` events were observed; sampled review runs such as `26080864487` logged `SERENA_ENABLED: false`.
   - **Root cause category:** rollout-disabled, not availability failure.
   - **Exact fix:** keep `SERENA_ENABLED: false` as the default until rollout resumes; when re-enabled, emit explicit probe outcome lines so availability failures can be distinguished from runtime fallbacks.
   - **Expected reliability impact:** avoids chasing a nonexistent outage.
   - **Rollback/fail-open:** current disabled default already is the smallest safe mitigation.

## AI Memory Health

- I used strict JSON parsing and deduped combined-log/substep duplicates by `(run_id, payload)`. That produced `22` deduped operational AI-memory events (`20` from `review_autofix`, `2` from `workflow_log_analysis`); raw valid-JSON line count was `26`.
- **Retrieve effectiveness is currently zero.** Deduped `retrieve` events: `5`. Hits (`records_selected > 0`): `0`. **Hit rate:** `0%`. **Average `estimated_tokens`:** `0.0`. **`keyword_method` distribution:** `none=5`, `plain=0`, `llm=0`. Evidence runs: `26048685679`, `26044573064`, `26080864487`, `26047394132`, `26046485259`.
- **No degraded retrieve behavior was observed.** `fail_open: true` on retrieves: `0`. `enabled: false` on retrieves: `0`. This looks more like an ineffective retrieval path than a flaky service.
- **Write-side health is mostly fine.** Deduped `record-candidate` events: `5`; only run `26048685679` needed `push_attempts: 2`, all others were `1`.
- **The only trustworthy token telemetry in-window came from memory summarization, not prompt-cache stats.** `workflow_log_analysis` run `26039903791` logged `summarized=77`, `targeted=100`, `tokens_used=144087`; run `26045043369` logged `summarized=96`, `targeted=100`, `tokens_used=183891`. Combined: `327,978` `gpt-5.4-mini` tokens.
- **Recommendation:** fix retrieval before increasing memory volume. Concretely: add a `plain` keyword fallback when `keyword_method` would otherwise be `none`, emit an alert when rolling retrieve hit rate stays below `10%`, and log retrieval budget vs selected-token estimate so “0-hit/0-token” patterns are immediately visible.

## GH API Call Audit

1. **`review_autofix` check-run polling is the dominant API hotspot**
   - **Evidence:** `.github/workflows/review_autofix.yml:1962-2011` polls `repos/.../commits/${HEAD_SHA}/check-runs?per_page=100` in a loop. In run `26080864487`, `review / codex-agent / Collect PR check-run failures` logged `60` wait iterations before timing out after `1200s`; that implies roughly `61` commit-check-run polls for one step (**inference**: 60 waits + terminal poll).
   - **Missed batching/reuse:** the step re-fetches the full commit check-run list every interval instead of narrowing to known run IDs or backing off.
   - **Concrete change:** use the shorter timeout universally, add backoff, and stop polling once only irrelevant long-running checks remain.
   - **Estimated reduction:** `50–90%` fewer calls on slow review runs.
   - **Rate-limit risk reduction:** high. No production 429s were observed, but this is the clearest latent risk.

2. **`test_and_mark_stable` repeats the same dispatch/watch loop in many places**
   - **Evidence:** the same PRE → dispatch → NEW_ID → JSON watch pattern appears at `.github/workflows/test-and-mark-stable.yml:3374-3437`, `3550-3585`, `3613-3645`, `3672-3708`, `3784-3810`, `4020-4061`, `4128-4133`, and `4379-4465`. Deep-dive run `26045005085` shows repeated `actions/workflows/.../runs` lookups and repeated `actions/runs/${RID}` status polling across multiple steps.
   - **Missed batching/reuse:** each test reimplements its own watcher instead of calling a shared helper with common backoff.
   - **Concrete change:** extract one shell/Python helper for “dispatch and wait for workflow run”, with adaptive polling and uniform error semantics.
   - **Estimated reduction:** dozens of Actions API calls per long self-test run.
   - **Rate-limit risk reduction:** medium.

3. **The fast skip path in `internal-review.yml` pays for a second API call it does not need**
   - **Evidence:** `.github/workflows/internal-review.yml:98-101`; recent run `26083764871` logged that `gh api` was used twice in `resolve-claude-branch-pr` and that this step dominated a `6s` run.
   - **Missed batching/reuse:** default branch is already in the event payload.
   - **Concrete change:** use `github.event.repository.default_branch` and keep only the open-PR lookup.
   - **Estimated reduction:** `1` API call per quick-path skip run.
   - **Rate-limit risk reduction:** low but free.

4. **Review gate metadata fetching is reasonable but still worth tightening**
   - **Evidence:** `.github/workflows/review_autofix.yml` fetches PR state/additions/deletions, commit metadata, conditional `/files`, and linked issues. Comments already show awareness of API hygiene, especially around skipping `/files` unless needed.
   - **Missed batching/reuse:** some PR metadata is still re-fetched in later stages rather than passed forward explicitly.
   - **Concrete change:** reuse already-fetched PR payloads and linked-issue state wherever possible; keep `/files` strictly conditional.
   - **Estimated reduction:** modest.
   - **Rate-limit risk reduction:** low-medium.

- **Observed rate-limit incidents:** none. The main issue is redundancy, not active throttling. Keep the existing `gh_retry` fail-open wrappers.

## Prompt Cache & Memory System

- **Prompt-cache design is directionally good.** `.github/workflows/review_autofix.yml:1435-1446` pre-assembles a stable static prefix explicitly so the provider can cache it across runs, and slow review runs such as `26080864487` logged `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
- **But effectiveness is unmeasurable today.** No trustworthy operational `cache_creation_input_tokens` or `cache_read_input_tokens` counters were emitted in sampled review/CI logs; those strings only appeared inside generated `workflow_log_analysis` text, which is not runtime telemetry.
- **Likely cache-fragmentation causes (**inference**):** dynamic check-run snapshots, overflow Semble file pulls, reviewer bundles, and run-specific temp paths. The static prefix step is correct; the remaining work is to keep every volatile artifact after that prefix and avoid needless prompt variance.
- **The review-ledger cache is effectively prefix-restore-only by design.** Restore/save keys in `.github/workflows/review_autofix.yml:3392-3400,3608-3614` include `github.run_id` and `github.run_attempt`, so exact-key cross-run reuse cannot happen. Example: run `26080864487` missed key `review-ledger-...-26080864487-1`.
- **Memory retrieval effectiveness is currently the bigger problem than cache logic.** Until retrieve hit rate rises above `0%`, adding more memory writes is unlikely to improve prompts.
- **Recommended changes:** emit provider cache create/read counters plus a prompt-prefix hash; log exact-hit vs prefix-restore vs miss for the review-ledger cache; keep volatile Semble overflow and check-run context at the very end of prompts; and fix retrieval before expanding memory scope.
- **Estimated impact:** tokens = medium, latency = low-medium, reliability = medium.

## Orchestrator Health

- **Front-door phase gating looks healthy.** Recent runs `26084017147` (`clarify`), `26084017137` (`plan`), `26084017148` (`implement`), and `26084017149` (`orchestrate_clarify_respond`) all skipped in `1–2s` because their `if` conditions evaluated false. Family skip-heavy ratios are high but appear intentional: `clarify` other/skipped `94.7%`, `plan` `95.4%`, `implement` `93.1%`, `orchestrate_clarify_respond` `98.8%`.
- **No orchestrator runtime failure cluster was visible in this window.** `orchestrate_poll` was `24/24` successful (`avg=101.1s`), and `orchestrate` was `2/2` successful (`avg=243.5s`).
- **Data gap:** no explicit `ORCHESTRATOR_STATE_V2` or stall-state telemetry appeared in the sampled deep-dive logs, so clarification loops, deferrals, and conflict-heal retries are not directly measurable here.
- **The main orchestrator pain point is downstream handoff latency, not front-door orchestration logic.** Example: run `26079204283` spent `2027s` in `review_autofix`; run `26080835104` spent `2210s`, including runner waits.
- **Track these indicators next:** median `implement → review_autofix` completion time, `review_autofix` cancellation rate, count of check-run wait timeouts, CI failed-step concentration, and the ratio of intentionally skipped child workflows vs unexpected skips.

## Pipeline Flow Bottlenecks

1. **Clarify → plan → implement is not the bottleneck**
   - `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` all have `p50≈2s`, and recent runs were mostly intentional skips.

2. **Review/autofix is the dominant compute bottleneck**
   - `review_autofix` is `avg=1001.2s`, `p50=634.5s`, `p95=3017.8s`.
   - Run `26080864487` shows the dominant components clearly: `1200.6s` check-run wait + `1509.7s` reviewer models + `1126.2s` editor.

3. **Queueing amplifies the review bottleneck**
   - In the deep-dive set, `18/30` runs waited for a hosted runner. By family: `review_autofix 7/8`, `ci 5/5`, `workflow_log_analysis 2/2`, `test_and_mark_stable 2/2`.
   - Because `review_autofix` has separate `gate` and `codex-agent` jobs, every unnecessary codex-agent run can also pay a second queue penalty.

4. **Retry/cancellation overhead is materially large**
   - `23` cancelled `review_autofix` runs consumed `30,809s`; even without a confirmed root cause, that is enough waste to justify early supersession checks and cancellation-cause logging.

5. **Validation/audit loops are long but rare**
   - `workflow_log_analysis` averaged `4058s` over `2` runs.
   - `test_and_mark_stable` averaged `4203s` over `2` runs.
   - These matter for CI capacity, but they are lower end-to-end priority than `review_autofix` and `ci`.

**Ordered fix sequence for end-to-end impact:**  
1) check-run wait loop, 2) small-diff skip parser, 3) adaptive reviewer/editor effort, 4) consolidator preflight, 5) CI failure-fast sharding, 6) shared GH watcher helper for self-tests.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` runtime and variance (`130` runs, `p95=3017.8s`)
  - `ci` late-surfacing orchestrate-poll test failures (`5/62`)
  - Rare but expensive audit/self-test workflows (`workflow_log_analysis`, `test_and_mark_stable`)

- **Top failure modes**
  - Deterministic `tests/test_orchestrate_poll_process.py` regressions in `ci`
  - `review_autofix` cancellation waste (`23` cancellations, `30,809s`)
  - Consolidator zero-output / missing-prompt fail-open paths

- **Highest-cost drivers**
  - 6-reviewer, two-pass, `xhigh` review panel
  - Expensive consolidator calls that sometimes return no output
  - Semble overflow context on large files
  - Long GH API polling windows for check-runs
  - Rare but real `workflow_log_analysis` summarization tokens (`327,978` in 2 runs)

- **Top 3 prioritized actions**
  1. Make the short/backoff check-run polling path universal and keep the long `1200s` path manual-only.
  2. Replace the TSV/read small-diff gate parsing with JSON field extraction.
  3. Repair the failing orchestrate-poll CI contracts and run the highest-risk subset earlier.

## Metrics Appendix

| Scope | Runs | Success | Failure | Cancel | Other* | Fail % | Cancel % | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 287 | 5 | 27 | 681 | 0.5 | 2.7 | 210.1 | 2.0 | 1138.2 |
| `ci` | 62 | 57 | 5 | 0 | 0 | 8.1 | 0.0 | 758.5 | 772.5 | 807.9 |
| `review_autofix` | 130 | 103 | 0 | 23 | 4 | 0.0 | 17.7 | 1001.2 | 634.5 | 3017.8 |
| `orchestrate_poll` | 24 | 24 | 0 | 0 | 0 | 0.0 | 0.0 | 101.1 | 61.5 | 238.2 |
| `workflow_log_analysis` | 2 | 2 | 0 | 0 | 0 | 0.0 | 0.0 | 4058.0 | 4058.0 | 4755.5 |
| `test_and_mark_stable` | 2 | 2 | 0 | 0 | 0 | 0.0 | 0.0 | 4203.0 | 4203.0 | 4864.5 |
| `clarify` | 188 | 10 | 0 | 0 | 178 | 0.0 | 0.0 | 8.4 | 2.0 | 59.1 |
| `plan` | 173 | 8 | 0 | 0 | 165 | 0.0 | 0.0 | 7.5 | 2.0 | 10.4 |
| `implement` | 173 | 8 | 0 | 4 | 161 | 0.0 | 2.3 | 14.5 | 2.0 | 21.0 |
| `orchestrate_clarify_respond` | 173 | 2 | 0 | 0 | 171 | 0.0 | 0.0 | 2.1 | 2.0 | 5.0 |

\* `Other` is dominated by intentional skipped runs.

| `review_autofix` workflow name | Runs | Success | Cancel | Other | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|
| `Internal: AI Review & Autofix` | 89 | 67 | 18 | 4 | 1095.9 | 738.0 | 3355.0 |
| `Codex PR Self-Healing Semantic Agent` | 22 | 17 | 5 | 0 | 1475.0 | 1893.0 | 2561.2 |
| `Internal: AI Review Autofix Sweep` | 19 | 19 | 0 | 0 | 8.8 | 8.0 | 13.1 |

| AI memory metric | Value |
|---|---|
| Raw valid JSON telemetry lines | 26 |
| Deduped operational events (`run_id + payload`) | 22 |
| Deduped `review_autofix` events | 20 |
| Deduped `workflow_log_analysis` events | 2 |
| Deduped `retrieve` events | 5 |
| Retrieve hit rate | `0/5 = 0%` |
| Avg `estimated_tokens` per retrieve | `0.0` |
| `keyword_method` distribution | `none=5`, `plain=0`, `llm=0` |
| Retrieve `fail_open=true` | 0 |
| Retrieve `enabled=false` | 0 |
| `record-candidate` with `push_attempts > 1` | 1 (`run 26048685679`) |

| Token telemetry (trustworthy in-window) | Model | Targeted | Summarized | Skipped empty | Tokens used |
|---|---|---:|---:|---:|---:|
| `workflow_log_analysis` run `26039903791` | `openai/gpt-5.4-mini` | 100 | 77 | 23 | 144,087 |
| `workflow_log_analysis` run `26045043369` | `openai/gpt-5.4-mini` | 100 | 96 | 4 | 183,891 |
| **Combined** | — | 200 | 173 | 27 | **327,978** |
| `review_autofix` / `ci` prompt+cache counters | — | — | — | — | **not emitted operationally** |

| Prompt/cache signal | Observed value | Notes |
|---|---|---|
| `OPENROUTER_PROMPT_CACHE_DISABLED` | `false` in sampled slow review runs | prompt caching is enabled |
| `cache_creation_input_tokens` | not observed operationally | only seen in generated analysis text |
| `cache_read_input_tokens` | not observed operationally | cache hit rate unmeasurable |
| Review-ledger restore example | miss on run `26080864487` | restore key `review-ledger-...-26080864487-1` not found |
| Review-ledger key design | includes `run_id` + `run_attempt` | exact-key cross-run reuse impossible by design |

| MCP / server telemetry | Query count | Fallback count | Probe count | Logged bytes | Response bytes | Notes |
|---|---:|---:|---:|---:|---:|---|
| Semble — slow `review_autofix` runs (normalized) | 13 | 0 | 0 | 117,564 | 0 | `reviewer-context=66,784` bytes; `overflow=50,780` bytes |
| Semble — `test_and_mark_stable` | 0 | 10 | 0 | 0 | 0 | all fallbacks were expected `missing_semble` fixture paths in successful tests |
| Semble — `workflow_log_analysis` self-analysis | 13 | 0 | 0 | 149,194 | 0 | self-analysis traffic; keep separate from production reviewer runs |
| Serena — operational sampled logs | 0 | 0 | 0 | 0 | 0 | no operational telemetry observed; sampled runs show `SERENA_ENABLED:false` |
| Other MCP servers observed | 0 | 0 | 0 | 0 | 0 | none observed |

| MCP availability telemetry (observed probes only) | Target | probe_ok | probe_failed | probe_skipped | Note |
|---|---|---:|---:|---:|---|
| Semble | n/a | 0 | 0 | 0 | no operational probe lines observed |
| Serena | n/a | 0 | 0 | 0 | no operational probe lines observed |
| Other MCP servers | n/a | 0 | 0 | 0 | none observed |

| GH API hotspot | Evidence run(s) | Pattern | Observed call signal |
|---|---|---|---|
| `review_autofix` check-run polling | `26080864487` | commit check-runs poll loop | `60` wait iterations; about `61` poll calls in one step (**inference**) |
| `review_autofix` shorter timeout path | `26044573064`, `26046485259`, `26048685679` | same poll loop | timed out after `120s` instead of `1200s` |
| `internal-review` quick skip path | `26083764871`, `26083579359` | two `gh api` calls in `resolve-claude-branch-pr` | dominated `6–10s` runs |
| `test_and_mark_stable` watcher duplication | `26045005085` | repeated PRE/NEW_ID/JSON watch loops | repeated Actions API polling across many test steps |
| Rate-limit incidents | sampled deep-dive logs | 429 / secondary rate | **none observed operationally** |

| Deep-dive runner wait metric | Value |
|---|---:|
| Deep-dive runs inspected (`errors` + `slow` + `recent`) | 30 |
| Runs that waited for a hosted runner | 18 |
| `review_autofix` deep dives with runner wait | 7 / 8 |
| `ci` deep dives with runner wait | 5 / 5 |
| `workflow_log_analysis` deep dives with runner wait | 2 / 2 |
| `test_and_mark_stable` deep dives with runner wait | 2 / 2 |

**Normalization notes:**  
- Semble query counts were normalized to remove duplicate emission across combined job logs and step-specific logs.  
- AI memory counts were deduped by `run_id + payload` for the same reason.
