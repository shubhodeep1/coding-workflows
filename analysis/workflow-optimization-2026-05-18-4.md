## Executive Summary

- `review_autofix` is the dominant bottleneck: 110 runs consumed 92,539s (54.7% of sampled runtime), and 22 canceled runs alone burned 24,059s (26.0% of `review_autofix` runtime). The highest-leverage fix is restoring stale-base helper gates so stale runs exit before reviewer/editor spend. **Estimated impact:** save ~35-45 minutes on stale outliers and reduce queue pressure. **Confidence:** high.
- `ci` is the main reliability blocker: 57 runs, 10 failures (17.5%), all concentrated in two buckets — 6 in `Validation self-test unit tests` and 4 in `Orchestrate poll process unit tests`. Fixing those two regressions plus sharding the single `lint` job should materially reduce both failure rate and wall time. **Estimated impact:** near-eliminate current CI failures and save ~2-5 minutes per CI run. **Confidence:** high on failure reduction, medium on latency savings.
- The release-validation path is too long for its value: `workflow_log_analysis` averaged 3,521s and `test_and_mark_stable` averaged 3,658.5s. Run `26036110500` spent 1,904s / 919s / 826s in three sequential analysis jobs; run `26036073220` spent 3,414s in `e2e-smoke-test` polling. **Estimated impact:** save ~10-20 minutes on release/test runs by lowering analysis reasoning and backing off polling. **Confidence:** high.
- No-PR claude-branch review is still expensive even after its lightweight profile: run `26042542110` took 817s and still used 6 reviewers (`REVIEWERS_SUCCESSFUL: 6`). The remaining win is model-count reduction, not more reasoning tuning. **Estimated impact:** cut 40-60% of cost on no-PR branch reviews. **Confidence:** medium.
- Semble looks net-positive; Serena is effectively absent. In sampled operational logs, Semble emitted 7 real queries across 5 slow `review_autofix` runs totaling 83.9KB at ~0.49s/query; no operational `SERENA_QUERY` / `SERENA_FALLBACK` / `SERENA_PROBE` lines were found. AI memory retrieval also returned 0/5 hits. **Estimated impact:** keep Semble, do not spend optimization effort on Serena yet, and fix memory retrieval/observability first. **Confidence:** high.

## Speed Optimizations

1. **Re-enable stale-base gates before reviewer/editor spend** *(critical-path win)*  
   - **Evidence:** In all 5 sampled slow `review_autofix` deep dives (`26013098223`, `26014929366`, `26027445588`, `26037040844`, `26037045336`), the pre-review gate logged `check_external_branch_advance.sh missing from support scripts; fail-open` and `git_ref_health_check.sh missing from support scripts; fail-open` in `slow/.../review_autofix/.../step-040-review_codex-agent_Pre-review_deterministic_merge-topology_gate.log`. Run `26037045336` then spent 1,210s in `step-041-review_codex-agent_Run_reviewer_models.log` and 1,161s in `step-049-review_codex-agent_Apply_fixes_with_editor_model.log`, and later still carried `AUTOFIX_STALE_BASE_SKIP: true`.  
   - **Root cause:** Broken support-script packaging is masking stale-run detection via fail-open behavior.  
   - **Exact change:** Ensure `check_external_branch_advance.sh` and `git_ref_health_check.sh` are always present in the support checkout used by `review_autofix`; for PR-backed runs, missing helpers should trigger an early soft-exit before reviewer/editor work rather than a full fail-open.  
   - **Estimated time savings:** **Inference:** ~35-45 minutes on stale slow runs like `26037045336`.  
   - **Implementation risk:** Low-medium; the main risk is over-eager exits if helper output is wrong, so keep fail-open only for comment-only/no-PR mode until helper availability is stable.

2. **Shard `ci.yml` so long test suites stop sharing one serial `lint` job** *(critical-path win)*  
   - **Evidence:** `.github/workflows/ci.yml` has a single `lint` job containing all static checks and unit suites. Successful CI runs such as `26042976008` (769s), `26042269397` (792s), `26042582898` (684s), and `26041477563` (758s) all reported `lint` dominating runtime in their `log_summary`. Failed run `26042958244` spent 624.4s in `errors/.../26042958244/step-006-lint_Orchestrate_poll_process_unit_tests.log`.  
   - **Root cause:** One runner owns the full CI critical path.  
   - **Exact change:** Split at least three jobs: `poll-process-tests`, `validation-selftest-tests`, and `static/contract-lint`; keep all as required checks.  
   - **Estimated time savings:** **Inference:** ~2-5 minutes per CI run.  
   - **Implementation risk:** Low; main work is updating required-check names and shared setup/cache reuse.

3. **Shorten the release-analysis path instead of only raising timeouts** *(critical-path win)*  
   - **Evidence:** `workflow_log_analysis` run `26036110500` spent 825.9s in `step-001-api-redundancy.log`, 919.1s in `step-002-deep-audit.log`, and 1,904.0s in `step-003-analyze-commit-notify.log`. In `.github/workflows/workflow-log-analysis.yml`, the workflow comments already say the next lever for slow audit passes should be lowering reasoning before more timeout growth. `test_and_mark_stable` run `26036073220` then waited 3,795.7s in `step-013-workflow-log-analysis-test.log`.  
   - **Root cause:** Sequential LLM-heavy jobs with `xhigh`-style deliberation are sitting directly on the release-test path.  
   - **Exact change:** Lower `workflow_log_analysis` reasoning from `xhigh` to `high` first for deep-audit / api-redundancy, and increase watcher backoff after repeated idle loops in `test_and_mark_stable`.  
   - **Estimated time savings:** **Inference:** ~10-20 minutes on release/test runs.  
   - **Implementation risk:** Low-medium; validate output schema and notification content before broad rollout.

4. **Reduce reviewer fan-out on no-PR claude-branch review** *(targeted critical-path win)*  
   - **Evidence:** Run `26042542110` (`Internal: AI Review & Autofix`) was a no-PR claude-branch review, logged `AUTOFIX_GATE_NO_PR_FALLBACK`, still used 6 reviewer models, and took 817s. By contrast, run `26044570751` short-circuited in 9s once `existing_pr=2760` was found. `review_autofix.yml` already applies low/single-pass mode for no-PR reviews, so the remaining cost is the 6-model panel.  
   - **Root cause:** Comment-only branch review still fans out to the full reviewer pool.  
   - **Exact change:** For `CLAUDE_BRANCH_REVIEW_MODE=true && PR_NUMBER=''`, override `REVIEWER_MODELS` to a 2-3 model subset and keep the existing low/single-pass profile.  
   - **Estimated time savings:** **Inference:** ~5-8 minutes on no-PR branch-review runs.  
   - **Implementation risk:** Low-medium; this path posts comments but does not need merge-grade confidence.

5. **Guard the `Free disk space` step by actual need** *(micro-optimization)*  
   - **Evidence:** Run `26037045336` spent 167s in `slow/.../step-029-review_codex-agent_Free_disk_space.log` before checkout.  
   - **Root cause:** Disk reclamation runs unconditionally even on the workflow-source repo.  
   - **Exact change:** Skip that step when free space is already above a safe threshold, or only enable the full reclaim path on large consumer repos.  
   - **Estimated time savings:** **Inference:** ~1-3 minutes per affected run.  
   - **Implementation risk:** Medium; keep current behavior for asset-heavy repos until thresholds are proven safe.

## Cost Optimizations

1. **Trim no-PR review cost by reducing model count, not by weakening reasoning further**  
   - **Evidence:** Run `26042542110` was already on the no-PR lightweight path but still reported `REVIEWERS_SUCCESSFUL: 6`, `MODEL_EDITOR: openai/gpt-5.4`, and `XPOLL_SUMMARISER_MODEL: openai/gpt-5.4-mini` across an 817s run.  
   - **Root cause:** The path already lowered reasoning/single-pass, so remaining spend is six-model fan-out.  
   - **Exact change:** Keep the lightweight profile but shrink `REVIEWER_MODELS` for no-PR mode to 2-3 reviewers; optionally skip re-trying a reviewer that returns empty output on that path.  
   - **Estimated savings:** **Inference:** 40-60% lower reviewer-token spend on no-PR runs.  
   - **Quality-risk notes:** Low-medium; comment-only branch reviews do not need the same ensemble width as merge-blocking PR reviews.

2. **Stop paying for stale or eventually canceled `review_autofix` runs**  
   - **Evidence:** `review_autofix` had 22 canceled runs out of 110, averaging 1,093.6s each and totaling 24,059s. That is 26.0% of family runtime and 14.2% of all sampled runtime. Examples include canceled runs `26039162783` (1,795s) and `26034365879` (1,326s).  
   - **Root cause:** Superseded runs are reaching expensive setup/LLM stages before being canceled or marked stale.  
   - **Exact change:** Restore stale-base helper scripts, and add one early supersession check after runner start but before reviewer/editor work.  
   - **Estimated savings:** Eliminate most of the 24,059s currently burned in canceled `review_autofix` runs.  
   - **Quality-risk notes:** Low; the change skips only runs already known to be stale or superseded.

3. **Lower `workflow_log_analysis` reasoning before spending more timeout budget**  
   - **Evidence:** `.github/workflows/workflow-log-analysis.yml` already documents repeated timeout increases and explicitly says lowering reasoning should be the next lever. Sampled run `26036110500` still spent 3,759s total.  
   - **Root cause:** High-deliberation analysis jobs are over-provisioned for an informational workflow.  
   - **Exact change:** Reduce deep-audit / api-redundancy reasoning from `xhigh` to `high`; keep `gpt-5.4` if output quality matters, but avoid paying `xhigh` on every pass.  
   - **Estimated savings:** **Inference:** 15-30% lower token/latency spend on those jobs.  
   - **Quality-risk notes:** Low if markdown/report validation stays in place.

4. **Keep Semble; it is not the current cost problem**  
   - **Evidence:** In sampled operational logs (excluding `workflow_log_analysis` false positives), Semble emitted 7 real queries across 5 slow `review_autofix` runs: 5 `target=reviewer-context`, 1 `target=conflict-resolver-context`, 1 `target=overflow`. Total logged query volume was 83,893 bytes, average ~11.98KB, average ~493ms.  
   - **Root cause:** The large spend is the six-reviewer + editor pipeline, not Semble retrieval bytes.  
   - **Exact change:** Keep Semble enabled; use it to trim context before touching reviewer prompts.  
   - **Estimated savings:** Avoids a false optimization. **Inference:** Semble is more likely reducing prompt expansion than adding noisy low-value context.  
   - **Quality-risk notes:** Low; sampled Semble payloads are small relative to the surrounding reviewer/editor work.

5. **Do not spend time tuning Serena until it is actually participating**  
   - **Evidence:** No operational `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines were found in sampled deep-dive logs. The 5 sampled slow `review_autofix` runs all showed `SERENA_ENABLED: false` and `SERENA_AVAILABLE: false`.  
   - **Root cause:** Serena is currently out of path.  
   - **Exact change:** Treat Serena optimization as blocked on enablement/rollout; first decide whether it should be used at all on this repo.  
   - **Estimated savings:** Prevents wasted engineering time; no measurable runtime/token savings available yet.  
   - **Quality-risk notes:** None, because there is no active Serena path to degrade.

6. **Fix token/cache observability before deeper cost tuning**  
   - **Evidence:** No operational `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens` were found. The current collector/view path (`compute_run_metrics`, `_normalized_run_view`) only carries duration/outcome/failure/log_summary-level fields in the supplied context.  
   - **Root cause:** Cost instrumentation is not being normalized into the analysis dataset.  
   - **Exact change:** Extend the collector to parse per-step token/cache/model counters into `analysis_context.json`.  
   - **Estimated savings:** No direct savings, but it unlocks accurate model and cache tuning.  
   - **Quality-risk notes:** None.

## Reliability Improvements

1. **Fix the `implement.yml` resolved-ref logging contract drift**  
   - **Failure evidence:** Runs `26029332723`, `26031068317`, `26032757226`, `26034365768`, `26035468279`, and `26035946559` all failed with `AssertionError: implement.yml missing resolved-ref log output` in `errors/shubhodeep1_coding-workflows/ci/<run_id>/step-001-lint.log`.  
   - **Root cause category:** Workflow/test contract drift.  
   - **Exact fix:** Restore the expected resolved-ref log emission in `implement.yml`, or intentionally update `tests/test_workflow_checkout_integration_ref_audit.py` if the contract changed by design.  
   - **Expected reliability impact:** Removes 6/10 current CI failures; CI failure rate would drop from 17.5% to about 7.0% if nothing else changed.  
   - **Rollback / fail-open:** Keep the audit test; if the contract is deprecated, replace it with a narrower assertion on the new intended output rather than disabling it.

2. **Fix the baseline regression bucket in `test_verify_integration_fingerprints_baseline_regressions`**  
   - **Failure evidence:** Runs `26037045079`, `26039161815`, `26040817342`, and `26042958244` failed `test_verify_integration_fingerprints_baseline_regressions` in `errors/.../26042958244/step-006-lint_Orchestrate_poll_process_unit_tests.log`.  
   - **Root cause category:** Verifier/test capability drift.  
   - **Exact fix:** Bring `scripts/verify_integration_fingerprints.py` back into parity with the baseline-regression tests, or intentionally revise the baseline tests if verifier behavior changed on purpose.  
   - **Expected reliability impact:** Removes the remaining 4/10 current CI failures.  
   - **Rollback / fail-open:** If behavior truly changed, update the baseline fixtures and keep a smaller regression suite on the new contract.

3. **Unmask the broken `review_autofix` helper rollout instead of letting it fail open forever**  
   - **Failure evidence:** Every sampled slow `review_autofix` run logged missing helper warnings before stale-base/merge-topology gates; run `26037045336` logged the stale-base helper missing both before review (`step-040`) and before editor (`step-044`).  
   - **Root cause category:** Packaging/checkout regression hidden by fail-open behavior.  
   - **Exact fix:** Make support-script availability a first-class contract of the support checkout; emit one structured metric when helper absence forces fail-open.  
   - **Expected reliability impact:** Reduces stale reviews, cancellation churn, and “work completed on the wrong SHA” risk.  
   - **Rollback / fail-open:** Keep fail-open only for the comment-only/no-PR path until support-script availability is proven stable.

4. **Separate healthy fail-open tests from real MCP/runtime problems**  
   - **Failure evidence:** 10 `SEMBLE_FALLBACK target=overflow ... missing_semble` lines were observed, all in `test_and_mark_stable` validate-scripts runs `26036073220` and `26039859964`; they appeared immediately around passing test output (`192 passed`, `9 passed`, then `PASS test_install_semble_fails_open_and_marks_unavailable_on_install_error`). No operational `SERENA_FALLBACK` or `SERENA_PROBE` lines were seen. One orchestrate-poll summary run (`26042814296`) reported `SEMBLE_AVAILABLE: false` / `SEMBLE_INDEX_AVAILABLE: false`, which is an availability/config issue rather than a runtime fallback.  
   - **Root cause category:** Mixed healthy fail-open coverage plus one sampled availability gap.  
   - **Exact fix:** Keep Semble fail-open tests as-is; add a startup notice/metric when Semble is unavailable in runtime workflows, but do not hard-fail the poller.  
   - **Expected reliability impact:** Better operator signal with minimal behavior change.  
   - **Rollback / fail-open:** Preserve current runtime fail-open semantics.

## AI Memory Health

AI memory telemetry was observed only in 5 slow `review_autofix` runs: `26013098223`, `26014929366`, `26027445588`, `26037040844`, `26037045336`.

| Metric | Observed value |
|---|---:|
| Total `AI_MEMORY_TELEMETRY` lines | 20 |
| `retrieve` ops | 5 |
| `record-candidate` ops | 5 |
| `record-run-event` ops | 10 |
| Retrieve hit rate (`records_selected > 0`) | 0/5 = 0% |
| Avg `estimated_tokens` on retrieve | 0 |
| `keyword_method` distribution | `none`: 5, `plain`: 0, `llm`: 0 |
| `fail_open: true` retrieves | 0 |
| `enabled: false` retrieves | 0 |
| Push retries > 1 | 0 |
| `promote` / `compact` / `finalize-task` / `processed-command-*` observed | 0 |

- **Interpretation:** The write path is working (`record-run-event` and `record-candidate` always pushed on first attempt), but retrieval is effectively non-functional in this sample.
- **Most important gap:** every retrieve selected 0 records and used `keyword_method: none`, so memory is not contributing context.
- **Recommendation:** seed retrieval with changed files, PR title, workflow family, and recent consensus labels; alert when a 5-run rolling window stays at 0% hit rate.
- **Scope note:** no AI-memory telemetry was observed outside sampled slow `review_autofix` runs; if memory is expected in `implement` / `orchestrate`, verify emission there too.

## GH API Call Audit

1. **`test_and_mark_stable` polling is the main API hot spot**  
   - **Evidence:** `slow/shubhodeep1_coding-workflows/test_and_mark_stable/26036073220/step-011-e2e-smoke-test.log` contained 74 `gh api` mentions, 7 `gh workflow run` mentions, 81 `status=in_progress` lines, and 161 `idle` lines.  
   - **Root cause:** Fixed-interval polling repeatedly refetches state during long idle windows.  
   - **Concrete change:** Cache invariant repo/issue metadata once, then back off polling from 10s to 20-30s after repeated idle loops; tighten the polled fields to only what drives state transitions.  
   - **Estimated reduction:** **Inference:** ~40-60% fewer API calls on similar long watcher runs, with lower rate-limit risk.

2. **`internal-review.yml` does one avoidable REST lookup on every branch-push resolution path**  
   - **Evidence:** `recent/.../review_autofix/26044570751/step-003-resolve-claude-branch-pr_Resolve_PR_for_head_branch.log` shows two direct API calls:  
     - `repos/${REPOSITORY}/pulls?state=open&head=...`  
     - `repos/${REPOSITORY}` for `.default_branch`  
   - **Root cause:** Default branch is fetched via REST even though the event payload already carries repository metadata.  
   - **Concrete change:** Replace the repo lookup with `github.event.repository.default_branch` (with `main` fallback only if missing).  
   - **Estimated reduction:** 1 call removed from 2 for this step (50% reduction), plus one fewer failure point.

3. **Keep and reuse the repo’s good API hygiene patterns**  
   - **Evidence:**  
     - `recent/.../review_autofix/26044030565/step-003-sweep_Enumerate_open_PRs_and_dispatch_internal-review.yml.log` already batches well: 1 paginated open-PR fetch plus 4 active-run snapshot calls (2 workflows × 2 statuses) were enough to evaluate 2 candidates locally.  
     - `recent/.../issue_pr_status/26044990884/step-010-sync-status_sync-issue-status_Update_linked_issue_labels_when_PR_closes.log` uses GraphQL first for linked issues, then falls back to REST only where needed.  
   - **Root cause:** API hygiene is uneven across workflows, not uniformly bad.  
   - **Concrete change:** Treat `review_autofix_sweep` and `issue_pr_status` as the local templates: batch first, snapshot once, filter client-side.  
   - **Estimated reduction:** Prevents regressions into per-item REST fan-out.

4. **Rate-limit note**  
   - **Evidence:** No real 429 or secondary-rate-limit incidents were confirmed in sampled operational logs; most matching text came from wrapper source or from `workflow_log_analysis`’s own analysis output.  
   - **Concrete change:** Keep current retry wrappers, but prioritize call-count reduction over more retry logic.

## Prompt Cache & Memory System

- **Prompt cache is configured but not measurable.** Sampled `review_autofix` logs repeatedly show `OPENROUTER_PROMPT_CACHE_DISABLED: false`, but no operational `cache_creation_input_tokens`, `cache_read_input_tokens`, `prompt_tokens`, `completion_tokens`, or `total_tokens` were emitted.
- **Why this is happening:** the current collector path shown in `scripts/collect_workflow_logs.py::compute_run_metrics` and `scripts/analyze_workflow_logs.py::_normalized_run_view` carries duration/outcome/failure/log-summary fields, but not token/cache/API metrics into the analysis view.
- **Semble assessment:** keep it. Sampled real Semble usage was small and targeted (7 queries, 83.9KB total, mostly `target=reviewer-context`). **Inference:** this is more likely shrinking prompt expansion than adding noisy low-value bytes.
- **Serena assessment:** no operational Serena queries/fallbacks/probes were observed, and sampled slow `review_autofix` runs showed it disabled/unavailable. It is neither saving nor wasting tokens in this window.
- **Memory retrieval assessment:** current memory writes are healthy, but retrieval returns nothing useful (0% hit rate).
- **Likely cache-fragmentation causes (inference):** sampled `review_autofix` runs expose many dynamic prompt ingredients per run (`PR_DIFF_FILE`, comments context, check-runs context, memory context, Semble query files). If those dynamic blocks land before the stable instruction prefix, cache reuse will be poor.
- **Concrete improvements:**
  1. Extend the collector to persist token/cache/model counters into `analysis_context.json`.
  2. Log a stable prompt-prefix hash per reviewer/editor request so cache fragmentation can be measured.
  3. Keep static instructions first and append dynamic diff/comments/check-runs/memory blocks last.
  4. Improve memory retrieval keywords before expanding memory writes further.

## Orchestrator Health

- **Mostly healthy gating, not a primary compute problem:** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` produced 730 runs, but 688 were skipped and total runtime was only 8,976s (5.3% of sampled runtime). This is run-volume noise more than compute pain.
- **Good fail-closed behavior is visible:** recent summaries for `clarify` run `26042956076` and `implement` run `26042956101` reported wave/blocking behavior tied to integration regression detection. That looks like healthy protection, not orchestrator flakiness.
- **Main operational pain point:** the poll/watch path is still chatty and sometimes silently degraded. `orchestrate_poll` itself is stable (13/13 success, avg 131.2s, p95 250.4s), but one sampled poller summary (`26042814296`) reported `SEMBLE_AVAILABLE: false` / `SEMBLE_INDEX_AVAILABLE: false`.
- **Smallest safe mitigations:**
  - Add one structured “tool availability” summary per poll run.
  - Standardize blocked-wave reason codes across `clarify` / `implement` / `respond`.
  - Track wrapper skip ratio and poller p95, but do not prioritize wrapper-run suppression until `ci` and `review_autofix` are fixed.
- **Indicators to track:**  
  - `review_autofix` canceled-runtime share  
  - count of stale-base helper fail-open warnings  
  - wrapper skip ratio  
  - blocked-wave count  
  - `orchestrate_poll` p95 duration  
  - `% poller runs with Semble unavailable`

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence | Recommended fix |
|---|---|---|---|
| Clarify → Plan → Implement → Respond gates | Trigger noise, not compute | 730 wrapper runs, 688 skipped, only 8,976s total | Leave for later; keep monitoring skip ratio |
| Review / Autofix | Queueing + LLM compute + stale-run waste | `review_autofix` = 92,539s total (54.7% of runtime); run `26037045336` waited ~4,601s for a runner, then spent 1,210s reviewer + 1,161s editor | Restore stale-base helpers, early-exit stale runs, shrink no-PR reviewer pool |
| CI / Validate | Serialized test execution + recurrent contract failures | `ci` = 57 runs, 10 failures (17.5%), p50 769s, p95 817s; all failures in 2 test buckets | Fix the two regressions and shard `ci.yml` |
| Poll / Watch loops | Repeated API polling during idle windows | `test_and_mark_stable` run `26036073220` had 74 `gh api` mentions, 161 idle-loop lines, 81 `in_progress` lines | Back off polling and cache invariant metadata |
| Release analysis | Sequential heavy LLM passes | `workflow_log_analysis` run `26036110500`: 826s + 919s + 1,904s sequential | Lower reasoning first; only parallelize if report coupling can be relaxed safely |
| Merge / conflict / stale-head handling | Late or masked stale detection | `review_autofix` helper gates fail-opened in all 5 sampled slow runs | Re-enable helper scripts and make helper absence visible |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** `review_autofix` (54.7% of total runtime), `ci` (25.3%), and release-validation workflows (`workflow_log_analysis` + `test_and_mark_stable`, 8.5% combined).
- **Top failure modes:**  
  1. `implement.yml missing resolved-ref log output` (`26029332723`, `26031068317`, `26032757226`, `26034365768`, `26035468279`, `26035946559`)  
  2. `test_verify_integration_fingerprints_baseline_regressions` (`26037045079`, `26039161815`, `26040817342`, `26042958244`)
- **Highest-cost drivers:** six-reviewer `review_autofix` fan-out, stale/canceled `review_autofix` runs, and long poll/watch loops in release validation.
- **Top 3 prioritized actions:**
  1. Restore `review_autofix` stale-base helper scripts and early-exit stale/superseded runs.
  2. Fix the two recurring CI regressions, then split `ci.yml` into parallel jobs.
  3. Lower `workflow_log_analysis` reasoning and back off `test_and_mark_stable` polling; add token/cache telemetry so future tuning is measurable.

## Metrics Appendix

*Notes:*  
- MCP telemetry counts below exclude `workflow_log_analysis` logs because that workflow embeds prior analysis text that echoes telemetry markers and would otherwise create false positives.  
- Mirrored step logs were deduped by run + event line when counting operational MCP events.

### Run profile

| Scope / family | Runs | Success | Failure | Cancelled | Other | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Repo total | 1000 | 270 | 10 | 27 | 693 | 169.2 | 2.0 | 798.0 |
| `ci` | 57 | 47 | 10 | 0 | 0 | 750.2 | 769.0 | 817.0 |
| `review_autofix` | 110 | 85 | 0 | 22 | 3 | 841.3 | 559.0 | 2312.8 |
| Orchestrator wrappers (`clarify` + `plan` + `implement` + `orchestrate_clarify_respond`) | 730 | 37 | 0 | 5 | 688 | 12.3 | 1.0 | 17.0 |
| `orchestrate_poll` | 13 | 13 | 0 | 0 | 0 | 131.2 | 129.0 | 250.4 |
| `workflow_log_analysis` | 2 | 2 | 0 | 0 | 0 | 3521.0 | 3521.0 | 3735.2 |
| `test_and_mark_stable` | 2 | 2 | 0 | 0 | 0 | 3658.5 | 3658.5 | 3829.9 |

### CI failure buckets

| Failure point | Runs | Share of CI failures | Example evidence |
|---|---:|---:|---|
| `lint / Validation self-test unit tests` | 6 | 60% | `26031068317` → `AssertionError: implement.yml missing resolved-ref log output` in `errors/.../step-001-lint.log` |
| `lint / Orchestrate poll process unit tests` | 4 | 40% | `26042958244` → `FAIL test_verify_integration_fingerprints_baseline_regressions` in `errors/.../step-006-lint_Orchestrate_poll_process_unit_tests.log` |

### `review_autofix` churn and outlier metrics

| Metric | Value |
|---|---:|
| Total `review_autofix` runs | 110 |
| Cancelled `review_autofix` runs | 22 (20.0%) |
| Cancelled `review_autofix` runtime | 24,059s |
| Cancelled runtime share of `review_autofix` | 26.0% |
| Cancelled runtime share of all sampled runtime | 14.2% |
| Longest sampled `review_autofix` run | `26037045336` = 7,269s |
| Runner-wait example | `26037045336` system wait ≈ 4,601s (`13:38:07` → `14:54:48`) |
| Reviewer step example | `26037045336` reviewer step ≈ 1,210s |
| Editor step example | `26037045336` editor step ≈ 1,161s |

### Token and cache observability

| Metric | Observed value |
|---|---|
| `prompt_tokens` | Not emitted in sampled operational logs |
| `completion_tokens` | Not emitted |
| `total_tokens` | Not emitted |
| `cache_creation_input_tokens` | Not emitted |
| `cache_read_input_tokens` | Not emitted |
| Prompt cache config | `OPENROUTER_PROMPT_CACHE_DISABLED: false` seen in sampled `review_autofix` runs |
| Collector normalization | Current supplied analysis view carries duration/outcome/failure/log-summary fields, but not token/cache counters |

### GH API sampled hotspots

| Workflow / run / step | Observed pattern | Summary |
|---|---|---|
| `test_and_mark_stable` / `26036073220` / `step-011-e2e-smoke-test.log` | 74 `gh api` mentions, 7 `gh workflow run`, 81 `status=in_progress`, 161 idle-loop lines | Main polling hot spot |
| `internal-review` / `26044570751` / `step-003-resolve-claude-branch-pr...log` | 2 direct REST calls (`pulls?state=open&head=...` and repo `.default_branch`) | One call is redundant |
| `review_autofix_sweep` / `26044030565` / `step-003-sweep_Enumerate_open_PRs...log` | 1 paginated open-PR fetch + 4 active-run snapshot calls for 2 candidates | Good batching pattern |
| `issue_pr_status` / `26044990884` / `step-010-sync-status...log` | GraphQL batch for linked issues, REST fallback only where needed | Good GraphQL-first pattern |

### Semble / Serena telemetry (sampled operational logs)

| Server | Query count | Fallback count | Probe count | Logged query bytes | Response bytes | Runs observed | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| Semble | 7 | 10 | 0 | 83,893 | n/a | 5 query runs + 2 fallback test runs | Queries all in slow `review_autofix`; fallbacks all in validate-scripts tests |
| Serena | 0 | 0 | 0 | 0 | 0 | 0 | No operational Serena lines observed |
| Other MCP servers observed | 0 | 0 | 0 | 0 | 0 | 0 | None observed operationally |

### Per-target MCP availability

| Server | Target | `probe_ok` | `probe_failed` | `probe_skipped` | Other observed events | Note |
|---|---|---:|---:|---:|---|---|
| Semble | `reviewer-context` | 0 | 0 | 0 | 5 queries | No `*_PROBE` lines emitted |
| Semble | `conflict-resolver-context` | 0 | 0 | 0 | 1 query | No probe telemetry emitted |
| Semble | `overflow` | 0 | 0 | 0 | 1 query, 10 fallbacks | Fallbacks were test-fixture PASS cases |
| Serena | `<none observed>` | 0 | 0 | 0 | 0 | Sampled slow `review_autofix` runs showed `SERENA_ENABLED: false` / `SERENA_AVAILABLE: false` |

### AI memory telemetry

| Metric | Value |
|---|---:|
| Runs with memory telemetry | 5 |
| Total telemetry lines | 20 |
| `retrieve` ops | 5 |
| Retrieve hit rate | 0% |
| Avg `estimated_tokens` on retrieve | 0 |
| `keyword_method=none` | 5 |
| `keyword_method=plain` | 0 |
| `keyword_method=llm` | 0 |
| `record-candidate` ops | 5 |
| `record-run-event` ops | 10 |
| `fail_open: true` | 0 |
| `enabled: false` | 0 |
| Push attempts > 1 | 0 |


## Deep Audit — Workflows & Scripts (2026-05-18)

### Section 1: Bug & Correctness Sweep

Earlier sections of the report already cover the dominant correctness/reliability items (stale-helper fail-open, the two recurring CI regressions, and release-path polling). A repo-wide static sweep of `.github/workflows/*.yml`, `scripts/*.sh`, and `scripts/*.py` did not surface additional High/Critical correctness bugs in the current tree, but it did find the following shell-safety issue:

- **ID** — SHELL-001  
  **File path** — `scripts/review_commit_changes.sh:482-489; scripts/review_conflict_resolve.sh:1474-1475`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — Both scripts set the authenticated origin URL with an unquoted token-bearing argument: `git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}`. ShellCheck flags SC2086 at both sites. The current values are usually safe, but both commands still depend on word-splitting/globbing never occurring in a secret-bearing URL.  
  **Recommended fix** — Build the URL in a quoted variable and pass it once, e.g. `remote_url="https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}"` then `git remote set-url origin "$remote_url"`, or centralize that logic in a helper under `scripts/gh_helpers.sh`.

### Section 2: GitHub API Call Redundancy Audit

Already covered earlier in the report: the broad `test_and_mark_stable` polling hotspot and the redundant default-branch lookup in `internal-review.yml`. Additional static call-site findings:

- **ID** — API-001  
  **File path** — `.github/workflows/test-and-mark-stable.yml:2798-2806`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The cancel-on-close wait loop fetches `repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}` twice per poll iteration: once for `.status` and again for `.conclusion`. Current call count: **2 calls per poll iteration**. Proposed call count: **1 call per poll iteration** by fetching `{status, conclusion}` once. The same workflow already uses the single-call object pattern later at lines `3423`, `3570`, `3631`, `3694`, `3808`, and `4043`.  
  **Recommended fix** — Replace the two scalar fetches with one `gh api ... --jq '{status, conclusion}'`, then parse both fields locally.

- **ID** — API-002  
  **File path** — `scripts/orchestrate_poll_process.sh:3994-3999,4048-4050`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — Final-merge handling fetches the same PR endpoint three times before merge (`.state`, `.mergeable`, `.merged_at != null`) and three more times after merge failure. Current call count: **6 calls per finalize attempt**. Proposed call count: **2 calls total** (one pre-merge snapshot, one post-merge snapshot). The script already has `_fetch_pr_json()` / `_jq_field()` at `785-804`, which is the existing single-object fetch pattern to extend.  
  **Recommended fix** — Snapshot the PR once with `_fetch_pr_json "${final_pr}"`, derive `state`, `mergeable`, and `merged` from that JSON, and reuse the same approach for the post-merge refresh.

- **ID** — API-003  
  **File path** — `scripts/orchestrate_poll_process.sh:5768-5775,7570-7571,11082-11087`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — Three different recovery paths fetch the same issue twice back-to-back to read title and body separately. Current call count: **2 REST calls per issue at each site**. Proposed call count: **1 REST call per issue** by fetching both fields together. This is the same object-fetch consolidation opportunity already embodied by `_fetch_pr_json()` and `_safe_gh_jq`.  
  **Recommended fix** — Add a helper such as `_fetch_issue_title_body_json "$issue_num"` returning `{title, body}`, or inline one `_safe_gh_jq "repos/.../issues/${issue_num}" --jq '{title:(.title // \"\"), body:(.body // \"\")}'` and split locally.

- **ID** — API-004  
  **File path** — `scripts/orchestrate_poll_process.sh:6963-6969`  
  **Severity** — High  
  **Category tag** — `api-batching`  
  **Description** — The standalone stall sweep loops over 7 pipeline labels and runs `gh issue list --label ...` once for each label, then unions the results in `jq`. Current call count: **7 REST calls per sweep**. Proposed call count: **1 GraphQL/search batch per sweep**. This is a looped API pattern in the main poller and matches the “review-blocker” shape called out in `CLAUDE.md` §15. A one-call replacement is feasible by extending the multi-alias GraphQL pattern already used in `_fetch_standalone_marker_issues_graphql()` at `6378-6418` or the batch issue-query structure in `_fetch_candidate_issue_details_graphql()` at `6458-6574`. [NEEDS VERIFICATION]  
  **Recommended fix** — Replace the 7-call label scan with one batched GraphQL query (aliases for each label-search term, then `unique_by(.number)` client-side), keeping the current marker-issue merge logic unchanged.

- **ID** — API-005  
  **File path** — `scripts/orchestrate_poll_process.sh:10773-10800`  
  **Severity** — High  
  **Category tag** — `api-batching`  
  **Description** — After review-blocked handling, the script rebuilds `LABELS_JSON` by hitting `issues/${inum}/labels` once for every current wave issue and again for any reissued issues not already present. Current call count: **N + R REST calls** for `N` current issues and `R` reissued issues. Proposed call count: **1 GraphQL batch** over the union of those issue numbers. The needed helper already exists: `_fetch_issue_labels_batch_graphql()` at `1508-1565`.  
  **Recommended fix** — Build one JSON array containing the union of `ISSUE_NUMS` and `REISSUED_NUMS`, pass it to `_fetch_issue_labels_batch_graphql`, and fall back to per-issue REST only for missing keys.

- **ID** — API-006  
  **File path** — `.github/workflows/review_autofix.yml:514-566`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The standalone-validate short-circuit step first fetches linked issues via GraphQL with labels included. When that returns empty, the body-text fallback synthesizes issue numbers with `labels: null`, then line `540` performs `gh issue view ... --json labels` once per issue to recover label state. Current fallback-path call count: **1 GraphQL call + 1 PR REST fetch + N label lookups**. Proposed fallback-path call count: **1 GraphQL call + 1 PR REST fetch + 1 batched label query**. Existing batching patterns to extend are `_fetch_issue_labels_batch_graphql()` in `scripts/orchestrate_poll_process.sh` and the aliased issue-label query in `.github/workflows/issue_pr_status.yml:288-330`. [NEEDS VERIFICATION]  
  **Recommended fix** — After parsing fallback issue numbers, batch-fetch their labels in one GraphQL alias query and reuse the current TSV loop only for dispatch/edit decisions.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — DUP-001  
  **File path** — `.github/workflows/clarify.yml:56-129; .github/workflows/plan.yml:107-182; .github/workflows/implement.yml:265-338; .github/workflows/validate.yml:79-152; .github/workflows/orchestrate_clarify_respond.yml:90-163`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Five workflows carry near-identical “Resolve integration ref” bootstrap code: choose `stable` vs `${{ github.sha }}`, stage a temporary clone of `shubhodeep1/coding-workflows`, sanitize clone logs, and invoke `scripts/resolve_integration_ref.sh`. The resolver itself already exists as a canonical script in `scripts/resolve_integration_ref.sh:1-218`, but the checkout/bootstrap wrapper is duplicated and already drifting slightly (`plan.yml` emits an extra `integration_branch_meta` output).  
  **Recommended fix** — Move the bootstrap into a shared module such as `scripts/bootstrap_integration_ref.sh` with a signature like `bootstrap_integration_ref.sh --issue "$ISSUE_NUMBER" --repo "$GITHUB_REPOSITORY" --resolver-ref "$RESOLVER_REF" [--emit-meta]`, and update all five callers to consume its stdout/outputs.

- **ID** — DUP-002  
  **File path** — `.github/workflows/mark-stable.yml:418-440; .github/workflows/test-and-mark-stable.yml:4742-4764; scripts/mark-stable.sh:1-110`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Both release workflows inline the same tag/pointer mutations even though `scripts/mark-stable.sh` already owns the canonical release-tag flow, including extra safety checks that the workflows currently bypass. That leaves three places to keep release semantics in sync.  
  **Recommended fix** — Replace both inline workflow blocks with `bash scripts/mark-stable.sh "${VERSION}"` after the git identity setup, so `mark-stable.yml` and `test-and-mark-stable.yml` share one implementation.

- **ID** — DUP-003  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/mark-stable.yml:330-357,479-506; .github/workflows/orchestrate_poll.yml:79-113; .github/workflows/test-and-mark-stable.yml:4643-4672; scripts/gh_helpers.sh:391-446`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Multiple workflows embed bespoke `_rl_wait` / `_gh_retry` shells while the repo already has a richer canonical `gh_retry()` in `scripts/gh_helpers.sh`. The inline variants differ in stderr handling, breaker behavior, and permanent-failure detection, so retry semantics are inconsistent across workflows.  
  **Recommended fix** — Centralize on `scripts/gh_helpers.sh::gh_retry` via a tiny shared bootstrap module (for example `scripts/gh_retry_bootstrap.sh` exposing `gh_retry "$@"`), then update the listed workflows to source/use that shared implementation.

- **ID** — DUP-004  
  **File path** — `.github/workflows/test-and-mark-stable.yml:3374-3440,3550-3585,3613-3649,3672-3709,3784-3818,4015-4056`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Six steps repeat the same dispatch-and-watch structure: snapshot `PRE`, run `gh workflow run`, poll for `NEW_ID`, then watch `status/conclusion` until timeout. That duplication is already causing drift: some blocks use one-call `{status, conclusion}` snapshots while others still split fields or vary error handling.  
  **Recommended fix** — Extract a shared watcher such as `scripts/watch_dispatched_workflow.sh --repo <repo> --workflow <file> --register-timeout <secs> --deadline <secs> [--field k=v ...] [--accept success,skipped] [--nonblocking]`, and have each listed step call it.

- **ID** — DUP-005  
  **File path** — `scripts/validate_process.sh:1152-1204,1787-1875; scripts/validate_driver.sh:212-296,384-438`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — `validate_process.sh` and `validate_driver.sh` each implement their own failure accumulator / JSON result emitter and overlapping validation preflight logic. The shapes are close enough to drift riskily, but different enough that fixes must currently be ported twice.  
  **Recommended fix** — Extract a shared `scripts/validation_result_helpers.sh` with functions like `append_failure <failures_file> <test_name> <error> <log_source> [tail_lines]` and `emit_result <result> <phase> <total> <passed> <failed> <failures_file> <start_ts>`, plus a small shared preflight helper for `python3`/`docker`/`docker compose config` checks; update both callers to source it.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — EXPR-001  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1203-1587`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — `Phase 4: Wait for review & autofix to complete` has an estimated interpolated `run:` body size of **19,899 chars**, leaving only **~1,101 chars** of headroom before GitHub’s 21,000-character expression cap. This is already above the 85% threshold and mixes a long poll loop with many `${{ }}` insertions and inline diagnostics.  
  **Recommended fix** — Extract the whole wait loop to an external script such as `scripts/wait_for_review_run.sh`, passing only environment variables from YAML. Follow the same “move large run logic into scripts/” pattern already used for `scripts/orchestrate_poll_process.sh`.

- **ID** — EXPR-002  
  **File path** — `.github/workflows/validate.yml:210-583`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — `Fetch workflow support files` has an estimated interpolated `run:` body size of **17,416 chars**, leaving **~3,584 chars** of headroom. The block inlines clone/copy helpers, many `${{ github.repository }}` branches, and a long file-copy manifest, so routine feature growth can push it over the limit.  
  **Recommended fix** — Move this step into `scripts/fetch_workflow_support_files.sh` or a composite action, and keep the workflow step as thin env/input wiring.

- **ID** — EXPR-003  
  **File path** — `.github/workflows/review_autofix.yml:1476-1865`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — `Collect PR metadata` has an estimated interpolated `run:` body size of **17,408 chars**, leaving **~3,592 chars** of headroom. It combines retry helpers, PR/comment fetches, linked-issue fallback parsing, file generation, and diff collection in one templated block.  
  **Recommended fix** — Extract the step to `scripts/collect_pr_metadata.sh` and have the workflow pass only the needed env vars and output paths.

- **ID** — EXPR-004  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1673-2078`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — `Phase 4b: Verify editor restored canary (pytest + retry)` has an estimated interpolated `run:` body size of **17,408 chars**, leaving **~3,592 chars** of headroom. The step inlines installer fallback, API retry helpers, fetch functions, pytest classification, redispatch logic, and a retry poll loop.  
  **Recommended fix** — Extract this logic to `scripts/verify_editor_canary.sh`, or split it into separate fetch/verify/retry steps with smaller `run:` bodies.

No workflow file exceeded the 800 KB warning threshold; the largest current workflow is `.github/workflows/review_autofix.yml` at **345,188 bytes**.

### Section 5: Cross-Cutting Concerns

No `TODO` / `FIXME` / `HACK` markers were found under `.github/workflows/` or `scripts/`. Retry-wrapper inconsistency is already captured in **DUP-003**; the shellcheck sweep did not produce additional high-signal findings beyond **SHELL-001**.

- **ID** — DEAD-001  
  **File path** — `scripts/orchestrate_poll_process.sh:10377-10414; scripts/orchestrate_poll_process.sh:10824-10880`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `RB_FOLLOWUP_REFUSED` and `IF_BLOCKERS_SOURCE` are assigned, but a repository-wide search only finds those assignment sites and no reads. That makes the code look like it records follow-up refusal provenance and blocker provenance, but neither value affects state writes, comments, or branching.  
  **Recommended fix** — Either remove both variables and their assignments, or persist them into `STATE_FILE` / operator comments and add tests that prove the recorded provenance is consumed.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | API-004, API-005, EXPR-001 |
| Medium | 12 | API-001, API-002, API-003, API-006, DUP-001, DUP-002, DUP-003, DUP-004, DUP-005, EXPR-002, EXPR-003, EXPR-004 |
| Low | 2 | SHELL-001, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 11 | Large |
| Expression size reduction | 3 | Medium |
| Medium/Low fixes | 3 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-18)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is statically proven safe in the same code path with no mutation, retry, concurrency, pagination, or cache-contract change; `NEEDS_VERIFICATION` means the overlap is plausible but one or more SAFE preconditions are not proven from repo-local reading alone; `RISKY_SKIP` means the call sits in a poll/retry/race-defense/pagination/log-contract-sensitive path and must not be auto-implemented from this pass.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — NEEDS_VERIFICATION
- **File path / lines:** `.github/workflows/test-and-mark-stable.yml:443-447` and `.github/workflows/test-and-mark-stable.yml:449-449` (`Create E2E test issue` step)
- **Current call count:** 2 calls in the step.
- **Proposed call count:** 1 call in the step.
- **Endpoint(s):** `POST /repos/{repo}/issues` (via `gh api ... -f ...`), then `GET /repos/{repo}/issues/{issue_number}`.
- **Evidence:**
  ```bash
  ISSUE_NUMBER=$(gh api "repos/${TEST_REPO}/issues" \
    -f title="${TITLE}" \
    -f body="${BODY}" \
    --jq '.number')

  ISSUE_URL=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '.html_url')
  ```
  The second call is only used to populate `ISSUE_URL` for the log line at `.github/workflows/test-and-mark-stable.yml:452`.
- **Proposed fix:** In the `Create E2E test issue` step, capture both `.number` and `.html_url` from the create response in one parse (for example `--jq '[.number, .html_url] | @tsv'`), then populate `ISSUE_NUMBER` and `ISSUE_URL` locally and fail the step if either field is empty.
- **Safety rationale:** `NEEDS_VERIFICATION` because the consolidation depends on the create-issue response carrying `.html_url` under the repo’s current `gh api` defaults, which is not proven from repo-local code alone.
- **Downstream signal:** Verify on a disposable repo or recorded fixture that this exact `gh api "repos/${TEST_REPO}/issues" -f ...` response includes non-empty `.number` and `.html_url`; only then collapse the GET into the POST response parse and keep an explicit empty-field failure check.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — SAFE_TO_MERGE
- **File path / lines:** `scripts/review_rb_judge.sh:221-223` and `scripts/review_rb_judge.sh:253-256` (top-level linked-issue fallback block)
- **Current call count:** 2 `GET /pulls/{pr}` calls on the `ISSUE_NUMBERS` fallback path.
- **Proposed call count:** 1 `GET /pulls/{pr}` call on the normal fallback path; keep the second read only when the cached JSON is empty/invalid.
- **Endpoint(s):** `GET /repos/{repo}/pulls/{pull_number}`.
- **Evidence:**
  ```bash
  _pr_meta="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" 2>/dev/null || echo '{}')"
  _pr_state="$(printf '%s\n' "${_pr_meta}" | jq -r '.state // ""')"
  _pr_merged="$(printf '%s\n' "${_pr_meta}" | jq -r '(.merged_at != null) or (.merged == true)')"
  ...
  unset _pr_meta _pr_state _pr_merged
  ...
  if [ -z "${ISSUE_NUMBERS}" ]; then
    PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' || echo "")"
  fi
  ```
  The earlier `_pr_meta` snapshot already contains the later-needed `.title` and `.body`.
- **Proposed fix:** Reuse `_pr_meta` to derive `PR_DATA` before unsetting it, then move `unset _pr_meta _pr_state _pr_merged` below the fallback block; preserve the existing `_safe_gh_jq ... --jq '.title + " " + (.body // "")'` call only when `_pr_meta` is empty or fails `jq`.
- **Safety rationale:** `SAFE_TO_MERGE` because both reads hit the same PR endpoint in the same top-level flow, no intervening mutation changes the PR, and retaining the current second read on invalid-cache fallback preserves the existing fail-open/non-retried behavior.
- **Downstream signal:** Reuse `_pr_meta` for `PR_DATA` in `scripts/review_rb_judge.sh`, move the `unset _pr_meta...` below the fallback block, and keep the current `_safe_gh_jq` call only when `_pr_meta` is empty or unparsable.

#### REUSE-002 — NEEDS_VERIFICATION
- **File path / lines:** `scripts/implement_diagnose_post_codex_failure.sh:166-172` and `scripts/implement_diagnose_post_codex_failure.sh:261-272` (top-level post-Codex diagnose flow)
- **Current call count:** 2 `GET /issues/{issue}` calls on the `ISSUE_META_FILE` cache-miss path.
- **Proposed call count:** 1 `GET /issues/{issue}` call on the same cache-miss path; keep the later body-only GET only when the shared JSON is empty/invalid.
- **Endpoint(s):** `GET /repos/{repo}/issues/{issue_number}`.
- **Evidence:**
  ```bash
  if [ -z "${ISSUE_LABELS_JSON}" ]; then
    ISSUE_LABELS_JSON="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}" --jq '[.labels[].name]' || echo '[]')"
  fi
  ...
  gh_retry gh issue edit "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" \
    --add-label 'ai:implementation-failed' \
    --remove-label 'ai:implementing' \
    --remove-label 'ai:awaiting-approval' >/dev/null 2>&1 || \
  gh_retry gh issue edit "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" \
    --add-label 'ai:implementation-failed' >/dev/null 2>&1 || true
  ...
  if [ ! -s "${ISSUE_BODY_FILE}" ]; then
    gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}" --jq '.body // ""' > "${ISSUE_BODY_FILE}" || printf '' > "${ISSUE_BODY_FILE}"
  fi
  ```
  On cache miss, the script re-reads the same issue later for `.body`, but it also mutates labels between the two reads.
- **Proposed fix:** Add a cache-miss-only `FALLBACK_ISSUE_META_JSON` fetch in `scripts/implement_diagnose_post_codex_failure.sh`, parse `[.labels[].name]` from it for the early label check, and later parse `.body // ""` from that same JSON before falling back to the existing body-only GET when the shared JSON is empty/invalid.
- **Safety rationale:** `NEEDS_VERIFICATION` because the script edits issue labels between the two reads, so reusing the earlier snapshot is only safe if downstream logic truly depends on `.body` alone and never on refreshed label state.
- **Downstream signal:** Verify on the `ISSUE_META_FILE` cache-miss path that reusing a pre-label-edit issue snapshot yields the same `ISSUE_BODY_FILE`, `TRACKING_ISSUE_NUM`, and diagnose prompt inputs as the current second GET; if it does, replace the two GETs with one shared JSON fetch plus the existing body-only GET as invalid-cache fallback.

### Dead Calls (DEAD-API-###)
No findings.

### Cross-References to Deep Audit Section
- API-001: RISKY_SKIP — inside the `test-and-mark-stable` cancel/wait poll loop, so this pass should not auto-change polling semantics.
- API-002: RISKY_SKIP — lives in `scripts/orchestrate_poll_process.sh` final-merge race-defense logic, which the prompt explicitly excludes from SAFE treatment.
- API-003: RISKY_SKIP — lives in `scripts/orchestrate_poll_process.sh` recovery handling and would change a race-sensitive fail-open path.
- API-004: RISKY_SKIP — batching the 7-label sweep would alter pagination/query semantics inside the standalone stall watchdog in `scripts/orchestrate_poll_process.sh`.
- API-005: RISKY_SKIP — the label rebuild sits in `scripts/orchestrate_poll_process.sh` current-wave/reissue handling, so it requires manual review rather than auto-implementation.
- API-006: NEEDS_VERIFICATION — batching fallback label recovery in `review_autofix.yml` looks valid, but GraphQL partial-miss and fail-open parity need to be proven first.

### Summary Counts
| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 1 | REUSE-001 |
| NEEDS_VERIFICATION | 2 | MERGE-001, REUSE-002 |
| RISKY_SKIP | 0 | — |

### Implement-Stage Handoff
- REUSE-001
