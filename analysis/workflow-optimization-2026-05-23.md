## Executive Summary

- `review_autofix` is the dominant speed/cost lever: 81 runs consumed **100,287s / 135,001s total runtime (74.3%)**, with **p50 18s** but **p95 4,545s**. Sampled slow runs `26289024192`, `26296423247`, `26293979422`, `26292599330` spent **4,090-5,934s** mostly inside `codex-agent`. **Estimated impact:** save **5-45 min** on affected PR cycles. **Confidence:** high.
- CI reliability problems are concentrated, not broad: all **3 CI failures** in this window were `shubhodeep1/coding-workflows` → `CI` → `lint / Orchestrate poll process unit tests` (`26284120209`, `26285068197`, `26289023940`). **Estimated impact:** restore CI from **88% to 100%** in this sample and remove ~**2,673s** of failed run time. **Confidence:** high.
- The orchestrator is currently blocked by a real integration regression, not infra noise: recent `orchestrate_poll` run `26328229210` reported **`must_contain` 70/79 (88%)**, then `Wave 2 dispatch BLOCKED` for branch `orchestrator/project-2867`. This fans out into repeated skipped `clarify/plan/implement/respond` runs. **Estimated impact:** unblock wave progression and remove recurring no-op dispatch storms. **Confidence:** high.
- AI memory is on but ineffective in sampled runs: **8/8** `retrieve` operations returned **0 records**, average `estimated_tokens=0`, and `keyword_method=none` every time. The workflow currently calls reviewer memory with only `--pr-number` (`.github/workflows/review_autofix.yml:1485-1487`). **Estimated impact:** medium token/quality gain once retrieval gets real query text. **Confidence:** high.
- Semble is not the current bottleneck: deep logs show **16 operational `SEMBLE_QUERY` lines**, **129,949 logged bytes**, **8.0s total** across 7 slow `review_autofix` runs, while Serena had **0 operational `SERENA_*` lines** and was explicitly disabled in slow run `26296423247`. **Estimated impact:** keep Semble, focus savings on duplicate/autofix reruns instead. **Confidence:** medium-high.
- `other_count=402` is mostly intentional skips, not hidden failures: **clarify 101**, **implement 100**, **plan 100**, **orchestrate_clarify_respond 100**, **review_autofix 1**. **Estimated impact:** improves prioritization by keeping attention on `ci`, `review_autofix`, and orchestrator blocking rather than on the skip-heavy control plane. **Confidence:** high.

## Speed Optimizations

1. **[Critical path] Re-enable narrow self-trigger autofix dedupe**
   - **Evidence:** `review_autofix` has **17 cancelled runs** totaling **20,587s**. There are near-simultaneous long cancel/success pairs: e.g. cancelled `26280019102` (**2928s**) followed **13s** later by successful `26280029220` (**2926s**); cancelled `26283773767` (**2819s**) followed **14s** later by `26283784032` (**2813s**); cancelled `26286988237` (**2693s**) followed **12s** later by `26286997039` (**2693s**). Slow gate logs also show `AUTOFIX_SKIP_SELF_TRIGGERED:` blank in `26296423247`, while the gate comments say the self-trigger follow-up pass is pure cost and restoring the skip saves roughly half the autofix LLM spend.
   - **Root cause:** bot-authored `[ai-autofix]` synchronize events are still spawning full follow-up review/autofix passes.
   - **Exact change:** set repository variable `AUTOFIX_SKIP_SELF_TRIGGERED=true`, or reintroduce the same guard only for bot-authored `[ai-autofix]` synchronize events while preserving fail-open behavior on metadata lookup failure.
   - **Estimated time savings:** removes one full extra autofix cycle on affected PRs; observed redundant long runs were **2,693-4,545s** each.
   - **Implementation risk:** **medium** — current concurrency is intentionally conservative; keep the skip scoped to verified bot-authored follow-up commits only.

2. **[Critical path] Shorten `review_autofix` check-run wait loops on verification reruns**
   - **Evidence:** actual wait-loop lines in slow runs were: `26292599330` **47** waits, `26286532978` **45**, `26296423247` **45**, `26293979422` **44**, `26289024192` **39**. The repeated line is `Waiting for 1-2 in-progress/queued check-run(s)... (sleep 20s, deadline in 1199s)...`. `README.md:65-68` documents `CHECK_RUNS_WAIT_TIMEOUT_SECS=1200` and `CHECK_RUNS_POLL_INTERVAL_SECS=20`.
   - **Root cause:** long `review_autofix` runs spend large chunks of wall time polling sibling check-runs before proceeding.
   - **Exact change:** on self-triggered verification reruns or same-SHA continuation runs, lower `CHECK_RUNS_WAIT_TIMEOUT_SECS` to **300-600s** and skip waiting entirely when only the workflow’s own follow-up checks remain; rely on the existing fail-open snapshot behavior at timeout.
   - **Estimated time savings:** about **780-948s** on the sampled outliers.
   - **Implementation risk:** **low-medium** — the current design already proceeds fail-open after timeout.

3. **[Secondary] Reduce `orchestrate_poll` setup tax**
   - **Evidence:** recent successful poll run `26328229210` took **149s** total. Inside it, `Process each tracking issue` spanned **59.1s**, `Checkout repository` **14.1s**, and `Install semble` **10.7s`; `poll/system` also logged hosted-runner wait (`Requested labels: ubuntu-latest`, `Job is waiting for a hosted runner to come online.`). Family-wide `orchestrate_poll` p50 is **142.5s**.
   - **Root cause:** every poll tick pays repeated setup/download costs before real issue processing starts.
   - **Exact change:** cache/reuse Semble installation and index when the support SHA is unchanged, and update the `astral-sh/setup-uv` action path so Node24 is the normal path instead of a deprecation fallback.
   - **Estimated time savings:** **10-25s per poll run**.
   - **Implementation risk:** **low**.

4. **[Fail-fast] Pull the two hottest `orchestrate_poll_process` regressions forward in CI**
   - **Evidence:** all three CI failures surfaced only after **863-945s** in `lint`, and they were always the same two tests: `test_no_labels_with_open_linked_pr_skips_retrigger_pipeline` (`26284120209`, `26285068197`) and `test_backward_scan_promotes_ready_to_merge_with_merged_pr_to_merged` (`26289023940`).
   - **Root cause:** the current job discovers these regressions late inside a long test phase.
   - **Exact change:** add a tiny preflight shard for those two tests before the full `lint` suite, or run `tests/test_orchestrate_poll_process.py` early as a dedicated fast-fail step.
   - **Estimated time savings:** **13-15 min faster feedback** per recurrence.
   - **Implementation risk:** **low**.

## Cost Optimizations

1. **Highest ROI: restore self-trigger autofix skipping**
   - **Evidence:** slow gate logs for `26296423247` and `26286532978` explicitly describe the follow-up pass as no new work in the common case and estimate roughly **~7 LLM calls per follow-up run** and **~50% autofix LLM spend reduction per fix cycle** if skipped. The variable is currently blank (`AUTOFIX_SKIP_SELF_TRIGGERED:` empty in `26296423247`).
   - **Root cause:** the pipeline pays for a second full review/autofix round on bot-authored verification reruns.
   - **Exact change:** enable `AUTOFIX_SKIP_SELF_TRIGGERED=true`, scoped to verified bot-authored `[ai-autofix]` synchronize events.
   - **Estimated savings:** **highest in window**; one full follow-up reviewer/editor cycle avoided on affected PRs.
   - **Quality-risk notes:** bounded if restricted to bot-authored reruns; keep `force-review`/human-push bypasses and fail-open on metadata lookup failure.

2. **If full self-skip is not acceptable, downshift the reviewer stack only on bot-authored verification reruns**
   - **Evidence:** `.github/workflows/review_autofix.yml:96-103` configures **6 reviewer models**; `:107-129` sets reviewer/editor reasoning to **`xhigh`** by default; `:140` has `ENABLE_REVIEWER_TWO_PASS=true`. All seven sampled long runs logged `REVIEWERS_SUCCESSFUL: 6`.
   - **Root cause:** the expensive reviewer panel is being rerun at full strength even when the preceding fix cycle already handled the findings.
   - **Exact change:** on bot-authored follow-up reruns only, set `ENABLE_REVIEWER_TWO_PASS=false` and/or reduce reviewer pass-2 reasoning one notch unless the PR has a forced-review override or unresolved failing checks.
   - **Estimated savings:** removes one full reviewer pass on follow-up cycles; material but lower than a full self-skip.
   - **Quality-risk notes:** **medium** if applied too broadly; keep it narrowly scoped to bot-authored verification reruns.

3. **Make AI memory retrieval real before paying to ship empty context**
   - **Evidence:** all **8/8** `retrieve` events returned **0 records** with `keyword_method=none`; `.github/workflows/review_autofix.yml:1485-1487` passes only `--role reviewer --pr-number`, and `scripts/ai_memory_lib.py:1355-1365` returns `"none"` when both title and body are empty.
   - **Root cause:** memory retrieval is called without the text needed to generate keywords.
   - **Exact change:** pass PR title/body (and optionally labels or a short diff summary) into `memory_retrieve`; if no query text exists, skip retrieval entirely instead of attaching empty memory context.
   - **Estimated savings:** **medium, not directly quantifiable** in this window because token counters were not emitted.
   - **Quality-risk notes:** **low** — current behavior already contributes nothing.

4. **Inference: Semble is probably helping; Serena is currently a non-factor**
   - **Evidence:** operational Semble telemetry shows **16 queries**, **129,949 logged bytes**, **8,014ms** total across 7 slow `review_autofix` runs, mostly for `reviewer-context` and `overflow`. By contrast, sampled full editor prompts averaged **270,078 bytes** (`221,322-335,959` range). Serena had **0 operational `SERENA_QUERY/FALLBACK/PROBE` lines**, and slow run `26296423247` logged `SERENA_ENABLED: false`.
   - **Root cause:** cost is being driven by full reviewer/editor reruns, not by Semble overhead.
   - **Exact change:** keep Semble enabled for targeted overflow/context retrieval, but stop expecting Serena to offset downstream model/tool work until it is actually enabled and emitting telemetry.
   - **Estimated savings:** Semble itself is already low-overhead; the bigger savings come from not duplicating full review/autofix work.
   - **Quality-risk notes:** **low** for keeping Semble; **none** for Serena because it is not materially in use here.

5. **Prompt-cache opportunity exists, but observability is missing**
   - **Evidence:** `OPENROUTER_PROMPT_CACHE_DISABLED: false` was logged repeatedly, and `review_apply_fixes.sh:1139-1150` already places the stable static prompt first. But there were **0** observed `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens` lines. The stable static block was **117,087 bytes** in all 7 sampled long runs.
   - **Root cause:** cache may be working, but the system does not emit enough data to prove it.
   - **Exact change:** emit cache read/create counters and keep all invariant prompt text above variable run metadata; do not move dynamic noise ahead of the static block.
   - **Estimated savings:** **potentially medium**, but unquantified until counters are logged.
   - **Quality-risk notes:** **low**.

## Reliability Improvements

1. **Fix the two `orchestrate_poll_process` regressions that are breaking CI**
   - **Failure evidence:** `CI` failed in runs `26284120209`, `26285068197`, `26289023940`; all three ended `236 passed, 1 failed, 237 total`. Failures were `test_no_labels_with_open_linked_pr_skips_retrigger_pipeline` and `test_backward_scan_promotes_ready_to_merge_with_merged_pr_to_merged`.
   - **Root cause category:** poller behavior / test-contract drift.
   - **Exact fix:** repair the open-linked-PR no-retrigger guard (`tests/test_orchestrate_poll_process.py:6946+`) and the backward-scan `ai:ready-to-merge` → `ai:merged` promotion path (`:5004+`), then verify linked-PR fixture normalization around `:1210-1228`.
   - **Expected reliability impact:** restores CI from **22/25** to **25/25** in this sample.
   - **Rollback / fail-open:** none needed; these are existing tests that already fail loud on regressions.

2. **Heal the integration fingerprint contract before allowing more orchestrator wave movement**
   - **Failure evidence:** recent `orchestrate_poll` run `26328229210` logged `Current wave: 1/2, Judge cycle: 28 (stall: 28), Recovery count: 0/3`, then `must_contain satisfied 70/79 (88%)`, then `issue #2872 (PR #2894): must_contain pattern missing from 'scripts/orchestrate_poll_process.sh'`, then `Wave 2 dispatch BLOCKED`.
   - **Root cause category:** merge/conflict resolution regression on the orchestrator integration branch.
   - **Exact fix:** restore the missing fingerprinted content on `orchestrator/project-2867`, rerun the integration judge, and keep the current fail-closed block until the fingerprint is fully satisfied.
   - **Expected reliability impact:** unblocks wave progression and prevents repeated blocked-state churn.
   - **Rollback / fail-open:** keep fail-closed behavior if the fingerprint still fails; do **not** relax the fingerprint contract.

3. **Tighten cross-trigger dedupe for `review_autofix` without broad cancellation**
   - **Failure evidence:** 17 cancelled `review_autofix` runs, including multiple long overlaps with near-simultaneous successful siblings.
   - **Root cause category:** orchestration / dedupe race between PR-backed runs and dispatch-based follow-up runs.
   - **Exact fix:** add a pre-dispatch active-run check keyed by **PR number + head SHA** for workflow-dispatch/autofix-sweep paths, instead of widening `cancel-in-progress`. This respects the current intentional concurrency rules in `.github/workflows/internal-review.yml:35-50` and `.github/workflows/review_autofix.yml:754-755`.
   - **Expected reliability impact:** lower rerun/cancel rate and fewer stale duplicate review cycles.
   - **Rollback / fail-open:** if the active-run query fails, dispatch as today.

4. **Remove the repeated Node24 deprecation warning path**
   - **Failure evidence:** eight sampled `orchestrate_poll` `log_summary` entries (`26317683824`, `26319420481`, `26320447708`, `26322083816`, `26323661407`, `26325090686`, `26326226416`, `26327283873`) mention `Node.js 20 is deprecated` for `astral-sh/setup-uv@v3`.
   - **Root cause category:** dependency/action drift.
   - **Exact fix:** update the action version or pinned workflow-support path to a Node24-native setup path.
   - **Expected reliability impact:** low immediate impact, but reduces risk of sudden action breakage.
   - **Rollback / fail-open:** trivial pin rollback if needed.

**MCP fallback/probe note:** no operational `SEMBLE_FALLBACK`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines were observed in the deep logs. Semble queries were successful; Serena appears intentionally inactive rather than broken.

## AI Memory Health

- **Telemetry observed:** **35** `AI_MEMORY_TELEMETRY` records total:
  - `record-run-event`: **20**
  - `retrieve`: **8**
  - `record-candidate`: **7**
- **Retrieve effectiveness:** **0/8 hits (0%)**. Every retrieve logged:
  - `records_selected=0`
  - `estimated_tokens=0`
  - `keyword_method=none`
- **Health flags:** no `enabled:false`, no `fail_open:true`, max `push_attempts=1`.
- **Coverage gap:** no `finalize-task`, `promote`, `compact`, or `processed-command-*` telemetry appeared in this window.
- **Why this is happening:** reviewer retrieval is invoked with only `--role reviewer --pr-number` (`.github/workflows/review_autofix.yml:1485-1487`), and `scripts/ai_memory_lib.py:1355-1365` returns no keywords when title/body are empty.
- **Run-specific examples:** slow runs `26285071146`, `26286532978`, `26289024192`, `26289219666`, `26292599330`, `26293968972`, `26293979422`, `26296423247` all showed empty retrieve results; recent `orchestrate_poll` run `26328229210` only recorded `poll_started` / `poll_completed` run events.
- **Recommendation:** pass PR title/body into reviewer retrieval, and emit any retrieval token budget if one exists. If title/body are absent, skip retrieval instead of writing empty context.

## GH API Call Audit

1. **The biggest sampled API hotspot is `review_autofix` check-run polling**
   - **Evidence:** sampled deep `review_autofix` logs contain **32 traced `check-runs` API command lines**, and `README.md:65-68` documents one paginated `gh api ... check-runs?per_page=100` per poll iteration.
   - **Why it matters:** each wait iteration costs both wall time and shared GitHub API budget.
   - **Concrete change:** on self-triggered verification reruns, reuse the first snapshot longer and reduce polling budget; only repoll when a distinct non-review workflow is still pending.
   - **Estimated reduction:** **30-70% fewer polling calls** on the slow outliers.

2. **PR metadata is being re-fetched across gate + codex-agent instead of handed off once**
   - **Evidence:** sampled slow `review_autofix` runs show repeated traced PR/meta/file/comment lookups: **23** PR meta/list lines, **10** commit-meta, **10** PR-files, **14** issue-comments, **7** GraphQL traces.
   - **Repo rule cross-check:** `CLAUDE.md:415-445` explicitly says to batch/reuse existing API calls and treat per-iteration `gh api` calls in loops as a review-blocker.
   - **Concrete change:** materialize a single `pr_context.json` artifact in the gate job containing PR metadata, commit metadata, changed files, issue/review comment excerpts, and the initial check-run snapshot; make downstream scripts consume that artifact first.
   - **Estimated reduction:** removes several repeated lookups per long run and lowers secondary rate-limit risk.

3. **`review_autofix` sweep does work even when there is nothing to do**
   - **Evidence:** recent sweep run `26327839176` logged `AUTOFIX_SWEEP_START ... candidates=0` and `AUTOFIX_SWEEP_END dispatched=0 ... candidates=0`, but its deep log still traced paginated `pulls` and `workflow runs` API commands.
   - **Concrete change:** exit immediately after the first candidate scan when the filtered candidate set is empty; do not query workflow runs for zero-candidate sweeps.
   - **Estimated reduction:** trims at least one paginated loop from every no-op sweep.

4. **Low-confidence minor issue from `log_summary` only: drift-audit is spending time on missing logs**
   - **Evidence:** `drift_audit` run `26323235819` (log-summary evidence only) reported repeated `log fetch failed: log not found` warnings across historical runs while also reporting `no drift markers found`.
   - **Concrete change:** stop fetching logs for runs whose archives are already unavailable, or fail fast after the first missing-log pass.
   - **Estimated reduction:** low, but it should cut unnecessary API/log-fetch churn in that audit workflow.

**Rate-limit note:** I did **not** find confirmed runtime `GitHub API rate limit hit` warning emissions in the deep logs; the 429-related text present was script source/comments, not clear evidence of live throttling.

## Prompt Cache & Memory System

- **Prompt cache status:** enabled in sampled runs (`OPENROUTER_PROMPT_CACHE_DISABLED: false`), but **no hit/miss counters** were logged.
- **Prompt-shape evidence:** in 7 sampled long `review_autofix` runs:
  - `pre_assembled_static.txt`: **117,087 bytes** every time
  - `editor_prompt_body.txt`: **103,845-218,478 bytes**
  - full editor prompt: **221,322-335,959 bytes**, average **270,078**
- **Good news:** `scripts/review_apply_fixes.sh:1139-1150` already assembles the static prompt block first, which is the right cache-friendly shape.
- **Risk / inference:** because the static prefix is stable but the body varies widely, prefix caching should help only if provider-side counters confirm hits; beyond the stable block, prompt variance is high enough that cache reuse may fragment.
- **Memory side:** retrieval is currently ineffective, so the memory subsystem is not reducing prompt size or improving context quality in sampled reviewer runs.
- **Concrete improvements:**
  1. Emit `cache_creation_input_tokens` and `cache_read_input_tokens`.
  2. Keep all invariant instructions above variable run metadata; do not move timestamps/sha churn upward.
  3. Fix reviewer memory retrieval inputs so empty memory blocks stop consuming prompt real estate.
  4. Fix Semble availability reporting in summaries: recent `orchestrate_poll` `log_summary` for `26328229210` said Semble unavailable, but deep steps `013` and `015` showed `SEMBLE_AVAILABLE=true` and `SEMBLE_INDEX_AVAILABLE=true`.

## Orchestrator Health

- **Current state is unhealthy but explicit:** recent poll run `26328229210` showed `Judge cycle: 28`, `stall: 28`, `Recovery count: 0/3`, and then hard-blocked Wave 2 because the integration fingerprint check failed.
- **The blocker is propagating into no-op workflows:** deep logs for:
  - `plan` run `26328259857`
  - `clarify` run `26328259867`
  - `orchestrate_clarify_respond` run `26328259868`
  
  all show the blocked comment body being expanded into the workflow condition and then ending with `Result: false`.
- **Observed skip bursts:** on **May 23, 2026**, there were repeated **8-run clusters** at **06:53**, **07:47**, and **08:35** across `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`.
- **Concurrency nuance matters:** `clarify.yml` and `orchestrate_clarify_respond.yml` are per-issue with `cancel-in-progress: true`; `implement.yml` is per repo+issue with `cancel-in-progress: false`; `plan.yml` has no explicit concurrency block. That combination suppresses some duplicate work but still allows blocked-state dispatch churn.
- **Smallest safe mitigation:** write a dedicated blocked state/label (for example, an orchestrator state flag rather than only comment text) and have `clarify/plan/implement/respond` ignore dispatches while that blocked flag is present.
- **Indicators to track:**
  - `judge_cycle`
  - `stall`
  - `recovery_count`
  - integration fingerprint satisfaction ratio
  - blocked skip-run burst count per issue
  - `review_autofix` cancel rate

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Recommended fix |
|---|---|---|---|
| clarify → plan → implement → respond | **Control-plane churn, not compute** | 402/600 runs are skips; recent blocked bursts created 8 no-op runs at a time | Add a blocked-state gate so these workflows do not dispatch when orchestrator already knows Wave 2 is blocked |
| orchestrate_poll | **Queue/setup + issue-processing** | Family p50 **142.5s**; run `26328229210` had runner wait, **14.1s** checkout, **10.7s** Semble install, **59.1s** issue processing | Cache setup artifacts and reduce per-tick bootstrap |
| review_autofix | **Main compute + wait bottleneck** | **74.3%** of total runtime; sampled `codex-agent` spans **4,090-5,934s**; 39-47 check-run waits in slow runs | Re-enable self-trigger skip, shorten wait budget on verification reruns, and dedupe active runs |
| CI / merge validation | **Long validation gate with concentrated failures** | `ci` p50 **1081s**, p95 **1141.6s**; all 3 failures from same poller unit-test step | Fix the two regressions and add a fail-fast shard |
| merge/conflict overhead | **Integration branch blocked** | `Wave 2 dispatch BLOCKED` in `26328229210`; no recovery attempts yet because the block is fail-closed | Heal fingerprint contract before further wave movement |
| auxiliary validation/audits | **Low share but occasional wasted work** | `validation_refresh` **312s** once; `drift_audit` **252s** with missing-log warnings (log-summary evidence only) | Fail fast on missing logs / avoid unnecessary audit fetches |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix`: **81 runs**, **74.3%** of total runtime, p95 **4545s**
  - `ci`: **25 runs**, p50 **1081s**, all failures concentrated here
  - `orchestrate_poll`: **30 runs**, p50 **142.5s**, setup/runner wait visible

- **Top failure modes**
  - Poller unit-test regressions in `tests/test_orchestrate_poll_process.py`
  - Orchestrator integration fingerprint block on `orchestrator/project-2867`
  - Duplicate/overlapping `review_autofix` runs causing cancellations and wasted runtime

- **Highest-cost drivers**
  - Full reviewer/editor reruns on bot-authored autofix follow-ups
  - Long check-run polling waits inside `review_autofix`
  - Large but mostly unmeasured prompt payloads with missing cache/token observability

- **Top 3 prioritized actions**
  1. **Fix the two `orchestrate_poll_process` regressions** so `CI` returns to full pass rate.
  2. **Enable narrow self-trigger autofix dedupe** and shorten `CHECK_RUNS_WAIT_TIMEOUT_SECS` for verification reruns.
  3. **Add an explicit orchestrator blocked-state gate** so blocked Wave 2 does not keep dispatching skip-only downstream workflows.

## Metrics Appendix

### Window Summary

| Metric | Value |
|---|---:|
| Total runs | 600 |
| Success | 178 |
| Failure | 3 |
| Cancelled | 17 |
| Other | 402 |
| Avg duration (s) | 225.0 |
| p50 duration (s) | 2.0 |
| p95 duration (s) | 1150.0 |
| Sampled success runs | 2 |

**Note:** in this window, `Other` is almost entirely **skipped** runs, not unknown states.

### Workflow Family Summary

| Workflow family | Runs | Success | Failure | Cancelled | Other | Failure rate | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 81 | 63 | 0 | 17 | 1 | 0.0% | 1238.1 | 18.0 | 4545.0 |
| ci | 25 | 22 | 3 | 0 | 0 | 12.0% | 1060.0 | 1081.0 | 1141.6 |
| orchestrate_poll | 30 | 30 | 0 | 0 | 0 | 0.0% | 145.8 | 142.5 | 188.0 |
| copilot_pull_request_reviewer | 10 | 10 | 0 | 0 | 0 | 0.0% | 202.7 | 197.0 | 279.5 |
| drift_audit | 1 | 1 | 0 | 0 | 0 | 0.0% | 252.0 | 252.0 | 252.0 |
| validation_refresh | 1 | 1 | 0 | 0 | 0 | 0.0% | 312.0 | 312.0 | 312.0 |
| nightly_validation_selftest | 1 | 1 | 0 | 0 | 0 | 0.0% | 116.0 | 116.0 | 116.0 |
| integration_pr_readiness | 18 | 18 | 0 | 0 | 0 | 0.0% | 9.3 | 9.0 | 11.3 |
| lint_pr_body_auto_close | 19 | 19 | 0 | 0 | 0 | 0.0% | 8.3 | 8.0 | 11.1 |
| issue_pr_status | 6 | 6 | 0 | 0 | 0 | 0.0% | 24.0 | 16.0 | 54.8 |
| cancel_on_pr_close | 6 | 6 | 0 | 0 | 0 | 0.0% | 8.2 | 8.5 | 9.8 |
| forward_merge_stable_to_main | 1 | 1 | 0 | 0 | 0 | 0.0% | 31.0 | 31.0 | 31.0 |
| clarify | 101 | 0 | 0 | 0 | 101 | 0.0% | 1.3 | 1.0 | 2.0 |
| plan | 100 | 0 | 0 | 0 | 100 | 0.0% | 1.5 | 1.0 | 2.1 |
| implement | 100 | 0 | 0 | 0 | 100 | 0.0% | 1.6 | 1.0 | 5.0 |
| orchestrate_clarify_respond | 100 | 0 | 0 | 0 | 100 | 0.0% | 1.5 | 1.0 | 2.0 |

### Runtime Share by Major Family

| Workflow family | Total runtime (s) | Runtime share |
|---|---:|---:|
| review_autofix | 100,287 | 74.3% |
| ci | 26,501 | 19.6% |
| orchestrate_poll | 4,374 | 3.2% |
| copilot_pull_request_reviewer | 2,027 | 1.5% |

### Token / Cache Metrics

| Metric | Value | Note |
|---|---:|---|
| `prompt_tokens` | unavailable | no token counters emitted in deep logs |
| `completion_tokens` | unavailable | no token counters emitted in deep logs |
| `total_tokens` | unavailable | no token counters emitted in deep logs |
| `cache_creation_input_tokens` | 0 observed | counter not emitted |
| `cache_read_input_tokens` | 0 observed | counter not emitted |
| Prompt cache disabled flag | `false` | observed in sampled `review_autofix` deep logs |
| Stable static prompt bytes | 117,087 | identical across 7 sampled long review runs |
| Editor prompt bytes | 221,322-335,959 | average 270,078 |

### AI Memory Metrics

| Metric | Value |
|---|---:|
| Telemetry records observed | 35 |
| `record-run-event` | 20 |
| `record-candidate` | 7 |
| `retrieve` | 8 |
| Retrieve hit rate | 0.0% |
| Avg retrieve `estimated_tokens` | 0.0 |
| `keyword_method=none` retrieves | 8 |
| `enabled=false` entries | 0 |
| `fail_open=true` entries | 0 |
| Max `push_attempts` | 1 |

### GH API Summary (sampled deep logs)

| Pattern | Sampled traced command lines | Main workflows / runs |
|---|---:|---|
| `check-runs` polling | 32 | slow `review_autofix` runs |
| PR meta/list | 23 | slow `review_autofix`, recent sweep `26327839176` |
| issue comments | 14 | slow `review_autofix` |
| commit meta | 10 | slow `review_autofix` |
| PR files | 10 | slow `review_autofix` |
| GraphQL | 7 | slow `review_autofix` |
| workflow runs | 2 | recent sweep `26327839176` |

**Sampling note:** these are traced `gh api` command lines from deep logs, not a complete accounting of underlying paginated/retried HTTP requests.

### Semble / Serena / Other MCP Summary

| Server | Queries | Fallbacks | Probes | Logged bytes | Response bytes | Notes |
|---|---:|---:|---:|---:|---:|---|
| Semble | 16 | 0 | 0 | 129,949 | n/a | 7 slow `review_autofix` runs; avg 500.9ms/query |
| Serena | 0 | 0 | 0 | n/a | n/a | no operational telemetry; `SERENA_ENABLED: false` seen in run `26296423247` |
| Other MCP servers observed | 0 | 0 | 0 | n/a | n/a | none observed |

### Semble Target Breakdown

| Target | Queries | Logged bytes | Avg bytes | Total ms | Avg ms |
|---|---:|---:|---:|---:|---:|
| reviewer-context | 7 | 74,784 | 10,683 | 3,531 | 504.4 |
| overflow | 8 | 53,088 | 6,636 | 4,014 | 501.8 |
| conflict-resolver-context | 1 | 2,077 | 2,077 | 469 | 469.0 |

### MCP Availability Rows (`probe_ok` / `probe_failed` / `probe_skipped`)

| Server | Target | probe_ok | probe_failed | probe_skipped | Note |
|---|---|---:|---:|---:|---|
| Semble | reviewer-context | 0 | 0 | 0 | no `SEMBLE_PROBE` telemetry emitted |
| Semble | overflow | 0 | 0 | 0 | no `SEMBLE_PROBE` telemetry emitted |
| Semble | conflict-resolver-context | 0 | 0 | 0 | no `SEMBLE_PROBE` telemetry emitted |
| Serena | any | 0 | 0 | 0 | no operational `SERENA_PROBE` telemetry emitted |

