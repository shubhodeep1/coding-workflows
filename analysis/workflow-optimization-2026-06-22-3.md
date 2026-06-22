## Executive Summary

- **Fix the recurring `review_autofix` contract-test regression first.** The same exit-`127` failure in `test_reviewer_failback_harness_reuses_cached_open_state_and_skips_unmapped_models` hit six `CI` runs (`27922954670`, `27923218357`, `27923226092`, `27923287456`, `27923312145`, `27924497867`) and one `Test & Mark Stable Release` run (`27922954497`), wasting `5,668s` (~`1.6h`) by itself. **Estimated impact:** high. **Confidence:** high.

- **`review_autofix` is the dominant latency sink and no-op waste source.** Full-window stats show `66` `review_autofix` runs with p50 `315.5s`, p95 `3287.5s`, avg `1155.3s`; `15` cancelled/skipped runs still burned `21,158s` (~`5.9h`). Logs show explicit `300s` check-settle waits and runner wait/startup overhead. **Estimated impact:** high. **Confidence:** high.

- **Stable-release editor/canary verification needs an early integrity check.** Run `27926259717` failed after `2419s` in `e2e-smoke-test` → `Phase 4b: Verify editor restored canary (pytest + retry)` because the restored canary still contained bait content instead of the expected 3-line spec. **Estimated impact:** medium-high. **Confidence:** high.

- **Cost tuning is blocked by missing OpenRouter/prompt-cache telemetry, not by excessive model spend.** Across `122` runs with log telemetry there were `137` OR calls but `or_prompt_tokens=0`, `or_completion_tokens=0`, `or_total_tokens=0`, `or_cache_* = 0`, and `cache_hit_rate=null`. Meanwhile Semble logged `51` queries / `500,352` bytes with `101` fallbacks (`97` contract-test), while Serena produced `1` zero-byte query and `3` fallbacks. **Estimated impact:** high for cost governance, medium for immediate dollar savings. **Confidence:** high.

- **AI memory is writing data but not helping retrieval.** Deep-dive logs show `9/9` `retrieve` operations returned `0` records; validate run `27923692562` also hit fail-open `ai-memory` clone errors during `force-tick-get/put`. **Estimated impact:** medium. **Confidence:** high.

## Speed Optimizations

1. **[Critical-path] Early-gate `review_autofix` and cut the `300s` settle wait.**
   - **Evidence:** `review_autofix` had `66` runs full-window with p95 `3287.5s`; even successful outliers reached `3957s` (`27911188250`), `3499s` (`27912850883`), and `3316s` (`27921899203`). Non-success runs still consumed `21,158s`; skipped runs took `882s` (`27924692438`), `725s` (`27927529462`), and `366s` (`27926674718`). `.github/workflows/review_autofix.yml:180-182` sets `CHECK_RUNS_WAIT_TIMEOUT_SECS=300` and `CHECK_RUNS_POLL_INTERVAL_SECS=20`. Run `27911188250` spent the full `300.3s` waiting on check-run settle.
   - **Root cause:** deterministic skip/no-op conditions and check-settle polling happen too late, after runner allocation and after review/autofix orchestration has already started.
   - **Exact change:** move event/PR/no-op gating into a cheap preflight job or job-level `if`, carry forward a single “work required” result, and reduce settle timeout to `120-180s` with early exit as soon as required checks stabilize.
   - **Estimated time savings:** `5-25` minutes on affected `review_autofix` runs; at least `300s` on the long settle outliers.
   - **Implementation risk:** low.

2. **[Critical-path] Block `Test & Mark Stable Release` from re-running known red CI failures.**
   - **Evidence:** the same contract-test failure that broke six `CI` runs later broke stable run `27922954497` after `3871s` in `validate-scripts` → `Unit tests`. The `test_and_mark_stable` family went `2/2` failures with p50 `3145s`.
   - **Root cause:** downstream stable-release work is starting even when the same SHA already has failing contract/unit-test evidence.
   - **Exact change:** require latest same-SHA green `CI` before starting `Test & Mark Stable Release`, or reuse CI unit-test results/artifacts instead of rerunning the same suite.
   - **Estimated time savings:** up to `64.5` minutes per prevented stable run, plus repeated `CI` waste.
   - **Implementation risk:** low.

3. **[Critical-path] Add a canary checksum/byte-compare preflight before Phase 4b.**
   - **Evidence:** stable run `27926259717` failed after `2419s` in `e2e-smoke-test` when `tests/test_e2e_editor_smoke_canary.py` found the restored canary still contained 6-line bait content; final summary reported `Editor Bait. FAILED (retry_timeout)`.
   - **Root cause:** canary integrity is checked too late, after expensive editor/smoke work has already run.
   - **Exact change:** verify the canary byte-for-byte immediately after restore and again before the retry-heavy smoke phase; if mismatched, restore from source-of-truth or fail fast.
   - **Estimated time savings:** `30-40` minutes per bad stable-release run.
   - **Implementation risk:** low.

4. **[Micro] Trim polling-heavy maintenance workflows.**
   - **Evidence:** `Validation Refresh` runs `27922970667`, `27926282612`, and `27927736467` each took about `741-744s`; `orchestrate_poll` had p95 `618s`; `Drift Audit` run `27929740337` spent ~`200s` in `drift-audit/Run drift audit` while repeatedly reporting `log not found`.
   - **Root cause:** fixed polling/log-fetch loops dominate otherwise healthy runs.
   - **Exact change:** early-exit refresh/poll workflows when no new state delta exists, back off after the first idle cycle, and cap repeated missing-log fetch attempts.
   - **Estimated time savings:** `2-5` minutes per maintenance run.
   - **Implementation risk:** low-medium.

## Cost Optimizations

Direct LLM spend is small in this window (`12,156` Codex tokens across `6` calls); the larger immediate cost is GitHub Actions minutes and noisy MCP/context work.

1. **Eliminate avoidable reruns before tuning models.**
   - **Evidence:** six repeated `CI` contract-test failures cost `1797s`; the same defect later cost `3871s` in stable run `27922954497`; stable canary run `27926259717` cost another `2419s`. That is `8087s` (~`2.25h`) of obvious rerun waste before counting cancelled/skipped `review_autofix` time.
   - **Root cause:** known-red changes continue into downstream workflows.
   - **Exact change:** gate stable/review/autofix entry on same-SHA CI state, and cancel superseded `review_autofix` runs earlier.
   - **Estimated savings:** ~`2.25h` of immediate compute in this window, with more upside from reducing `review_autofix` non-success waste.
   - **Quality-risk notes:** low; this removes duplicate work rather than reducing review quality.

2. **Reduce Semble overflow churn and suppress contract-test-only fallbacks.**
   - **Evidence:** full-window Semble totals were `51` queries / `500,352` bytes (~`9.8 KB/query`) and `101` fallbacks, of which `97` (`96.0%`) were contract-test-generated. In deep-dive logs, `reviewer-context` accounted for `22` queries / `321,561` bytes, while `overflow` added `18` queries / `116,880` bytes and `62` fallbacks.
   - **Root cause:** overflow retrieval is noisy, and contract tests are polluting production-facing fallback counts.
   - **Exact change:** memoize one `reviewer-context` snapshot per run, cap repeated `overflow` retries, and disable/mock Semble in contract tests so dashboards reflect production behavior only.
   - **Estimated savings:** likely `20-35%` of Semble byte volume plus major reduction in fallback-log noise.
   - **Quality-risk notes:** low if `reviewer-context` remains enabled.
   - **Semble effectiveness call:** **Inference:** `reviewer-context` is probably preventing larger inline prompt expansion, but OR token telemetry is missing, so net token savings cannot be proven in this window.

3. **Keep Serena off the hot path until it returns useful data.**
   - **Evidence:** full-window Serena totals were `1` query, `0` response bytes, `0` tool calls, `3` fallbacks, and `1` `probe_skipped`. Validate run `27923692562` logged `SERENA_FALLBACK target=validate phase=diagnose reason=disabled` twice.
   - **Root cause:** a disabled/partial rollout is still exercising control-flow branches.
   - **Exact change:** if `SERENA_ENABLED=false` or probe is skipped, bypass Serena entirely and go straight to the existing diagnose path.
   - **Estimated savings:** small direct dollar savings, but clear reduction in dead-end orchestration work and log noise.
   - **Quality-risk notes:** none; the current fail-open path already exists.
   - **Serena effectiveness call:** Serena is **not** replacing downstream tool/model work efficiently in this window.

4. **Fix OR token/cache telemetry before changing model mix or reasoning effort.**
   - **Evidence:** `or_calls=137`, but all OR token fields and prompt-cache fields are zero and `cache_hit_rate=null`. Run `27926608110` logged `XPOLL_SUMMARISER_MODEL: openai/gpt-5.4-mini` with `XPOLL_SUMMARISER_REASONING: medium`.
   - **Root cause:** cost instrumentation gap.
   - **Exact change:** emit per-call OR token/cache metrics plus a cache-hit boolean; after that, A/B the XPOLL summarizer at lower reasoning on unchanged-status summaries. Do **not** downshift the Copilot reviewer yet; run `27927455351` still produced four critical findings in `268s`.
   - **Estimated savings:** unquantified now; this is the prerequisite for evidence-based token reduction.
   - **Quality-risk notes:** low if summary-quality canaries are used.

5. **Stabilize prompt prefixes so future cache reuse is possible and measurable.**
   - **Evidence:** `cache_hit_rate` is absent, `or_cache_write_tokens=0`, `or_cache_read_tokens=0`, and there were no `CONTEXT_BUDGET_WARN` events.
   - **Root cause:** prompt-cache reuse is unobservable; **inference:** volatile run/PR metadata may be entering prompts too early and fragmenting cacheable prefixes.
   - **Exact change:** keep system/rubric text first, move volatile PR/run metadata to the tail, and reuse identical reviewer/editor context blobs across phases.
   - **Estimated savings:** unquantified until telemetry exists.
   - **Quality-risk notes:** low-medium.

## Reliability Improvements

No `BREAK_GLASS` or `CONTEXT_BUDGET_WARN` events were observed in the full window, so the current failures are not pointing to policy-pressure or prompt-size exhaustion.

1. **Fix the exit-`127` review-autofix plumbing contract regression.**
   - **Failure evidence:** `CI` runs `27922954670`, `27923218357`, `27923226092`, `27923287456`, `27923312145`, and `27924497867` all failed in `lint` → `Review autofix review-pipeline plumbing contract test`; stable run `27922954497` failed in `validate-scripts` → `Unit tests`. The failing test was `test_reviewer_failback_harness_reuses_cached_open_state_and_skips_unmapped_models`, raising `CalledProcessError` with exit status `127`.
   - **Root cause category:** contract-test harness / subprocess path regression.
   - **Exact fix:** make the subprocess target explicit and preflight its existence in the test harness; if the unmapped-model path is intentional, mock or stub the missing command instead of shelling out in CI.
   - **Expected reliability impact:** should remove at least `7` observed failures in this window.
   - **Rollback / fail-open:** if releases are blocked, temporarily quarantine only this one contract test rather than weakening the whole suite.

2. **Align the `implement.yml` stall-guard contract across the ref actually under test.**
   - **Failure evidence:** `CI` runs `27917624531`, `27921426217`, `27923341895`, and `27923418606` failed in `lint` → `Orchestrate lib unit tests` because tests reported missing `observed|killed)` in `.github/workflows/implement.yml`. Current checkout `.github/workflows/implement.yml:2092-2261` already contains the required state parsing and `observed` / `killed` handling.
   - **Root cause category:** **Inference:** stale ref / stable-marker drift / golden mismatch, not the current checked-out file contents.
   - **Exact fix:** print the workflow blob SHA in CI/stable jobs, ensure tests read the target ref rather than an older stable copy, and regenerate the golden/contract assertion if the helper shape intentionally changed.
   - **Expected reliability impact:** should remove `4` false-negative CI failures.
   - **Rollback / fail-open:** if urgently needed, let the test accept both spellings only if both map to identical runtime behavior.

3. **Fail fast on editor/canary corruption.**
   - **Failure evidence:** stable run `27926259717` failed after `2419s` because the canary remained corrupted after restore.
   - **Root cause category:** workspace restore / editor cleanup correctness.
   - **Exact fix:** add a byte-for-byte canary assertion immediately after restore and before the expensive smoke retry path.
   - **Expected reliability impact:** turns a 40-minute late failure into an early, diagnosable failure and prevents bad stable marks.
   - **Rollback / fail-open:** keep the assert fail-closed for stable release only; allow debug-only continuation in non-release smoke jobs if needed.

4. **Separate healthy fail-open behavior from broken MCP rollout behavior.**
   - **Failure evidence:** full-window Semble had `101` fallbacks, but only `4` were runtime fallbacks; `97` were contract-test-generated. Deep-dive logs show `overflow` carried `62` fallbacks. Serena had `3` fallbacks and `1` `probe_skipped`; no `probe_failed` events, no useful response bytes, and no tool calls.
   - **Root cause category:** noisy test telemetry masking real runtime signal; disabled Serena rollout still executing control paths.
   - **Exact fix:** alert separately on Semble runtime fallbacks vs contract-test fallbacks; dedupe `overflow` retries; skip Serena entirely when disabled or probe-skipped.
   - **Expected reliability impact:** clearer MCP health signals, fewer masked rollout issues, and fewer needless diagnose branches.
   - **Rollback / fail-open:** preserve current fail-open paths; this is mostly routing and alert hygiene.
   - **Interpretation:** Semble currently looks like mostly healthy rare fail-open behavior with noisy test pollution; Serena looks like a disabled rollout being exercised.

5. **Treat `workflow_log_analysis` runner shutdown as infrastructure and auto-rerun once.**
   - **Failure evidence:** run `27922969841` (`Workflow Log Analysis`) ended with `The runner has received a shutdown signal` / `The operation was canceled.`
   - **Root cause category:** runner/infrastructure interruption.
   - **Exact fix:** auto-rerun once on runner shutdown/cancel for deep-audit jobs, and exclude this class from product-regression tallies unless repeated.
   - **Expected reliability impact:** small, but avoids spending engineering effort on the wrong failure domain.
   - **Rollback / fail-open:** none needed; this is retry classification.

## AI Memory Health

Deep-dive logs do contain `AI_MEMORY_TELEMETRY`, and the current picture is “writes happen, retrievals do not help.”

| Signal | Value | Interpretation |
|---|---:|---|
| `retrieve` ops | 9 | Coverage exists |
| Retrieve hit rate | `0/9` (`0%`) | No sampled retrieval returned records |
| Avg `estimated_tokens` on retrieve | `0` | Retrieval cost estimate is effectively zeroed |
| Retrieval budget field | not surfaced | Budget adherence cannot be measured from sampled logs |
| `keyword_method` distribution | `llm=9`, `plain=0`, `none=0` | All retrievals used LLM keywording |
| `record-run-event` ops | 19 | Writes are occurring |
| `record-candidate` ops | 9 | Candidate creation is occurring |
| `fail_open:true` ops | 4 | All came from validate run `27923692562` `force-tick-get/put` clone failures |
| `enabled:false` ops | 0 observed | Memory was not explicitly disabled |

- **Zero-hit evidence:** slow `review_autofix` run `27927452559` logged a `retrieve` with `records_selected=0`, while later `record-run-event` / `record-candidate` operations still fired. That means memory writes are not yet feeding useful reads.

- **Fail-open evidence:** validate run `27923692562` logged fail-open `force-tick-get` / `force-tick-put` clone errors against `ai-memory`, but the workflow continued and failed for its validation outcome, not for memory itself.

- **Push retry evidence:** most observed memory writes succeeded in one push attempt; additional `log_summary` evidence shows `issue_pr_status` run `27927529477` (`finalize-task`) and `memory_maintenance` run `27926672565` (`compact`) each needed `push_attempts=2`.

- **Recommendation:** repair clone/push robustness first, then re-sample retrieval quality. If hit rate stays near `0%` after the storage path is healthy, stop paying the LLM-keyword retrieval tax on every review/validate path and switch to `plain`/`none` retrieval or sampled retrieval until corpus quality improves.

## GH API Call Audit

No separate repo-specific API hygiene rules were surfaced in the inspected repository files, so this audit is based on observed workflow/script patterns. No explicit GitHub rate-limit errors were visible in the sampled logs, but there is clear avoidable redundancy.

1. **`review_autofix` duplicates a paginated PR-files fetch.**
   - **Evidence:** `.github/workflows/review_autofix.yml:498` and `:555` both call `gh api --paginate "repos/.../pulls/${PR_NUMBER}/files"`.
   - **Problem pattern:** same expensive paginated call repeated inside one workflow.
   - **Recommendation:** fetch once, write to a temp JSON/artifact, and reuse across reviewer/editor phases.
   - **Estimated reduction:** `1` paginated PR-files walk per `review_autofix` run; highest benefit on large PRs.

2. **Reviewer/editor watchdogs re-check PR state independently.**
   - **Evidence:** `scripts/review_run_reviewers.sh:131` does PR-state preflight and `:2789` re-checks PR state in the watchdog loop; `scripts/review_apply_fixes.sh:1577` does a similar editor watchdog check.
   - **Problem pattern:** repeated lookup in loops across long-running review steps.
   - **Recommendation:** share one cached PR JSON + `updatedAt`/ETag across reviewer/editor loops and refresh only when stale.
   - **Estimated reduction:** **Inference:** likely tens of API calls on 30-60 minute `review_autofix` runs, with lower rate-limit risk.

3. **`orchestrate_poll` still has an obvious batching miss.**
   - **Evidence:** `scripts/orchestrate_poll_process.sh:11269-11276` loops over `7` labels and issues one `gh issue list` call per label. The same file contains `97` `gh api` call sites, `5` `gh issue list`, and `5` `gh workflow run` call sites. By contrast, `.github/workflows/orchestrate_poll.yml:155-160` already batches initial discovery with `gh issue list --label ai:orchestrator-tracking --json ...`.
   - **Problem pattern:** unbatched per-label calls in a polling workflow.
   - **Recommendation:** replace the 7-label loop with one batched query plus client-side filtering, extending the already-batched pattern used earlier in the workflow.
   - **Estimated reduction:** up to `6` API calls per poll cycle; **inference:** likely triple-digit monthly reduction at current poll volume.

4. **GraphQL marker fetch should not fall back to REST on empty results.**
   - **Evidence:** `scripts/orchestrate_poll_process.sh:10549-10565` uses GraphQL marker fetch with REST fallback.
   - **Problem pattern:** potential duplicate lookup path.
   - **Recommendation:** cache marker results per poll cycle and only take the REST fallback on permission/schema errors, not empty-result cases.
   - **Estimated reduction:** `1-2` API calls per poll cycle.

5. **`Drift Audit` should stop missing-log fetches sooner.**
   - **Evidence:** run `27929740337` spent about `200s` in `drift-audit/Run drift audit` while repeatedly warning `log not found` for many run IDs.
   - **Problem pattern:** repeated fetch attempts in a likely-empty range.
   - **Recommendation:** cap missing-log retries and stop scanning earlier once a contiguous missing range is reached.
   - **Estimated reduction:** fewer log-fetch calls and `1-3` minutes on affected runs.

## Prompt Cache & Memory System

- **Prompt-cache visibility is currently missing.** Full-window telemetry shows `or_calls=137`, but `or_prompt_tokens=0`, `or_completion_tokens=0`, `or_total_tokens=0`, `or_cache_write_tokens=0`, `or_cache_read_tokens=0`, and `cache_hit_rate=null`. This is almost certainly an observability gap, not proof that prompt caching or token accounting is truly zero.

- **The only cache hit explicitly surfaced in sampled logs was not a prompt-cache hit.** Run `27926608110` logged a `setup-uv` cache hit; that is dependency caching, not model prompt caching.

- **No prompt-growth pressure signals were observed.** `context_budget_warn_count=0` and `break_glass_count=0` full-window. That is good, but it does **not** replace missing OR token/cache telemetry.

- **Semble is the main context-shaping system right now.** Full-window Semble logged `51` queries / `500,352` bytes; deep-dive logs show `reviewer-context` alone used `321,561` bytes. **Inference:** this likely avoids some raw prompt expansion. However, deep-dive `overflow` added `116,880` bytes and `62` fallbacks, so the overflow path looks like low-value context churn.

- **Serena is not helping the prompt/memory stack in its current state.** It logged `1` query, `0` response bytes, `0` tool calls, `3` fallbacks, and `1` `probe_skipped`, so it is not currently replacing downstream tool/model work.

- **Concrete improvements:**
  1. Emit per-call OR token counts, cache read/write tokens, and an explicit cache-hit boolean; warn whenever an OR call records zero metrics.
  2. Freeze the prompt prefix: stable system prompt + rubric + repo policy first, volatile PR/run metadata last.
  3. Build reviewer/editor context once per run and pass it by reference/file path instead of rebuilding prompt variants.
  4. Stop invoking AI-memory retrieval on every path while retrieval hit rate remains `0%`; retry after the storage path is fixed.

- **Estimated impact:** token and latency impact are currently unquantifiable because `cache_hit_rate` is missing, but even a modest real hit rate on `137` OR calls would matter. Reliability impact is clearer: fewer dead-end retrieval/fallback branches.

## Orchestrator Health

- **What looks healthy:** `orchestrate_poll` succeeded in `27/27` full-window runs, with p50 `184s` and p95 `618s`. No `BREAK_GLASS` or `CONTEXT_BUDGET_WARN` events surfaced.

- **What looks unhealthy:** terminal-state detection is late. `review_autofix` no-op/skip runs still spent `366s` (`27926674718`), `725s` (`27927529462`), and `882s` (`27924692438`) before resolving to skip. Run `27911188250` burned a full `300.3s` waiting on check-run settle. Runs `27926608110` and `27929237059` also showed hosted-runner wait/startup overhead before useful work.

- **Late deferral evidence:** skipped run `27927529462` eventually showed `Result: false` in `resolve-claude-branch-pr/system`, `review-claude-branch-push/system`, and `review/system`; the problem is not that the orchestrator failed to decide, but that it decided too late.

- **Evidence gaps:** explicit clarification-loop, wave-progression, deferral-counter, and conflict-heal-retry telemetry did not surface in the sampled logs. Today, dwell time and skip/cancel behavior are the best proxy signals.

- **Smallest safe mitigations:**
  - emit per-phase timestamps (`clarify`, `plan`, `implement`, `review`, `validate`, `poll`, `settle`);
  - count `skipped_after_start` and `settle_wait_seconds`;
  - carry PR/check snapshots between poll cycles instead of refetching everything;
  - lower the maximum settle wait and exit as soon as no delta remains.

- **Indicators to track next:** `review_autofix` cancelled/skip seconds, poll cycles per orchestrator item, check-settle wait seconds, runtime Semble fallbacks, Serena probe skips, and AI-memory retrieve hit rate.

## Pipeline Flow Bottlenecks

| Phase | Evidence | Bottleneck type | Priority fix |
|---|---|---|---|
| Clarify | No dedicated clarify-stage workflow or explicit clarify-loop telemetry surfaced in the sampled window. | Instrumentation gap | Add phase-level timing/loop counters before changing behavior. |
| Plan | Sampled successful `Internal: AI Plan` runs took `239-409s` (`27923013299`, `27926341810`, `27923988530`, `27921624585`, `27917846585`). | Moderate compute | Keep stable; lower priority than review/stable-release fixes. |
| Implement | Sampled successful `Internal: AI Implement` runs took `444-608s` (`27921743869`, `27923104554`, `27926423368`, `27924113434`, `27918002191`). | Moderate compute plus downstream handoff sensitivity | Add canary/stall-guard preflight at handoff, not a major speed target yet. |
| Review / Autofix | Full-window `66` runs, avg `1155.3s`, p95 `3287.5s`; `15` non-success runs consumed `21,158s`; long settle waits and runner waits are visible. | **Primary queueing + polling + late-gating bottleneck** | Earliest gate possible, shorter settle timeout, shared PR/check snapshots. |
| Validate | Success path is reasonable (`229-244s` in `27922968266` and `27926282486`), but failure path stretched to `695s` in `27923692562` with `VALIDATION_STATUS=fail`, two Serena fallbacks, one Semble diagnose query, and AI-memory fail-open clone errors. | Retry/diagnose overhead | Skip disabled Serena, repair memory path, shorten diagnose dead ends. |
| Orchestrate / Poll / Refresh | `orchestrate` successes were `220-246s`; `orchestrate_poll` p95 was `618s`; `validation_refresh` was a near-fixed `~742s`. | Polling overhead | Batch status/API fetches and early-exit on idle/no-delta states. |
| Stable release loop | `test_and_mark_stable` failed twice: `3871s` for a repeated unit-test regression (`27922954497`) and `2419s` for late canary corruption (`27926259717`). | Expensive downstream rerun / late failure detection | Gate on same-SHA green CI and verify canary integrity early. |

- **Queueing overhead:** visible in `review_autofix` runner-wait logs (`27926608110`, `27929237059`).
- **Compute overhead:** concentrated in `review_autofix`, `validation_refresh`, and stable release.
- **Retry overhead:** most visible in stable canary retry flow and validate diagnose path.
- **Merge/conflict overhead:** no direct conflict-heal or merge-resolution evidence surfaced in sampled logs; instrument before optimizing.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Window summary:** `1000` runs; `221` success, `14` failure, `15` cancelled, `750` other/skipped; repo-wide p50 `2.0s`, p95 `695.9s`, avg `136.154s`. The repo is dominated by cheap no-op runs overall, but the long tail is very expensive.

- **Top bottlenecks:**
  1. `review_autofix` latency and no-op waste (`66` runs, p95 `3287.5s`, `21,158s` spent on cancelled/skipped runs).
  2. Stable-release reruns of known failures (`27922954497`, `27926259717`).
  3. Polling-heavy maintenance flows (`validation_refresh`, `orchestrate_poll`, `drift_audit`).

- **Top failure modes:**
  1. Exit-`127` contract-test regression in review/autofix plumbing.
  2. **Inference:** `implement.yml` contract mismatch caused by stale ref/golden drift rather than current file contents.
  3. Late editor/canary corruption detection in stable release.
  4. Disabled Serena and fail-open AI-memory behavior adding diagnose noise.

- **Highest-cost drivers:**
  1. Runner minutes lost to `review_autofix` cancels/skips and long check-settle waits.
  2. Stable-release workflows re-executing already-known failures.
  3. Semble overflow/context churn plus noisy contract-test fallbacks.
  4. Missing OR token/cache telemetry, which blocks precision cost tuning.

- **Top 3 prioritized actions:**
  1. **Fix the exit-`127` contract harness and gate stable release on same-SHA green CI.**
  2. **Move `review_autofix` skip/no-op checks earlier and cut `CHECK_RUNS_WAIT_TIMEOUT_SECS` from `300s` to `120-180s`.**
  3. **Repair OR/prompt-cache telemetry and keep Serena bypassed until it returns useful response bytes/tool calls.**

## Metrics Appendix

**Scope note:** “Full window” below refers to the `1000`-run `workflow_log_report.json` / `analysis_context` window. “Deep-dive subset” refers to inspected logs under `/home/runner/work/_temp/workflow-log-output`; MCP target and AI-memory breakdowns come from that subset only.

### Repo summary

| Repo | Runs | Success | Failure | Cancelled | Other/Skipped | Success rate | Failure rate | p50 dur (s) | p95 dur (s) | Avg dur (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 221 | 14 | 15 | 750 | 22.1% | 1.4% | 2.0 | 695.9 | 136.154 |

### Key workflow-family timings

| Workflow family | Runs | Outcome mix | Success / failure rate | p50 dur (s) | p95 dur (s) |
|---|---:|---|---|---:|---:|
| review_autofix | 66 | 51 success / 11 cancelled / 4 skipped | 77.3% success / 0% failure | 315.5 | 3287.5 |
| ci | 21 | 10 failures / 11 others | 47.6% failure | 1485.0 | 1768.0 |
| test_and_mark_stable | 2 | 2 failures | 100% failure | 3145.0 | 3798.4 |
| validate | 3 | 2 success / 1 failure | 66.7% success / 33.3% failure | 244.0 | 649.9 |
| workflow_log_analysis | 2 | 1 success / 1 failure | 50.0% success / 50.0% failure | 1259.5 | 1483.1 |
| orchestrate_poll | 27 | 27 success | 100% success | 184.0 | 618.0 |
| validation_refresh | 3 | 3 success | 100% success | ~742.0 | ~744.0 |

**Observed sampled stage durations (not full-family aggregates):**
- `plan`: `239-409s`
- `implement`: `444-608s`
- `orchestrate`: `220-246s`

### Full-window cost / cache / MCP telemetry

| Metric | Value | Notes |
|---|---:|---|
| Runs with log telemetry | 122 | subset of 1000 runs |
| `codex_tokens_used` | 12156 | small versus runner-minute waste |
| `codex_calls` | 6 |  |
| `or_calls` | 137 | telemetry present for calls, not for token/cache totals |
| `or_prompt_tokens / completion / total` | 0 / 0 / 0 | instrumentation gap |
| `or_cache_write_tokens / read_tokens` | 0 / 0 | prompt-cache instrumentation gap |
| `cache_hit_rate` | null | no prompt-cache visibility |
| `semble_query_calls / bytes` | 51 / 500352 | avg ~9.8 KB per query |
| `semble_fallbacks` | 101 | 97 contract-test (96.0%), 4 runtime (4.0%) |
| `serena_query_calls / response_bytes / tool_calls` | 1 / 0 / 0 | ineffective or zero-value rollout |
| `serena_fallbacks` | 3 | mostly disabled-path noise |
| `serena_probe_ok / failed / skipped` | 0 / 0 / 1 | skipped probe, not a hard probe failure |
| `break_glass_count` | 0 | no policy-override events observed |
| `context_budget_warn_count` | 0 | no prompt-budget warning events observed |
| `wall_clock_p50_ms / p99_ms` | 10000 / 3796600 | telemetry subset only (`121` samples) |

### GH API hotspot summary (static inspection + log evidence)

| Area | Static CLI call density | Specific hotspot | Estimated reduction |
|---|---|---|---|
| `.github/workflows/review_autofix.yml` | 29 `gh api`, 6 `gh workflow run` call sites | duplicate paginated PR-files fetch at lines 498 and 555 | `-1` paginated walk per `review_autofix` run |
| `scripts/review_run_reviewers.sh` + `scripts/review_apply_fixes.sh` | repeated PR-state lookups in long-running loops | PR-state checks at lines 131 / 2789 and 1577 | **Inference:** tens of calls saved on long runs by shared cache |
| `scripts/orchestrate_poll_process.sh` | 97 `gh api`, 5 `gh issue list`, 5 `gh workflow run` call sites | 7-label `gh issue list` loop at lines 11269-11276 | up to `-6` calls per poll cycle |
| `scripts/orchestrate_poll_process.sh` | same | GraphQL marker fetch with REST fallback at lines 10549-10565 | `-1` to `-2` calls per poll cycle |
| `Drift Audit` run `27929740337` | runtime evidence, not static count | repeated `log not found` fetches | fewer fetches + `1-3` min saved on affected runs |

### Deep-dive Semble target breakdown

| Server | Target | Queries | Logged bytes | Fallbacks | Contract-test fallbacks | Runtime fallbacks |
|---|---|---:|---:|---:|---:|---:|
| Semble | reviewer-context | 22 | 321561 | 0 | 0 | 0 |
| Semble | overflow | 18 | 116880 | 62 | 57 | 5 |
| Semble | validate-diagnose-context | 1 | 3729 | 0 | 0 | 0 |
| Semble | `<none>` | 6 | 0 | 5 | 0 | 5 |

### Deep-dive Serena target / availability breakdown

| Server | Target | Queries | Response bytes | Tool calls | Fallbacks | Probe ok | Probe failed | Probe skipped |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Serena | validate | 0 | 0 | 0 | 2 | 0 | 0 | 0 |
| Serena | `<none>` | 1 | 0 | 0 | 1 | 0 | 0 | 1 |

**Per-tool Serena breakdown:** none recorded (`serena_query_tool_calls=0`).

**Other MCP servers observed:** none.

## Deep Audit — Workflows & Scripts (2026-06-22)

### Section 1: Bug & Correctness Sweep
Scope audited: `40` workflows under `.github/workflows/` and `100` repo scripts under `scripts/`. No reportable secret-leak or shell-injection path surfaced in this pass.

#### BUG-001
- **ID** — BUG-001
- **File path** — `.github/workflows/test-and-mark-stable.yml:3831-3833,3946-3953`
- **Severity** — High
- **Category tag** — bug
- **Description** — This step enables `set -euo pipefail`, then computes `CHILD_COUNT=$(echo "${CHILDREN}" | tr ',' '\n' | grep -c .)`. When the first poll sees no child issues yet—the exact eventual-consistency case the surrounding comments describe—`grep -c .` prints `0` but exits `1`, so the assignment aborts the step before the intended 90-second retry loop can continue. That turns a designed retry path into a false stable-release failure.
- **Recommended fix** — Count children with a non-failing primitive (`jq 'length'` on `CHILDREN_JSON`, `awk 'NF{c++} END{print c+0}'`, or `grep -c . || true`) and keep the loop condition on the numeric result. Add a regression test for “0 children on first poll, >0 later”.

#### BUG-002
- **ID** — BUG-002
- **File path** — `.github/workflows/orchestrate_poll.yml:155-160`; `scripts/orchestrate_poll_process.sh:12833-12840`
- **Severity** — Medium
- **Category tag** — bug
- **Description** — The poll workflow says “Find all open issues” but fetches `ai:orchestrator-tracking` issues with `--limit 20`, writes that truncated list to `tracking_issues.json`, and the processor iterates only that cached file. **Inference:** once the repo has more than `20` open tracking issues, later issues are silently skipped for the full poll cycle.
- **Recommended fix** — Page the discovery step (`--limit 1000` at minimum, or `gh api --paginate`) and keep the full JSON list as script input. If a cap is intentional, fail loudly when the cap is hit so operators see starvation instead of silent omission.

### Section 2: GitHub API Call Redundancy Audit
This section omits the `review_autofix` and `orchestrate_poll` hotspots already documented in the current report and focuses on additional code-level candidates.

#### API-001
- **ID** — API-001
- **File path** — `.github/workflows/test-and-mark-stable.yml:2933-2941`
- **Severity** — Medium
- **Category tag** — api-redundancy
- **Description** — The cancel-on-close waiter polls `repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}` twice per iteration: once for `.status` and again for `.conclusion`. Both fields come from the same payload.
- **Current call count** — `2` GETs per 5-second poll tick; up to `240` GETs over the `600s` budget.
- **Proposed call count after fix** — `1` GET per tick; up to `120` GETs over the same budget.
- **Pattern to extend** — None required from `gh_helpers.sh`/`orchestrate_poll_process.sh`; this is a same-endpoint collapse. Reuse a single JSON fetch plus local `jq`, as this workflow already does elsewhere.
- **Recommended fix** — Fetch the run JSON once per iteration and extract both fields locally. Use the same single-fetch parse style already present in this workflow at `.github/workflows/test-and-mark-stable.yml:618-629` and `:881-886`.

#### BATCH-001
- **ID** — BATCH-001
- **File path** — `.github/workflows/test-and-mark-stable.yml:4403-4405,4479-4490`
- **Severity** — Medium
- **Category tag** — api-batching
- **Description** — The alt-model smoke path snapshots phase runs by looping over `clarify`, `plan`, `implement`, and `review_autofix`, then repeats the same workflow-run listing after issue creation. That is eight separate list-runs calls for one smoke cycle.
- **Current call count** — `8` REST list calls (`4` pre + `4` post).
- **Proposed call count after fix** — `2` repo-wide `actions/runs` snapshots (`1` pre + `1` post), then client-side filtering by workflow name/branch.
- **Pattern to extend** — `scripts/orchestrate_poll_process.sh:8384-8533` (`_load_actions_runs_cached`) or `scripts/gh_helpers.sh:1171-1188` (`autofix_retrigger_has_inflight_peer`), both of which already rely on a single repo-wide `actions/runs` fetch.
- **Recommended fix** — Replace the per-workflow loop with repo-wide snapshots and filter in `jq`. If `per_page=100` is too small for this repo’s volume, page once more or add the same `created=>...` lower bound already used elsewhere in this workflow.

#### BATCH-002
- **ID** — BATCH-002
- **File path** — `scripts/gh_helpers.sh:916-932`
- **Severity** — Low
- **Category tag** — api-batching
- **Description** — `_gh_issue_timeline_with_cross_refs_rest()` fetches a paginated issue timeline and then performs one extra `gh api` call per cross-referenced PR URL to enrich state. On this fallback path, expensive issues scale as `1` paginated timeline walk `+ N` PR GETs instead of a bounded batch. [NEEDS VERIFICATION]
- **Current call count** — `1` paginated timeline fetch `+ N` PR GETs, where `N` is the number of linked PRs.
- **Proposed call count after fix** — `1` total call if the GraphQL path is extended far enough, or `2` max (timeline + batched PR-state lookup).
- **Pattern to extend** — `scripts/orchestrate_poll_process.sh:10744-10822` (`_fetch_linked_pr_status_graphql`).
- **Recommended fix** — Keep the existing GraphQL-first path, but when fallback is unavoidable, batch PR enrichment instead of walking URLs one-by-one.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **ID** — DUP-001
- **File path** — `.github/workflows/workflow-log-analysis.yml:242-280,879-920,1355-1395`
- **Severity** — Medium
- **Category tag** — duplication
- **Description** — Three jobs repeat the same Codex CLI tool-cache persist/restore and config-generation sequence. The copies already diverge slightly in comments/env handling, which raises drift risk the next time the cache layout or bootstrap rules change.
- **Recommended fix** — Extract this to a shared module.
  - **Shared module** — `scripts/setup_codex_cli.sh`
  - **Suggested signature** — `setup_codex_cli.sh --tool-cache-dir <dir> --model <model> --thinking-level <level> --mode auto`
  - **Callers to update** — the three `workflow-log-analysis.yml` jobs that currently embed the duplicate Codex cache/config block

#### DUP-002
- **ID** — DUP-002
- **File path** — `scripts/validate_driver.sh:212-296`; `scripts/validate_process.sh:1180-1232`
- **Severity** — Medium
- **Category tag** — duplication
- **Description** — `append_failure()` and `emit_result()` are duplicated in two validation entrypoints, but already drift: one version captures compose logs and propagates `${PHASE}`, while the other hardcodes `"runtime_validation"` and truncates logs differently. Future fixes can easily land in one path and miss the other.
- **Recommended fix** — Extract the result/failure serialization into a shared helper.
  - **Shared module** — `scripts/validate_result_helpers.sh`
  - **Suggested signature** — `append_failure <failures_file> <test> <error> [log_file] [mode]`; `emit_validate_result <result> <phase> <failures_file> <total> <passed> <failed> <start_ts>`
  - **Callers to update** — `scripts/validate_driver.sh`, `scripts/validate_process.sh`

#### DUP-003
- **ID** — DUP-003
- **File path** — `.github/workflows/mark-stable.yml:312-338`; `.github/workflows/test-and-mark-stable.yml:3442-3468`
- **Severity** — Low
- **Category tag** — duplication
- **Description** — The stable-release and stable-test workflows carry an identical “verify every `scripts/*` reference in workflows exists” shell loop, including the same optional-script allowlist and missing-file accounting.
- **Recommended fix** — Extract the integrity check into one script.
  - **Shared module** — `scripts/check_workflow_script_refs.sh`
  - **Suggested signature** — `check_workflow_script_refs.sh --workflow-root <dir> [--optional <path>]...`
  - **Callers to update** — `.github/workflows/mark-stable.yml`, `.github/workflows/test-and-mark-stable.yml`

### Section 4: Expression Size Limit Risk Assessment
No reportable expression-size findings.

Across all `40` workflows, no `run:` or `if:` block containing `${{ }}` crossed the `15,000`-character reporting threshold, and no workflow exceeded `800 KB`. The largest workflow files were:
- `review_autofix.yml` — `342,496` bytes
- `test-and-mark-stable.yml` — `280,742`
- `implement.yml` — `270,190`

The largest interpolated `run:` bodies measured in this audit were:
- `implement.yml` — `12,608` characters (`2,392` below the 15k medium-risk threshold; `8,392` below the 21k hard limit)
- `plan.yml` — `11,340`
- `workflow-log-analysis.yml` — `9,499`

These are worth watching, but none are currently at the prompt’s Medium/High risk thresholds.

### Section 5: Cross-Cutting Concerns

#### DEAD-001
- **ID** — DEAD-001
- **File path** — `scripts/orchestrate_poll_process.sh:9313-9319`
- **Severity** — Low
- **Category tag** — dead-code
- **Description** — `read_standalone_state_json()` is defined but not referenced elsewhere in the repo. It also duplicates a full paginated issue-comments fetch, so keeping it around increases maintenance surface and leaves a stale API path available for accidental reuse.
- **Recommended fix** — Remove the function if obsolete, or add a real caller plus test coverage if it is intended to stay. If retained, share its comments-fetch path with `get_standalone_state_comment_id()` instead of maintaining a second wrapper.

#### CONSIST-001
- **ID** — CONSIST-001
- **File path** — `.github/workflows/test-and-mark-stable.yml:503-508,618-629,838-846,2917-2941,3904-3907,3947-3950,4403-4490`
- **Severity** — Medium
- **Category tag** — consistency
- **Description** — The same workflow already uses `gh_api_safe_print` for some run-discovery paths, but later polling/discovery blocks fall back to raw `gh api`. That leaves one release-gate workflow with two different retry/error-handling models: wrapped calls get standardized stderr handling and safer JSON parsing, while raw calls do fixed-interval polling with ad hoc parsing.
- **Recommended fix** — Standardize the stable workflow on one API access layer. Concretely, lift the raw run/issue polling blocks onto `gh_api_safe_print`/`gh_api_safe_quiet_print` (or a tiny local wrapper built on them) so retry/backoff, stderr capture, and JSON validation behave consistently across the file.

No `TODO`, `FIXME`, or `HACK` markers were present under `.github/workflows/` or `scripts/` in this pass.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 6 | BUG-002, API-001, BATCH-001, DUP-001, DUP-002, CONSIST-001 |
| Low | 3 | BATCH-002, DUP-003, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 1 workflow | Small |
| API call optimization | 1 workflow, 1 script | Medium |
| Code modularization | 3 workflows, 2 scripts, plus 2 new shared helpers | Medium |
| Expression size reduction | 0 | Small |
| Medium/Low fixes | 3 workflows/scripts | Small |

## API Call Consolidation & Dead-Call Analysis (2026-06-22)

### Safety Tag Legend
`SAFE_TO_MERGE` means the consolidation is proven equivalent on endpoint/filter/error-handling scope and can be implemented directly. `NEEDS_VERIFICATION` means the overlap is real but a human or follow-on analysis must verify caller coverage, freshness, or behavior before changing it. `RISKY_SKIP` means the redundancy is visible, but the call sits in a pagination/race-recovery/cancellation-sensitive path and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

#### MERGE-001
- **ID** — MERGE-001
- **Safety tag** — RISKY_SKIP
- **File path and line ranges** — `.github/workflows/cancel_on_pr_close.yml:102-132`
- **Current call count** — `2` paginated GETs per run
- **Proposed call count** — `1` paginated GET per run
- **Endpoint(s)** — `GET /repos/{repo}/actions/runs`
- **Evidence** — The same branch/event-scoped endpoint is fetched twice, differing only by `status`:
  ```bash
  queued_runs_json="$(
    gh_retry gh api \
      --method GET \
      "repos/${REPOSITORY}/actions/runs" \
      --paginate \
      -f status=queued \
      -f event=pull_request \
      -f "branch=${PR_HEAD_REF}" \
      -f per_page=100 \
  ...
  in_progress_runs_json="$(
    gh_retry gh api \
      --method GET \
      "repos/${REPOSITORY}/actions/runs" \
      --paginate \
      -f status=in_progress \
      -f event=pull_request \
      -f "branch=${PR_HEAD_REF}" \
      -f per_page=100 \
  ```
- **Proposed fix** — If manually approved, replace the paired status-specific fetches with one branch/event-scoped `actions/runs` fetch, then filter `queued|in_progress` client-side before building `target_run_ids`.
- **Safety rationale** — `RISKY_SKIP` because both calls use `--paginate`; collapsing server-side status filters into one paginated query can change page-boundary coverage for the cancel set.
- **Downstream signal** — Do not auto-implement; manually validate on a branch with `>100` matching active runs and confirm the merged query preserves both the cancel set and current log output.

#### MERGE-002
- **ID** — MERGE-002
- **Safety tag** — RISKY_SKIP
- **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:1014-1054`
- **Current call count** — `3` GETs on the first-stable-attempt path; up to `11` if all `5` stability attempts run
- **Proposed call count** — `2` on the first-stable-attempt path; up to `10` with `5` attempts
- **Endpoint(s)** — `GET /repos/{repo}/pulls/{pr}`
- **Evidence** — The step double-reads the PR head SHA for race protection, then immediately fetches the full PR again for the closed/merged guard:
  ```bash
  HEAD_A=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' ...)
  sleep 3
  HEAD_B=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' ...)
  ...
  PR_META=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" 2>/dev/null || echo "")
  PR_STATE=$(printf '%s' "${PR_META}" | jq -r '.state // ""' ...)
  PR_MERGED=$(printf '%s' "${PR_META}" | jq -r '.merged // false' ...)
  ```
  The surrounding comments explicitly call out stale-parent and auto-merge races.
- **Proposed fix** — If optimized manually, make the second stability read fetch a small JSON object (`head.sha`, `state`, `merged`, `merged_at`, `closed_at`) and reuse it for the immediate closed/merged guard.
- **Safety rationale** — `RISKY_SKIP` because this is an explicit upstream-race defense path; removing or merging reads changes the protection contract.
- **Downstream signal** — Do not auto-implement; any change must prove it still prevents stale-parent `PUT /contents` conflicts and still catches PR auto-merge between stability polling and bait injection.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001
- **ID** — REUSE-001
- **Safety tag** — NEEDS_VERIFICATION
- **File path and line ranges** — `.github/workflows/review_autofix.yml:289-302,780-782,4706-4715`; `scripts/review_enable_auto_merge.sh:65-74,127-137`
- **Current call count** — `2` API calls per `review_enable_auto_merge.sh` invocation
- **Proposed call count** — `1` on the common path; `2` only when a head-ref suppressor still requires a live body cross-check
- **Endpoint(s)** — `GET /repos/{repo}/issues/{pr}/labels?per_page=100`; `GET /repos/{repo}/pulls/{pr}`
- **Evidence** — The gate job already fetches and outputs PR head ref from `/pulls/{n}`:
  ```bash
  if _pr_gate="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" \
    --jq '{..., head_ref: (.head.ref // ""), labels: ((.labels // []) | map(.name)), ..., title: (.title // ""), body: (.body // "")}'
  ...
  echo "head_ref=${pr_head_ref}" >> "${GITHUB_OUTPUT}"
  ```
  But the later auto-merge step does not consume that output:
  ```bash
  env:
    GH_TOKEN: ${{ secrets.GH_PAT }}
    ENABLE_AUTO_MERGE: ${{ vars.ENABLE_AUTO_MERGE || 'true' }}
    FORWARD_MERGE_FALLBACK_AUTO_MERGE: ...
    ORCH_INTEGRATION_BRANCH_PATTERN: ...
  run: |
    bash "${SUPPORT_SCRIPTS_DIR}/review_enable_auto_merge.sh"
  ```
  and the script re-fetches PR metadata:
  ```bash
  if ! _ORCH_PR_META_JSON="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" ... )"; then
  ...
  _orch_pr_head_ref="$(printf '%s' "${_ORCH_PR_META_JSON}" | jq -r '.head.ref // ""' ...)"
  ```
- **Proposed fix** — Pass `PR_HEAD_REF: ${{ needs.gate.outputs.head_ref }}` into the auto-merge step and update `scripts/review_enable_auto_merge.sh` to skip `GET /pulls/{pr}` unless `PR_HEAD_REF` matches the forward-merge or orchestrator-integration suppressors and the body cross-check is still needed.
- **Safety rationale** — `NEEDS_VERIFICATION` because the reuse crosses jobs and the late-step suppressor decision can still depend on fresh PR/body/label state.
- **Downstream signal** — Verify whether head-ref/body-based suppressor inputs can change after the gate job starts; if yes, reuse only `PR_HEAD_REF` and keep live body/label fetches.

#### REUSE-002
- **ID** — REUSE-002
- **Safety tag** — NEEDS_VERIFICATION
- **File path and line ranges** — `.github/workflows/internal-review.yml:91-116,137-140`
- **Current call count** — `1` fallback GET when `EVENT_DEFAULT_BRANCH` is empty
- **Proposed call count** — `0`
- **Endpoint(s)** — `GET /repos/{repo}`
- **Evidence** — The workflow already injects the default branch from the event payload, then re-fetches the same value only if that env is empty:
  ```bash
  EVENT_DEFAULT_BRANCH: ${{ github.event.repository.default_branch || '' }}
  ...
  base_ref="${EVENT_DEFAULT_BRANCH:-}"
  if [ -z "${base_ref}" ]; then
    base_ref="$(gh api "repos/${REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo 'main')"
  fi
  ```
  The resolved `base_ref` is then forwarded directly into the no-PR review call:
  ```bash
  force_claude_branch_review: true
  head_ref_override: ${{ needs.resolve-claude-branch-pr.outputs.head_ref }}
  head_sha_override: ${{ needs.resolve-claude-branch-pr.outputs.head_sha }}
  base_ref_override: ${{ needs.resolve-claude-branch-pr.outputs.base_ref }}
  ```
- **Proposed fix** — Trust `github.event.repository.default_branch` as the sole source for this workflow, keep forwarding it via `base_ref_override`, and drop the repo lookup fallback.
- **Safety rationale** — `NEEDS_VERIFICATION` because this is only safe if every repo-local trigger path into `internal-review.yml` always includes `github.event.repository.default_branch`.
- **Downstream signal** — Verify one real payload for each trigger path into `internal-review.yml` and confirm `github.event.repository.default_branch` is never empty before removing the fallback GET.

### Dead Calls (DEAD-API-###)

#### DEAD-API-001
- **ID** — DEAD-API-001
- **Safety tag** — NEEDS_VERIFICATION
- **File path and line ranges** — `scripts/review_collect_pr_metadata.sh:176-181`; supporting caller path `.github/workflows/internal-review.yml:129-140`; input surface `.github/workflows/review_autofix.yml:40-69,77-90,1183-1185`
- **Current call count** — `1` dead fallback call site (`0` executions on repo-local workflow paths)
- **Proposed call count** — `0` call sites
- **Endpoint(s)** — `GET /repos/{repo}`
- **Evidence** — The fallback exists only in no-PR claude-branch mode:
  ```bash
  BASE_REF_OVERRIDE="${BASE_REF_OVERRIDE_INPUT:-}"
  if [ -z "${BASE_REF_OVERRIDE}" ]; then
    BASE_REF_OVERRIDE="$(gh api "repos/${REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo 'main')"
  fi
  ```
  But, in this repository:
  1. no-PR mode is exposed only on `workflow_call` inputs, not on `workflow_dispatch`, and
  2. the only repo-local caller (`internal-review.yml`) always passes `base_ref_override`.
- **Proposed fix** — Remove the `BASE_REF_OVERRIDE="" -> gh api repos/${REPOSITORY}` fallback from `scripts/review_collect_pr_metadata.sh` for repo-local no-PR mode, or replace it with an explicit hard error telling callers to supply `base_ref_override`.
- **Safety rationale** — `NEEDS_VERIFICATION` because the fallback is dead for current repo workflows but could still matter for tests, manual script runs, or future callers.
- **Downstream signal** — Verify there are no tests, wrapper workflows, or operator runbooks that invoke no-PR review mode without `base_ref_override` before deleting the fallback.

### Cross-References to Deep Audit Section
- API-001: SAFE_TO_MERGE — same `actions/runs/{id}` payload is polled twice in one loop and can supply both `.status` and `.conclusion`.
- BATCH-001: NEEDS_VERIFICATION — repo-wide run snapshots are promising, but page-boundary and workflow-name filtering behavior must be checked before replacing per-workflow list calls.
- BATCH-002: RISKY_SKIP — the fallback path is paginated and fail-open; batching changes here need manual review of page-boundary and fallback semantics.

### Summary Counts
| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 3 | REUSE-001, REUSE-002, DEAD-API-001 |
| RISKY_SKIP | 2 | MERGE-001, MERGE-002 |

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.
