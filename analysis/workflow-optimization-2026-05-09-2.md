## Executive Summary

- **`workflow_log_analysis` is the highest-impact critical-path problem right now.** In `shubhodeep1/coding-workflows` run `25589394934` (`Test & Mark Stable Release`), job `orphan-workflows-test`, step `Dispatch & watch — workflow-log-analysis` failed after `9,874s` only because child run `25589406932` (`Workflow Log Analysis`) ended `cancelled`. In that child, `deep-audit` had already finished and pushed its report section by `03:52:56Z`; the remaining `api-redundancy` tail ran until cancellation at `05:23:14Z`. **Estimated impact:** remove roughly `90–130 minutes` from worst-case smoke/release validation and eliminate the sampled `test_and_mark_stable` failure mode. **Confidence:** high.

- **`review_autofix` is the dominant active-work latency and cost driver, especially on `claude/*` comment-only reviews.** Evidence-grade run summaries show `review_autofix` runs `25594520531` (`1654s`), `25595263155` (`1540s`), and `25595784150` (`1505s`) all took `~25–28 minutes` even though the gate logged `editor/commit/judge/auto-merge skipped`; those same runs reported `REVIEWERS_SUCCESSFUL: 6`. Family-level stats back this up: `review_autofix` p95 is `2335.6s` with `16` cancellations in `63` runs. **Estimated impact:** `10–20 minutes` faster on the slow comment-only path and materially lower token/model spend. **Confidence:** high.

- **Validate-hints caching is configured to help, but it is not actually saving.** Recent `validate` runs `25596119024` (`174s`), `25595170096` (`184s`), and `25594130477` (`173s`) all logged `[warning]Path Validation Error: Path(s) specified in the action for caching do(es) not exist, hence no cache is being saved.` Run `25596119024` also logged `Cache not found for input keys: validate-hints-v1-...`. The workflow intends this cache to skip the Codex discovery call entirely (`.github/workflows/validate.yml:482-497`; `scripts/validate_process.sh:1993-2009`). **Estimated impact:** lower validate latency by tens of seconds per run and reduce repeated LLM discovery work. **Confidence:** high.

- **GitHub API usage has a few clear, fixable hotspots.** The `test-and-mark-stable` watcher loop in run `25589394934` emitted `645` `status=` lines, proving at least `645` `actions/runs/{id}` status polls from one step. Separately, `.github/workflows/review_autofix.yml:1427-1488` can make up to `20` per-issue REST calls after a GraphQL miss, and `scripts/orchestrate_poll_process.sh:11310-11345` does an `N+1` open-PR sweep (`1` list call + `1` pull-details call per candidate). **Estimated impact:** large reduction in API pressure and secondary-limit risk; moderate latency improvement in analysis/orchestration tails. **Confidence:** high.

- **AI memory is helping `implement`, but it is mostly not helping `review_autofix`.** Across deep-dive logs I parsed `8` `retrieve` telemetry events: only `2` hits (`25%` hit rate), both from `implement` runs `25571327387` and `25580421671`; all `6` sampled `review_autofix` retrieves returned `0` records with `keyword_method=none`. **Estimated impact:** better reviewer memory seeding could improve review quality and reduce repeated prompt/context expansion on the repo’s most expensive workflow family. **Confidence:** medium.

## Speed Optimizations

1. **[Critical path] Add a smoke-mode to `workflow_log_analysis` when dispatched from release/smoke gating.**
   - **Evidence:** In `shubhodeep1/coding-workflows` run `25589394934`, job `orphan-workflows-test`, step `Dispatch & watch — workflow-log-analysis` failed after `9,874s` because child run `25589406932` concluded `cancelled`. The child had already completed `deep-audit` and pushed its section by `03:52:56Z`; only the later `api-redundancy` tail remained before cancellation at `05:23:14Z`.
   - **Root cause:** `test-and-mark-stable` is synchronously waiting on a four-job analysis workflow that is heavier than the smoke gate needs.
   - **Exact change:** Add a boolean input such as `smoke_mode` or two booleans such as `run_deep_audit` / `run_api_redundancy` to `.github/workflows/workflow-log-analysis.yml`, and in `.github/workflows/test-and-mark-stable.yml:3320-3405` pass the reduced mode from `Dispatch & watch — workflow-log-analysis`. In smoke mode, wait only for `collect-logs` and `analyze-commit-notify`; keep `deep-audit` and `api-redundancy` for scheduled/manual runs or dispatch them asynchronously after the gate completes.
   - **Estimated time savings (inference):** about `90 minutes` if only `api-redundancy` is removed from the smoke critical path, and up to `~130 minutes` if both `deep-audit` and `api-redundancy` move off that path in worst-case runs like `25589406932`.
   - **Implementation risk:** Medium. Keep the full workflow unchanged by default and scope the lighter behavior only to smoke-gate dispatches.

2. **[Critical path] Batch or scope the `api-redundancy` standalone PR sweep that was still running at cancellation.**
   - **Evidence:** The tail of child run `25589406932` `step-001-api-redundancy.log` shows cancellation while in `Standalone PR conflict sweep`, immediately after `gh pr list` and `default_branch` fetches. The code at `scripts/orchestrate_poll_process.sh:11293-11390` lists open PRs once, then fetches each PR again via REST.
   - **Root cause:** The analysis tail contains an `N+1` PR-scan pattern that can grow with open PR volume and keep the workflow alive long after the core report is ready.
   - **Exact change:** Replace the `gh pr list` + per-PR `gh api repos/.../pulls/{n}` pattern in `scripts/orchestrate_poll_process.sh:11310-11345` with one aliased GraphQL prefetch that returns `number`, `headRefName`, `baseRefName`, and mergeability fields for all candidate PRs, then iterate locally from a cached JSON file. For the `workflow_log_analysis` call site, additionally cap the sweep to recently updated PRs or skip it entirely in smoke mode.
   - **Estimated time savings (inference):** potentially large inside the `~90 minute` `api-redundancy` tail of run `25589406932`; the exact share of that tail attributable to this specific sweep is not exposed in the bundle.
   - **Implementation risk:** Medium. Use the repo’s required fail-open pattern from `CLAUDE.md §15`: if the batched prefetch fails, warn and skip or fall back to the legacy path only for non-smoke/manual runs.

3. **[Critical path] Create a lighter reviewer profile for `claude/*` comment-only reviews.**
   - **Evidence:** Recent `review_autofix` runs `25594520531` (`1654s`), `25595263155` (`1540s`), and `25595784150` (`1505s`) all logged `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped`, yet still consumed `~25–28 minutes`. Those runs also reported `REVIEWERS_SUCCESSFUL: 6`. The workflow config defines `6` reviewer models in `.github/workflows/review_autofix.yml:92-98`, and `scripts/review_run_reviewers.sh:1060-1275` runs a two-pass reviewer architecture by default.
   - **Root cause:** The workflow skips the expensive edit/merge tail for `claude/*`, but it still pays almost the full multi-model, two-pass review cost.
   - **Exact change:** When `.github/workflows/review_autofix.yml:264-285` sets `CLAUDE_BRANCH_REVIEW=true`, immediately write branch-specific overrides into `GITHUB_ENV`: `ENABLE_REVIEWER_TWO_PASS=false`, a narrowed `REVIEWER_MODELS` list, and optionally `REVIEWER_REASONING_EFFORT=medium` for small diffs. Keep the current full reviewer panel for normal autofix PRs.
   - **Estimated time savings (inference):** `~600–1200s` per `claude/*` review run based on current `1505–1654s` durations and the fact that the skipped editor/judge tail is not the bottleneck.
   - **Implementation risk:** Low-medium. Scope it to `CLAUDE_BRANCH_REVIEW=true` and keep a manual override input/label for full-depth reviews.

4. **[Medium] Make the validate-hints cache actually save so `validate` can skip rediscovery.**
   - **Evidence:** Runs `25596119024`, `25595170096`, and `25594130477` all logged the same cache-save warning that `.ai/validate-hints-cache` did not exist. Run `25596119024` also showed a cache miss. The intended behavior is explicit in `.github/workflows/validate.yml:482-497` and `scripts/validate_process.sh:1993-2009`: a valid cache should let Phase 0 skip the Codex discovery call.
   - **Root cause:** The workflow restores/saves a directory that may not exist at save time, so a successful future cache never materializes.
   - **Exact change:** Pre-create `.ai/validate-hints-cache` before the `actions/cache@v5` step in `.github/workflows/validate.yml:482-497`, or cache the concrete file path `.ai/validate-hints-cache/hints.yml` after ensuring the parent directory exists. Keep the existing writeback logic in `scripts/validate_process.sh:2218-2219`.
   - **Estimated time savings (inference):** likely `~20–60s` per validate run once a warm cache exists, plus lower LLM/API work.
   - **Implementation risk:** Low. The workflow is already designed to fail open when the cache is absent.

5. **[Medium] If the Copilot reviewer workflow is configurable, stop letting artifact cleanup dominate run time.**
   - **Evidence:** In `copilot_pull_request_reviewer` runs `25595178408` (`136s`) and `25594521708` (`266s`), the evidence-grade `log_summary` marks `Cleanup artifacts` as the dominant step and flags `gh api /repos/shubhodeep1/coding-workflows/actions/runs/<run_id>/artifacts` as the hotspot.
   - **Root cause:** Artifact enumeration/deletion is consuming more wall time than the main review work on sampled runs.
   - **Exact change:** Record uploaded artifact IDs during the upload step and delete only those IDs in `Cleanup artifacts`; if the dynamic workflow is not editable from this repo, deprioritize this item and focus on the repo-local workflows above.
   - **Estimated time savings:** `~100–260s` per Copilot review run in the sampled window.
   - **Implementation risk:** Low if cleanup fails open to artifact retention policy.

6. **[Micro-optimization] Add wrapper-level gating to the noisiest skipped workflows.**
   - **Evidence:** Family-level totals show `clarify` had `206` skipped/other runs out of `216`, and `orchestrate_clarify_respond` had `197` skipped/other runs out of `201`. Recent runs `25596469328`, `25596274725`, `25596119963` (`clarify`) and `25596469332`, `25596274736`, `25596119973` (`orchestrate_clarify_respond`) all finished in `0–1s`. Unlike `.github/workflows/internal-plan.yml:12-16` and `.github/workflows/internal-implement.yml:12-17`, `.github/workflows/internal-clarify.yml:13-16` and `.github/workflows/internal-orchestrate-clarify-respond.yml:11-14` have no wrapper job-level `if`.
   - **Root cause:** `issue_comment` fan-out is creating large volumes of wrapper runs that are almost immediately skipped downstream.
   - **Exact change:** Mirror the downstream reusable-workflow gate at the wrapper level for `issue_comment` events in `internal-clarify.yml` and `internal-orchestrate-clarify-respond.yml`, while preserving the `issues.opened` path for clarify.
   - **Estimated time savings (inference):** small per run, but removes `~403` low-signal wrapper runs from the observed window and reduces queue noise.
   - **Implementation risk:** Low-medium. Gate only the comment-triggered path and keep issue-open behavior unchanged.

## Cost Optimizations

1. **Reduce reviewer breadth and disable two-pass review on `claude/*` comment-only paths.**
   - **Evidence:** `review_autofix` `claude/*` runs `25594520531` (`1654s`), `25595263155` (`1540s`), and `25595784150` (`1505s`) all skipped editor/commit/judge/auto-merge but still reported `REVIEWERS_SUCCESSFUL: 6`. The workflow declares `6` reviewer models in `.github/workflows/review_autofix.yml:92-98`, and `scripts/review_run_reviewers.sh:1060-1275` runs a two-pass architecture by default.
   - **Root cause:** The most expensive part of the path is still active on a branch mode whose only deliverable is a comment.
   - **Exact change:** In `CLAUDE_BRANCH_REVIEW` mode, set `ENABLE_REVIEWER_TWO_PASS=false` and narrow `REVIEWER_MODELS` to a smaller fixed set of high-signal models; optionally keep the cross-model summarizer but only once.
   - **Estimated savings (inference):** up to `~75%` fewer reviewer model invocations on that path (`6 models x 2 passes = up to 12 reviewer invocations` today versus `3 models x 1 pass = 3` in a slimmed profile), plus less summarizer/context overhead.
   - **Quality-risk notes:** Medium. Keep the full six-model/two-pass path for normal autofix PRs and expose a manual “full review” override.

2. **Make the existing pass-2 diff-size gate real instead of leaving it as a no-op.**
   - **Evidence:** `.github/workflows/review_autofix.yml:105-114` sets both `REVIEWER_PASS2_REASONING_SMALL` and `REVIEWER_PASS2_REASONING_LARGE` to `xhigh` by default. `scripts/review_run_reviewers.sh:1200-1264` explicitly says this makes the size gate a no-op unless operators override repo vars. Recent expensive `claude/*` runs were small enough to be plausible candidates for a cheaper pass-2 profile (`25595784150`: `2 files`, `58 additions`; `25594520531`: `2 files`, `96 additions`).
   - **Root cause:** The workflow has the right lever, but the default configuration spends the same reasoning budget on small and large diffs.
   - **Exact change:** Set `REVIEWER_PASS2_REASONING_SMALL=medium` for small diffs while keeping `REVIEWER_PASS2_REASONING_LARGE=xhigh`; for `CLAUDE_BRANCH_REVIEW`, disable pass 2 entirely.
   - **Estimated savings (inference):** moderate on the current slow-review tail because every small-diff pass-2 reviewer call becomes cheaper or disappears.
   - **Quality-risk notes:** Medium. Watch for reviewer recall on subtle issues; roll back by restoring `xhigh` if quality regresses.

3. **Stop paying for full `workflow_log_analysis` tails during smoke/release validation.**
   - **Evidence:** The sampled release-gate failure (`25589394934`) spent `9,874s` waiting on child analysis run `25589406932` that later cancelled; that child had already finished the core deep-audit report section before the cancellation.
   - **Root cause:** The release gate is paying for a full deep analysis pipeline even when it only needs a smoke-safety signal.
   - **Exact change:** Introduce a smoke mode for `.github/workflows/workflow-log-analysis.yml` and use it from `.github/workflows/test-and-mark-stable.yml:3320-3405`; dispatch the full audit asynchronously or on schedule instead of charging it to the release-critical path.
   - **Estimated savings:** removal of an entire `~2.7 hour` failed validation/rerun path in the worst sampled case.
   - **Quality-risk notes:** Low if the full analysis still runs and still comments/labels on failure; only the blocking semantics change.

4. **Repair the validate-hints cache so `validate` stops re-buying discovery work.**
   - **Evidence:** Runs `25596119024`, `25595170096`, and `25594130477` all failed to save the hints cache, and `scripts/validate_process.sh:1993-2009` documents that a warm cache should skip the Codex discovery call entirely.
   - **Root cause:** The semantic cache path never persists, so the workflow keeps paying the cold-start discovery cost.
   - **Exact change:** Ensure `.ai/validate-hints-cache` exists before `actions/cache@v5` save/restore and keep writing `hints.yml` back into that path on successful discovery.
   - **Estimated savings (inference):** one discovery call per validate run, plus lower latency.
   - **Quality-risk notes:** Low. Cache keys already hash repo-structure inputs, so invalidation safety is designed in.

5. **Fix prompt-cache observability before trying bigger cache-cost tuning.**
   - **Evidence:** Across `12` `review_autofix` cache-probe lines from slow runs `25572966637`, `25581923656`, `25583401583`, `25586809675`, `25587463930`, and `25587969927`, every line reported `cache_enabled=true` but all usage fields were `na` (`prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`). The probe also only checks the first reviewer model (`scripts/review_run_reviewers.sh:113-161` selects `head -n1`, which is `minimax/minimax-m2.5` in current config).
   - **Root cause:** The cache probe is fail-open and safe, but it is not producing usable hit/miss data and it observes only one of six reviewer models.
   - **Exact change:** Emit raw usage JSON from the real reviewer/summarizer calls, or extend `scripts/openrouter_prompt_cache.py:77-97` / `scripts/review_run_reviewers.sh:61-109` so the current Codex/OpenRouter usage envelope is captured. Probe at least one model per provider family if you keep the synthetic probe.
   - **Estimated savings:** unquantified in the current window; this is a prerequisite to reliable prompt-cache cost tuning.
   - **Quality-risk notes:** Low. This is measurement and instrumentation, not behavioral change.

**Model-selection note:** I do **not** recommend globally downgrading `MODEL_EDITOR=openai/gpt-5.4` in `implement` or `validate` yet. The evidence in this window is strongest for `review_autofix` reviewer breadth/reasoning changes; end-to-end token telemetry for implement/validate is not available.

## Reliability Improvements

1. **Prevent `workflow_log_analysis` child cancellation from failing release validation.**
   - **Failure evidence:** `shubhodeep1/coding-workflows` run `25589394934` (`Test & Mark Stable Release`) failed only at `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` after child run `25589406932` concluded `cancelled`.
   - **Root cause category:** Hard-coupled auxiliary analysis / timeout-sensitive downstream dependency.
   - **Exact fix:** Treat smoke-gate `workflow_log_analysis` as a reduced non-blocking dependency: require success from `collect-logs` + `analyze-commit-notify`, but let `deep-audit`/`api-redundancy` fail open into comments/labels/artifacts instead of failing the release test.
   - **Expected reliability impact:** removes the only sampled `test_and_mark_stable` family failure (`1/1` failure in the current window) and prevents an auxiliary analysis tail from blocking release validation.
   - **Rollback / fail-open:** If the reduced path proves too permissive, restore blocking only for manual full-release runs and keep smoke mode fail-open.

2. **Bound and batch the `api-redundancy` standalone PR scan so analysis runs stop cancelling late.**
   - **Failure evidence:** Child run `25589406932` was still inside `Standalone PR conflict sweep` when it was cancelled; its parent failed because of that cancellation.
   - **Root cause category:** Unbounded GH API fan-out / long-tail audit work.
   - **Exact fix:** In `scripts/orchestrate_poll_process.sh:11293-11390`, prefetch candidate PR metadata in batched GraphQL form, cache it cycle-locally, and add an analysis-mode cap so stale or low-value candidates do not keep the job alive indefinitely.
   - **Expected reliability impact:** reduces the risk of `workflow_log_analysis` cancellations and secondary-rate-limit stalls turning into user-visible parent failures.
   - **Rollback / fail-open:** If the batched prefetch fails, warn and skip the sweep rather than cancelling the whole analysis workflow.

3. **Expose nightly self-test fixture failures directly in stdout and the collector bundle.**
   - **Failure evidence:** `nightly_validation_selftest` run `25590300627` failed in `validation-selftest / Run validation self-test matrix` after reporting `fixtures=3 passed=1 failed=2`, but the collector bundle does not contain the names of the two failing fixtures. The workflow uploads artifacts and writes a step summary in `.github/workflows/nightly-validation-selftest.yml:61-115`, but the deep-dive logs do not surface those fixture names.
   - **Root cause category:** Diagnostics / observability gap.
   - **Exact fix:** Tee the computed `fixture_lines` from `.github/workflows/nightly-validation-selftest.yml:82-115` to stdout before exiting, and include the compact summary JSON in the collector’s `errors/` bundle so failures remain visible without downloading artifacts.
   - **Expected reliability impact:** reduces triage time and unnecessary reruns caused by opaque self-test failures; does not directly change the pass rate.
   - **Rollback / fail-open:** Additive logging only; no behavior change to the self-test itself.

4. **Make validate-hints cache creation deterministic to avoid persistent cold-start behavior.**
   - **Failure evidence:** Three recent `validate` runs all warned that the cache path did not exist, so no cache was saved.
   - **Root cause category:** Cache configuration / path lifecycle mismatch.
   - **Exact fix:** Pre-create `.ai/validate-hints-cache` before the cache step and keep the current fail-open path if the directory is empty.
   - **Expected reliability impact:** lower chance that validate runs depend on a fresh discovery call during transient LLM/API issues; also cuts repetitive warnings that obscure real failures.
   - **Rollback / fail-open:** Safe; if the cache is absent or invalid, `validate_process.sh` already falls back to discovery.

5. **Add retry/backoff to Copilot-review GH API calls if that workflow is editable.**
   - **Failure evidence:** `copilot_pull_request_reviewer` runs `25594521708` and `25595178408` both used `actions/github-script@v8` with `retries: 0` on API-heavy steps (`Prepare` and `Cleanup artifacts`).
   - **Root cause category:** Transient API flake exposure.
   - **Exact fix:** Add bounded retries/backoff around PR file pagination and artifact cleanup, or configure built-in retries where the managed workflow allows it.
   - **Expected reliability impact:** low-medium; mostly preventive, since sampled runs succeeded, but it reduces the risk of sporadic reruns from transient API errors.
   - **Rollback / fail-open:** If cleanup retries still fail, skip cleanup and rely on retention instead of failing the run.

## AI Memory Health

Telemetry exists in this window, but it is not emitted uniformly. I found `AI_MEMORY_TELEMETRY` in `implement`, `validate`, `review_autofix`, and `workflow_log_analysis` helper steps; recent evidence-grade summaries for `cancel_on_pr_close` run `25596460603` and `orchestrate_poll` run `25596091201` explicitly said telemetry was not present in the provided logs.

### Parsed telemetry summary

| Metric | Value | Notes |
|---|---:|---|
| Deep-dive run folders inspected | 27 | `errors/`, `slow/`, `recent/` |
| Total telemetry JSON lines parsed | 49 | Includes 8 `summarize_unselected_runs` helper events from `workflow_log_analysis` |
| Memory / processed-command ops | 41 | Excludes the 8 summarizer helper events |
| `record-run-event` ops | 17 | No high retry counts observed |
| `record-candidate` ops | 8 | All sampled writes succeeded |
| `retrieve` ops | 8 | Key health indicator |
| `processed-command-check` / `claim` / `complete` | 2 / 2 / 2 | All sampled ops succeeded |
| `finalize-task` ops | 2 | Both sampled ops succeeded |
| `promote` / `compact` ops | 0 / 0 | Not observed in this window |

### Retrieve effectiveness

- **Hit rate:** `2/8` (`25%`) retrieves selected at least one record.
- **Zero-record retrieves:** `6/8` (`75%`).
- **Average `estimated_tokens`:** `7.0` overall. The two hits were both `28`; the six misses were all `0`.
- **Budget comparison:** not possible from current telemetry, because no per-retrieve budget field was emitted.
- **`keyword_method` distribution:** `none=6`, `plain=2`, `llm=0`.
- **`enabled:false` retrieves:** `0`.
- **`fail_open:true` retrieves:** `0`.
- **High push retry counts on retrieves:** `0`; no retrieve event showed `push_attempts > 1`.

### Workflow-specific pattern

- **Every sampled `review_autofix` retrieve missed.** Runs `25572966637`, `25581923656`, `25583401583`, `25586809675`, `25587463930`, and `25587969927` all logged `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`.
- **Both sampled `implement` retrieves hit.** Runs `25571327387` and `25580421671` each logged `records_selected=1`, `estimated_tokens=28`, `keyword_method=plain`.

### Assessment

- **Healthy:** the memory system is enabled, fail-open behavior did not surface as an error path in sampled `retrieve` operations, and sampled write/processed-command operations completed cleanly.
- **Unhealthy:** reviewer retrieval is not adding value on the repo’s most expensive workflow family. The combination of `records_selected=0` and `keyword_method=none` across all six sampled `review_autofix` retrieves is the clearest current memory weakness.
- **Recommendation:** align reviewer retrieval inputs with the implementation path. Concretely, seed reviewer retrieval from PR title/body plus `LINKED_ISSUE_CONTEXT_FILE` and emit extra telemetry for why `keyword_method` fell back to `none`. That is partly an **inference**, but it is strongly suggested by the `implement` hit/miss contrast in this same window.

## GH API Call Audit

`CLAUDE.md §15` requires three things that matter here: reuse existing calls, prefer batched GraphQL for N-item fetches, and use cycle-local caches with fail-open fallback. The repo already follows that guidance in some places — for example, `.github/workflows/review_autofix.yml:235-240` extends the existing `/pulls/{n}` fetch to include labels and size metadata so small diffs avoid a `/files` call. The gaps below are the highest-value remaining targets.

1. **`test_and_mark_stable` watcher loop is too chatty for long-running child workflows.**
   - **Evidence:** In run `25589394934`, step `Dispatch & watch — workflow-log-analysis` printed `645` `status=` lines. That step’s code at `.github/workflows/test-and-mark-stable.yml:3379-3403` polls `repos/${TEST_REPO}/actions/runs/${NEW_ID}` every `15s`.
   - **Pattern:** high-volume repeated status lookups for a single run.
   - **Concrete change:** switch to adaptive polling in `.github/workflows/test-and-mark-stable.yml:3379-3403` — for example `15s` for the first `10m`, `30s` for the next `20m`, then `60s` afterward.
   - **Estimated call reduction (inference):** about `645` status GETs down to roughly `~213` on a `9794s` watch, or `~67%` fewer calls, with much lower rate-limit exposure.

2. **`review_autofix` linked-issue body-text fallback is still an unbatched per-item loop.**
   - **Evidence:** `.github/workflows/review_autofix.yml:1427-1488` falls back from GraphQL to body parsing, then loops over `_fallback_numbers` and calls `gh api repos/.../issues/${_fb_num}` once per issue, capped at `20`.
   - **Pattern:** initial GraphQL miss followed by up to `20` per-issue REST fetches.
   - **Concrete change:** replace the per-issue REST loop with a single aliased GraphQL follow-up that fetches title/body for all parsed issue numbers at once; store the result in the same lightweight cache file.
   - **Estimated call reduction:** up to `20` extra fallback calls down to `1` extra fallback call on that branch, while preserving current fail-open behavior.

3. **`orchestrate_poll_process.sh` standalone PR conflict sweep is an `N+1` API pattern.**
   - **Evidence:** `scripts/orchestrate_poll_process.sh:11310-11345` does one `gh pr list` and then one `gh api repos/.../pulls/${S_PR}` call per candidate PR.
   - **Pattern:** list once, then refetch each item to get mergeability.
   - **Concrete change:** use one batched GraphQL query for candidate PR metadata and mergeability, write it to a cycle-local JSON cache, and iterate locally.
   - **Estimated call reduction (inference):** from `1 + N` calls per sweep to roughly `ceil(N / batch_size)` GraphQL calls. With a batch size of `50`, a `100`-PR sweep would drop from `101` calls to about `2`.
   - **Rate-limit impact:** meaningful, because this sweep was still running when `workflow_log_analysis` child run `25589406932` was cancelled.

4. **Copilot review’s artifact cleanup is API-expensive relative to its value.**
   - **Evidence:** Runs `25595178408` and `25594521708` both flagged `gh api /repos/shubhodeep1/coding-workflows/actions/runs/<run_id>/artifacts` as a hotspot in `Cleanup artifacts`; both also paginated PR files in `Prepare`.
   - **Pattern:** artifact-list cleanup plus PR-file pagination with `retries: 0`.
   - **Concrete change:** delete only artifact IDs produced by the current run, not the whole artifact list; add bounded retry/backoff around pagination and cleanup if the workflow is editable.
   - **Estimated impact:** lower API variance and lower chance of cleanup dominating a `136–266s` workflow family.

5. **Positive pattern to preserve:** `review_autofix` gate already follows the repo rule.**
   - **Evidence:** `.github/workflows/review_autofix.yml:235-240` extends the existing PR fetch instead of adding a new call; `.github/workflows/review_autofix.yml:401-417` only falls back to `/files` when the cheap size gate is insufficient.
   - **Recommendation:** use that same “extend one call first” pattern in the fallback and PR-sweep code above.

## Prompt Cache & Memory System

1. **Prompt-cache observability is effectively broken on the highest-cost workflow family.**
   - **Evidence:** Across `12` observed `review_autofix_cache_probe` lines from slow runs `25572966637`, `25581923656`, `25583401583`, `25586809675`, `25587463930`, and `25587969927`, every line reported `cache_enabled=true` but every cache/token field was `na`.
   - **What this means:** The system is attempting to use prompt caching, but it is not producing usable creation/read metrics. Right now there is no trustworthy way to tell whether cache tuning is helping or hurting.
   - **Concrete improvement:** extend `scripts/openrouter_prompt_cache.py:77-97` and `scripts/review_run_reviewers.sh:61-109` to capture the current raw usage envelope from actual reviewer calls, not only the synthetic probe.
   - **Estimated impact:** medium on cost/latency tuning confidence; direct token savings are currently unquantifiable.

2. **The synthetic cache probe only covers one reviewer model, not the real panel.**
   - **Evidence:** `scripts/review_run_reviewers.sh:113-161` chooses the first non-empty reviewer model (`head -n1`), which is currently `minimax/minimax-m2.5`, then runs two probe calls only for that model.
   - **What this means:** even if the probe were working, it would not tell you whether the other five reviewer models or the summarizer are cacheable.
   - **Concrete improvement:** either instrument the actual reviewer invocations directly, or probe at least one model per provider family. Keep the probe fail-open (`run_cache_probe || true`) as it is today.
   - **Estimated impact:** medium observability improvement; low implementation risk.

3. **Likely cache-fragmentation driver: too much dynamic context is assembled near the front of reviewer prompts.** **(Inference)**
   - **Evidence:** `scripts/review_run_reviewers.sh:727-755` assembles reviewer prompts as `pre_assembled_static.txt` followed immediately by dynamic memory context, runtime hints, prompt-body text, and optional extra context. In two-pass mode, `scripts/review_run_reviewers.sh:1133-1161` adds a large cross-pollination summary before pass 2.
   - **Why it matters:** If the provider caches from the start of a single prompt/message, per-run memory and cross-pollination variance near the front of the prompt reduces the length of reusable cached prefix.
   - **Concrete improvement:** keep the longest stable instructions block contiguous at the top, and move highly variable memory/runtime/cross-pollination sections later in the prompt. For `CLAUDE_BRANCH_REVIEW`, the cleaner option is simply to skip pass 2.
   - **Estimated impact (inference):** moderate on `review_autofix`, because the current expensive path fans out six reviewers and sometimes two passes.

4. **Memory retrieval is effective for `implement` and ineffective for `review_autofix`; unify the good path.**
   - **Evidence:** `implement` runs `25571327387` and `25580421671` both retrieved `1` record with `keyword_method=plain`, while all six sampled `review_autofix` retrieves returned `0` records with `keyword_method=none`.
   - **Concrete improvement:** reuse the implementation-style keyword seeding in the reviewer path, and log additional telemetry such as `query_source`, `input_issue_number`, and an explicit retrieval budget so future analysis can compare `estimated_tokens` against budget instead of just raw estimate.
   - **Estimated impact:** moderate on review quality and prompt size; low reliability risk because the current memory path is already fail-open.

## Orchestrator Health

- **Overall health is better than the headline outliers suggest.** At repo level, `shubhodeep1/coding-workflows` had `1000` runs with only `2` failures (`0.2%`). `orchestrate_poll` was `25/25` successful, `validate` was `9/9` successful, and `ci` was `27/27` successful. The main problem is not systemic collapse; it is long-tail latency and noisy fan-out.

- **The biggest orchestrator pain point is event fan-out noise, not correctness.** `clarify` had only `10` successes in `216` runs, `plan` only `12` in `202`, `implement` only `13` in `202`, and `orchestrate_clarify_respond` only `4` in `201`. The wrappers are doing a lot of “wake up, check, skip” work. The smallest safe mitigation is wrapper-level gating for the two wrappers that currently lack it (`internal-clarify.yml`, `internal-orchestrate-clarify-respond.yml`).

- **Queueing is widespread enough to be an orchestration metric, not just runner noise.** I found runner-wait messages in `15` of `27` deep-dive runs, and in **all `7` inspected `review_autofix` deep-dive runs**. Because `.github/workflows/orchestrate_poll.yml:47-52` serializes one poller per repo (`cancel-in-progress: false`), every unnecessary long-running job directly increases freshness lag for the next useful cycle.

- **`orchestrate_poll` appears correct but has tail inefficiency.** Evidence-grade summary for run `25596091201` says work was effectively done around `08:06:14Z`, but cleanup only started around `08:07:59Z`, leaving a non-trivial end-of-cycle tail inside a `137s` run. Family stats also show a very wide spread (`p50=116s`, `p95=1181s`). I would treat that as a track-and-measure issue before changing behavior.

- **Conflict/recovery logic is extensive and safety-biased, which is good — but analysis workflows are now paying for some of that same machinery.** The late cancellation in `workflow_log_analysis` happened while running a standalone PR conflict sweep from `scripts/orchestrate_poll_process.sh`. The smallest safe mitigation is not to weaken orchestrator conflict handling; it is to stop calling the full sweep on smoke-gated analysis paths.

### Observable indicators teams should track weekly

1. **Runner-wait rate** by workflow family, especially `review_autofix`.
2. **`review_autofix` cancellation rate** (`16/63` in this window, `25.4%`).
3. **Skipped-wrapper rate** for `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`.
4. **Watcher poll count per dispatch step** — run `25589394934` already hit `645`.
5. **Validate cache-save success rate** — `0/3` on sampled recent validate deep dives.
6. **Memory retrieve hit rate** — currently `25%` overall, `0%` for sampled `review_autofix`.
7. **Cleanup share of wall time** for `orchestrate_poll` and Copilot review workflows.

## Pipeline Flow Bottlenecks

| Pipeline stage | Dominant bottleneck type | Evidence | End-to-end impact | Recommended fix |
|---|---|---|---|---|
| Clarify / respond wrappers | Queueing / no-op fan-out | `clarify` `206/216` skipped; `orchestrate_clarify_respond` `197/201` skipped; recent runs are `0–1s` skips | Low per run, but constant noise | Add wrapper-level `if` gates for comment-triggered paths |
| Plan | Data gap for active-path tuning | Family totals are dominated by skips (`190/202` other/skipped); no deep-dive active plan logs in this bundle | Unknown | First add better active-plan duration/token emission before changing logic |
| Implement | Compute-heavy when active | Slow runs `25571327387` (`2232s`) and `25580421671` (`2480s`) | Medium, but lower frequency than review | Defer major tuning until review path is cheaper; memory retrieval is already helping here |
| Review / autofix | Compute + queueing | `review_autofix` p95 `2335.6s`; all `7/7` deep-dive review runs had runner wait; `claude/*` comment-only runs still took `1505–1654s` | Highest | Slim `claude/*` reviewer profile and make diff-size reasoning gate real |
| Validate | Repeated cold-start discovery | Runs `25596119024`, `25595170096`, `25594130477` all failed to save hints cache | Medium | Fix validate-hints cache path creation |
| Orchestrate poll / analysis tails | Retry/poll/API fan-out | `orchestrate_poll` p95 `1181s`; release watcher emitted `645` polls in `25589394934`; `workflow_log_analysis` child `25589406932` spent its last `~90m` in `api-redundancy` | High on freshness and release-critical paths | Add smoke mode, adaptive polling, and batched PR sweeps |
| Merge / conflict overhead | API-heavy recovery scans | `scripts/orchestrate_poll_process.sh:11293-11390` does PR conflict sweep via `N+1` calls; Copilot cleanup is artifact-heavy | Medium | Batch GraphQL fetches and make cleanup targeted/fail-open |

### Ordering by end-to-end impact

1. **Release-gate analysis tail** (`test_and_mark_stable` → `workflow_log_analysis`)  
2. **`review_autofix` compute depth on actual review runs**  
3. **Queueing amplified by long-running review jobs**  
4. **Validate cold-start rediscovery because cache never saves**  
5. **Wrapper fan-out noise in clarify/respond**

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` is the largest active bottleneck: `63` runs, p95 `2335.6s`, `16` cancelled; long claude-branch comment-only runs still take `1505–1654s`.
- Release validation is exposed to a heavy downstream analysis tail: `test_and_mark_stable` run `25589394934` failed only because child `workflow_log_analysis` run `25589406932` cancelled after `9794s`.
- `validate` is paying cold-start cost repeatedly because the hints cache is not being saved.

**Top failure modes**
- Smoke/release gate failure caused by a cancelled child analysis workflow, not by core release logic.
- Nightly validation self-test fails opaquely (`25590300627`: `3` fixtures, `2` failures) because failing fixture names are not visible in the collected error bundle.
- Long analysis/review tails increase cancellation exposure and runner queue amplification.

**Highest-cost drivers**
- Six-model, two-pass reviewer architecture in `review_autofix`.
- Full `workflow_log_analysis` deep-audit/api-redundancy chain being paid on release/smoke paths.
- Repeated validate discovery due to unsaved hints cache.
- Blind prompt-cache observability on `review_autofix` (`12/12` probe lines had `na` usage fields).

**Top 3 prioritized actions**
1. **Introduce smoke-mode / reduced-mode `workflow_log_analysis` for `test-and-mark-stable`.**
2. **Create a branch-specific slim review profile for `CLAUDE_BRANCH_REVIEW` and activate the existing small-diff reasoning gate.**
3. **Fix validate-hints cache creation and improve reviewer memory/cache telemetry so the next round of tuning is evidence-backed.**

## Metrics Appendix

### Repo summary

| Repo | Total runs | Success | Failure | Cancelled | Other/skipped | Success rate | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 195 | 2 | 19 | 784 | 19.5% | 0.2% | 110.377 | 1.0 | 645.0 |

### Workflow-family summary

| Workflow family | Runs | Success | Failure | Cancelled | Other/skipped | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| `review_autofix` | 63 | 43 | 0 | 16 | 4 | 48.0 | 2335.6 |
| `ci` | 27 | 27 | 0 | 0 | 0 | 643.0 | 674.6 |
| `orchestrate_poll` | 25 | 25 | 0 | 0 | 0 | 116.0 | 1181.0 |
| `validate` | 9 | 9 | 0 | 0 | 0 | 173.0 | 183.2 |
| `copilot_pull_request_reviewer` | 21 | 21 | 0 | 0 | 0 | 156.0 | 291.0 |
| `clarify` | 216 | 10 | 0 | 0 | 206 | 1.0 | 7.25 |
| `plan` | 202 | 12 | 0 | 0 | 190 | 1.0 | 16.0 |
| `implement` | 202 | 13 | 0 | 2 | 187 | 1.0 | 237.4 |
| `orchestrate_clarify_respond` | 201 | 4 | 0 | 0 | 197 | 1.0 | 2.0 |
| `test_and_mark_stable` | 1 | 0 | 1 | 0 | 0 | 9874.0 | 9874.0 |
| `workflow_log_analysis` | 1 | 0 | 0 | 1 | 0 | 9794.0 | 9794.0 |
| `nightly_validation_selftest` | 1 | 0 | 1 | 0 | 0 | 83.0 | 83.0 |
| `cancel_on_pr_close` | 14 | 14 | 0 | 0 | 0 | 10.0 | 16.35 |
| `forward_merge_stable_to_main` | 2 | 2 | 0 | 0 | 0 | 21.5 | 25.55 |
| `issue_pr_status` | 13 | 13 | 0 | 0 | 0 | 61.0 | 79.2 |
| `orchestrate` | 1 | 1 | 0 | 0 | 0 | 153.0 | 153.0 |
| `validation_refresh` | 1 | 1 | 0 | 0 | 0 | 213.0 | 213.0 |

### AI memory telemetry

| Metric | Value |
|---|---:|
| Parsed telemetry lines | 49 |
| Memory / processed-command lines | 41 |
| `retrieve` ops | 8 |
| Retrieve hit rate | 25.0% |
| Zero-record retrieves | 6 |
| Avg `estimated_tokens` | 7.0 |
| `keyword_method=none` | 6 |
| `keyword_method=plain` | 2 |
| `keyword_method=llm` | 0 |
| `enabled:false` retrieves | 0 |
| `fail_open:true` retrieves | 0 |
| Retrieve events with `push_attempts > 1` | 0 |
| `promote` / `compact` observed | 0 / 0 |

### Available token and cache telemetry

| Metric | Value | Coverage / caveat |
|---|---:|---|
| `log_summary_meta.tokens_used` total | 142,302 | Collector-side unselected-run summarization only; `76` summarized run rows |
| `log_summary_meta.tokens_used` average | 1,872.39 | Same collector-side coverage |
| End-to-end workflow runtime token totals | Unavailable | Most deep-dive logs expose model names / env, not aggregate runtime tokens |
| `review_autofix` cache-probe lines with usable token/cache counters | 0 / 12 | All `12` observed lines had `na` for prompt/completion/total/cache-read/cache-write tokens |
| `review_autofix` cache-probe runs sampled | 6 | Runs `25572966637`, `25581923656`, `25583401583`, `25586809675`, `25587463930`, `25587969927` |
| Recent validate runs with cache-save warning | 3 / 3 sampled | Runs `25596119024`, `25595170096`, `25594130477` |

### GH API hotspot summary

| Workflow / step | Evidence | Observed or structural call volume | Main risk |
|---|---|---|---|
| `test_and_mark_stable` → `Dispatch & watch — workflow-log-analysis` | Run `25589394934`; `.github/workflows/test-and-mark-stable.yml:3379-3403` | `645` run-status polls observed in one step | Shared rate-limit pressure; long watcher tails |
| `review_autofix` linked-issue fallback | `.github/workflows/review_autofix.yml:1427-1488` | Up to `20` extra REST calls after the initial GraphQL miss | N+1 fallback branch violating `CLAUDE.md §15` intent |
| `orchestrate_poll_process.sh` standalone PR conflict sweep | `scripts/orchestrate_poll_process.sh:11310-11345`; child run `25589406932` cancelled while in this sweep | `1 + N` calls per sweep | Long-tail analysis/orchestrator runtime and secondary-limit exposure |
| `copilot_pull_request_reviewer` `Cleanup artifacts` | Runs `25595178408`, `25594521708` | Exact call count unavailable; artifact-list/delete hotspot dominates `116–266s` runs | Cleanup overhead larger than useful work |
| `review_autofix` gate PR fetch | `.github/workflows/review_autofix.yml:235-240` | Positive example: one extended call instead of extra size/label calls | Keep and reuse this pattern elsewhere |

