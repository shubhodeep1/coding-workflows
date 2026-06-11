## Executive Summary

Window is sufficient (`insufficient_data=false`); only `shubhodeep1/coding-workflows` is in scope.

- Collapse unconditional pass-2 review fan-out in `review_autofix`. Family rollup shows `51` runs with `p95=2758s`; heavy run `27326479941` (`Internal: AI Review & Autofix`) spent `2039.4s` in `step-046-review_codex-agent_Run_reviewer_models.log`, with pass 2 alone taking `935.6s`. Estimated impact: **15-20 minutes faster** on heavy review runs and materially fewer review-model calls. Confidence: **high**.
- Put a hard budget around `implement` runs. Run `27326124830` (`Internal: AI Implement`) used `2,652,382` Codex tokens across `26` calls in `692s`—over **99.7%** of repo Codex tokens in this window (`2,658,461`). Estimated impact: **265k-664k tokens saved per similar run** with a 10-25% cap. Confidence: **medium**.
- Fix the post-Codex diagnosis regression first. CI runs `27325582989` and `27328440076` both failed `step-021-lint_Implement_post-Codex_recovery_unit_tests.log` with the same two tests; CI family failure rate is `22.2%` (`2/9`). Estimated impact: **removes 2 of 3 observed failures** in the window. Confidence: **high**.
- GH API waste is concentrated and fixable. In run `27326479941`, `step-002-review_gate.log` called the same paginated PR-files endpoint twice, and `step-032-review_codex-agent_Collect_PR_check-run_failures_CI_lint_autofix_context.log` spent `141.6s` polling to produce only `344` bytes of context. Estimated impact: **1-2 minutes saved** on heavy reviews plus lower API pressure. Confidence: **high**.
- Memory and cache systems are safe but underperforming. Deep-dive `AI_MEMORY_TELEMETRY` showed `0/9` retrieve hits, average `estimated_tokens=0`, and repo-wide `cache_hit_rate=null`; all `108` recorded OR/OpenRouter calls also had `0` OR token totals recorded. Estimated impact: **medium cost/latency improvement once fixed**, plus much better cost visibility. Confidence: **high**.
- Control-plane churn is inflating queue time. All `277` repo `other/skipped` runs came from `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`; `orchestrate_poll` still costs `p50=159s` across `31/31` successful runs, and `10` log-summary rows mention runner waits. Estimated impact: **tens of seconds per short workflow** plus lower queue noise. Confidence: **medium-high**.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Make pass 2 conditional and enable the existing reviewer risk tier** (**critical-path**)
   - **Evidence:** `review_autofix` has `p95=2758s` across `51` runs. Heavy run `27326479941` lasted `2875s`; four steps consumed `2725.8s` (`94.8%`) of that run: `Run reviewer models` `2039.4s`, `Apply fixes with editor model` `450.0s`, `Collect PR check-run failures...` `141.6s`, and `Free disk space` `94.7s`. In `step-046-review_codex-agent_Run_reviewer_models.log`, the workflow logged `REVIEWER_RISK_TIER: ... reviewers=6 enabled=false reason=disabled`, `Two-pass review enabled`, pass 1 `1102.6s`, and pass 2 `935.6s`.
   - **Root cause:** unconditional two-pass, six-reviewer fan-out even when the built-in risk-tier machinery already exists.
   - **Exact change:** turn on the existing risk-tier gate for non-critical diffs, and only run pass 2 when pass 1 shows reviewer disagreement, high-confidence findings, or files matching the existing `REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX`.
   - **Estimated time savings:** `~936s` on run `27326479941` from pass 2 alone; **15-20 minutes** on similar heavy reviews.
   - **Implementation risk:** **medium**. Keep full fan-out for `scripts/`, `.github/workflows/`, `prompts/`, `ai-memory/`, and other forced-full paths.

2. **Shorten check-run polling and stop fetching log tails before they are needed** (**critical-path**)
   - **Evidence:** In run `27326479941`, `step-032-review_codex-agent_Collect_PR_check-run_failures_CI_lint_autofix_context.log` used `CHECK_RUNS_WAIT_TIMEOUT_SECS=300`, `CHECK_RUNS_POLL_INTERVAL_SECS=20`, and `CHECK_RUNS_LOG_TAIL_BYTES=16384`; it then slept `20s`, `40s`, and `80s`, spent `141.6s` total, and emitted only `Check-run context bytes: 344`.
   - **Root cause:** long poll windows and eager log-tail work for very small final context.
   - **Exact change:** cap this context collector at `90-120s`, fetch once on terminal state, and set `CHECK_RUNS_LOG_TAIL_BYTES=0` unless a required check actually failed.
   - **Estimated time savings:** **60-120s** per heavy review run.
   - **Implementation risk:** **low**.

3. **Fix the missing consolidator prompt and shrink editor input before the editor model runs** (**critical-path**)
   - **Evidence:** In run `27326479941`, `step-054-review_codex-agent_Apply_fixes_with_editor_model.log` showed `stage=consolidator ... missing=review-consolidator.txt failopen=1`, `stage=parser ... parse_failed=1`, `./pre_assembled_static.txt bytes: 151522`, `editor_prompt_body.txt bytes: 156983`, `reviewer_bundle.txt bytes: 22613`, and `Editor prompt bytes: 308899`. The same `missing=review-consolidator.txt` and `parse_failed=1` pattern appeared in `7` unique slow review runs: `27258415267`, `27260525159`, `27319734128`, `27319768240`, `27321277175`, `27322762742`, `27326479941`. The repo does contain `prompts/review-consolidator.txt`, but `scripts/stage_workflow_support.sh` does not stage it.
   - **Root cause:** support-file staging gap plus raw reviewer output being passed through to the editor path.
   - **Exact change:** stage `prompts/review-consolidator.txt` in `scripts/stage_workflow_support.sh`, assert it in preflight, and feed the editor a condensed consolidator output instead of the full raw bundle.
   - **Estimated time savings:** **60-180s** on heavy review runs (**inference**, because per-call editor tokens are not logged).
   - **Implementation risk:** **low**.

4. **Gate `Free disk space` behind actual disk pressure** (**micro-optimization**)
   - **Evidence:** In run `27326479941`, `step-029-review_codex-agent_Free_disk_space.log` took `94.7s`.
   - **Root cause:** always-on maintenance in a workflow already dominated by model time.
   - **Exact change:** run the cleanup only when free space is below a threshold or when checkout/artifact size exceeds a threshold.
   - **Estimated time savings:** **up to 95s** on heavy reviews.
   - **Implementation risk:** **low-medium**.

5. **Reduce control-plane runner waits and no-op dispatches** (**flow optimization**)
   - **Evidence:** `clarify` has `76` runs with `72` `other/skipped`; `plan` `70/66`; `implement` `71/67`; `orchestrate_clarify_respond` `72/72`. Those four families account for **all `277` repo `other/skipped` runs**. `orchestrate_poll` still has `31/31` success with `p50=159s`, `p95=291s`. `10` log-summary rows mention runner waits, including `orchestrate_poll` run `27326545610`, `clarify` run `27325801336`, `implement` run `27326124830`, and `issue_pr_status` run `27328437396`.
   - **Root cause:** lightweight/no-op workflows are still being dispatched and waiting for runners.
   - **Exact change:** tighten parent-workflow `if:` conditions before dispatching child workflows, and add concurrency groups to collapse superseded `poll`/`clarify`/`review_autofix` control runs before they acquire a runner.
   - **Estimated time savings:** **10-60s** on short workflows, plus queue relief.
   - **Implementation risk:** **medium**.

## Cost Optimizations

Ranked by expected token/dollar savings. Dollar precision is limited because repo telemetry recorded `108` OR/OpenRouter calls but `0` OR prompt/completion/total tokens.

1. **Add a hard budget and stop condition to `implement`**
   - **Evidence:** Run `27326124830` (`Internal: AI Implement`) used `2,652,382` Codex tokens across `26` calls in `692s`, while the repo total was `2,658,461` tokens and `31` Codex calls. `implement` family rollup is therefore effectively the entire Codex spend in this window.
   - **Root cause:** **inference** — the logs expose the outlier but not the per-call prompt composition, so the likely drivers are prompt/context expansion or too many sequential Codex turns.
   - **Exact change:** add a per-run token/call ceiling to `implement`; once exceeded, stop with a resumable “needs-human/needs-follow-up” outcome instead of continuing the expensive loop.
   - **Estimated savings:** on runs like `27326124830`, **10%** saves `~265k` tokens; **25%** saves `~663k` tokens.
   - **Quality-risk notes:** **medium**. Safe if the run exits with a clear partial-result summary rather than silent truncation.

2. **Reduce reviewer-model fan-out in `review_autofix`**
   - **Evidence:** `review_autofix` is the only family with OR calls: `108` recorded calls in family rollup, equal to the repo total. Heavy run `27326479941` logged `24` OR calls and `2` Semble queries totaling `30,694` bytes. `step-046-review_codex-agent_Run_reviewer_models.log` shows `6` reviewers in pass 1, a pass-1 summariser, `6` reviewers in pass 2, and a second summariser: at least **14 review-model invocations** before the editor phase.
   - **Root cause:** two-pass full-width panel as the default.
   - **Exact change:** same as Speed #1 — make pass 2 conditional and enable risk tier for safe paths.
   - **Estimated savings:** skipping pass 2 removes **6 reviewer calls + 1 summariser call** on heavy runs; overall **40-60% review-model cost reduction** on eligible PRs.
   - **Quality-risk notes:** **medium**. Keep full fan-out for forced-full paths and disagreement cases.

3. **Shrink editor prompts and lower reasoning only when needed**
   - **Evidence:** Run `27326479941` built a `308,899`-byte editor prompt. Recent `review_autofix` log-summary run `27324325637` also logged `EDITOR_REASONING_EFFORT: xhigh`. Meanwhile, six `review_autofix` log summaries (`27324325637`, `27325581441`, `27325583108`, `27328403402`, `27328437413`, `27328440238`) printed `CONTEXT_BUDGET_WARN_RATIO: 0.7`, but actual aggregate `context_budget_warn_count` remained `0`.
   - **Root cause:** oversized raw prompt bodies and a max-reasoning editor path staying on too often.
   - **Exact change:** after the consolidator is fixed, keep the stable prompt prefix but strip raw duplicate reviewer text, and default the editor to `high` reasoning; escalate to `xhigh` only after a failed patch/apply attempt.
   - **Estimated savings:** **10-20%** of editor-side tokens/latency on heavy reviews (**inference**, because editor token totals are not emitted).
   - **Quality-risk notes:** **low-medium**. Escalation-on-retry preserves the harder cases.

4. **Extend deterministic skip beyond docs-only diffs**
   - **Evidence:** Docs-only `review_autofix` runs `27328415047` and `27328403402` logged `AUTOFIX_GATE_DET_SKIP_EVAL ... doc_only=true ... skip=true` and finished in `29s` and `70s` respectively, versus `review_autofix` family `p95=2758s`.
   - **Root cause:** the current gate already proves that early semantic skips save large amounts of work, but it is narrow.
   - **Exact change:** cautiously add other safe skip classes such as metadata-only, manifest-only, or generated-status-only diffs, while preserving the current forced-full file classes.
   - **Estimated savings:** entire-review avoidance on eligible PRs; measured docs-only cases show **minutes to tens of minutes** saved when it triggers.
   - **Quality-risk notes:** **low-medium**. This is an **inference**; no missed eligible PR was directly observed in the sample.

5. **Eliminate the duplicate issue GET on the post-Codex fallback path**
   - **Evidence:** `scripts/implement_diagnose_post_codex_failure.sh` reads issue labels via `gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}"` at lines `167-172`, then issue body through a separate GET at lines `272-275`. All six reviewers in run `27326479941` recorded consensus memory around the same defect, and the two failing CI tests in runs `27325582989` and `27328440076` point at the same logic.
   - **Root cause:** split fallback reads after removal of a shared issue snapshot/cache helper.
   - **Exact change:** fetch the issue JSON once on fallback, derive both labels and body from that single payload, and keep the current safe empty defaults if parsing still fails.
   - **Estimated savings:** **1 GitHub issue GET per fallback invocation** plus lower rerun risk.
   - **Quality-risk notes:** **low**.

6. **Keep Semble for bounded reviewer context, but suppress overflow noise; Serena is not active yet**
   - **Evidence:** Repo aggregate shows `15` Semble calls, `173,848` logged bytes, and `0` fallbacks. Example: run `27326479941`, `step-046-review_codex-agent_Run_reviewer_models.log`, logged `SEMBLE_QUERY target=reviewer-context chunks=12 bytes=15347 ms=520`. That is small relative to the `308,899`-byte editor prompt, so **inference:** Semble is probably reducing prompt expansion rather than inflating it. By contrast, overflow examples in slow runs `27260525159` and `27258415267` each pulled `6464` bytes for `.github/workflows/test-and-mark-stable.yml`, which looks low-value. Serena had `0` query calls, `0` response bytes, `0` fallbacks, `0` probes; seven recent `review_autofix` log summaries explicitly logged `SERENA_ENABLED: false`.
   - **Root cause:** Semble target selection is mostly good, but overflow fetches can still add noise; Serena is simply not participating.
   - **Exact change:** keep `reviewer-context` queries, but disable overflow retrieval unless the reviewer/editor explicitly references the file; do not spend effort tuning Serena until it is enabled in production paths.
   - **Estimated savings:** direct byte savings are **small** (at least `12,928` bytes across the two sampled overflow runs), but this keeps low-value context from growing.
   - **Quality-risk notes:** **low**.

**Visibility note:** prompt-cache and OR cost telemetry are incomplete in this window. Repo aggregate has `cache_hit_rate=null`, `or_prompt_tokens=0`, `or_total_tokens=0`, `or_cache_write_tokens=0`, and `or_cache_read_tokens=0` despite `108` recorded OR calls. No actual `CONTEXT_BUDGET_WARN` events were observed (`context_budget_warn_count=0`).

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

1. **Restore a single cached issue snapshot on the post-Codex diagnosis fallback path**
   - **Failure evidence:** CI runs `27325582989` and `27328440076` both failed `workflow=CI`, `job=lint`, `step=Implement post-Codex recovery unit tests`; `step-021-lint_Implement_post-Codex_recovery_unit_tests.log` ended with `68 passed, 2 failed, 70 total`, failing `test_diagnose_prompt_contract_round_trip_and_fixup_metadata` and `test_diagnose_uses_safe_issue_api_body_fallback_when_issue_meta_invalid_or_mismatched`.
   - **Root cause category:** regression in fallback data-source reuse / inconsistent issue snapshot handling.
   - **Exact fix:** fetch the issue JSON once on fallback, reuse it for both labels and body, and keep the current safe empty defaults plus an explicit warning when the fetch or parse fails.
   - **Expected reliability impact:** likely removes **2 of the 3 observed failures** in this window and reduces CI family failure rate from `22.2%`.
   - **Rollback / fail-open:** if the single fetch is bad, keep fail-open labels `[]` and empty body, but log the degradation.

2. **Clean or relocate generated workspace files before branch checkout**
   - **Failure evidence:** review/autofix run `27263032560` failed `workflow=Internal: AI Review & Autofix`, `job=review / codex-agent`, `step=Checkout PR head branch`; `step-001-review_codex-agent.log` showed `error: Your local changes ... would be overwritten by checkout: .ai/.workspace_source_manifest.txt`, then `Aborting`.
   - **Root cause category:** workspace cleanliness / generated-file branch-switch hygiene.
   - **Exact fix:** either write `.ai/.workspace_source_manifest.txt` outside the tracked working tree, or explicitly reset/remove that file before branch checkout and regenerate it afterward.
   - **Expected reliability impact:** removes the only hard `review_autofix` failure observed in the window.
   - **Rollback / fail-open:** regenerate the manifest post-checkout if the pre-clean step is too aggressive.

3. **Stage `review-consolidator.txt` and stop silently degrading the review parser**
   - **Failure evidence:** `missing=review-consolidator.txt` plus `parse_failed=1` repeated in `7` slow review runs: `27258415267`, `27260525159`, `27319734128`, `27319768240`, `27321277175`, `27322762742`, `27326479941`.
   - **Root cause category:** support-file packaging/staging gap.
   - **Exact fix:** add `prompts/review-consolidator.txt` to `scripts/stage_workflow_support.sh`, assert it during preflight, and convert the current silent parser fail-open into an explicit “degraded review structure” warning.
   - **Expected reliability impact:** fewer silently degraded review/editor passes and fewer follow-on reruns or bad autofixes.
   - **Rollback / fail-open:** keep the current raw-bundle fallback only as the explicit degraded path.

4. **Memoize missing log archives in `drift_audit`**
   - **Failure evidence:** run `27323925287` (`Drift Audit`) succeeded in `56s`, but its `log_summary` recorded four consecutive `log fetch failed: log not found` warnings while scanning recent runs.
   - **Root cause category:** repeated fail-open fetches against missing log archives.
   - **Exact fix:** cache “log not found” results per run/archive ID for the duration of the scan and skip repeat fetches.
   - **Expected reliability impact:** lower noisy-warning rate and lower chance of audit flakiness under API pressure.
   - **Rollback / fail-open:** keep current warning-only behavior if the memoization misses a rare late-arriving archive.

5. **Update deprecated cache actions and clean benign post-job warnings**
   - **Failure evidence:** review runs including `27324325637` log the warning `Node.js 20 is deprecated... actions/cache/restore@v4, actions/cache/save@v4`; failed/summary runs also showed `fatal: /usr/lib/git-core/git-submodule cannot be used without a working tree.` (e.g. failed `27263032560`, implement `27326124830` summary).
   - **Root cause category:** runtime/dependency drift and cleanup noise.
   - **Exact fix:** move to the current supported `actions/cache` release line and guard post-job submodule cleanup behind “working tree present”.
   - **Expected reliability impact:** mostly preventative, but it removes known future breakpoints and noisy warning channels.
   - **Rollback / fail-open:** low risk; keep the current version pins available until the new cache action is green.

**Policy / fallback signals:** repo aggregate had `break_glass_count=0` and `context_budget_warn_count=0`; no `BREAK_GLASS:` lines were observed. Six `review_autofix` log summaries printed only the threshold `CONTEXT_BUDGET_WARN_RATIO: 0.7`, which is configuration, not an event. Semble also showed `0` fallbacks; Serena showed `0` queries, `0` fallbacks, and `0` probes. That looks like disabled/absent coverage rather than a broken rollout, especially because seven recent `review_autofix` summaries logged `SERENA_ENABLED: false`. Three `orchestrate_poll` summaries (`27326545610`, `27326635345`, `27328474705`) logged `SEMBLE_AVAILABLE: false` / `SEMBLE_INDEX_AVAILABLE: false`, but the runs still succeeded; the smallest safe mitigation is clearer preflight status, not rollback.

## AI Memory Health

`AI_MEMORY_TELEMETRY` is present in the deep-dive sample, but retrieval quality is poor.

- **Retrieve hit rate:** `0/9 = 0%` across `9` unique retrieve operations, from runs `27263032560`, `27260525159`, `27258415267`, `27319734128`, `27319768240`, `27321205609`, `27321277175`, `27322762742`, `27326479941`. Example: run `27326479941`, `step-031-review_codex-agent_Retrieve_reviewer_memory_context_fail-open.log`, logged `records_selected=0`.
- **Average `estimated_tokens` vs budget:** average `estimated_tokens` was `0`; no retrieve-budget field was emitted in the sampled telemetry, so budget comparison is **not available**.
- **`keyword_method` distribution:** `llm` on `9/9` unique retrieve ops (`100%`); `plain` and `none` did not appear.
- **Health flags:** no sampled retrieve had `fail_open: true`; none had `enabled: false`; no retrieve error states were observed.
- **Write behavior:** sampled telemetry contained `20` `record-run-event` ops and `8` `record-candidate` ops. Push retries were usually `1`; one `record-candidate` in run `27319768240` needed `push_attempts=2`.
- **Missing op types:** no sampled `finalize-task`, `promote`, `compact`, `processed-command-claim`, or `processed-command-complete` telemetry was observed.
- **Latency overhead:** in heavy review run `27326479941`, memory-related steps took about `67s` total (`step-026` `~19.8s`, `step-031` `~8.3s`, `step-050` `~19.4s`, `step-067` `~19.5s`) while retrieval still returned zero records.

**Recommendation:** keep the write path, but short-circuit reviewer retrieval when the recent hit rate stays below a threshold (for example `<5%` over the last N runs), and batch or defer non-critical writes where possible. Also emit a retrieval-budget field so memory usefulness can be evaluated against prompt pressure.

## GH API Call Audit

No repository-specific API hygiene rules were provided in this window; the audit below uses observed workflow behavior.

1. **Reuse the PR-files payload inside `review_gate`**
   - **Evidence:** run `27326479941`, `step-002-review_gate.log`, called `gh api --paginate "repos/${REPOSITORY}/pulls/${PR_NUMBER}/files"` twice in the same step.
   - **Why it matters:** this is the clearest in-step API redundancy in the sample.
   - **Concrete change:** fetch the paginated PR-files JSON once, store it in a temp file or shell variable, and reuse it for all downstream checks in that step.
   - **Estimated reduction:** **1 paginated PR-files series per `review_gate` run**; **50%** reduction for that endpoint in that step.

2. **Collapse the fallback issue lookup to one GET**
   - **Evidence:** `scripts/implement_diagnose_post_codex_failure.sh` issues one fallback GET for labels and another for body; run `27326479941` reviewer consensus flagged the same defect.
   - **Why it matters:** unbatched duplicate GETs waste calls and can read inconsistent issue state if the issue changes mid-run.
   - **Concrete change:** fetch one issue payload, derive both body and labels from it.
   - **Estimated reduction:** **1 issue GET per fallback invocation**.

3. **Reduce check-run poll churn and per-check tail fetches**
   - **Evidence:** run `27326479941`, `step-032-review_codex-agent_Collect_PR_check-run_failures_CI_lint_autofix_context.log`, waited `20s`, `40s`, `80s`, then produced `344` bytes.
   - **Why it matters:** the collector already batches check-run listing with `gh api --paginate --slurp`, but it still spends time and follow-on calls before useful failure context exists.
   - **Concrete change:** lower the timeout, fetch on terminal state, and only tail logs for failed required checks.
   - **Estimated reduction:** **3+ poll reads** plus some tail-download work per heavy review run.

4. **Memoize missing log archives during drift scans**
   - **Evidence:** run `27323925287` hit four `log fetch failed` events in one `Drift Audit`.
   - **Why it matters:** repeated missing-log fetches are pure overhead.
   - **Concrete change:** treat “log not found” as a cached negative result for the remainder of the scan.
   - **Estimated reduction:** **4 log-download attempts** on that run.

5. **Overall rate-limit posture looks local, not systemic**
   - **Evidence:** run `27328437396` (`Internal: Issue-PR Status Sync`) explicitly reported no secondary rate limits, no backoff events, and no retries.
   - **Implication:** fix the redundant patterns above first; the sample does **not** show repo-wide API saturation.

## Prompt Cache & Memory System

1. **Prompt-cache visibility is currently inadequate**
   - **Evidence:** repo aggregate has `cache_hit_rate=null`, `or_cache_write_tokens=0`, `or_cache_read_tokens=0`, and OR token totals at `0` despite `108` OR calls.
   - **Impact:** current dashboards cannot tell whether prompt-prefix caching is working or whether prompt variance is destroying reuse.
   - **Concrete improvement:** emit cache read/write/hit metrics from reviewer/editor model wrappers before tuning model mix.
   - **Estimated impact:** indirect but high-leverage; this is the minimum needed to measure future token and latency savings.

2. **Non-AI caches are healthy, which makes the prompt-cache gap more obvious**
   - **Evidence:** `setup-uv` cache hits were explicitly logged in `6` run summaries (`27328474705`, `27326635345`, `27326124830`, `27325829934`, `27325711721`, `27324325637`). Plan run `27325829934` also logged a cache hit on primary key `codex-v0.114.0-v2`.
   - **Impact:** dependency/tool caches are working; the blind spot is specifically prompt/model-side caching.
   - **Concrete improvement:** keep these caches, but align prompt-cache telemetry with the same standard of observability.

3. **Prompt variance is likely eroding reuse**
   - **Evidence:** heavy review run `27326479941` separated a large stable-looking file (`pre_assembled_static.txt` `151,522` bytes) from dynamic inputs (`editor_prompt_body.txt` `156,983` bytes and `reviewer_bundle.txt` `22,613` bytes), then built a total editor prompt of `308,899` bytes. Six `review_autofix` summaries logged `CONTEXT_BUDGET_WARN_RATIO: 0.7`, though actual warn count stayed `0`.
   - **Impact:** **inference** — if volatile content is inserted too early or duplicated, cache reuse will be poor even if a stable prefix exists.
   - **Concrete improvement:** keep the stable static prefix first, append volatile reviewer/check-run data last, and rely on the consolidator to collapse duplicate reviewer output before the editor stage.
   - **Estimated impact:** likely **10-20%** lower prompt tokens/latency on heavy review runs once cache telemetry is available.
   - **Reliability note:** no actual `CONTEXT_BUDGET_WARN` events were observed yet; the risk is latent, not already firing.

4. **Review-ledger cache is safe but not yet proven reusable**
   - **Evidence:** run `27326479941`, `step-033-review_codex-agent_Restore_review-issue_ledger.log`, missed exact key `review-ledger-shubhodeep1/coding-workflows-pr-3281-27326479941-1` and restore prefix `review-ledger-shubhodeep1/coding-workflows-pr-3281-`; `step-058-review_codex-agent_Save_review-issue_ledger.log` then saved the same run-specific key.
   - **Impact:** the miss path is safe, but this sample shows only a cold-start experience.
   - **Concrete improvement:** verify whether prefix restore is actually hitting on reruns; if not, add a second stable save key keyed by PR plus head SHA so restores prefer the most relevant prior ledger.
   - **Estimated impact:** unquantified in this window, but it should reduce repeat issue-state rediscovery on reruns.

5. **Memory retrieval is not earning its place on the critical path yet**
   - **Evidence:** `0/9` retrieve hits, `67s` of memory step overhead on run `27326479941`.
   - **Concrete improvement:** skip reviewer retrieval when recent hit rate is effectively zero, but keep write-side learning.
   - **Estimated impact:** **~8s immediate** if only the retrieve step is bypassed; larger if write steps are later batched safely.

## Orchestrator Health

- **Healthy signals**
  - `orchestrate_poll` succeeded in `31/31` runs, with `p50=159s`, `p95=291s`.
  - No direct evidence of stuck terminal states, conflict-heal retry loops, or repeated backoff storms was found in the sampled logs.
  - `forward_merge_stable_to_main` run `27328809004` had bounded retry logic configured but succeeded on `fetch_attempt=1`, which suggests retry overhead is not a dominant issue.

- **Operational pain points**
  - **Skip churn:** all `277` repo `other/skipped` runs came from `clarify` (`72`), `plan` (`66`), `implement` (`67`), and `orchestrate_clarify_respond` (`72`).
  - **Runner waits:** `10` log-summary rows mention runner waits across `review_autofix`, `integration_pr_readiness`, `issue_pr_status`, `orchestrate_poll`, `implement`, and `clarify`.
  - **Long cancelled work:** two cancelled `review_autofix` runs still consumed `2735s` (`27321268901`) and `2770s` (`27258405540`). **Inference:** these are probably superseded or no-longer-needed runs, but the sampled logs do not show an explicit cancellation cause.
  - **Semble availability skew:** three `orchestrate_poll` summaries logged `SEMBLE_AVAILABLE: false`; seven `review_autofix` summaries logged `SERENA_ENABLED: false`.

- **Smallest safe mitigations**
  - Tighten dispatch conditions so skip-only workflows are not launched.
  - Add/expand concurrency groups for poll and review control flows so superseded runs are cancelled before meaningful work begins.
  - Move generated workspace files out of branch-switch paths.
  - Emit explicit availability counters for “enabled but unavailable” Semble/Serena states.

- **Indicators to track**
  - Skip-to-success ratio by workflow family.
  - `orchestrate_poll` p50/p95 duration and runner-wait share.
  - Count of long cancelled `review_autofix` runs.
  - Count of checkout-cleanliness failures.
  - AI memory retrieve hit rate.
  - Semble availability rate vs actual query/fallback rate.

## Pipeline Flow Bottlenecks

| Pipeline stage | Bottleneck type | Evidence | Highest-leverage fix |
|---|---:|---|---|
| Clarify | Queueing / no-op churn | `clarify` has `76` runs, `72` `other/skipped`; successful run `27325801336` still took `87s`, with `clarify/system` dominated by runner allocation | Stop dispatching clarify runs when the parent gate already knows no action is needed |
| Plan | No-op churn with some cache wins | `plan` has `70` runs, `66` `other/skipped`; successful run `27325829934` took `498s` but did hit `setup-uv` and `codex-v0.114.0-v2` caches | Tighten plan triggers before job dispatch |
| Implement | Model-compute cost | run `27326124830` used `2,652,382` tokens / `26` Codex calls in `692s`; family `p95=225.5s` but cost is the real outlier | Add hard token/call ceilings and stop conditions |
| Review / Autofix | Model-compute + GH API wait | `review_autofix` family `p95=2758s`; run `27326479941` spent `2039.4s` reviewer, `450.0s` editor, `141.6s` check-run polling, `94.7s` disk cleanup | Make pass 2 conditional, fix consolidator staging, shorten check-run collector |
| Validate / CI | Long validation plus deterministic regression | CI family `p50=1639s`, `p95=1773.2s`; failures `27325582989` and `27328440076` both hit the same two unit tests | Fix the regression first, then front-load the targeted unit-test shard |
| Orchestrate / Poll | Queueing / poll tax | `orchestrate_poll` `31/31` success, `p50=159s`; runs `27325711721` and `27326545610` log poll dominance and runner waits | Collapse superseded poll runs and reduce idle poll waiting |
| Merge / Checkout | Workspace hygiene | run `27263032560` failed on checkout because `.ai/.workspace_source_manifest.txt` would be overwritten | Move or reset generated files before branch switches |

**Bottom line:** queueing and model compute dominate end-to-end latency; retry/backoff overhead is secondary in this sample.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` long tail (`51` runs, `p95=2758s`, `108` OR calls, `11` Semble calls / `146,864` bytes).
  - `orchestrate_poll` fixed tax (`31/31` success, `p50=159s`, runner waits in recent summaries).
  - CI validation is long and currently fragile (`9` runs, `p50=1639s`, `22.2%` failure rate).

- **Top failure modes**
  - Duplicate issue-fetch regression in `scripts/implement_diagnose_post_codex_failure.sh`, surfaced by CI runs `27325582989` and `27328440076`.
  - Workspace checkout conflict on `.ai/.workspace_source_manifest.txt` in review run `27263032560`.
  - Repeated review quality degradation from missing `review-consolidator.txt` in `7` slow review runs.

- **Highest-cost drivers**
  - One `implement` run (`27326124830`) consumed `2,652,382` Codex tokens / `26` calls.
  - All recorded OR calls (`108`) are concentrated in `review_autofix`, but token totals for those calls were not captured.
  - Semble usage is moderate and bounded (`15` repo calls / `173,848` bytes total) and looks more helpful than harmful when used for reviewer-context.

- **Top 3 prioritized actions**
  1. **Make `review_autofix` pass 2 conditional and enable the existing risk tier** — biggest speed/cost win.
  2. **Restore a single fallback issue snapshot and fix the two failing CI tests** — biggest reliability win.
  3. **Stage `review-consolidator.txt` and trim editor prompt assembly** — improves speed, quality, and cacheability together.

## Metrics Appendix

### Repo summary

| Repo | Log telemetry coverage | Total runs | Success | Failure | Cancelled | Other/skipped | Avg duration (s) | p50 (s) | p95 (s) | wall_clock_p50_ms | wall_clock_p99_ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | `115 / 423` | `423` | `138 (32.6%)` | `3 (0.7%)` | `5 (1.2%)` | `277 (65.5%)` | `149.0` | `2.0` | `1002.0` | `6000` | `2773520` |

### Workflow family summary

| Family | Runs (S/F/C/O) | Success % | Failure % | Avg s | p50 s | p95 s | Key cost signals |
|---|---:|---:|---:|---:|---:|---:|---|
| `review_autofix` | `51 (46/1/4/0)` | `90.2` | `2.0` | `691.9` | `18.0` | `2758.0` | OR calls `108`; Semble `11 / 146,864B` |
| `implement` | `71 (4/0/0/67)` | `5.6` | `0.0` | `39.6` | `1.0` | `225.5` | Codex `2,652,382 / 26`; Semble `2 / 15,670B` |
| `plan` | `70 (4/0/0/66)` | `5.7` | `0.0` | `25.5` | `1.0` | `165.6` | Codex `2,027 / 3`; cache hit on `codex-v0.114.0-v2` in run `27325829934` |
| `orchestrate_poll` | `31 (31/0/0/0)` | `100.0` | `0.0` | `178.7` | `159.0` | `291.0` | Codex `4,052 / 2`; Semble `2 / 11,314B` |
| `ci` | `9 (6/2/1/0)` | `66.7` | `22.2` | `1346.0` | `1639.0` | `1773.2` | No model telemetry in sampled failures |
| `clarify` | `76 (4/0/0/72)` | `5.3` | `0.0` | `7.4` | `1.0` | `30.0` | Control-plane mostly skipped |
| `issue_pr_status` | `6 (6/0/0/0)` | `100.0` | `0.0` | `51.3` | `66.5` | `74.8` | Runner-wait hotspot in run `27328437396` |
| `orchestrate_clarify_respond` | `72 (0/0/0/72)` | `0.0` | `0.0` | `2.6` | `1.0` | `9.0` | All no-op / other |

### Cost telemetry totals

| Metric | Value |
|---|---:|
| `codex_tokens_used` | `2,658,461` |
| `codex_calls` | `31` |
| `or_calls` | `108` |
| `or_prompt_tokens` | `0` |
| `or_completion_tokens` | `0` |
| `or_total_tokens` | `0` |
| `or_cache_write_tokens` | `0` |
| `or_cache_read_tokens` | `0` |
| `semble_query_calls` | `15` |
| `semble_query_bytes` | `173,848` |
| `semble_fallbacks` | `0` |
| `semble_contract_test_fallbacks` | `0` |
| `semble_runtime_fallbacks` | `0` |
| `serena_query_calls` | `0` |
| `serena_query_response_bytes` | `0` |
| `serena_query_tool_calls` | `0` |
| `serena_query_ms` | `0` |
| `serena_fallbacks` | `0` |
| `serena_probe_ok` | `0` |
| `serena_probe_failed` | `0` |
| `serena_probe_skipped` | `0` |
| `break_glass_count` | `0` |
| `context_budget_warn_count` | `0` |
| `cache_hit_rate` | `null` |
| `wall_clock_sample_count` | `113` |

**Note:** six `review_autofix` log summaries printed `CONTEXT_BUDGET_WARN_RATIO: 0.7`, but no actual `CONTEXT_BUDGET_WARN` event was counted.

### GH API hotspot summary

| Run / code path | Workflow / step | Observed hotspot | Estimated avoidable calls |
|---|---|---|---:|
| `27326479941` | `Internal: AI Review & Autofix` / `step-002-review_gate.log` | Same paginated PR-files endpoint fetched twice | `1` paginated series / run |
| `27326479941` | `step-032-review_codex-agent_Collect_PR_check-run_failures_CI_lint_autofix_context.log` | Poll waits `20s + 40s + 80s`; final context only `344B` | `3+` poll reads / heavy run |
| `scripts/implement_diagnose_post_codex_failure.sh` | Fallback issue lookup path | Separate GETs for labels and body on cache miss | `1` issue GET / fallback invocation |
| `27323925287` | `Drift Audit` | Four consecutive `log fetch failed` events | `4` log-download attempts in that run |

### Cache and memory metrics

| Signal | Value | Evidence |
|---|---:|---|
| Prompt `cache_hit_rate` | `null` | Repo aggregate |
| OR cache read/write tokens | `0 / 0` | Repo aggregate |
| `setup-uv` cache-hit summaries | `6` runs | `27328474705`, `27326635345`, `27326124830`, `27325829934`, `27325711721`, `27324325637` |
| Codex primary cache-hit summaries | `1` run | `plan` run `27325829934` hit `codex-v0.114.0-v2` |
| Review-ledger restore | miss | run `27326479941` exact key + prefix both missed |
| Review-ledger save | run-specific key only | run `27326479941` saved `review-ledger-...-27326479941-1` |
| AI memory retrieve hit rate | `0 / 9 (0%)` | Deep-dive unique retrieve ops |
| AI memory avg `estimated_tokens` | `0` | Deep-dive unique retrieve ops |
| AI memory `keyword_method` | `llm 9 / 9` | Deep-dive unique retrieve ops |
| AI memory `fail_open:true` retrieves | `0` | Deep-dive unique retrieve ops |
| AI memory `enabled:false` retrieves | `0` | Deep-dive unique retrieve ops |
| AI memory pushes with retry `>1` | `1` | run `27319768240` `record-candidate` had `push_attempts=2` |

### MCP telemetry

| System | Target | Query calls | Logged bytes | Response bytes | Fallbacks | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| `Semble` | all targets | `15` | `173,848` | n/a | `0` | n/a | n/a | n/a | Reviewer-context example: run `27326479941` logged `15,347B` in `520ms`; overflow examples `27260525159` and `27258415267` logged `6,464B` each for `.github/workflows/test-and-mark-stable.yml` |
| `Serena` | all targets | `0` | `0` | `0` | `0` | `0` | `0` | `0` | Seven recent `review_autofix` log summaries logged `SERENA_ENABLED: false` |
| `Other MCP servers observed` | none | `0` | `0` | `0` | `0` | `0` | `0` | `0` | No other `<NAME>_QUERY`, `<NAME>_FALLBACK`, or `<NAME>_PROBE` lines found |

**Semble availability note:** three `orchestrate_poll` summaries (`27328474705`, `27326635345`, `27326545610`) logged `SEMBLE_AVAILABLE: false` / `SEMBLE_INDEX_AVAILABLE: false`, but no `SEMBLE_FALLBACK` lines were observed.

## Deep Audit — Workflows & Scripts (2026-06-11)

### Section 1: Bug & Correctness Sweep

#### BUG-001
- **File path / line range** — `scripts/tg_helpers.sh:296-356,365-427`
- **Severity** — Medium
- **Category tag** — `bug`
- **Description** — `tg_cleanup_phase_msgs()` and `tg_cleanup_msgs()` paginate `issues/{n}/comments?per_page=100&page=${page}`, delete matching tracking comments from the same issue inside that loop, then advance `page`. Because the collection is mutated while it is being paged, later matching comments can shift onto an earlier page and be skipped, leaving stale Telegram tracking comments and undeleted TG message IDs behind.
- **Recommended fix** — Fetch a stable snapshot first, extract all matching comment IDs/message IDs, then delete from that frozen list. If page-at-a-time fetching must stay, re-query `page=1` until no matching `<!-- tg_* -->` marker remains.

#### BUG-002
- **File path / line range** — `.github/workflows/clarify.yml:334-340,423-427,1063-1082,1225-1239`; `.github/workflows/plan.yml:383-389,421-427,481-500,1888-1898`; `.github/workflows/orchestrate.yml:959-977`; `.github/workflows/orchestrate_clarify_respond.yml:423-449,1309-1318`
- **Severity** — Medium
- **Category tag** — `bug`
- **Description** — These steps make `source scripts/gh_helpers.sh` explicitly optional via `2>/dev/null || true`, then immediately call `gh_retry` with no local fallback. If `gh_helpers.sh` is missing or fails to source, the step stops being fail-open and instead hard-fails under `set -euo pipefail` when `gh_retry` is undefined. The repo already uses the safe bootstrap pattern elsewhere, e.g. `plan.yml:1687-1697` and `implement.yml:1251-1252,1386-1387`.
- **Recommended fix** — After every optional `source scripts/gh_helpers.sh 2>/dev/null || true`, add the standard shim `type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }`, plus `_safe_gh_jq` where needed. If the helper is truly required, remove `|| true` and fail early with an explicit missing-helper error instead.

_No additional high-confidence secret-leak, shell-injection, or YAML-structure defects were found beyond the already-documented issues in the current report._

### Section 2: GitHub API Call Redundancy Audit

_Already documented in the current report and intentionally not repeated here: the duplicate PR-files fetch in `review_gate`, the duplicate issue GET in `implement_diagnose_post_codex_failure.sh`, and the oversized `review_autofix` check-run polling path._

#### API-001
- **File path / line range** — `.github/workflows/orchestrate_clarify_respond.yml:62-84,423-449`
- **Severity** — Low
- **Category tag** — `api-redundancy`
- **Description** — `Check orchestrator metadata` fetches the child issue once and conditionally fetches the tracking issue title once; `Fetch issue and tracking context` then fetches the same child issue again and fetches the tracking issue body again. On the common orchestrator-managed path with a tracking issue, current call count is **4 issue GETs for 2 resources**.
- **Recommended fix** — Persist the first-step child/tracking payloads into `RUNTIME_DIR` or step outputs and reuse them in the second step, following the cycle-local cache pattern used in `scripts/orchestrate_poll_process.sh:11256-11292`. **Current call count:** 4 GETs. **Proposed call count:** 2 GETs.

#### API-002
- **File path / line range** — `.github/workflows/test-and-mark-stable.yml:2963-2972`
- **Severity** — Low
- **Category tag** — `api-redundancy`
- **Description** — Inside the 600-second wait loop, each poll iteration calls `repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}` twice: once for `.status`, then again for `.conclusion`. **Current call count:** 2 run GETs per iteration.
- **Recommended fix** — Fetch `{status, conclusion}` once per poll iteration and parse both fields locally, using the `_safe_gh_jq` pattern from `scripts/gh_helpers.sh:520-546` or a shared `gh_api_safe` wrapper. **Proposed call count:** 1 run GET per iteration.

#### API-003
- **File path / line range** — `.github/workflows/review_autofix.yml:823-860`
- **Severity** — Medium
- **Category tag** — `api-batching`
- **Description** — When `issue_nodes_json` reaches the linked-issue loop with `labels_known != true`, the workflow calls `gh issue view ... --json labels` once per linked issue. That makes the fallback path cost **N REST calls for N linked issues**, even though this repo already has batched GraphQL issue-fetch patterns.
- **Recommended fix** — Batch-hydrate missing labels before the loop with an aliased GraphQL query, using the same style as `scripts/orchestrate_poll_process.sh:10585-10708` (or chunked at 25 items like that helper). **Current call count:** N REST calls on the fallback path. **Proposed call count:** 1 GraphQL call for up to 25 issues per batch.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **File path / line range** — `.github/workflows/test-and-mark-stable.yml:493-505,626-638,845-857,1336-1355,2553-2564`; `scripts/comprehensive_test_and_release_gh_api.sh:3-40`; `.github/workflows/comprehensive-test-and-release.yml:48-56,282-287`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — `test-and-mark-stable.yml` defines five near-identical `gh_api_safe()` wrappers inline, while the repo already has a shared implementation in `scripts/comprehensive_test_and_release_gh_api.sh` that is sourced by `comprehensive-test-and-release.yml`. The copies have already drifted slightly in stderr handling and logging behavior.
- **Recommended fix** — Make `scripts/comprehensive_test_and_release_gh_api.sh` the owner. **Function signature:** `gh_api_safe <gh api args...>` with result returned via `GH_API_SAFE_OUTPUT`. **Callers to update:** the five inline blocks in `test-and-mark-stable.yml`.

#### DUP-002
- **File path / line range** — `.github/workflows/clarify.yml:56-128`; `.github/workflows/plan.yml:107-182`; `.github/workflows/validate.yml:84-157`; `.github/workflows/orchestrate_clarify_respond.yml:91-164`; `.github/workflows/implement.yml:293-366`; `scripts/resolve_integration_ref.sh:1-92`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — The same staging wrapper for integration-ref resolution is repeated across five workflows: create temp dirs, clone `shubhodeep1/coding-workflows`, fetch the resolver ref, mask auth in clone logs, run `scripts/resolve_integration_ref.sh`, and clean up. Any future fix to auth masking, fallback ref logic, or staging layout now requires five edits.
- **Recommended fix** — Keep `scripts/resolve_integration_ref.sh` as the pure resolver and extract the staging wrapper into a shared script. **Owning module:** new `scripts/stage_and_resolve_integration_ref.sh`. **Function signature:** `stage_and_resolve_integration_ref <issue_number> <repo_slug> <resolver_repo> <resolver_ref>`. **Callers to update:** `clarify.yml`, `plan.yml`, `validate.yml`, `orchestrate_clarify_respond.yml`, and `implement.yml`.

#### DUP-003
- **File path / line range** — `scripts/label_helpers.sh:112-207`; `.github/workflows/issue_pr_status.yml:313-327`; `.github/workflows/review_autofix.yml:4661-4700,4881-4890,5935-5948`
- **Severity** — Low
- **Category tag** — `duplication`
- **Description** — Multiple workflows redefine simplified fallbacks for `ensure_label_exists` / `set_issue_phase_label_resilient` even though `scripts/label_helpers.sh` already owns the canonical implementations. The inline copies are weaker: they only POST the target label and do not preserve/replace phase labels the way the canonical helper does.
- **Recommended fix** — Make `scripts/label_helpers.sh` the sole owner. **Function signatures:** `ensure_label_exists <label_name> [repo]` and `set_issue_phase_label_resilient <issue_number> <target_label> [repo]`. **Callers to update:** `issue_pr_status.yml` and the three late-stage label paths in `review_autofix.yml`.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001
- **File path / line range** — `.github/workflows/implement.yml:3465-3840`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — The `Commit changes` `run:` block normalizes to approximately **17,502 characters** and contains `${{ }}` interpolations, leaving only about **3,498 characters** of headroom before GitHub Actions’ **21,000-character** expression limit. This repository has already hit that limit in other workflows per the pre-audit context.
- **Recommended fix** — Extract the commit/staging/destructive-guard flow into an external script (preferred, e.g. `scripts/implement_commit_changes.sh`) and keep only small env wiring in YAML, or split the block into separate steps for artifact cleanup, staging, destructive/scope guards, and no-op handling.

_Scan result: this was the only interpolated `run:` block above 15,000 characters. The next largest was `review_autofix.yml:1768-2114` at approximately 14,586 characters. No workflow file exceeds 800 KB; the largest is `review_autofix.yml` at 376,477 bytes._

### Section 5: Cross-Cutting Concerns

_Repo-wide scan note: no `TODO` / `FIXME` / `HACK` markers were present in `.github/workflows/*.yml` or `scripts/*.{sh,py}`._

#### DEAD-001
- **File path / line range** — `scripts/orchestrate_poll_process.sh:9279-9286,11294-11326`
- **Severity** — Low
- **Category tag** — `dead-code`
- **Description** — `read_standalone_state_json()` is a full wrapper that re-fetches paginated issue comments and then calls `_extract_standalone_state_json_from_comments()`, but the live standalone-stall loop already has `comments_json` and calls the pure parser directly at `11325-11326`. Repo-local search of workflows/scripts found no caller of `read_standalone_state_json()`.
- **Recommended fix** — Remove the unused wrapper, or route the live caller through it if a public helper is still desired. As written, it preserves an extra dormant comments-fetch path with no active consumer.

#### SHELL-001
- **File path / line range** — `scripts/review_conflict_resolve.sh:431-439,954-958`
- **Severity** — Low
- **Category tag** — `shellcheck`
- **Description** — `RESOLVER_ESCALATION_COMMENT_MARKER` is assigned in shell but never read, while the exact same literal is separately hard-coded inside the embedded Python block as `ESCALATION_COMMENT_MARKER`. This is an SC2034-style unused-variable warning and a drift risk because the shell and Python values can silently diverge.
- **Recommended fix** — Keep one source of truth: either pass the marker into Python via env/argv and delete the duplicated Python constant, or remove the unused shell variable entirely.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 6 | BUG-001, BUG-002, API-003, DUP-001, DUP-002, EXPR-001 |
| Low | 5 | API-001, API-002, DUP-003, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 3 workflows (+ optional shared helper touch) | Medium |
| Code modularization | 8 workflows + 2 scripts | Large |
| Expression size reduction | 1 workflow + 1 helper script | Medium |
| Medium/Low fixes | 7 files | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-06-11)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is fully proven safe to collapse without changing retry/pagination/error semantics; `NEEDS_VERIFICATION` means the overlap is real but at least one semantic invariant still needs a human or follow-up analysis to confirm; `RISKY_SKIP` means the duplication is visible but sits in pagination/retry/race-defensive code that must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — NEEDS_VERIFICATION
- **File path / line ranges** — `scripts/review_collect_pr_metadata.sh:154-159`, `scripts/review_collect_pr_metadata.sh:182-207`
- **Current call count / proposed call count** — fallback-hydration portion: `N` REST issue GETs (`N <= 20`) after the existing GraphQL miss → `1` aliased GraphQL batch for up to 20 fallback issues.
- **Endpoint(s)** — current: `POST /graphql` for `pullRequest.closingIssuesReferences`, then `GET /repos/{repo}/issues/{issue_number}` per fallback issue; proposed: keep the existing `closingIssuesReferences` query and replace the per-issue REST loop with one aliased GraphQL batch.
- **Evidence**
  ```sh
  if gh_retry "${_linked_tmp}" api graphql \
    -f owner="${REPOSITORY_OWNER}" \
    -f name="${REPOSITORY_NAME}" \
    -F number="${PR_NUMBER}" \
    -f query='query(...) { ... closingIssuesReferences(first:50){nodes{number title body}} ... }'
  ```

  ```sh
  if [ "${_linked_context_raw}" = "[]" ] && [ "${LINKED_ISSUE_FALLBACK_NUMBERS_JSON}" != "[]" ]; then
    _FALLBACK_MAX_ISSUES=20
    ...
    while IFS= read -r _fb_num; do
      if gh_retry "${_fb_issue_tmp}" api "repos/${REPOSITORY}/issues/${_fb_num}" \
        --jq '{number: (.number // 0), title: (.title // ""), body: (.body // "")}'; then
  ```
- **Proposed fix** — In `scripts/review_collect_pr_metadata.sh`, replace the `while ... gh_retry ... api "repos/${REPOSITORY}/issues/${_fb_num}"` loop with one chunked aliased GraphQL lookup returning `{number,title,body}` for the capped fallback set, using the `_fetch_candidate_issue_details_graphql` style from `scripts/orchestrate_poll_process.sh`; keep the current REST loop only as the fail-open fallback when the batch query fails or omits a node.
- **Safety rationale** — This is not in a retry/poller path, but it changes a fallback-only transport from per-issue REST to batched GraphQL, so it does not meet the strict “same endpoint / same semantics” bar for `SAFE_TO_MERGE`.
- **Downstream signal** — Verify on a PR with multiple body-text fallback issue refs that (1) `LINKED_ISSUE_CONTEXT_FILE` ordering/content is unchanged, (2) missing/404 issues still warn-and-skip exactly once, and (3) a batch-query failure still falls back to the current REST loop.

#### MERGE-002 — RISKY_SKIP
- **File path / line ranges** — `.github/workflows/clarify.yml:423-443`
- **Current call count / proposed call count** — when `SEMANTIC_CACHE_BACKEND != none`: `2` comment GETs → `1` merged fetch on the success path.
- **Endpoint(s)** — `GET /repos/{repo}/issues/{issue_number}/comments`
- **Evidence**
  ```sh
  # Keep clarify prompt context bounded to preserve historical behavior.
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=50" > "${ISSUE_COMMENTS_FILE}"

  if [ "${SEMANTIC_CACHE_BACKEND}" != "none" ]; then
    if ! gh_retry gh api --paginate --slurp "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=100" \
      | jq -r 'add // [] | .[] | ...' > "${THREAD_HISTORY_FILE}"; then
  ```
- **Proposed fix** — In the `Fetch issue comments` step, when semantic cache is enabled, fetch the full comment history once into a temp JSON file, derive `ISSUE_COMMENTS_FILE` from the first 50 chronological comments, and derive `THREAD_HISTORY_FILE` from the full merged array; keep the current single-call path when semantic cache is disabled.
- **Safety rationale** — One of the calls is paginated (`--paginate --slurp`), and the policy explicitly marks pagination merges as `RISKY_SKIP` because page-boundary and fail-open semantics can change.
- **Downstream signal** — Do not auto-implement; manual review must confirm that the merged path still preserves the 50-comment prompt bound and the current semantic-cache fail-open sentinel behavior on API failure.

#### MERGE-003 — RISKY_SKIP
- **File path / line ranges** — `scripts/orchestrate_poll_process.sh:7123`, `scripts/orchestrate_poll_process.sh:7198-7204`
- **Current call count / proposed call count** — `2` pull GETs → `1` pull GET on the recorded-final-PR branch.
- **Endpoint(s)** — `GET /repos/{repo}/pulls/{pull_number}`
- **Evidence**
  ```sh
  finalize_integration_merge_if_needed() {
    ...
    existing_pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
    existing_pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
  ```
- **Proposed fix** — If this is ever changed manually, fetch the PR JSON once inside `finalize_integration_merge_if_needed()` and derive both `.state` and `.merged_at != null` locally.
- **Safety rationale** — This lives inside `scripts/orchestrate_poll_process.sh`, which the policy explicitly treats as a race-defensive path that must be `RISKY_SKIP` even when the duplicate endpoint is obvious.
- **Downstream signal** — Do not auto-implement; manual review must prove that no race-defense, retry diagnostics, or log-grep consumer depends on those two reads remaining separate.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — NEEDS_VERIFICATION
- **File path / line ranges** — `scripts/review_rb_judge.sh:720-725`, `scripts/review_rb_judge.sh:749-772`
- **Current call count / proposed call count** — on the `closingIssuesReferences` path: `1` GraphQL query + `1..N` issue GETs → `1` GraphQL query with no extra issue GETs on that path.
- **Endpoint(s)** — current: `POST /graphql` for `pullRequest.closingIssuesReferences`, then `GET /repos/{repo}/issues/{issue_number}`; proposed: extend the existing GraphQL query to include the body/labels already needed later.
- **Evidence**
  ```sh
  ISSUE_NUMBERS="$(gh_retry gh api graphql \
    ...
    -f query='query(...) { ... closingIssuesReferences(first: 50) { nodes { number } } ... }' \
    --jq '.data.repository.pullRequest.closingIssuesReferences.nodes[].number' || true)"
  ```

  ```sh
  # Labels of the parent (FIRST_ISSUE) issue. Captured from the same
  # REST GET that already fetches the body ...
  while IFS= read -r issue_number; do
    ISSUE_META_JSON="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" || echo '{}')"
    BODY="$(printf '%s' "${ISSUE_META_JSON}" | jq -r '.body // ""' 2>/dev/null || echo "")"
    if [ -z "${FIRST_ISSUE}" ]; then
      FIRST_ISSUE="${issue_number}"
      FIRST_ISSUE_LABELS_JSON="$(printf '%s' "${ISSUE_META_JSON}" | jq -c '[(.labels // [])[]?.name]' 2>/dev/null || echo '[]')"
  ```
- **Proposed fix** — Extend the existing `closingIssuesReferences` query in `scripts/review_rb_judge.sh` to request `number`, `body`, and `labels(first: 50) { nodes { name } }`; build `FIRST_ISSUE`, `FIRST_ISSUE_BODY`, and `FIRST_ISSUE_LABELS_JSON` from that payload, and keep the current REST loop only when `ISSUE_NUMBERS` came from `LINKED_ISSUE_FALLBACK_NUMBERS_JSON` / PR-text fallback or the GraphQL payload is missing required fields.
- **Safety rationale** — The reuse is local to one script execution and extends an existing query, but it changes the “first issue / first non-empty body” derivation path, so it needs verification before it can satisfy the `SAFE_TO_MERGE` semantics bar.
- **Downstream signal** — Verify on PRs with multiple linked issues that (1) `FIRST_ISSUE` stays pinned to the first linked issue, (2) `FIRST_ISSUE_BODY` still picks the first non-empty body, and (3) PR-text fallback numbers still use the current REST path.

#### REUSE-002 — NEEDS_VERIFICATION
- **File path / line ranges** — `.github/workflows/implement.yml:92-105`, `.github/workflows/implement.yml:417-434`, `.github/workflows/implement.yml:1254-1255`
- **Current call count / proposed call count** — on non-skipped implement runs before `ISSUE_META_FILE` exists: `2` issue GETs → `1` issue GET.
- **Endpoint(s)** — `GET /repos/{repo}/issues/{issue_number}`
- **Evidence**
  ```sh
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '{state: (.state // "open"), labels: [.labels[].name]}')"
  ```

  ```sh
  issue_meta_json=""
  if ! issue_meta_json="$(gh_api_with_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"; then
    ...
  fi
  printf '%s\n' "${issue_meta_json}" > "${ISSUE_META_FILE}"
  ```

  ```sh
  if [ ! -s "${ISSUE_META_FILE}" ]; then
    gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"
  fi
  ```
- **Proposed fix** — In `Precheck approval phase label`, capture the full issue JSON once (not just the `{state,labels}` projection), carry it across the step boundary via a temp file or multiline env handoff, and have `Resolve integration/default checkout ref and cache issue metadata` populate `ISSUE_META_FILE` from that cached payload before falling back to a live GET; keep the existing `Fetch issue metadata` file gate unchanged.
- **Safety rationale** — The calls hit the same issue endpoint with identical auth and no intervening issue mutation, but the reuse crosses workflow steps and must preserve the later step’s current fail-open branch-selection behavior, so static reading alone cannot prove `SAFE_TO_MERGE`.
- **Downstream signal** — Verify on both normal and skip paths that (1) the cached full issue JSON survives the step boundary, (2) `Resolve integration/default checkout ref...` still falls back to `DEFAULT_CHECKOUT_REF` when the cache is missing/corrupt, and (3) `Fetch issue metadata` remains a no-op when `ISSUE_META_FILE` is already present.

### Dead Calls (DEAD-API-###)
No findings.

### Cross-References to Deep Audit Section
- API-001: NEEDS_VERIFICATION — the child/tracking issue payload reuse is real, but it spans workflow steps and needs cache-path/output verification before it is safe.
- API-002: RISKY_SKIP — the duplicate run GET sits inside a wait loop, so loop/race semantics must be reviewed manually rather than auto-merged.
- API-003: NEEDS_VERIFICATION — batching the linked-issue label hydration is directionally correct, but replacing per-issue REST with GraphQL still needs payload-parity verification.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 3 | `MERGE-001`, `REUSE-001`, `REUSE-002` |
| RISKY_SKIP | 2 | `MERGE-002`, `MERGE-003` |

### Implement-Stage Handoff
No SAFE_TO_MERGE findings in this pass.
