## Executive Summary

- `review_autofix` is the dominant bottleneck: 174 runs consumed 61.3h, or 70.0% of operational runtime. In run `26175029695`, `codex-agent / Collect PR check-run failures` took **865.7s** and `codex-agent / Run reviewer models` took **508.8s**. **Estimated impact:** 15-30% faster full review runs if fixed. **Confidence:** High.
- Long cancellation waste is concentrated in `Internal: AI Review & Autofix`: 42 of 50 `review_autofix` cancels, and all 15 long cancels (`>=1200s`), were this workflow, burning **9.78h** of runtime; runs `26170953180` and `26173251165` are representative. **Estimated impact:** major rerun/cancel reduction and ~8-10h less wasted runtime per sampled window. **Confidence:** High.
- CI is serialized around one oversized step: run `26176563280` spent **800.9s of 1008.9s** in `lint / Orchestrate_poll_process_unit_tests`, with the rest of lint, ShellCheck, and contract tests queued behind it. **Estimated impact:** ~3-4 minutes off CI wall time per run by splitting jobs. **Confidence:** High.
- `test_and_mark_stable` is operationally opaque: **7 of 8** runs failed, all with `duration_seconds=0` and missing log archives (`404` on runs `26145595163`, `26145606096`, `26146100910`, `26147010589`, `26147743293`, `26148050482`, `26149097679`). **Estimated impact:** high debuggability gain and lower blind rerun rate. **Confidence:** High on the observability gap, Medium on the hidden underlying workflow fault.
- Review-time model spend is inflated by universal two-pass fan-out and very large editor prompts: all **8 sampled full review runs** executed **6 reviewers in pass 1 + 6 in pass 2**; editor prompts were **251,072-352,737 bytes** (avg **317,706**). **Estimated impact:** 30-50% reviewer/editor token reduction on low-risk PRs. **Confidence:** Medium because operational token counters were not emitted.
- AI memory and cache systems are under-instrumented and underperforming: **9/9** `retrieve` operations returned **0 records**, `estimated_tokens=0`, `keyword_method=none`, and no prompt-cache hit/miss counters were emitted anywhere. **Estimated impact:** medium cost/latency gain after telemetry and retrieval gating fixes. **Confidence:** High.

## Speed Optimizations

1. **[Critical path] Stop blocking `review_autofix` on long check-run polling.**
   - **Evidence:** Run `26175029695` spent **865.7s** in `codex-agent / Collect PR check-run failures (CI/lint autofix context)` and logged **43** wait iterations before producing just **336 bytes** of context. Across 7 loop-heavy deep-dive `review_autofix` runs (`26145607995`, `26154599231`, `26170059805`, `26146860961`, `26166840517`, `26162540427`, `26175029695`), wait spans were **553.3s-1182.7s** (avg **828.6s**, median **809.0s**).
   - **Root cause:** fixed 20-second polling waits for external CI completion even though the step is explicitly fail-open.
   - **Exact change:** reduce `CHECK_RUNS_WAIT_TIMEOUT_SECS` for `review_autofix` from `1200` to `300`, or switch to adaptive polling with an early snapshot once only one check remains or after ~10 polls.
   - **Estimated time savings:** ~4-14 minutes on affected review runs.
   - **Implementation risk:** Low.

2. **[Critical path] Gate pass-2 reviewer fan-out on consensus/risk.**
   - **Evidence:** Run `26175029695` spent **508.8s** in `codex-agent / Run reviewer models`; that step ran **6 reviewers in pass 1** and **6 in pass 2** even though the log said the diff was **7 LOC**. The same `6 + 6` pattern appeared in all **8 sampled full review runs**.
   - **Root cause:** unconditional two-pass review on small or low-risk diffs.
   - **Exact change:** keep pass 1 universal, but skip pass 2 when pass-1 consensus is strong and there is no failing CI context or merge-conflict signal; alternatively trim pass 2 to 2-3 reviewers or lower `REVIEWER_PASS2_REASONING_SMALL`.
   - **Estimated time savings:** ~4-8 minutes per full review run.
   - **Implementation risk:** Medium.

3. **[Critical path] Split the CI mega-job so `Orchestrate_poll_process_unit_tests` no longer serializes the rest of lint.**
   - **Evidence:** CI run `26176563280` lasted **1008.9s**; `lint / Orchestrate_poll_process_unit_tests` alone took **800.9s**. Other notable steps were `ShellCheck static analysis` (**52.4s**), `Integration-ahead-by gate regression tests` (**48.0s**), and `Targeted file context contract tests` (**30.4s**).
   - **Root cause:** one long unit-test file monopolizes a single job while unrelated checks wait.
   - **Exact change:** move `tests/test_orchestrate_poll_process.py` into its own job or matrix shard; keep ShellCheck/actionlint/contract tests in parallel jobs.
   - **Estimated time savings:** ~3-4 minutes per CI run.
   - **Implementation risk:** Medium.

4. **[Critical path on cancel-heavy paths] Detect conflict-heavy forward merges before reviewer/editor work starts.**
   - **Evidence:** All **15** long canceled `review_autofix` runs (`>=1200s`, **9.64h** total) were `Internal: AI Review & Autofix`. Run `26170953180` was canceled after **2471s** and its summary listed conflicts in `.github/workflows/*.yml`, `README.md`, `scripts/orchestrate_poll_process.sh`, `scripts/review_apply_fixes.sh`, `scripts/review_rb_judge.sh`, and tests. Run `26173251165` was canceled after **1936s** after a long gate wait.
   - **Root cause:** conflict-heavy maintenance PRs are detected too late.
   - **Exact change:** add an early `git merge-tree` / merge-conflict probe for forward-merge PRs before `Run reviewer models`, and short-circuit to deterministic conflict handling or a fail-open comment if the merge is already dirty.
   - **Estimated time savings:** ~20-40 minutes on doomed runs.
   - **Implementation risk:** Medium.

5. **[Micro] Remove duplicated `tests/test_orchestrate_integration_ahead_by_gate.py` coverage from `test-and-mark-stable`.**
   - **Evidence:** `.github/workflows/test-and-mark-stable.yml` runs the same test file at lines `2121` and `3233`.
   - **Root cause:** duplicate serial execution.
   - **Exact change:** keep one invocation in `test-and-mark-stable`; let CI own the rest of the coverage.
   - **Estimated time savings:** ~45-60s per `test_and_mark_stable` run (**inference** from CI run `26176563280`, where the analogous regression step took **48.0s**).
   - **Implementation risk:** Low.

## Cost Optimizations

Exact dollar estimates are not possible from this window because no operational `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens` lines were emitted. Savings estimates below use model-call count, prompt bytes, and runtime as proxies.

1. **Trim or skip pass 2 for low-risk reviews.**
   - **Evidence:** All **8 sampled full review runs** executed **12 reviewer model calls** each (`6` pass 1 + `6` pass 2). In run `26175029695`, the two-pass reviewer step cost **508.8s** on a **7-LOC** diff.
   - **Root cause:** the current reviewer policy spends full second-pass budget even on tiny diffs.
   - **Exact change:** skip pass 2 on strong pass-1 consensus; otherwise lower `REVIEWER_PASS2_REASONING_SMALL` from `xhigh` to `high` or `medium`.
   - **Estimated savings:** roughly **50% of reviewer-call spend** on low-risk PRs, plus ~4-8 minutes saved.
   - **Quality-risk notes:** Medium; keep full pass 2 for disagreement, high-risk files, or failing CI context.

2. **Shrink the editor prompt and stabilize its cacheable prefix.**
   - **Evidence:** Sampled editor prompts across 8 full review runs were **251,072-352,737 bytes** (avg **317,706**). The stable `pre_assembled_static.txt` chunk was **106,825-112,761 bytes** (avg **110,505**, ~**35.2%** of the editor prompt).
   - **Root cause:** a large static preamble plus dynamic reviewer/context payloads create oversized prompts.
   - **Exact change:** keep the stable preamble first and byte-stable; remove duplicated guidance; once consolidation is fixed, pass a concise issue list instead of the raw reviewer bundle when possible.
   - **Estimated savings:** ~15-35% editor input-byte reduction per run.
   - **Quality-risk notes:** Low if the change is limited to deduping invariant boilerplate and replacing raw reviewer text with verified consolidated output.

3. **Fix `review-consolidator.txt` staging so downstream prompts can be smaller and more structured.**
   - **Evidence:** `scripts/review_consolidate.sh` expects `prompts/review-consolidator.txt`, and the file exists in the repo, but `.github/workflows/review_autofix.yml` stages other prompts and omits this one. In all **8 sampled full review runs**, logs showed `missing=review-consolidator.txt failopen=1 output_bytes=0` and `parse_failed=1`. In run `26175029695`, the editor still built a **352,737-byte** prompt and produced **0 changed files**.
   - **Root cause:** workflow support-file staging drift.
   - **Exact change:** stage `prompts/review-consolidator.txt` alongside the other required prompt files, then only feed consolidated issue blocks to the parser/editor path.
   - **Estimated savings:** smaller editor prompts and fewer expensive no-op editor passes on low-signal runs.
   - **Quality-risk notes:** Low; current behavior is already fail-open.

4. **Prevent sunk model/runtime cost in canceled internal review runs.**
   - **Evidence:** `review_autofix` cancels consumed **35,372s (9.83h)**; **34,705s (9.64h)** of that came from the 15 long cancels, all `Internal: AI Review & Autofix`.
   - **Root cause:** late cancellation after substantial reviewer/editor work.
   - **Exact change:** move stale-base/conflict detection earlier in the workflow and exit before reviewer/editor model invocation when the run is already obsolete or structurally conflicted.
   - **Estimated savings:** removes most spend on long canceled internal review runs.
   - **Quality-risk notes:** Low-Medium; preserve current path on probe failure.

5. **Use Semble selectively; do not expect Serena savings yet.**
   - **Evidence:** Operational deep-dive logs showed **26 `SEMBLE_QUERY`** lines totaling **248,893 bytes** across 8 review runs: `reviewer-context` **115,432 bytes**, `overflow` **101,260**, `conflict-resolver-context` **32,201**. In run `26175029695`, Semble contributed **15,677** reviewer-context bytes and **7,935** overflow bytes, but the editor prompt still reached **352,737 bytes**. No operational `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines were observed.
   - **Assessment:** **Inference:** Semble is probably helping targeted overflow retrieval, but it is not the main lever on prompt cost because the static/editor scaffold is much larger. Serena is currently providing neither savings nor extra noise.
   - **Exact change:** keep Semble limited to overflow/conflict retrieval; avoid adding new Semble targets until cache metrics exist; do not invest in Serena replacement paths until probe telemetry proves availability.
   - **Estimated savings:** medium future savings after prompt-size fixes; low immediate savings from more Semble expansion.
   - **Quality-risk notes:** Low.

## Reliability Improvements

1. **Restore `test_and_mark_stable` log capture before treating those failures as actionable workflow defects.**
   - **Failure evidence:** Runs `26145595163`, `26145606096`, `26146100910`, `26147010589`, `26147743293`, `26148050482`, and `26149097679` all failed with `duration_seconds=0`, no failure point, and missing log archives (`gh api ... /logs` returned `404`).
   - **Root cause category:** observability / collection gap.
   - **Exact fix:** when `/logs` returns `404`, fall back to per-job log download and persist job metadata; explicitly label these rows as `missing_logs` instead of plain workflow failure.
   - **Expected reliability impact:** much faster root-cause isolation and fewer blind reruns for a workflow family currently showing **87.5%** failure.
   - **Rollback / fail-open:** if log download still fails, keep the metadata-only fallback and raise a soft collector warning instead of dropping context entirely.

2. **Fix the `review-consolidator.txt` packaging bug.**
   - **Failure evidence:** All **8 sampled full review runs** logged `missing=review-consolidator.txt failopen=1 output_bytes=0` and `parse_failed=1`; this is deterministic, not intermittent.
   - **Root cause category:** workflow packaging/config drift.
   - **Exact fix:** add `prompts/review-consolidator.txt` to the staged support prompts in `.github/workflows/review_autofix.yml`, and add a contract test that the staged prompt set matches what `scripts/review_consolidate.sh` requires.
   - **Expected reliability impact:** removes a guaranteed fail-open branch from every sampled full review run and should improve editor precision.
   - **Rollback / fail-open:** current fail-open remains safe if the prompt cannot be found.

3. **Repair the two concrete CI regressions now blocking clean merges.**
   - **Failure evidence:**  
     - Run `26151309804`, `lint / Orchestrate poll process unit tests`: `NameError: name 'json_line' is not defined` in `tests/test_orchestrate_integration_ahead_by_gate.py`.  
     - Run `26157790206`, `lint / Review-blocked judge label propagation contract test`: `test_prompt_budget_helpers_fail_closed_on_non_numeric_counters` failed with the message that `review_rb_judge.sh` must coerce non-numeric `_size` values to `0` before integer comparisons.
   - **Root cause category:** code/test regression.
   - **Exact fix:** restore the missing `json_line` initialization path and reinstate the non-numeric guard in `_embed_input_file()` in `review_rb_judge.sh`.
   - **Expected reliability impact:** should return CI from **79/81 success (97.5%)** toward a clean pass rate.
   - **Rollback / fail-open:** no special rollback needed; tests already cover the fixes.

4. **Short-circuit obsolete/conflicted internal review runs earlier.**
   - **Failure evidence:** `Internal: AI Review & Autofix` accounts for **42/50** `review_autofix` cancellations and all **15** long cancellations. Run `26170953180` was canceled after **2471s** with a conflict-heavy PR body; run `26173251165` was canceled after **1936s** after long gate wait.
   - **Root cause category:** late conflict/staleness detection.
   - **Exact fix:** run stale-base and merge-conflict probes before reviewer/editor work; if they fail, exit with a comment and let the newer run proceed.
   - **Expected reliability impact:** materially lower cancel/requeue churn and less lost work on forward-merge maintenance PRs.
   - **Rollback / fail-open:** if the probe itself errors, continue with the current path.

5. **Clean up AI memory telemetry serialization so reliability analytics stay trustworthy.**
   - **Failure evidence:** every memory-enabled sampled run emitted an unlabeled raw JSON companion line after the prefixed `AI_MEMORY_TELEMETRY:` line, and 4 slow review runs (`26146174572`, `26154599231`, `26170059805`, `26146860961`) emitted an unparseable raw `record-candidate` payload.
   - **Root cause category:** logging/serialization defect.
   - **Exact fix:** emit exactly one prefixed JSON object per line, per event.
   - **Expected reliability impact:** better postmortems and safer automation based on memory telemetry.
   - **Rollback / fail-open:** none needed; this is observability-only.

**MCP fail-open health**
- `SEMBLE_FALLBACK` appeared **15** times, all `target=overflow`, all in test-only paths: **10** lines in CI run `26176563280` (`lint / Targeted file context contract tests`) and **5** lines in `test_and_mark_stable` run `26151428977` (`validate-scripts`). The reason was always a temporary `missing_semble` binary. This looks like **healthy fail-open contract coverage**, not a broken operational rollout.
- No operational `SERENA_FALLBACK` or `SERENA_PROBE` lines were observed. Sampled runtime logs instead showed Serena disabled/unavailable (`SERENA_ENABLED: false` / `SERENA_AVAILABLE: false`), so the issue is **non-deployment**, not flaky fallback behavior.
- Smallest safe mitigation: emit separate counters for **test-only fallback lines** vs **production fallback lines** so future audits do not over-read contract-test noise.

## AI Memory Health

- **Coverage:** I found **33 prefixed `AI_MEMORY_TELEMETRY` lines** across **10** deep-dive runs: **20** `record-run-event`, **9** `retrieve`, and **4** `record-candidate`. No `finalize-task`, `promote`, `compact`, `processed-command-claim`, or `processed-command-complete` operations were observed.
- **Retrieve effectiveness:** `retrieve` is currently not helping this path. All **9/9** retrieves returned **0 records** (**0% hit rate**), all had `estimated_tokens=0`, and `keyword_method` was `none` in **9/9** cases.
- **Budget visibility gap:** the retrieve payloads did **not** emit a budget field, so estimated-tokens-vs-budget comparison is unavailable.
- **Flags:** no retrieve entry showed `fail_open: true`; none showed `enabled: false`; all observed retrieve entries were enabled but empty.
- **Retry outlier:** one write-side retry stood out: run `26170059805` logged `push_attempts=2` on its completion-side `record-run-event`.
- **Telemetry quality:** recent review run `26176563751` explicitly reported `AI_MEMORY_TELEMETRY` absent in its summary, and recent Copilot run `26175028589` also lacked memory telemetry. So memory emission is inconsistent across workflow paths.
- **Recommendation:** fix serialization first, then either improve reviewer-memory retrieval or skip retrieval entirely when the keyword method is `none`; the current retrieve step adds latency without observed benefit.

## GH API Call Audit

- **`review_autofix / codex-agent / Collect PR check-run failures` is the clearest API hotspot.**
  - **Evidence:** Run `26175029695` logged **43** waits in that step; the 7 loop-heavy sampled runs logged **295** waits total. The workflow code polls `repos/{repo}/commits/{sha}/check-runs?per_page=100`.
  - **Redundancy:** repeated polling of the same endpoint with a fixed 20-second cadence, even when the context payload is tiny.
  - **Concrete change:** adaptive backoff + early snapshot after a bounded number of polls.
  - **Estimated API reduction:** cut from ~42 polls/run on loop-heavy runs to ~10-15, saving ~27-32 calls per affected run.
  - **Rate-limit benefit:** moderate; no actual 429/secondary-rate-limit events were seen, but this is the largest avoidable call volume.

- **`test_and_mark_stable` still has loop-level API duplication.**
  - **Evidence:** the workflow code polls `/actions/runs/{run_id}/jobs?per_page=100` with retry/recheck loops, and on the closed-PR path it fetches `/actions/runs/{id}` **twice per iteration**: once for `.status` and once for `.conclusion`.
  - **Redundancy:** two calls per poll cycle for the same run resource.
  - **Concrete change:** fetch the run JSON once per loop and extract both fields; reuse `jobs` JSON within an iteration instead of re-fetching.
  - **Estimated API reduction:** **inference** from workflow code: status polling can be halved from **240** calls to **120** over a 600-second wait loop; jobs polling can also be cut materially on indexing rechecks.
  - **Rate-limit benefit:** high for this workflow family, especially because 7 failing runs had no logs and cannot be debugged cheaply today.

- **Copilot review has chatty but lower-priority API behavior.**
  - **Evidence:** run `26175028589`, `copilot-pull-request-reviewer / Processing Request Linux`, made **37** Copilot API calls: **32** `PUT /agents/sessions/{id}/logs`, **3** `PUT /agents/sessions/{id}`, **1** `GET /agents/swe/agent/jobs/{id}`, **1** `POST /agents/swe/agent/jobs/{id}/progress`.
  - **Assessment:** this is high-frequency log streaming, but the total workflow cost is only **2.77h** across 39 runs, so it is not the first repo-local target.
  - **Concrete change:** monitor first; only pursue if the action exposes a lower-frequency log-upload setting.

- **Positive pattern already exists in `scripts/orchestrate_poll_process.sh`; copy it.**
  - **Evidence:** the script uses `_ENSURED_LABELS_CACHE` to avoid repeated label lookups and `_load_actions_runs_cached()` with `ETag` / `If-None-Match` plus a shared memory cache to collapse `actions/runs` fetches.
  - **Recommendation:** refactor `review_autofix` and `test_and_mark_stable` pollers to reuse this shared-loader / conditional-fetch pattern instead of per-loop `gh api` calls.

- **Rate-limit posture:** no operational deep-dive log showed a live GitHub secondary-rate-limit, 429, or abuse-detection warning. The current risk is wasted call volume, not active throttling.

## Prompt Cache & Memory System

- **Prompt-cache behavior is configured but unmeasured.** Review run `26175029695` logged `OPENROUTER_PROMPT_CACHE_DISABLED: false`, but no prompt-cache create/read counters were emitted anywhere, so hit rate is unknowable.
- **There is a clear cacheable-prefix opportunity.** Across 8 sampled full review runs, `pre_assembled_static.txt` averaged **110,505 bytes** and was stable within a narrow band. **Inference:** if this remains the first byte-stable block in editor/consolidator prompts, it is an ideal cache candidate.
- **Current fragmentation sources are visible.**
  - dynamic PR metadata and diff artifacts,
  - check-run context,
  - raw reviewer bundle text,
  - per-file Semble overflow snippets,
  - and the missing consolidator prompt, which forces the editor path to carry more raw text than necessary.
- **Memory retrieval is not yet earning its keep.** Reviewer-memory retrieval returned zero records in every observed case, so it currently adds complexity and a few seconds of latency without visible recall value.
- **Concrete improvements:**
  1. Emit cache create/read counters in `step-049` token-usage logging.
  2. Keep the static preamble first and byte-stable; move volatile metadata later.
  3. Fix `review-consolidator.txt` staging so the editor consumes structured consensus instead of raw reviewer sprawl.
  4. Skip review-time memory retrieval when `keyword_method=none`.
- **Estimated impact:** medium token and latency benefit; reliability benefit is high because future regressions become measurable.

## Orchestrator Health

- **Guard logic is functioning, but it is noisy.** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` accounted for **533** runs, of which **517** were skipped. Those four families consumed only **3025s** total, so this is mostly orchestration churn, not compute waste.
- **The poller looks healthy.** `orchestrate_poll` went **11/11 success** with `p50=62s` and `p95=152s`. Recent run `26176381910` succeeded in **60s** even though Semble was enabled but unavailable, which is healthy fail-open behavior.
- **The pain is at the handoff into review/autofix, not in clarify loops.** The highest friction signals are long internal review cancellations, long check-run waits, and runner queue messages in review-heavy workflows.
- **Observable indicators teams should track:**
  - skipped-to-success ratio for `clarify` / `plan` / `implement` / `respond`,
  - `review_autofix` long-cancel count and hours,
  - check-run wait span per review run,
  - missing-log-archive count,
  - AI memory retrieve hit rate,
  - Semble bytes per full review run.
- **Smallest safe mitigation:** evaluate the same skip guards earlier in the dispatcher/comment-trigger path so obviously no-op clarify/plan/implement/respond workflows never start.

## Pipeline Flow Bottlenecks

| Stage | Bottleneck | Evidence | Overhead type | Fix order |
|---|---|---|---|---|
| Clarify → Plan → Implement → Respond | High no-op churn, low compute | 533 runs, 517 skipped, only 3025s total | orchestration noise | 5 |
| Review / Autofix | Main critical path | 174 runs, 61.3h runtime, p95 3261.3s; check-run wait 553-1183s; reviewer step 508.8s in run `26175029695` | compute + queueing + late conflict handling | 1 |
| CI | Single serialized long test bucket | run `26176563280`: 800.9s `Orchestrate_poll_process_unit_tests` inside 1008.9s total | compute serialization | 2 |
| Internal review cancels | Long wasted runs on conflict-heavy PRs | 15 long cancels, 9.64h, all `Internal: AI Review & Autofix` | merge/conflict overhead | 3 |
| Validate / stable-mark path | Opaque failures plus very heavy success path | 7/8 failed with missing logs; the single success run `26151428977` had very long `workflow-log-analysis-test` (4868.7s) and `e2e-smoke-test` (4528.8s) jobs | observability + heavy downstream validation | 4 |

**Ordered end-to-end fixes**
1. Shorten `review_autofix` check-run waiting.
2. Gate or trim pass-2 reviewer fan-out.
3. Add an early conflict/stale-base probe to `Internal: AI Review & Autofix`.
4. Parallelize CI around `test_orchestrate_poll_process.py`.
5. Repair `test_and_mark_stable` log capture, then simplify duplicate coverage.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix`: **174 runs**, **61.3h** runtime, **50 cancels**.
  - `ci`: **81 runs**, **20.3h** runtime, dominated by `Orchestrate_poll_process_unit_tests`.
  - `test_and_mark_stable`: only **8 runs**, but **87.5%** failed and the one success was very heavy.

- **Top failure modes**
  - Missing stable-mark log archives (`404`) on 7 failing runs.
  - CI regression in `tests/test_orchestrate_integration_ahead_by_gate.py` (`26151309804`).
  - CI regression in `review_rb_judge.sh` prompt-budget handling (`26157790206`).
  - Long `Internal: AI Review & Autofix` cancels on conflict-heavy forward merges.

- **Highest-cost drivers**
  - 12-reviewer two-pass review policy on every sampled full review run.
  - 251k-353k byte editor prompts.
  - Long PR check-run polling before reviewer/editor work.

- **Top 3 prioritized actions**
  1. Bound `review_autofix` check-run waiting and gate pass 2 on consensus.
  2. Fix `review-consolidator.txt` staging and shrink editor prompt input.
  3. Split CI so `test_orchestrate_poll_process.py` runs in parallel, and add an early conflict probe for internal review runs.

## Metrics Appendix

| Scope / family | Runs | Success | Failure | Cancelled | Other / skipped | p50 s | p95 s | Total runtime h |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Overall | 1000 | 420 (42.0%) | 9 (0.9%) | 52 (5.2%) | 519 (51.9%) | 3.0 | 2209.0 | 88.9 |
| review_autofix | 174 | 123 (70.7%) | 0 (0.0%) | 50 (28.7%) | 1 (0.6%) | 1283.5 | 3261.3 | 61.3 |
| ci | 81 | 79 (97.5%) | 2 (2.5%) | 0 (0.0%) | 0 (0.0%) | 911.0 | 1016.0 | 20.3 |
| test_and_mark_stable | 8 | 1 (12.5%) | 7 (87.5%) | 0 (0.0%) | 0 (0.0%) | 0.0 | 3195.4 | 1.4 |
| copilot_pull_request_reviewer | 39 | 39 (100.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 260.0 | 362.0 | 2.8 |
| orchestrate_poll | 11 | 11 (100.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 62.0 | 152.0 | 0.2 |
| clarify | 139 | 5 (3.6%) | 0 (0.0%) | 0 (0.0%) | 134 (96.4%) | 1.0 | 3.1 | 0.2 |
| plan | 131 | 4 (3.1%) | 0 (0.0%) | 0 (0.0%) | 127 (96.9%) | 1.0 | 9.5 | 0.2 |
| implement | 131 | 4 (3.1%) | 0 (0.0%) | 2 (1.5%) | 125 (95.4%) | 1.0 | 10.0 | 0.4 |
| orchestrate_clarify_respond | 132 | 1 (0.8%) | 0 (0.0%) | 0 (0.0%) | 131 (99.2%) | 1.0 | 3.0 | 0.1 |

*Operational hotspot sections exclude `workflow_log_analysis` from ranking; that workflow ran once for 4808s.*

| Review/autofix metric | Value |
|---|---|
| Total runtime | 220,669s (61.3h, 70.0% of operational runtime) |
| Cancelled runtime | 35,372s (9.83h across 50 runs) |
| Long cancelled runtime (`>=1200s`) | 34,705s (9.64h across 15 runs) |
| Long cancels owned by `Internal: AI Review & Autofix` | 15 / 15 |
| Sampled loop-heavy check-run waits | 295 waits across 7 runs |
| Check-run wait span (sampled) | 553.3s-1182.7s; avg 828.6s; median 809.0s |

| Prompt / cache metric | Value | Notes |
|---|---:|---|
| Operational token counters emitted | No | No `prompt_tokens`, `completion_tokens`, or `total_tokens` lines found |
| Prompt-cache counters emitted | No | No `cache_creation_input_tokens` or `cache_read_input_tokens` lines found |
| `OPENROUTER_PROMPT_CACHE_DISABLED` | `false` | Seen in run `26175029695`; config only, not effectiveness |
| Sampled editor prompt bytes | avg 317,706 | 8 full review runs; min 251,072, max 352,737 |
| Sampled `pre_assembled_static.txt` bytes | avg 110,505 | ~35.2% of editor prompt |
| Reviewer fan-out | 6 + 6 | Seen in 8/8 sampled full review runs |
| Consolidator packaging health | 8/8 sampled full review runs broken | `missing=review-consolidator.txt` + `parse_failed=1` |

| Workflow / step | API pattern | Count / span | Recommended reduction |
|---|---|---|---|
| `review_autofix / codex-agent / Collect PR check-run failures` | `GET commits/{sha}/check-runs` polling | 295 observed waits across 7 loop-heavy runs; 43 waits / 865.7s in run `26175029695` | adaptive backoff + early snapshot |
| `test_and_mark_stable` closed-PR wait loop | separate `GET actions/runs/{id}` for status and conclusion | **Inference from workflow code:** up to 240 calls over 600s | fetch run JSON once per loop |
| `test_and_mark_stable` jobs lookup | repeated `GET actions/runs/{run_id}/jobs` with retry/recheck loops | **Inference from workflow code:** up to 6 calls per tracked run id | cache/reuse per iteration |
| `copilot_pull_request_reviewer / Processing Request Linux` | Copilot API chatter | 37 calls in run `26175028589` (32 PUT log uploads, 3 PUT session, 1 GET job, 1 POST progress) | lower priority; monitor external action settings |
| `scripts/orchestrate_poll_process.sh` | shared cached `actions/runs` loader with ETag + label cache | positive example | reuse pattern elsewhere |

| Server | Query count | Logged bytes | Fallback count | Probe count | Response bytes | Notes |
|---|---:|---:|---:|---:|---:|---|
| Semble | 26 | 248,893 | 15 | 0 | n/a | Queries only observed in `review_autofix`; fallbacks were test-only (`10` CI, `5` `test_and_mark_stable`) |
| Serena | 0 | 0 | 0 | 0 | 0 | No operational lines observed |
| Other MCP servers observed | 0 | 0 | 0 | 0 | 0 | None |

| Semble target | Query count | Logged bytes |
|---|---:|---:|
| reviewer-context | 8 | 115,432 |
| overflow | 14 | 101,260 |
| conflict-resolver-context | 4 | 32,201 |

| Server | Target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---:|---:|---:|---|
| Semble | n/a | 0 | 0 | 0 | No `SEMBLE_PROBE` lines observed |
| Serena | n/a | 0 | 0 | 0 | No `SERENA_PROBE` lines observed |
| Other MCP | none | 0 | 0 | 0 | None observed |

| AI memory metric | Value | Notes |
|---|---|---|
| Prefixed telemetry lines | 33 | Deduped deep-dive operational logs |
| Ops observed | `record-run-event=20`, `retrieve=9`, `record-candidate=4` | No `finalize-task` / `promote` / `compact` ops seen |
| Retrieve hit rate | 0 / 9 (0%) | All `records_selected=0` |
| Avg `estimated_tokens` | 0 | Budget field not emitted |
| `keyword_method` distribution | `none=9`, `plain=0`, `llm=0` | Reviewer retrieval only |
| Retrieves with `enabled=false` | 0 | None observed |
| Retrieves with `fail_open=true` | 0 | None observed |
| Push retry outliers | 1 | Run `26170059805` completion event had `push_attempts=2` |
| Serialization quality issue | Present | 10 runs emitted unlabeled raw JSON companion lines; 4 slow review runs emitted unparseable candidate payloads (`26146174572`, `26154599231`, `26170059805`, `26146860961`) |

## Deep Audit — Workflows & Scripts (2026-05-20)

### Section 1: Bug & Correctness Sweep

#### CONSIST-001
- **File path:** `.github/workflows/review_autofix.yml:533-536,546-578,4640-4649,4655-4659,4762-4781,5689-5707; scripts/review_rb_judge.sh:378-388; .github/workflows/issue_pr_status.yml:195-210`
- **Severity:** Medium
- **Category tag:** `consistency`
- **Description:** `issue_pr_status.yml` explicitly narrowed its PR-body fallback to closing keywords/full repo-scoped links only because bare prose like `issue #N` and `issues/N` caused incorrect orchestrator-tracking transitions (`issue_pr_status.yml:196-204`). `review_autofix.yml` and `review_rb_judge.sh` still use the older, broader regex that matches those bare references. In `review_autofix.yml`, that fallback drives real mutations: standalone validation dispatch plus removal of `ai:orchestrator-validate-required` (`546-578`), `ai:ready-to-merge` application (`4655-4659`), and `ai:review-blocked` application (`4762-4781`, `5689-5707`). The same PR text can therefore be interpreted strictly in `issue_pr_status.yml` but broadly in review/autofix paths, which can mutate unrelated issues.
- **Recommended fix:** Extract one shared helper, e.g. `extract_linked_issue_numbers_from_pr_text <repo> <text> <mode>`, with a strict `closing-only` mode for any path that changes issue labels or lifecycle state. Replace the inline regex copies in `review_autofix.yml` and `review_rb_judge.sh` with the strict policy already documented in `issue_pr_status.yml`.

### Section 2: GitHub API Call Redundancy Audit

#### BATCH-001
- **File path:** `scripts/orchestrate_poll_process.sh:13445-13480,13724-13750`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** The standalone PR conflict sweep first does `gh pr list --json number,headRefName,baseRefName` (`13448-13452`) and then re-fetches each open PR via `GET /pulls/{n}` to read mergeability/state (`13460-13480`). Later, the noop-suspicious force-merge gate does another `GET /pulls/{n}` per threshold-hit PR to read state, mergeability, head SHA, and labels (`13727-13750`). These two paths consume the same PR metadata shape but fetch it one PR at a time.
- **Current call count:** `1 + N + M`, where `N` is the number of open PRs scanned by the conflict sweep and `M` is the number of noop-threshold PRs evaluated by Gate A.
- **Proposed call count after fix:** `1` cached GraphQL snapshot for PR metadata per orchestrator cycle, reused by both paths (targeted downstream comment/check-run calls unchanged).
- **Batching pattern to extend:** The alias-batched GraphQL style already used by `_fetch_candidate_issue_details_graphql()` and `_fetch_linked_pr_status_graphql()` in `scripts/orchestrate_poll_process.sh:7142-7259,7261-7279`.
- **Recommended fix:** Add a batched helper such as `_fetch_open_pr_metadata_graphql <state> <limit>` that returns `number`, `headRefName`, `baseRefName`, `isDraft`, `mergeable`, `mergeStateStatus`, `headRefOid`, and `labels`, cache it once, and feed both the conflict sweep and noop Gate A from that snapshot.

#### BATCH-002
- **File path:** `scripts/review_rb_judge.sh:378-405`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** `review_rb_judge.sh` already uses GraphQL to fetch `closingIssuesReferences`, but it asks only for issue numbers (`378-383`). It then loops over those linked issues and does per-issue REST fetches to read body and labels (`399-405`) until it finds a usable parent issue body. That is avoidable because the needed fields are available from the initial GraphQL response.
- **Current call count:** `1 + up to N`, where `N` is the linked-issue count; the loop can stop early once it finds a non-empty body, but still scales per issue.
- **Proposed call count after fix:** `1`.
- **Batching pattern to extend:** `scripts/orchestrate_poll_process.sh:_fetch_candidate_issue_details_graphql`.
- **Recommended fix:** Expand the existing GraphQL query in `review_rb_judge.sh` to request `number`, `body`, and `labels { nodes { name } }` inside `closingIssuesReferences.nodes`, then derive `FIRST_ISSUE`, `FIRST_ISSUE_BODY`, and `FIRST_ISSUE_LABELS_JSON` from that single payload.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **File path:** `scripts/label_helpers.sh:102-195; .github/workflows/review_autofix.yml:4601-4635,4725-4756,5665-5681; .github/workflows/issue_pr_status.yml:241-248; scripts/review_rb_judge.sh:252-305; scripts/validate_process.sh:919-953; scripts/orchestrate_poll_process.sh:1631-1680`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** The repo already has canonical label helpers in `scripts/label_helpers.sh`, but label creation/phase-swap logic is still duplicated across workflows and scripts. The copies have drifted: the canonical helper reads `.github/ai/label_contract.v1.json` and phase-swaps via GET+PUT to remove old AI phase labels (`160-195`), while several fallbacks in `review_autofix.yml` and `issue_pr_status.yml` only POST-add the new label, `review_rb_judge.sh` carries its own `_resilient_phase_swap`, `validate_process.sh` reimplements creation and always returns `0` on failure, and `orchestrate_poll_process.sh` embeds a private cache. That drift can leave contradictory phase labels or inconsistent label metadata when fallback paths execute.
- **Recommended fix:** Make `scripts/label_helpers.sh` the single owner. Standardize on `ensure_label_exists <label_name> [repo]` and `set_issue_phase_label_resilient <issue_number> <target_label> [repo]`, add any needed caching inside that module, and update callers in `review_autofix.yml`, `issue_pr_status.yml`, `review_rb_judge.sh`, `validate_process.sh`, and `orchestrate_poll_process.sh` to source it instead of carrying local copies.

#### DUP-002
- **File path:** `.github/workflows/implement.yml:3528-3568; scripts/orchestrate_poll_process.sh:5761-5813`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** The ancestor-chain no-op counter is implemented twice with near-identical logic. Both versions walk `Re-issued from #N`, both key off the same `"produced no repository changes"` marker, and both spend up to two GitHub calls per hop. Any future change to the marker text, max-depth semantics, or fail-open behavior now has to land in two critical paths.
- **Recommended fix:** Extract a shared helper into a small module such as `scripts/noop_helpers.sh` with signature `count_noop_ancestors <repo> <issue_num> [max_depth]`, then source it from both `.github/workflows/implement.yml` and `scripts/orchestrate_poll_process.sh`.

### Section 4: Expression Size Limit Risk Assessment

No new expression-limit findings.

A static source scan found:
- largest workflow file: `.github/workflows/review_autofix.yml` at ~359,044 characters,
- next largest: `.github/workflows/test-and-mark-stable.yml` at ~278,319 and `.github/workflows/implement.yml` at ~242,379,
- no workflow above the 800 KB warning threshold,
- no raw `${{ }}` body above 229 characters, far below the 15,000 / 18,000 risk thresholds.

The previously documented historical expression-limit regressions appear to have already been mitigated by moving large logic into scripts/files.

### Section 5: Cross-Cutting Concerns

#### CONSIST-002
- **File path:** `scripts/check_integration_pr_readiness.py:67-82,99-141; .github/workflows/integration-pr-readiness.yml:20-22,67-79; scripts/gh_helpers.sh:391-445`
- **Severity:** Low
- **Category tag:** `consistency`
- **Description:** `integration-pr-readiness.yml` documents `orchestrator/integration-pr-not-ready` as a branch-protection/required-status gate (`20-22`), but `check_integration_pr_readiness.py` performs one-shot `gh issue view` and `gh api -X POST` subprocess calls with no retry/backoff. On any transient GitHub/API failure, `_fetch_issue()` returns `None` or `_post_commit_status_or_error()` returns `False`, and the workflow exits non-zero, leaving a required status path blocked until manual rerun. That is inconsistent with the repo-wide `gh_retry` pattern in `gh_helpers.sh`.
- **Recommended fix:** Add a small Python retry helper that mirrors `gh_retry` semantics (retry transient failures, fail fast on 404/422/permission errors), or wrap the gh calls through a tiny shell shim that sources `scripts/gh_helpers.sh`.

#### DEBT-001
- **File path:** `.github/workflows/workflow-log-analysis.yml:16-20,525-526; scripts/analyze_workflow_logs.py:107-113; .github/workflows/comprehensive-test-and-release.yml:151-159`
- **Severity:** Low
- **Category tag:** `tech-debt`
- **Description:** `codex_mode` / `--codex-mode` are explicitly documented as deprecated no-ops, but the workflow still exposes the input, still passes `--codex-mode` to the analyzer, and `comprehensive-test-and-release.yml` still dispatches the workflow with `-f codex_mode=true`. That preserves a fake operator control surface even though behavior never changes.
- **Recommended fix:** Remove the workflow input and analyzer flag once callers are cleaned up, or keep only one compatibility shim and emit a deprecation warning from that layer.

#### DEBT-002
- **File path:** `scripts/validation_refresh_runner.py:125-140,390-398; .github/workflows/validation-refresh.yml:93-96`
- **Severity:** Low
- **Category tag:** `tech-debt`
- **Description:** `ValidationRefreshRunner` still accepts and stores `commit_message` and `pr_title`, and the CLI still advertises `--commit-message` and `--pr-title` as deprecated no-ops, even though the runner “never pushes” and the workflow caller does not pass either flag. This leaves obsolete PR-era surface area in a non-PR runner.
- **Recommended fix:** Remove the constructor fields and CLI options after any remaining tests/wrappers are updated, or fail fast with a deprecation error if a caller still supplies them.

#### DEBT-003
- **File path:** `.github/workflows/memory_maintenance.yml:37-55`
- **Severity:** Low
- **Category tag:** `tech-debt`
- **Description:** `memory_maintenance.yml` still wires `BATCH_API_DISABLED`, `BATCH_API_PROVIDER`, and `BATCH_API_POLL_TIMEOUT_HOURS` into the job environment, but the step immediately logs `event":"batch_noop","reason":"no_llm_path"` and states that the workflow has “no Codex execution path.” The variables do not affect behavior; they only preserve compatibility plumbing.
- **Recommended fix:** Remove the three env vars once downstream telemetry consumers stop depending on them, or collapse them into a single explicit compatibility flag so operators do not mistake them for live controls.

No `TODO` / `FIXME` / `HACK` markers were found under `.github/workflows` or `scripts` in this audit scope.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 4 | CONSIST-001, BATCH-001, BATCH-002, DUP-001 |
| Low | 5 | DUP-002, CONSIST-002, DEBT-001, DEBT-002, DEBT-003 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 2 | Medium |
| Code modularization | 8 | Large |
| Expression size reduction | 0 | Small |
| Medium/Low fixes | 10 | Medium |
