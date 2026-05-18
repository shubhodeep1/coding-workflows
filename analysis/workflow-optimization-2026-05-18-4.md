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

