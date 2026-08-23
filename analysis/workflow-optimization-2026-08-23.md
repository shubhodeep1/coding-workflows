## Executive Summary

- **Primary bottleneck is orchestrator polling, not failures.** `orchestrate_poll` ran **94/148 runs**, all successful, but consumed ~**18,784s / 92%** of observed runner time; p50 was ~**200s**. In **17/17 deep-dive poll runs**, the active tracking issue was processed and then logged **“Project already complete, skipping.”** Estimated impact: **up to ~3–5 runner-hours per window** if completed trackers are terminalized earlier. Confidence: **high for sampled deep-dive runs; medium for all 94 polls**.
- **Two AI memory event pushes are on the critical path.** Sampled poll runs show `Record_poll_run_start` and `Record_poll_run_end` averaging **~31.6s each**, with `AI_MEMORY_TELEMETRY` `push_attempts=1` and `ok=true`. Estimated impact: **30–60s saved per poll** by batching or deferring push work. Confidence: **high**.
- **A completed orchestrator tracker remains “active.”** Runs including `32630556461`, `32628574764`, `32629349573`, and `32629924720` all found **1 active tracking issue**, processed issue **#3749**, then skipped it as already complete, followed by zero-action sweeps. Estimated impact: **largest low-risk speed win**. Confidence: **high**.
- **Only hard failure was opaque security-audit failure.** Run `32627477867` failed after 70s in `Security Audit / Run security audit` with `security-audit: scope=full` followed by `Error: No such file or directory (os error 2)` and no missing path. Estimated impact: **eliminates one observed failure class and speeds triage**. Confidence: **high**.
- **Cost telemetry is not evidence-grade yet.** All token fields, OpenRouter calls, cache read/write tokens, Semble queries, Serena queries, `BREAK_GLASS`, and `CONTEXT_BUDGET_WARN` are **0/null**, despite model env such as `MODEL_EDITOR=openai/gpt-5.5` and `MODEL_REASONING_EFFORT_JUDGE=xhigh`. Estimated impact: **blocks reliable model/cache optimization**. Confidence: **high**.
- **Semble is installed/indexed but not observed reducing context.** Sampled poll runs show `SEMBLE_AVAILABLE` becomes `true` and `SEMBLE_INDEX_AVAILABLE` becomes `true`, but there are **0 `SEMBLE_QUERY` / `SEMBLE_FALLBACK` lines** and **0 logged query bytes**. Estimated impact: **~8–13s per poll saved if lazily installed only when queried**. Confidence: **high**.

## Speed Optimizations

1. **Terminalize completed orchestrator trackers before heavy setup.**
   - **Evidence:** `orchestrate_poll` has **94 runs**, p50 ~**200s**. In 17/17 deep-dive poll runs, logs show `Found 1 active tracking issue(s)`, then issue **#3749**, then `Project already complete, skipping.` Runs `32630556461`, `32628574764`, `32629349573`, and `32629924720` all follow this pattern.
   - **Root cause:** Completed project state does not remove or exclude the active tracking marker, so every poll continues through checkout, setup, memory pushes, and sweeps.
   - **Exact change:** When `Project already complete` is detected, remove the active tracking label or close/mark the tracker terminal; make `Find active tracking issues` exclude terminal labels before checkout/setup.
   - **Estimated savings:** **180–195s per completed-project poll**; up to ~**5h** in the observed window if representative.
   - **Risk:** Low-medium; gate on explicit terminal state and log `ORCH_TRACKER_TERMINALIZED tracker=# reason=project_complete`.

2. **Batch AI memory run-event pushes.**
   - **Evidence:** In sampled poll runs, `Record_poll_run_start` and `Record_poll_run_end` cost **~28–33s each**; 34 distinct `AI_MEMORY_TELEMETRY` events across 17 deep-dive poll runs all had `op=record-run-event`, `ok=true`, `push_attempts=1`.
   - **Root cause:** Two separate git-backed memory commits/pushes sit on the critical path.
   - **Exact change:** Record start/completion locally and perform one end-of-run push; keep artifact/local log authoritative if the final push fails.
   - **Estimated savings:** **30–60s per poll**.
   - **Risk:** Low if fail-open behavior is retained and telemetry logs `did_push`, `push_attempts`, `duration_ms`.

3. **Defer Codex/Semble setup until a non-terminal project requires it.**
   - **Evidence:** Run `32630556461` spent ~**27.6s** on repo checkout, ~**7.8s** installing Semble, ~**2.8s** installing Codex, and then skipped the completed project.
   - **Root cause:** Setup runs after only a coarse active-issue check, before terminal-state filtering.
   - **Exact change:** Move terminal-state check before Codex/Semble install; lazily install Semble only before first `SEMBLE_QUERY`.
   - **Estimated savings:** **~35–45s per no-work poll**, plus avoiding unused index churn.
   - **Risk:** Low; preserves behavior when real work exists.

4. **Shorten validation-refresh critical path.**
   - **Evidence:** `validation_refresh` run `32613597504` took **975s**. It processed **12 repos**, with `VALIDATION_SUMMARY processed=12 green=8 red=2 skipped=2 error=0`; all 12 discovery steps were `VALIDATION_DISCOVERY_SKIPPED_DEDUP`.
   - **Root cause:** Serial cross-repo validation plus long self-test waits; `shubhodeep1/digital_pa` waited for app health and failed after **120s**.
   - **Exact change:** Emit per-repo phase timers, skip expensive discovery earlier when deduped, cap per-repo self-test budget, and optionally run independent repo checks in a bounded local parallel batch.
   - **Estimated savings:** **2–6 minutes** on similar refreshes.
   - **Risk:** Medium; start with logging and timeout caps before parallelization.

5. **Avoid full checkout for no-op maintenance workflows.**
   - **Evidence:** `forward_merge_stable_to_main` run `32631386094` succeeded in **37s**, but checkout was ~**27.5s** before reporting `stable is fully merged into main — nothing to do`. `promote_main_to_stable` run `32631357419` spent ~**28.1s** in full-history stable checkout.
   - **Root cause:** Full branch/history fetch before cheap merge/tag decisions.
   - **Exact change:** Use targeted `git ls-remote` / shallow fetch of only `main`, `stable`, and required tags before full checkout.
   - **Estimated savings:** **20–28s** per no-op maintenance run.
   - **Risk:** Low-medium; preserve full fetch only for actual promotion/version operations.

## Cost Optimizations

1. **Fix model/token usage emission before changing model policy.**
   - **Evidence:** Aggregates report `codex_tokens_used=0`, `or_total_tokens=0`, `or_calls=0`, cache tokens `0`, and `cache_hit_rate=null`, while poll process steps set `MODEL_EDITOR=openai/gpt-5.5`, `MODEL_REASONING_EFFORT_JUDGE=xhigh`, and `OPENROUTER_PROMPT_CACHE_DISABLED=false`.
   - **Root cause:** Usage lines are not emitted or collector patterns miss them.
   - **Exact change:** Emit one structured usage line per model call: phase, model, prompt/completion/cache-read/cache-write tokens, latency, cache status, and run/issue ID.
   - **Estimated savings:** Measurement unlock; no safe dollar estimate until telemetry is real.
   - **Quality risk:** Low; logging-only.

2. **Do not initialize expensive AI tooling for completed/no-op polls.**
   - **Evidence:** Completed issue #3749 is repeatedly skipped, yet the workflow still installs Codex/Semble and records memory events.
   - **Root cause:** Work-needed check is too late.
   - **Exact change:** Filter terminal trackers first; then initialize model/Codex/Semble only if a live project requires judge/editor work.
   - **Estimated savings:** Potentially avoids model/tool overhead for all completed-project polls.
   - **Quality risk:** Low if terminal-state detection is conservative.

3. **Make Semble lazy and prove value with query telemetry.**
   - **Evidence:** `SEMBLE_AVAILABLE` and `SEMBLE_INDEX_AVAILABLE` become true in sampled poll runs, but aggregate `semble_query_calls=0`, `semble_query_bytes=0`, `semble_fallbacks=0`.
   - **Root cause:** Semble setup happens without observed queries.
   - **Exact change:** Install/build Semble on first query; emit `SEMBLE_QUERY target=... bytes=... elapsed_ms=... results=...`.
   - **Estimated savings:** **~8–13s per unused poll** plus less log noise.
   - **Quality risk:** Low; fallback to current path when query is needed.

4. **Reduce idle review-autofix sweeps.**
   - **Evidence:** `review_autofix` ran **47** times, all successful, avg **7.19s**. Sample run `32630010502` logged `AUTOFIX_SWEEP_START ... candidates=0` and `AUTOFIX_SWEEP_END dispatched=0 ... candidates=0`.
   - **Root cause:** Frequent scheduled sweep during no-candidate periods.
   - **Exact change:** Add adaptive backoff after N zero-candidate sweeps or trigger on PR activity when possible.
   - **Estimated savings:** Small, ~**5–6 runner-minutes** in this window.
   - **Quality risk:** Low-medium; ensure urgent PRs still trigger review.

## Reliability Improvements

1. **Add path-aware diagnostics around security audit file/model operations.**
   - **Failure evidence:** Run `32627477867` failed in `Security Audit / Run security audit`; after `security-audit: scope=full (no last-audited commit recorded on the tracker)`, the only error was `No such file or directory (os error 2)`.
   - **Root cause category:** Opaque filesystem/tool invocation failure, likely near prompt render or Codex execution after scope resolution.
   - **Exact fix:** Before `render_prompt.sh` and `codex exec`, log checked paths, `cwd`, prompt path, support dir, config path, and command phase; wrap failures as `SECURITY_AUDIT_ERROR phase=... missing_path=... tracker=#3576`.
   - **Expected reliability impact:** Faster triage; prevents blind reruns for missing-file failures.
   - **Rollback/fail-open:** Logging-only is safe; keep scan failure as failure unless the missing file is explicitly optional.

2. **Prevent completed orchestrator trackers from re-entering active poll loops.**
   - **Failure evidence:** 17/17 deep-dive poll runs repeatedly processed completed issue #3749 and then did zero-action sweeps.
   - **Root cause category:** Terminal-state/label hygiene.
   - **Exact fix:** On project completion, remove active label or close tracker; add invariant check at poll start.
   - **Expected reliability impact:** Reduces stuck/ghost active state and avoids repeated no-op cycles.
   - **Rollback/fail-open:** If label update fails, log warning and continue current behavior.

3. **Make validation-refresh red outcomes actionable.**
   - **Failure evidence:** Run `32613597504` concluded success but had red repo outcomes: `shubhodeep1/digital_pa` self-test failed because app stayed unhealthy for 120s; `shubhodeep1/binance-blessings` had `validation_assets_drifted_no_push` and self-test diagnostics.
   - **Root cause category:** Cross-repo validation reports red states without enough timing/root-cause detail.
   - **Exact fix:** Emit `VALIDATION_REPO_TIMING` and `VALIDATION_REPO_DIAGNOSTIC` per repo/phase; include timeout, command, exit code, and whether red should fail the workflow or only annotate.
   - **Expected reliability impact:** Better separation of expected red validation findings from pipeline failure.
   - **Rollback/fail-open:** Keep workflow success semantics initially; add annotations/issues only.

4. **Reconcile telemetry collector coverage.**
   - **Evidence:** Assembled context reports `runs_with_log_telemetry=115`; top-level `summary.json` reports `runs_with_log_telemetry=28`. Token/cache fields are zero across both.
   - **Root cause category:** Collector coverage/schema drift.
   - **Exact fix:** Add collector self-audit line with source file count, parsed run count, deep-log count, and fields dropped/missing.
   - **Expected reliability impact:** Prevents false confidence in cost/cache metrics.
   - **Rollback/fail-open:** Logging-only.

## AI Memory Health

- **Observed telemetry:** Only `AI_MEMORY_TELEMETRY` `op=record-run-event` was found. Across distinct deep-dive poll runs: **17 poll_started + 17 poll_completed** events, all `ok=true`, `did_push=true`, `push_attempts=1`.
- **Retrieve health:** No `retrieve` operations were found, so retrieve hit rate, `records_selected > 0`, `estimated_tokens`, budget utilization, and `keyword_method` distribution are **not computable**.
- **Fail-open / disabled:** No `fail_open: true` or `enabled: false` AI memory telemetry entries were observed.
- **Performance issue:** Individual memory event steps averaged **~31.6s** across sampled start/end steps, making memory persistence a critical-path cost.
- **Recommendation:** Emit structured telemetry for `retrieve`, `record-candidate`, `finalize-task`, `promote`, and `compact`; batch run-event pushes; include `duration_ms`, `records_selected`, `estimated_tokens`, `budget_tokens`, `keyword_method`, and retry counts.

## GH API Call Audit

- **No rate-limit failures observed.** Deep logs did not show HTTP 429 or secondary-rate-limit events.
- **Orchestrator hotspot:** `orchestrate_poll` uses `gh issue list --label "ai:orchestrator-tracking" --limit 20`; sampled runs repeatedly found one active completed tracker.
  - **Recommendation:** Reuse the active issue JSON for terminal-state decisions and label cleanup; emit `GH_API_CALL_SUMMARY route=issues.list count=... retries=... remaining=...`.
  - **Estimated reduction:** Avoids downstream calls/setup rather than many API calls; high runtime impact, low rate-limit impact.
- **Review-autofix hotspot:** `review_autofix` enumerates open PRs with `gh api --paginate GET repos/${REPOSITORY}/pulls`; sample run `32630010502` had `candidates=0`.
  - **Recommendation:** Keep one paginated call, but add zero-candidate adaptive backoff and call summary logging.
- **Validation-refresh gap:** Cross-repo validation touches 12 repositories, but no per-route API summary was emitted.
  - **Recommendation:** Add route-level counters per repo and phase, especially discovery, PR creation/update, workflow dispatch, and status polling.
- **Maintenance workflows:** `promote_main_to_stable` dispatches `test-and-mark-stable.yml`; forward-merge uses retry-wrapped fetch logic. No API failure observed.

## Prompt Cache & Memory System

- **Prompt cache evidence is missing.** `cache_hit_rate=null`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, and `or_calls=0` across the aggregate, despite `OPENROUTER_PROMPT_CACHE_DISABLED=false`.
- **No context pressure observed.** `context_budget_warn_count=0` and no `CONTEXT_BUDGET_WARN` lines were found.
- **No break-glass pressure observed.** `break_glass_count=0` and no `BREAK_GLASS` lines were found.
- **Semble is not yet proven useful.** It becomes available/indexed in sampled poll runs, but there are no `SEMBLE_QUERY` records, so there is no evidence it reduced prompt expansion.
- **Recommendations:**
  - Emit OpenRouter/Codex usage and cache-read/write fields per call.
  - Keep stable prompt prefixes before dynamic issue/run data to improve cache reuse.
  - Move volatile fields, timestamps, and run IDs later in prompts.
  - Emit Semble query bytes/results so cache and retrieval savings can be compared against setup cost.

## Orchestrator Health

- **Health signal:** `orchestrate_poll` had **94/94 successful runs**, no retries, no cancellations.
- **Operational pain point:** Success is not equivalent to progress. Sampled polls repeatedly processed completed tracker **#3749**, skipped it, then ran zero-action sweeps (`Found 0 open PR(s) to scan`, `Noop-suspicious recovery complete. Dispatched: 0, force-merged: 0, blocked: 0`).
- **Smallest safe mitigation:** Add terminal tracker cleanup and a `did_work` flag. If all active trackers are terminal/no-op, exit before checkout/model/memory setup.
- **Trackable indicators to add:** `ORCH_PROGRESS_SUMMARY active_trackers=... complete_skipped=... actions_dispatched=... stall_recoveries=... conflict_fixes=... duration_ms=...`.

## Pipeline Flow Bottlenecks

- **Clarify/plan/implement loop:** Not directly visible in this window; the active orchestrator project was already complete.
- **Poll preflight:** Active issue lookup is cheap (~sub-second in sampled logs) but does not filter completed trackers.
- **Setup/compute:** Repo checkout (~24–28s), Codex install (~3s), Semble install (~8–13s), and support checkouts happen even for completed projects.
- **Memory persistence:** Two run-event pushes add ~**60s** combined per sampled poll.
- **Review/autofix:** Fast and reliable, but mostly idle; sample candidate count was zero.
- **Validation/orchestrate loops:** Validation refresh is the largest single-run bottleneck at **975s**, driven by serial cross-repo validation and self-test waits.
- **Merge/conflict overhead:** Standalone conflict sweeps found **0 open PRs** and fixed **0** in sampled orchestrator runs; run them less often or only when PR candidates exist.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** `orchestrate_poll` dominates runtime (**94 runs**, avg **199.8s**, p50 **200s**); `validation_refresh` single run took **975s**.
- **Top failure mode:** `security_audit` run `32627477867` failed with opaque missing-file error.
- **Highest-cost drivers:** Repeated completed-project poll cycles, memory event pushes, full checkouts, unused Semble setup, and missing token/cache telemetry.
- **Top 3 prioritized actions:**
  1. Terminalize completed orchestrator tracker #3749 and filter terminal trackers before heavy setup.
  2. Batch AI memory start/end events into one fail-open push.
  3. Add structured model/cache/API/phase telemetry so future optimization is measurable.

## Metrics Appendix

### Run summary

| Scope | Runs | Success | Failure | Cancelled | Avg duration | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| All workflows | 148 | 147 | 1 | 0 | 137.9s | 194s | 218s |
| shubhodeep1/coding-workflows | 148 | 147 | 1 | 0 | 137.9s | 194s | ~217–218s |

### Workflow-family metrics

| Workflow family | Runs | Success | Failure | Avg | p50 | p95 / max noted |
|---|---:|---:|---:|---:|---:|---:|
| orchestrate_poll | 94 | 94 | 0 | 199.8s | 200s | ~219s |
| review_autofix | 47 | 47 | 0 | 7.2s | 7s | ~9.7s |
| validation_refresh | 1 | 1 | 0 | 975s | 975s | 975s |
| nightly_validation_selftest | 1 | 1 | 0 | 142s | 142s | 142s |
| security_audit | 1 | 0 | 1 | 70s | 70s | 70s |
| promote_main_to_stable | 1 | 1 | 0 | 40s | 40s | 40s |
| forward_merge_stable_to_main | 1 | 1 | 0 | 37s | 37s | 37s |
| drift_audit | 1 | 1 | 0 | 17s | 17s | 17s |
| workspace_cache_maintenance | 1 | 1 | 0 | 6s | 6s | 6s |

### Cost/cache telemetry

| Metric | Value |
|---|---:|
| Codex tokens / calls | 0 / 0 |
| OpenRouter total tokens / calls | 0 / 0 |
| OR prompt / completion tokens | 0 / 0 |
| OR cache write / read tokens | 0 / 0 |
| cache_hit_rate | null |
| break_glass_count | 0 |
| context_budget_warn_count | 0 |
| Wall-clock p50 / p99 from assembled context | 194,000ms / 225,860ms |
| Telemetry gap | Token/cache usage not emitted or not parsed |

### MCP telemetry

| Server | Query calls | Bytes | Fallbacks | Probe ok | Probe failed | Probe skipped | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| Semble | 0 | 0 | 0 | 0 | 0 | 0 | Available/indexed in sampled polls, but no query records |
| Serena | 0 | 0 | 0 | 0 | 0 | 0 | No query/probe/fallback records |
| Other MCP servers observed | 0 | 0 | 0 | 0 | 0 | 0 | None observed |

### Representative poll-step timings

| Run | Checkout | Semble install | Memory start | Process issue | Memory end | Outcome |
|---|---:|---:|---:|---:|---:|---|
| 32630556461 | ~27.6s | ~7.8s | ~32.4s | ~48.4s | ~31.7s | Project already complete |
| 32628574764 | ~27.4s | ~9.6s | ~33.3s | ~51.2s | ~32.4s | Project already complete |
| 32629349573 | ~24.1s | ~12.6s | ~30.2s | ~46.7s | ~28.0s | Project already complete |

### GH API summary

| Workflow | Observed call pattern | Evidence | Rate-limit signal | Recommendation |
|---|---|---|---|---|
| orchestrate_poll | `gh issue list` for active tracking issues | Runs repeatedly found 1 active issue | None | Filter terminal trackers and emit call summaries |
| review_autofix | `gh api --paginate` open PR list | Run `32630010502`, candidates=0 | None | Adaptive backoff after zero-candidate sweeps |
| validation_refresh | Cross-repo validation operations | 12 repos processed in run `32613597504` | None visible | Add per-repo route counters |
| promote_main_to_stable | `gh workflow run test-and-mark-stable.yml` | Run `32631357419` dispatched version `v1.25.0` | None | Keep; add dispatch call summary |

## Deep Audit — Workflows & Scripts (2026-08-23)

### Section 1: Bug & Correctness Sweep

- **ID** — `SEC-001`
  - **File path and line range** — `.github/workflows/memory_maintenance.yml:22-31`
  - **Severity** — Low
  - **Category tag** — `security`
  - **Description** — The workflow persists a credential-bearing origin URL with `git remote set-url origin "https://x-access-token:${GH_TOKEN}@${SERVER_HOST}/${GITHUB_REPOSITORY}"`. Same pattern appears in multiple workflows/scripts. No direct echo was found in this block, but the token remains in `.git/config`; inference: later diagnostics that print remotes/config could rely on log masking rather than avoiding the secret-bearing value entirely. [NEEDS VERIFICATION]
  - **Recommended fix** — Prefer per-command auth (`git -c http.extraheader=...`) or restore a tokenless remote immediately after the authenticated operation. If a credentialed remote is unavoidable, add a shared cleanup helper and call it before any diagnostic/archive step.

- **ID** — `SHELL-001`
  - **File path and line range** — `scripts/validate_process.sh:356-362`
  - **Severity** — Low
  - **Category tag** — `shellcheck`
  - **Description** — ShellCheck SC2155 flags `local msg="$1$(_tg_link_suffix)"`; this masks the return status of `_tg_link_suffix` under `set -euo pipefail`, making suffix-generation failures invisible.
  - **Recommended fix** — Split declaration and assignment:
    `local msg; msg="$1$(_tg_link_suffix)"`, then handle failure explicitly if suffix generation should affect alert delivery.

- **ID** — `SHELL-002`
  - **File path and line range** — `scripts/stage_workflow_support.sh:117-139,206-219`
  - **Severity** — Low
  - **Category tag** — `shellcheck`
  - **Description** — ShellCheck SC2043 flags one-item loops (`for f in transcript_archive.sh; do`, `for f in unattended_system_instructions.md; do`). These are not runtime bugs, but they obscure whether a list was intended to be extensible.
  - **Recommended fix** — Replace single-item loops with scalar helper calls, or move them into the same list-driven staging mechanism used by `REQUIRED_BOOTSTRAP_SCRIPTS` at `scripts/stage_workflow_support.sh:45-78`.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`
  - **File path and line range** — `.github/workflows/review_autofix.yml:345-347,1603-1615`; `scripts/review_collect_pr_metadata.sh:209-226`; `scripts/review_enable_auto_merge.sh:65-75,127-136`
  - **Severity** — Medium
  - **Category tag** — `api-redundancy`
  - **Description** — The review-autofix path re-fetches PR metadata and labels across gate, codex-agent defense, metadata collection, and auto-merge. Current fixed calls include at least 3 `GET /pulls/{PR_NUMBER}` calls plus 1 labels call, before comments/review-comment fetches. The collector already materializes `PR_PAYLOAD_FILE` and `PR_META_FILE`, but `review_enable_auto_merge.sh` independently fetches labels and PR metadata again.
  - **Recommended fix** — Extend `review_collect_pr_metadata.sh` to also write `PR_LABELS_FILE`, and update `review_enable_auto_merge.sh` to accept `PR_PAYLOAD_FILE`/`PR_META_FILE`/`PR_LABELS_FILE` inputs with live API fallback. Current duplicate PR/label calls: ~4; proposed normal-path duplicate calls: 0 extra after the collector. Reuse `scripts/gh_helpers.sh` `_safe_gh_jq` and the existing `PR_PAYLOAD_FILE` pattern.

- **ID** — `BATCH-001`
  - **File path and line range** — `scripts/orchestrate_poll_process.sh:4175-4223`
  - **Severity** — Medium
  - **Category tag** — `api-batching`
  - **Description** — `evaluate_sync_superseded_by_main` loops over issue numbers, calls `_issue_cross_ref_pr_numbers_unique` per issue, then `_fetch_pr_json` and `GET /pulls/{pr}/files` per PR. Current call count is O(I + 2P). Existing batched helpers already exist at `scripts/orchestrate_poll_process.sh:10774-10917`.
  - **Recommended fix** — Add a batch helper modeled on `_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql` to fetch issue cross-references and PR state for up to 25 aliases per GraphQL call. Projected call count: `ceil(I/25)` for issue/PR state plus REST file calls only for remaining candidate PRs. Batching PR files through GraphQL should be verified before replacing REST pagination. [NEEDS VERIFICATION]

- **ID** — `BATCH-002`
  - **File path and line range** — `scripts/orchestrate_poll_process.sh:18089-18102,18175-18203`
  - **Severity** — Medium
  - **Category tag** — `api-batching`
  - **Description** — The standalone PR noop-suspicious sweep reuses the PR list, but then does per-PR comments fetches and, for candidates, per-PR commits fetches. Current common-case call count is N issue-comments calls; suspicious candidates add commits/pulls/check-runs calls.
  - **Recommended fix** — Add a GraphQL alias batch for PR comments/commit metadata by PR number, similar to `STALL_MANAGED_LINKED_PR_CACHE` population at `scripts/orchestrate_poll_process.sh:16784-16810`. Projected common-case calls: N REST calls → `ceil(N/25)` GraphQL calls, then existing REST fallbacks only for candidates that cross the retry threshold.

- **ID** — `API-002`
  - **File path and line range** — `scripts/tg_helpers.sh:155-206,227-278,314-445`
  - **Severity** — Medium
  - **Category tag** — `api-redundancy`
  - **Description** — Telegram tracking helpers repeatedly scan issue comments and then perform raw `curl` PATCH/POST/DELETE mutations. A tracked send performs 1 Telegram call plus 1 GitHub comments scan and 1 GitHub mutation; cleanup paginates comments and deletes each tracking comment individually.
  - **Recommended fix** — Centralize marker-comment upsert/delete in `gh_helpers.sh` using `gh_retry`/`curl_gh_api`, cache the latest marker comment per issue+phase during a process, and batch cleanup by first collecting all marker IDs from one paginated pass. Current tracked-send GitHub calls: 2 per alert; proposed: 1 cached read per issue/phase plus 1 mutation per alert.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`
  - **File path and line range** — `scripts/gh_helpers.sh:407-562`; examples in `.github/workflows/plan.yml:1000-1001`, `.github/workflows/review_autofix.yml:1610-1614`, `.github/workflows/implement.yml:3931-3934`
  - **Severity** — Low
  - **Category tag** — `duplication`
  - **Description** — `gh_retry` / `_safe_gh_jq` fallback snippets are repeated across workflows and scripts even though canonical implementations exist in `scripts/gh_helpers.sh`.
  - **Recommended fix** — Add a tiny shared bootstrap function, e.g. `source_gh_helpers_or_fallback <path>`, owned by `scripts/gh_helpers.sh` or `scripts/bootstrap_gh_helpers.sh`. Update workflow run blocks and scripts to call that helper instead of retyping fallback definitions.

- **ID** — `DUP-002`
  - **File path and line range** — `scripts/label_helpers.sh:122-170`; duplicates in `scripts/orchestrate_poll_process.sh:2372-2495`, `scripts/validate_process.sh:1150-1184`, `.github/workflows/review_autofix.yml:1126-1139`
  - **Severity** — Medium
  - **Category tag** — `duplication`
  - **Description** — Label creation and phase-label mutation are implemented in several places with slightly different failure semantics. Example: `label_helpers.sh` returns failure on label-create failure, while `orchestrate_poll_process.sh:2425-2427` logs a warning and returns success.
  - **Recommended fix** — Move the cache-aware implementation into `scripts/label_helpers.sh` with signatures `ensure_label_exists <label> [repo]` and `set_issue_phase_label_resilient <issue> <phase-label> [repo]`. Source it from orchestrator, validation, and review workflows; keep workflow-local fallbacks only for bootstrap steps that cannot stage scripts.

- **ID** — `DUP-003`
  - **File path and line range** — `scripts/stage_workflow_support.sh:45-103,152-219`; `.github/workflows/implement.yml:866-1147`
  - **Severity** — Medium
  - **Category tag** — `duplication`
  - **Description** — Workflow-support staging exists both as a script and as a large inline implementation workflow block. The inline block repeats support-script, prompt, schema, Semble, and model-catalog staging logic.
  - **Recommended fix** — Extend `scripts/stage_workflow_support.sh` to support `--target implement` and replace `.github/workflows/implement.yml:853-1148` with a small call to that script. Suggested signature: `scripts/stage_workflow_support.sh implement --workflow-source shubhodeep1/coding-workflows --script-ref "$SCRIPT_REF"`.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`
  - **File path and line range** — `.github/workflows/plan.yml:998-1288`
  - **Severity** — High
  - **Category tag** — `expression-limit`
  - **Description** — Interpolated `run:` block estimated at 19,600 chars, leaving ~1,400 chars before GitHub’s 21,000-character expression limit. The block embeds a large planning prompt heredoc at lines 1003-1210 and also includes `${{ }}` interpolations.
  - **Recommended fix** — Move the heredoc to `prompts/mode-plan.txt` or a new external prompt template, and keep the workflow step as prompt assembly plus invocation only.

- **ID** — `EXPR-002`
  - **File path and line range** — `.github/workflows/implement.yml:3677-3922`
  - **Severity** — High
  - **Category tag** — `expression-limit`
  - **Description** — Interpolated destructive/scope-guard handler estimated at 18,329 chars, leaving ~2,671 chars. It includes multiple `${{ github.repository }}` / secret-derived env interpolations plus large inline comments and issue/Telegram message bodies.
  - **Recommended fix** — Extract the handler to `scripts/implement_handle_guard_rejection.sh` and pass current outputs through env vars. Keep only the `if:` and script invocation in YAML.

- **ID** — `EXPR-003`
  - **File path and line range** — `.github/workflows/implement.yml:853-1148`
  - **Severity** — Medium
  - **Category tag** — `expression-limit`
  - **Description** — Interpolated support-staging block estimated at 15,950 chars, leaving ~5,050 chars. It also duplicates staging logic from `scripts/stage_workflow_support.sh`.
  - **Recommended fix** — Same as `DUP-003`: move staging to `scripts/stage_workflow_support.sh` and invoke it from the workflow.

- **ID** — `EXPR-004`
  - **File path and line range** — `.github/workflows/memory_maintenance.yml:45-391`
  - **Severity** — Medium
  - **Category tag** — `expression-limit`
  - **Description** — Interpolated repository-learning extraction block estimated at 15,152 chars, leaving ~5,848 chars. The block embeds substantial Python code and `${{ github.run_id }}` / actor interpolation.
  - **Recommended fix** — Extract the Python logic to `scripts/extract_repo_learnings.py` and call it with explicit CLI args/env vars. Keep the workflow responsible for secrets/env only.

Additional scan note: no workflow file exceeded the 800 KB warning threshold; largest observed workflow was `.github/workflows/review_autofix.yml` at ~449,970 chars.

### Section 5: Cross-Cutting Concerns

- **ID** — `CONSIST-001`
  - **File path and line range** — `.github/workflows/review_autofix.yml:345-347,390-396,481-487,563-565,621-625`; `.github/workflows/review_autofix_sweep.yml:86-93,120-126`
  - **Severity** — Low
  - **Category tag** — `consistency`
  - **Description** — Some review-autofix gates use raw `gh api` while adjacent codex-agent steps source `gh_helpers.sh` and use `gh_retry`. Several raw calls intentionally fail open, but the retry/no-retry policy is encoded ad hoc in each block. [NEEDS VERIFICATION]
  - **Recommended fix** — Add a helper such as `gh_try_json --fail-open --max-attempts 1|3` in `scripts/gh_helpers.sh`, then use it consistently for fail-open gates and standard retries.

- **ID** — `DEAD-001`
  - **File path and line range** — `scripts/review_run_reviewers.sh:496-528,578-586,3365-3373,3931-3939`
  - **Severity** — Low
  - **Category tag** — `dead-code`
  - **Description** — ShellCheck reports unused assignments including `probe_prompt`, `RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE`, `RAW_REVIEWER_SYMBOL_DIFF_SUMMARY_FILE`, `REVIEWER_HEALTH_LAST_OPEN_UNTIL_EPOCH`, `REVIEWER_ATTEMPT_WD_REASON`, and `REVIEWER_ATTEMPT_CMD_RC`.
  - **Recommended fix** — Remove unused variables or export/consume them in telemetry if they are intended for later diagnostics. For retry telemetry, prefer emitting through the existing reviewer substate helpers rather than storing unused globals.

Cross-cutting scan notes:
- No `TODO`, `FIXME`, or `HACK` markers were found under `.github/workflows` or `scripts`.
- Python syntax compilation was already reported clean in prior work (`python_compile_errors 0`).
- Prior report findings about completed orchestrator trackers, AI memory push batching, lazy Semble/Codex setup, security-audit diagnostics, and validation-refresh timing are not duplicated here.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 |  |
| High | 2 | EXPR-001, EXPR-002 |
| Medium | 8 | API-001, BATCH-001, BATCH-002, API-002, DUP-002, DUP-003, EXPR-003, EXPR-004 |
| Low | 6 | SEC-001, SHELL-001, SHELL-002, DUP-001, CONSIST-001, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 workflows | Medium |
| API call optimization | 4 scripts/workflows | Medium |
| Code modularization | 4 scripts/workflows | Large |
| Expression size reduction | 3 workflows + 2 scripts/prompts | Medium |
| Medium/Low fixes | 5 scripts/workflows | Small |
