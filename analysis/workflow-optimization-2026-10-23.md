## Executive Summary

**Data caveat:** The run-logs directory (`/home/runner/work/_temp/workflow-log-output`) is not mounted in this sandbox, so I could not read any `errors/`, `slow/` or `recent/` logs. The evidence below comes from `analysis_context.json` (aggregates plus each run's `log_summary`) and from reading the repository source. The window is 1,000 runs, from about 2026-10-22 11:23Z to 2026-10-23 00:37Z. 616 of the 1,000 runs (61.6%) are skipped or "other", so the window covers only about 13 hours. `insufficient_data=false`.

- **None of the diagnostics proposed last cycle have landed, so the same three failures are still undiagnosed.** I searched for `PROMOTE_STABLE_FF`, `ORCHESTRATE_POLL_CYCLE_SUMMARY`, `GH_PAT_BUDGET_DETAIL`, `queue_seconds` and `CANCEL_UNLINKED_QUEUED` outside `analysis/` and found 0 matches.
  - Promote run 39774429376 still fails at `promote / Fast-forward stable to main`. The `git merge --ff-only` and `git push` exits at `.github/workflows/promote-main-to-stable.yml:271` and `:277` still have no annotation.
  - Impact: the release path stays undiagnosable. Confidence: high.
- **The run summaries throw away the telemetry they are asked to keep. This is the root cause of the "no AI_MEMORY_TELEMETRY / no poll visibility" gap.**
  - `scripts/summarize_unselected_runs.py:63-64` keeps only the first 1,000 and last 4,000 characters of each step (`DEFAULT_PER_STEP_HEAD_CHARS` / `DEFAULT_PER_STEP_TAIL_CHARS`).
  - Its prompt (line 102) asks the summarizer to keep `AI_MEMORY_TELEMETRY:` lines verbatim, but those lines are cut before the summarizer ever sees them.
  - All 19 poll summaries in this window report 852k–863k characters truncated.
  - Fix: pull lines with contractual prefixes out of the full text before truncating (Reliability item 2). Confidence: high.
- **The prompt-cache hit rate improved to 0.800, from 0.672 last window. `test_and_mark_stable` reads nothing from cache for the second release in a row.**
  - Run 39833545350 made 12 calls, wrote 45,099 cache tokens and read 0 (hit rate 0.0).
  - Inference: 4,640s across 12 calls is about 387s between calls, which is longer than the 5-minute cache lifetime, so every write expires unused.
  - Impact: small in dollars (about 45k tokens per release), but it is pure waste. Confidence: medium.
- **A push to `main` fans out at once and fills the hosted runners.**
  - At 20:00:22–24, CI 39845036834, promote-follow 39845036886, sweep 39845070743, historical triage 39845036727 and the PR 6576 follow-up 39845036019 all logged "All GitHub-hosted runners … are busy".
  - CI jobs waited 82–164s. The two single-use workflows spent 74s and 29s waiting just to no-op: `skip reason=already_filed issue=7300` and `skip reason=marker_present issue=7072`.
  - Poll runs separately waited up to 5m19s (39861355190) and about 4.5 min (39875140410).
  - Confidence: medium.
- **Review/autofix accounts for 99.88% of logged model tokens: 43.62M of 43.67M.**
  - Run 39874461125 alone used 28.4M tokens over 20 calls.
  - Its input per call (uncached prompt + cache read + cache write) was about 1.41M, against about 0.66M per call in 39800256892 and 39809581147.
  - Per-run phase data (`or_phases`) is computed by `cost_audit.py` but never exported to the run rows, so I cannot attribute the growth to a phase. Confidence: medium.
- **There is a fail-open overlay warning with no detail.** CI run 39844690617 logged `WORKFLOW_OVERLAY_SOURCE mode=trusted outcome=disabled reason=git_timeout`. `scripts/load_workflow_overlay.py:428-432` throws away which git operation hit the 60s timeout. This is one occurrence, low impact, and a cheap fix. Confidence: high.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Remove the two single-use workflows that still fire on every push to `main`. Critical path during bursts.**
   - **Evidence:**
     - Run 39845036727 (historical triage, PR #6611) took 81s, about 74s of it runner wait, and logged `HISTORICAL_TRIAGE skip reason=already_filed issue=7300`.
     - Run 39845036019 (PR 6576 follow-up) took 55s, with about 29s of runner wait for `locate`, and logged `PR6576_FOLLOWUP outcome=skip reason=marker_present issue=7072` and `32 passed in 2.85s`.
     - Both hit the 20:00:24 burst, the same moment CI 39845036834 jobs queued for up to 164s.
   - **Root cause:** One-shot workflows registered in `docs/scripts-pending-removal.md` (entries at lines 212 and 320) that have finished their job but have not been removed.
   - **Change:** Run the preflight checks listed in the registry: line 219 for `check-failure-triage-historical.yml`, lines 326–328 for `pr6576-shard-followup.yml`. If they pass, delete the workflow, script and test files and their registry entries in one PR (§20.F).
     - The PR 6576 trigger looks met: the marker is present on #7072.
     - The historical-triage trigger also needs issue #7300 to be closed, which I could not check here.
   - **Saving:** 3 fewer jobs per push to `main` (pr6576 has 2 jobs, historical has 1), and about 30–75s less queue pressure on the CI jobs that start in the same second.
   - **Risk:** Low. The registry already defines the safety checks.
2. **Measure queue time per job. This is diagnostic, but it is a prerequisite for any concurrency tuning.**
   - **Evidence:** Waits were found only in free-text summaries:
     - Poll: 5m19s (39861355190), about 4.5 min (39875140410), 2m43s (39860321537), 2m24s (39874278296), 2m10s (39852971636).
     - CI: 94s (39874460828) and 98s (39874477153).
   - **Change:** In `scripts/collect_workflow_logs.py`, from the `jobs` payload `extract_failure_point()` already reads (line 1045), add an additive per-run field: `queue_seconds_max` and `queue_seconds_by_job` (`started_at - created_at`). This adds no new API call (§14).
   - **Saving:** Indirect. The next report can rank contention sources instead of sampling them from summaries.
   - **Risk:** Low.
3. **Move `actions/cache/restore@v4` off Node 20. Micro, but it removes a recurring warning.**
   - **Evidence:** Every poll summary (for example 39872185748, 39870838733, 39868648847) warns `Node.js 20 is deprecated … actions/cache/restore@v4`.
   - There are 7 uses: `orchestrate_poll.yml:1243,1688,1701,2103`, `review_autofix.yml:2745`, `clarify.yml:1187` and `orchestrate_clarify_respond.yml:765`.
   - **Change:** Pin to the action's Node 24 major once its release notes confirm one exists. I could not verify that here, since web access is unavailable.
   - **Risk:** Low. Warning removal only, with no expected latency change.

## Cost Optimizations

1. **Stop the 0%-read cache writes in `test_and_mark_stable`.**
   - **Evidence:** In run 39833545350, `or_calls=12`, `or_prompt_tokens=49,132`, `or_cache_write_tokens=45,099` and `or_cache_read_tokens=0`. The previous release, 39768897865, showed the same pattern (91,618 written, 0 read). 92% of the prompt is written to cache every call, so the prefix is probably stable across calls.
   - **Root cause (inference):** The calls are about 387s apart (4,640s / 12), longer than the default 5-minute cache lifetime, so each write expires before the next call.
   - **Change:** Set `cache_enabled=false` for this phase. The OpenRouter usage line already records `cache_enabled=` and `cache_breakpoint_enabled=` (`scripts/cost_audit.py:107-108`), so the result can be confirmed afterwards.
   - **Saving:** About 45–92k tokens per release, billed at the base rate instead of the cache-write premium.
   - **Quality risk:** None.
2. **Find out why review run 39874461125 doubled its input per call.**
   - **Evidence:** 20 calls, 3,149,038 uncached prompt tokens, 24,044,826 cache reads and 1,015,606 cache writes, so about 1.41M input per call. The comparison runs:
     - 39800256892: 11 calls, about 657k per call.
     - 39809581147: 11 calls, about 710k per call.
     - The run took 2,137s with `AUTOFIX_EDITS_PUSHED: true`.
   - **Root cause:** Unknown. The almost doubled call count suggests a second review pass after autofix (inference).
   - **Change (diagnostic):** Export the `or_phases` map that `cost_audit.py` already aggregates (lines 697 and 957) into each run's `cost_telemetry` as an additive field (§10). The next report can then attribute tokens per phase.
   - **Saving:** If the second pass re-sends the full first-pass context, trimming it to the delta could save about 10M+ cache-read tokens per affected run (inference).
3. **Semble is adding value. Keep it.**
   - **Evidence:** 4 queries returned 60,803 bytes (about 15.2 KB each) from 30 sources (about 7.5 each), with `static_dup_bytes_total=0` and 0 runtime fallbacks.
   - Bootstraps averaged 46.7s (186,753 ms over 4).
   - Run 39874461125 bootstrapped twice in one run (99,358 ms). Inference: the index is rebuilt for a second phase.
   - **Change:** Reuse the first bootstrap's index within the same job when `SEMBLE_INDEX_PATH` already exists.
   - **Saving:** About 50s per two-pass review.
   - **Serena:** disabled (`SERENA_ENABLED: false` in 39874461125), with 0 queries and 0 probes, so there is nothing to assess.
4. **Reasoning level and model:** `MODEL_REASONING_EFFORT_JUDGE: high` and `MODEL_EDITOR: openai/gpt-6-sol`. The poller, implement, plan and clarify families still show 0 `or_*`, 0 `codex_*` and no `claude_*` fields in their run rows, so non-review spend is unmeasured. This gap is carried over from last cycle. No model change is recommended until it is closed.

## Reliability Improvements

Ranked by expected reduction in failure or rerun rate.

1. **Add an outcome line to the promote fast-forward step (carried over, still unlanded).**
   - **Failure evidence:** Run 39774429376, step `promote / Fast-forward stable to main`, failed after 158s. The `promote_main_to_stable` failure rate is 1/1.
   - **Root cause category:** Observability gap. Under `set -euo pipefail`, lines 271 (`git merge --ff-only "${TARGET}"`) and 277 (`git push origin "HEAD:refs/heads/stable"`) exit with no `::error::`, while the 9 other exits in the step are annotated.
   - **Fix:** Wrap both commands. The prefix `PROMOTE_STABLE_FF` has 0 matches; register it with an `agents.d/<pr>-promote-stable-ff.md` fragment under `Stable log prefixes (contractual)` (§25).
     ```bash
     if ! git merge --ff-only "${TARGET}" 2>/tmp/_ff_err; then
       echo "PROMOTE_STABLE_FF outcome=fail reason=merge_ff_only before=${BEFORE} target=${TARGET}"
       echo "::error::git merge --ff-only ${TARGET} failed: $(tail -n 3 /tmp/_ff_err | tr '\n' ' ')"
       exit 1
     fi
     if ! git push origin "HEAD:refs/heads/stable" 2>/tmp/_push_err; then
       echo "PROMOTE_STABLE_FF outcome=fail reason=push before=${BEFORE} target=${TARGET}"
       echo "::error::push to refs/heads/stable failed: $(tail -n 3 /tmp/_push_err | tr '\n' ' ')"
       exit 1
     fi
     ```
   - **Impact:** The next failure becomes classifiable as a race, ruleset rejection or auth problem from one line.
   - **Rollback:** Logging only. Exit codes are unchanged.
2. **Keep contractual telemetry lines through summary truncation.**
   - **Failure evidence:** 19 of 19 poll summaries truncated 852,912–862,786 characters. Every one says AI-memory, token and GH API signals "could not be assessed". The review summary for 39874461125 truncated 1,599,225 characters.
   - **Root cause:** `build_summary_input()` (`scripts/summarize_unselected_runs.py:309-363`) truncates each step to its head and tail before the model sees it.
   - **Fix:** Before the per-step truncation, scan the full `content` for prefixed lines and prepend one bounded block. This is additive, and it fails open because a scan error should skip the block.
     ```python
     _SIGNAL_PREFIX_RE = re.compile(
     	r"(AI_MEMORY_TELEMETRY:|BREAK_GLASS:|CONTEXT_BUDGET_WARN:|GH_PAT_BUDGET |GH_API_RETRY |"
     	r"SEMBLE_(QUERY|FALLBACK|BOOTSTRAP)|SERENA_(QUERY|FALLBACK|PROBE)|INFO:\s*openrouter\s+usage|::error::|::warning::)"
     )
     SIGNAL_BLOCK_MAX_LINES = 60  # bounded; per-prefix cap of 10 keeps one noisy family from crowding out others
     ```
     The block goes in front as `=== PRESERVED SIGNAL LINES (pre-truncation) ===` and counts against `char_cap`. Define `SIGNAL_BLOCK_MAX_LINES` with a default, per §8.
   - **Impact:** This closes the AI Memory Health and poller GH-API data gaps for about 100 summarized runs per window.
   - **Risk:** Low. The prompt input is still capped.
3. **Record which git operation timed out in the overlay loader.**
   - **Failure evidence:** One occurrence: CI 39844690617 `static-checks` logged `::warning::WORKFLOW_OVERLAY_SOURCE mode=trusted outcome=disabled reason=git_timeout`. The run still succeeded, so this is healthy fail-open behaviour, but the cause is opaque.
   - **Root cause:** `_trusted_git()` (`scripts/load_workflow_overlay.py:425-432`) converts `TimeoutExpired` to a bare `"git_timeout"`. The message cannot change, because line 577 matches it against a set of known reasons.
   - **Fix:** Attach an attribute instead of changing the message, and append it to the existing warning line at line 583. That is an additive trailing field. The existing test at `tests/test_workflow_overlay_core.py:444` only checks a prefix substring.
     ```python
     except subprocess.TimeoutExpired as exc:
     	err = WorkflowOverlayLoadError("git_timeout")
     	op = next((a for a in args if not a.startswith("-")), "unknown")
     	err.git_op = op if op in {"ls-remote", "check-ref-format", "init", "remote", "fetch", "rev-parse", "ls-tree", "show"} else "other"
     	raise err from exc
     # line 583:
     print(f"::warning::WORKFLOW_OVERLAY_SOURCE mode=trusted outcome=disabled reason={exc} git_op={getattr(exc, 'git_op', 'n/a')}", file=sys.stderr)
     ```
     The allowlist ensures the remote URL is never logged.
   - **Impact:** Separates a slow `ls-remote` (a GitHub-side blip) from a slow `fetch` (repo size or network).
4. **Classify cancelled runs that never created a job.**
   - **Evidence:** Review 39874477338, for the second head pushed to PR #7901 at 23:51:20, was "cancelled" after 1,987s with `failure_point` null and 0 model calls.
   - Sweep 39875514837 logged `AUTOFIX_SWEEP_SKIP pr=#7901 reason=active_run workflow=internal-review.yml count=2`.
   - `internal-review.yml:49-51` uses `cancel-in-progress: false`.
   - Inference: the run sat pending in the concurrency group and was replaced by the autofix push at 00:24:26, so no compute was wasted. It still inflates review p95 (2,761s) and the cancel counts.
   - **Fix:** In `_extract_cancelled_point()` (`collect_workflow_logs.py:1067-1086`), return `step_name="superseded_while_pending"` when `jobs` is empty. This is a new value for an existing field, so it is additive.
5. **Healthy signals that need no action:**
   - `BREAK_GLASS` and `CONTEXT_BUDGET_WARN`: 0 in all 111 runs with telemetry, so there is no rubric or prompt-size pressure.
   - Semble fallbacks: 44, all `contract_test` (CI 40, `test_and_mark_stable` 4), with `semble_runtime_fallbacks=0`. These are expected test-fixture lines. CI went from 4 per run (39844690617, 39845036834) to 8 per run (39874460828, 39874477153, 39877925950). Inference: the PR #7901 head added contract cases. Not a masked rollout.
   - Serena probes: ok, failed and skipped are all 0.
   - Stale unlinked queued runs 34748435167, 34748435180 and 32215723558 are still preserved in every scheduled cleanup (39875774595, 39874530816). This is carried over from last cycle and is noise only.

## AI Memory Health

- No `AI_MEMORY_TELEMETRY:` lines appear anywhere in this window's evidence. Every summary that mentions them reports them absent or truncated, and the deep-dive logs are not mounted.
- Emitters do exist in the source: `scripts/ai_memory.py`, `scripts/memory_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/review_rb_judge.sh`, `scripts/review_consolidate.sh` and `scripts/review_apply_fixes.sh`. The absence is therefore most likely a collection gap, not an emission gap. This is an inference.
- None of these could be assessed:
  - retrieve hit rate (`records_selected > 0`)
  - `estimated_tokens` against budget
  - the `keyword_method` mix (`llm` / `plain` / `none`)
  - `fail_open: true` entries and `enabled: false` entries
  - push retry counts
- Collection steps:
  1. Reliability item 2: preserve the signal lines through summary truncation.
  2. Have `collect_workflow_logs.py` count `AI_MEMORY_TELEMETRY` lines per `op` from the full archive into `cost_telemetry`, as it already does for `SEMBLE_*`.
  3. `memory_maintenance` ran twice (686s on average, e.g. 39840141173 at 793s) with no telemetry captured. Its log is a direct place to confirm `compact` and `promote` are emitted.

## GH API Call Audit

| Signal | Evidence | Assessment |
|---|---|---|
| Lowest PAT headroom | `remaining=2749` in sweep 39845070743 at about 20:00Z, during the push-to-main burst | Down from 3,859 last window. Inference: about 2.25k calls in that hourly window. No 429s. |
| Sweep cost | `used_in_job` = 2, 2, 2, 2, 3, 6, 7 (39868920075, 39865655875, 39861747919, 39850009515, 39845070743, 39872425980, 39853728172) | Efficient |
| `used_in_job=unknown` | 39875514837 (sweep), 39874461125 (codex-agent, `remaining=4648`) | Rollover cases; still unmeasured |
| Expected 404 | `GH_API_RETRY outcome=permanent … status=404 … tags/promote-cycle-frozen` in 39871624084, 39864725396, 39857068566, 39848854410 and 39845036886 | 1 call per run, as designed (`promote_candidate_follow.sh:168-175`). Logged as "permanent" noise. |
| Readiness and lint | 39877926092, 39874477212, 39874460814 made no visible API calls | Fine |
| Poller | Not visible: 852k+ characters truncated per run | Largest unknown |

- **Hot spot to watch:** the 20:00Z dip lines up with a push to `main` (forward-merge 39844690640, `forward-merged 1 commit(s) directly to main`) that fanned out to 6 or more workflows. Removing the two single-use workflows (Speed item 1) also removes their API calls. Historical triage alone made 2 `gh api` calls per run.
- **`unknown` usage (carried over):**
  - `scripts/gh_helpers.sh:68-71` computes `used` only when `reset` matches the start snapshot.
  - Add a separate line, `GH_PAT_BUDGET_DETAIL used_reason=window_rollover|no_start_snapshot start_remaining=… start_reset=…`.
  - Do not append to `GH_PAT_BUDGET`, because the collector anchors on `used_in_job=…$`.
  - This is a new prefix and needs an `agents.d/` fragment.
- **Poller:** No batching recommendations are possible until the poll-cycle summary exists. The existing `_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql` caches cannot be checked for regressions without a `gh_calls_used` field per cycle (Orchestrator Health).

## Prompt Cache & Memory System

- **`cache_hit_rate`:** 0.799985 for the repo and 0.801728 for `review_autofix`. Cache reads were 34,673,826 tokens, writes 2,041,029 and uncached prompt 6,628,266. The figure matches reads / (reads + prompt + writes).
- **Per review run:**
  - 39874461125: 0.852
  - 39809581147: 0.709
  - 39800256892: 0.704
  - The best run is also the largest. Inference: the second pass reused the first pass's prefix well. That suggests prefix stability is fine and the 0.70 runs pay mostly for first-pass per-PR context.
- **Write amortization:** 2.04M tokens written bought 34.67M read, about 17:1 (up from about 9:1 last window). Healthy.
- **Fragmentation causes:**
  - No unstable-prefix evidence this window.
  - The only fragmentation is the time-based expiry in `test_and_mark_stable` (Cost item 1).
- **Context budget:** `context_budget_warn_count=0`. The 1.41M input per call in 39874461125 did not trigger a warning. If an OpenRouter "call" is a multi-turn session (unverified), the warning's `prompt_tokens/model_context_window` check is applied per request, and a growing session would not trip it. Exporting `or_phases` (Cost item 2) would confirm this.
- **Fail-open:** `or_usage_unavailable_calls=0` across 54 calls. The npm cache hit in 39874461125 (`Cache hit occurred on the primary key claude-cli-npm-Linux-…`).
- **Memory retrieval:** unmeasurable (see AI Memory Health).

## Orchestrator Health

- **Poller:**
  - 70 runs: 67 successful, 3 cancelled (down from 9 of 60 last window). p50 643.5s, p95 961.3s.
  - Sampled start-to-start gaps are 8.2–19.6 minutes against run lengths of 420–964s, so the poller holds a runner most of the time (inference).
  - Queue waits before the poll started ranged from 12s to 5m19s (see Speed item 2).
- **Configuration observations** (from 39875140410, 39874278296, 39873315822):
  - `SEMBLE_ENABLED: true` with `SEMBLE_AVAILABLE: false` and an empty `SEMBLE_BIN`. There are no `SEMBLE_FALLBACK` lines, so this is consistent with lazy mode never needing Semble.
  - Recommendation: set `SEMBLE_ENABLED=false` for the poller only if a reader confirms the poller never queries Semble. Low priority.
- **Biggest pain point: observability.** There is still no per-cycle summary line (0 matches for `ORCHESTRATE_POLL_CYCLE_SUMMARY`). Add one fail-open final line in `scripts/orchestrate_poll_process.sh`, mirrored to `$GITHUB_STEP_SUMMARY`, and register it through an `agents.d/` fragment:
  ```bash
  echo "ORCHESTRATE_POLL_CYCLE_SUMMARY tracking_examined=${n_tracking:-0} dispatched=${n_dispatched:-0} deferred=${n_deferred:-0} stalled=${n_stalled:-0} waves_advanced=${n_waves:-0} clarify_pending=${n_clarify:-0} gh_calls_used=${gh_used:-unknown} elapsed_s=${SECONDS}"
  ```
  With Reliability item 2 in place, this line survives summary truncation even without the step-summary mirror.
- **Promote cycle:**
  - The follower advanced `promote-candidate` once (39845036886, `outcome=advanced from=27ff42e7… to=e5537d8f…`) and otherwise reported `outcome=current`.
  - All poll, cancel and review runs since then resolve `SCRIPT_REF e5537d8f92c4…`, so the pin is propagating consistently.
  - The promote-to-stable failure (39774429376) is still unexplained (Reliability item 1).
- **Indicators to track:** cancelled polls per window (3, down from 9), poll `elapsed_s` p95, queue seconds before the poll starts, `dispatched` and `stalled` per cycle (once the line exists), and `PROMOTE_CANDIDATE_FOLLOW outcome=` counts.
- **Fan-out of skipped runs:**
  - 610 skipped or "other" runs across clarify (121), plan (111), implement (106), orchestrate_clarify_respond (116), workflow_failure_heal (75), heal intake (46) and check triage (35).
  - Some long-lived skipped runs were probably pending in concurrency groups (inference): 39820931391 at 4,688s, 39797226940 at 3,977s, 39834597819 at 2,813s.
  - The main cost is that they take up 61.6% of the collector's 1,000-run window.

## Pipeline Flow Bottlenecks

| Stage | Compute | Queue | Retry/rework | Merge/conflict |
|---|---|---|---|---|
| clarify/plan | Real runs 798–1,368s (39834771040, 39796710495) | Skipped runs pending up to 1,651s (39838510964) | none seen | — |
| implement | 1,074–1,388s (39787860251, 39798273748) | Skipped run pending 4,688s (39820931391) | none seen | — |
| review/autofix | p50 262s, p95 2,761s; `codex-agent` about 35 min (39874461125) | Second head of PR #7901 pending 33 min, then superseded (39874477338) | 2 passes, 20 calls in 39874461125 | Autofix push at 00:24 restarted CI (39877925950) |
| CI | p50 867s, p95 1,228s; `tests-heal-plan-and-validation` about 12 min (39877925950) | 82–164s per job during the 20:00 burst (39845036834) | 2 failures, fixed last window | Two pushes 10s apart ran full CI twice (39874460828, 39874477153) |
| poll | about 7–12 min per cycle | up to 5m19s | — | — |
| release | `test_and_mark_stable` 4,640s; `validation_refresh` 2,525s | — | promote 1/1 failed | — |

Fixes in order of end-to-end impact:
1. Promote fast-forward diagnostics. The release path is blocked on an unknown cause.
2. Summary signal preservation and the poll-cycle summary, which unlock poller, memory and API visibility.
3. Remove the two single-use workflows to cut push-burst contention.
4. Per-job `queue_seconds` in the collector, to decide whether CI `concurrency` or runner limits are worth changing. CI `cancel-in-progress` stays deferred because of the `ci_cancelled_rerun.py` interaction noted last cycle.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:**
  - About 35-minute review sessions (39874461125).
  - About 13–14 minutes of CI fan-out, set by `tests-heal-plan-and-validation` and `tests-release-and-log-analysis`.
  - Poll cycles of about 10 minutes, plus up to 5 minutes of queue.
  - Push-to-main bursts that saturate the hosted runners (20:00Z).
- **Top failure modes:**
  - The promote fast-forward failure (39774429376), with no diagnosis.
  - The promote-cycle test regression (39800256248, 39807709623), resolved last window.
  - Observability gaps: truncated poll and review logs, no memory telemetry.
- **Highest-cost drivers:** review/autofix at 43.6M tokens and a 0.80 hit rate, of which 28.4M comes from the single two-pass run 39874461125. Spend for the Claude-engine phases and the poller is not measured.
- **Top 3 actions:**
  1. Add the `PROMOTE_STABLE_FF` outcome lines and annotate lines 271 and 277 in `promote-main-to-stable.yml`.
  2. Preserve contractual signal lines in `summarize_unselected_runs.py`, and add `ORCHESTRATE_POLL_CYCLE_SUMMARY`.
  3. Run the removal preflights for `pr6576-shard-followup.yml` and `check-failure-triage-historical.yml`, and remove them if the checks pass. Also disable caching for the `test_and_mark_stable` model calls.

## Metrics Appendix

**Run outcomes** (window about 11:23Z 10-22 to 00:37Z 10-23; 1,000 runs; success sample rate 0.07)

| Metric | Value |
|---|---|
| success / failure / cancelled / other | 372 / 3 / 9 / 616 |
| failure rate | 0.3% |
| p50 / p95 duration | 9s / 1,015s |
| avg duration | 213.6s |
| runs with log telemetry | 111 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (repo, n=110) | 13,000 / 3,983,710 |

**Key families**

| Family | Runs | Success | Fail | Cancelled | Other | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| ci | 34 | 32 | 2 | 0 | 0 | 867 | 1,227.9 |
| review_autofix | 58 | 51 | 0 | 3 | 4 | 262 | 2,761.3 |
| orchestrate_poll | 70 | 67 | 0 | 3 | 0 | 643.5 | 961.3 |
| implement | 120 | 11 | 0 | 3 | 106 | 2 | 1,192.4 |
| plan | 119 | 8 | 0 | 0 | 111 | 1 | 805 |
| clarify | 129 | 8 | 0 | 0 | 121 | 1 | 565.2 |
| orchestrate_clarify_respond | 118 | 2 | 0 | 0 | 116 | 1 | 497.1 |
| workflow_failure_heal | 76 | 1 | 0 | 0 | 75 | 1 | 453.8 |
| cancel_on_pr_close | 61 | 61 | 0 | 0 | 0 | 14 | 562 |
| promote_candidate_follow | 23 | 23 | 0 | 0 | 0 | 15 | 584.6 |
| check_failure_triage_historical | 10 | 10 | 0 | 0 | 0 | 24.5 | 309.7 |
| promote_main_to_stable | 1 | 0 | 1 | 0 | 0 | 158 | 158 |
| test_and_mark_stable | 1 | 1 | 0 | 0 | 0 | 4,640 | 4,640 |

**Tokens and cache** (Codex calls and tokens are 0; `claude_*` fields are absent from the run rows)

| Scope | Calls | Prompt | Completion | Cache read | Cache write | Total | cache_hit_rate |
|---|---|---|---|---|---|---|---|
| repo | 54 | 6,628,266 | 377,728 | 34,673,826 | 2,041,029 | 43,674,960 | 0.799985 |
| review_autofix | 42 | 6,579,134 | 374,375 | 34,673,826 | 1,995,930 | 43,622,475 | 0.801728 |
| test_and_mark_stable | 12 | 49,132 | 3,353 | 0 | 45,099 | 52,485 | 0.0 |
| run 39874461125 | 20 | 3,149,038 | 198,218 | 24,044,826 | 1,015,606 | 28,406,898 | 0.852367 |
| run 39809581147 | 11 | 1,783,688 | 82,169 | 5,544,547 | 486,995 | 7,897,399 | 0.709454 |
| run 39800256892 | 11 | 1,646,408 | 93,988 | 5,084,453 | 493,329 | 7,318,178 | 0.703809 |

**Review telemetry**

| Metric | Value |
|---|---|
| `wall_clock_p50_ms` / `p99_ms` (review_autofix, n=15) | 19,000 / 3,388,640 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |

**GH API** (PAT budget lines)

| Run | Job | remaining | used_in_job |
|---|---|---|---|
| 39845070743 | sweep | 2,749 | 3 |
| 39853728172 | sweep | 3,728 | 7 |
| 39872425980 | sweep | 4,313 | 6 |
| 39868920075 / 39865655875 / 39861747919 / 39858088361 / 39850009515 | sweep | 3,662 / 4,367 / 3,720 / 4,502 / 4,353 | 2 each |
| 39875514837 | sweep | 3,425 | unknown |
| 39874461125 | codex-agent | 4,648 | unknown |

There were no HTTP 429 or secondary rate-limit events. `GH_API_RETRY` permanent 404s on `promote-cycle-frozen` occurred 5 times, all expected.

**Runner queue samples (from summaries)**

| Run | Family | Wait |
|---|---|---|
| 39861355190 | orchestrate_poll | about 5m19s |
| 39875140410 | orchestrate_poll | about 4.5m |
| 39860321537 | orchestrate_poll | about 2m43s |
| 39845036834 | ci (per job) | 30–164s |
| 39845036727 | historical triage | about 74s |

**Semble**

| Metric | Value |
|---|---|
| query calls / logged bytes | 4 / 60,803 (about 15.2 KB per query) |
| sources total / static_dup_bytes | 30 / 0 |
| bootstraps / failed / unused | 4 / 0 / 0 |
| bootstrap ms total | 186,753 (39874461125: 99,358 over 2; 39800256892: 47,228; 39809581147: 40,167) |
| fallbacks total / contract_test / runtime | 44 / 44 (ci 40, test_and_mark_stable 4) / 0 |
| echo lines dropped | 0 |

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | queries | response bytes | tool calls | fallbacks |
|---|---|---|---|---|---|---|---|
| serena (all workflows) | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

Other MCP servers observed: none.

**Data gaps:**
- The deep-dive logs directory was not mounted.
- Poll and review logs are truncated by 0.85–1.6M characters before summarization.
- No `AI_MEMORY_TELEMETRY` lines were found.
- `or_phases` and `claude_*` fields are missing from the run rows.
- There is no per-job queue data.
- 61.6% of the 1,000-run window is skipped runs.
## Deep Audit — Workflows & Scripts (2026-10-23)

**Scope and method.** I read the full text of these files, line by line:
- `.github/workflows/promote-main-to-stable.yml`
- `check-failure-triage-historical.yml`
- `pr6576-shard-followup.yml`
- `cancel_on_pr_close.yml`
- `internal-cancel-on-pr-close.yml`
- `scripts/promote_candidate_follow.sh`
- `scripts/gh_helpers.sh` (lines 1–1209 and 1718+)
- `scripts/label_helpers.sh` (lines 150–269)
- `tests/test_workflow_file_size_limit.py`

I also read selected step bodies in `implement.yml`, `test-and-mark-stable.yml`, `review_autofix.yml`, `scripts/orchestrate_poll_process.sh`, `scripts/review_rb_judge.sh` and `scripts/promote_main_cycle.sh`. Other workflows were covered only by repo-wide pattern sweeps: untrusted-input interpolation, `${{ }}` inside run bodies, `GH_API_RETRY_HELPERS`, `--input -`, and TODO markers. I did not run `wc`, `shellcheck` or `git`, so every character count below is an estimate from line counts and is marked as such.

**Already covered in this report, so not raised again:**
- The unannotated `git merge --ff-only` / `git push` at `promote-main-to-stable.yml:271,277` (Reliability item 1).
- Removing `check-failure-triage-historical.yml` and `pr6576-shard-followup.yml` (Speed item 1).
- The `git_timeout` detail in `load_workflow_overlay.py` (Reliability item 3).
- The `GH_PAT_BUDGET used_in_job=unknown` gap (GH API Call Audit).
- The `actions/cache/restore@v4` Node 20 warning (Speed item 3).

### Section 1: Bug & Correctness Sweep

**BUG-001: a retried label PUT re-reads a stdin pipe that is already empty**
- **File:** `scripts/label_helpers.sh:253-255`; the same pattern is in `scripts/review_rb_judge.sh:1014-1016`
- **Severity:** High · **Category:** `bug`
- **Description:**
  - Both sites run `printf '{"labels":%s}' … | gh_retry gh api -X PUT ".../labels" --input -`.
  - `gh_retry` re-runs `"$@"` up to `GH_RETRY_MAX_ATTEMPTS` (default 5) times (`scripts/gh_helpers.sh:1066,1082-1083`). A PUT is not classed as an unsafe POST (`_gh_api_args_unsafe_post`, `gh_helpers.sh:721-724`), so it is retried.
  - Every attempt inherits the same stdin. The first attempt drains the pipe, so attempts 2–5 send an **empty request body** to "set labels".
  - If GitHub reads an empty body as "no labels", one transient 5xx on the first attempt removes every label from the issue, including the phase and lock labels the orchestrator depends on. If GitHub returns 400/422 instead, the code drops to the POST fallback and the replace-semantics PUT is silently lost. [NEEDS VERIFICATION: how GitHub's Set-labels endpoint treats an empty body]
- **Recommended fix:**
  - Write the payload to a temp file and pass the path, so every retry re-reads it.
  - `gh_api_retry` already parses `--input <file>` (`gh_helpers.sh:686,698-699`).
  - Pick a new local name that is unused in the file (§10), for example `_sipl_payload_file`:
    ```bash
    _sipl_payload_file="$(mktemp)"
    printf '{"labels":%s}' "${_new}" > "${_sipl_payload_file}"
    if gh_retry gh api -X PUT "repos/${repo}/issues/${issue_number}/labels" \
    	--input "${_sipl_payload_file}" >/dev/null 2>&1; then
    	rm -f "${_sipl_payload_file}"
    	SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME="applied"
    	return 0
    fi
    rm -f "${_sipl_payload_file}"
    ```
  - Apply the same change in `review_rb_judge.sh:1014-1016`.

**BUG-002: read-modify-write race on issue labels**
- **File:** `scripts/label_helpers.sh:225-255`
- **Severity:** Medium · **Category:** `bug`
- **Description:**
  - `set_issue_phase_label_resilient` GETs every label (225-226), computes `(. - $p) + [$t]` (240-241), then PUTs the whole set (253-255).
  - If another workflow adds a non-phase label between the GET and the PUT, the PUT removes it. Examples are `ai:scope-blocked`, `ai:destructive-blocked`, `ai:security` or `ai:engine-claude`, which implement and review write concurrently.
  - That is a lost-update (TOCTOU) problem. It matters most for the circuit-breaker labels that `implement.yml:2326-2331` checks before redispatch.
- **Recommended fix:**
  - Replace the full-set PUT with additive operations: POST-add the target label, then DELETE each phase label currently present.
  - That pattern already exists in the repo at `scripts/review_rb_judge.sh:991-996`.
  - Concurrent additions of non-phase labels then survive. Keep the PUT only as an explicitly documented fallback.

**BUG-003: a failed comment POST aborts the step before the failure label is applied**
- **File:** `.github/workflows/implement.yml:5613-5630`
- **Severity:** Medium · **Category:** `bug`
- **Description:**
  - The step runs under `set -euo pipefail` (5404).
  - Line 5613 `gh_retry gh api "repos/…/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` has no `|| …` guard.
  - `gh api` with `-f` and no method is an inferred POST, so `gh_retry` makes exactly one attempt (`gh_helpers.sh:1067-1069`).
  - A single transient failure ends the step before `ai:implementation-failed` is added (5619-5630), so the orchestrator's re-issue path never sees the failure label.
  - The guard branches in the same step all use `|| true` (5440, 5447, 5451), so this line is the inconsistent one.
  - [NEEDS VERIFICATION: whether a later `failure()` step, such as "Comment on issue failure" at 6660, applies the label in this path]
- **Recommended fix:**
  - Apply the label first (5619-5630).
  - Then post the comment with `|| echo "::warning::no-op comment post failed for #${ISSUE_NUMBER}"`.

**BUG-004: a combined multi-`--remove-label` edit falls back to "add only", leaving stale phase labels**
- **File:** `.github/workflows/implement.yml:5441-5447` and `5575-5581`
- **Severity:** Medium · **Category:** `bug`
- **Description:**
  - The code runs one `gh issue edit` with 7 `--remove-label` flags plus `--add-label 'ai:closed'`. If that fails, it runs `--add-label 'ai:closed'` alone.
  - The repo's own helper notes that gh prints `'X' not found` "when --remove-label targets a label that is not on the issue" (`gh_helpers.sh:171-173`). That message is classed as permanent (no retry, line 194).
  - An issue almost never carries all 7 of those labels. So the first edit usually fails, and the fallback leaves `ai:implementing`, `ai:planning` and similar labels on a closed issue.
  - [NEEDS VERIFICATION: the current gh CLI behaviour for removing a label that is not on the issue]
- **Recommended fix:**
  - Call `set_issue_phase_label_resilient "${ISSUE_NUMBER}" "ai:closed" "${REPO}"` (`label_helpers.sh:205`), after BUG-001 and BUG-002 are fixed. It removes every `_AI_PHASE_LABELS` entry (line 153) in one operation.
  - Then run `gh issue close`.

**BUG-005: a transient failure reading the promote-candidate pin silently switches the support-code source**
- **Files:**
  - `.github/workflows/cancel_on_pr_close.yml:40-53`
  - `orchestrate_poll.yml:126-135` and `:522-531`
  - `plan.yml:351-360` and `:3161-3170`
  - `clarify.yml:384-393` and `:2352-2361`
  - `check_failure_triage.yml:314-323`
  - `workflow_failure_heal.yml:109-118`
  - `orchestrate.yml:205-214` and `:379-388`
- **Severity:** Medium · **Category:** `bug`
- **Description:**
  - Each resolver runs `source "${GH_API_RETRY_HELPERS:-/dev/null}"`. `GH_API_RETRY_HELPERS` is written to `GITHUB_ENV` only by a "Bootstrap GitHub API retry helper" step, which exists only in `implement.yml:133`, `review_autofix.yml:331`, `validate.yml:122`, `security-audit.yml:77` and `orchestrate_clarify_respond.yml:87`.
  - In the 7 workflows above, the `branches/promote-candidate` read is therefore a single unretried `gh api` call (for example `cancel_on_pr_close.yml:44-45`).
  - On failure the code falls back to `github.sha` (lines 34, 38-39). During a frozen promote cycle, the workflow YAML comes from `@promote-candidate` (`internal-cancel-on-pr-close.yml:16`) while the support scripts come from the event SHA. That is version skew the pin was designed to prevent.
  - Inference: this is not a privilege escalation, because `pull_request` workflow definitions already come from the merge ref.
- **Recommended fix:**
  - Add the existing bootstrap step (`implement.yml:133-181`) to these jobs, or the shared action from DUP-001.
  - Alternatively, give the resolver a bounded 3-attempt retry like the one in `implement.yml:158-176`.
  - Log `SUPPORT_REF source=promote_candidate_read_failed` on the fallback so the skew can be seen.

**BUG-006: a repo-wide concurrency group can drop pending close-event runs**
- **File:** `.github/workflows/cancel_on_pr_close.yml:14-16`
- **Severity:** Low · **Category:** `bug`
- **Description:**
  - The group is `cancel-on-pr-close-${{ github.repository }}`, shared by `pull_request: closed` and the `*/5` schedule (`internal-cancel-on-pr-close.yml:4-7`).
  - GitHub replaces an existing *pending* run when a new one queues in the same group, whatever `cancel-in-progress` is set to. A close event that is waiting behind a scheduled tick can therefore be dropped.
  - The impact is bounded: the scheduled path re-derives closed-PR runs through GraphQL (lines 258-316) within 5 minutes. The report window shows 61 of 61 runs succeeding, so this has not been observed.
- **Recommended fix:**
  ```yaml
      concurrency:
        group: cancel-on-pr-close-${{ github.repository }}-${{ github.event.pull_request.number || 'scheduled' }}
        cancel-in-progress: false
  ```
  - Run cancellation is idempotent. [NEEDS VERIFICATION: that `review_merge_train.sh release` (line 424) is safe to run concurrently]

**SEC-001: a step output is interpolated straight into a run body; it is safe only because of a regex 900 lines away**
- **File:** `.github/workflows/implement.yml:1958-1971`
- **Severity:** Low · **Category:** `security`
- **Description:**
  - `steps.baseline_refctx.outputs.branch` is expanded inside double-quoted shell (1964, 1969, 1971).
  - Today it is safe. The value is constrained by `re.fullmatch(r"ai/reissue-baseline/pr-(\d+)-([0-9a-f]{12})-\d+-\d+", branch)` (1167) and checked against GitHub refs (1268-1286), and the issue body that feeds it is user-controlled (1014).
  - Any future loosening of that regex would turn this into shell injection.
- **Recommended fix:** Pass `BASELINE_STATUS`, `BASELINE_BRANCH` and `CHECKOUT_BASELINE_OUTCOME` through the step's `env:` and reference `${BASELINE_BRANCH}`. This matches the pattern the file already uses for `IMPLEMENT_RESOLVED_FALLBACK_REF` (1953).

**SEC-002: a cross-repo composite action is pinned to the mutable `@stable` tag**
- **File:** `.github/workflows/validate.yml:435`
- **Severity:** Medium · **Category:** `security`
- **Description:**
  - `uses: shubhodeep1/coding-workflows/.github/actions/setup-runtime@stable`.
  - Every other cross-repo action is SHA-pinned (`install-codex@91b30f6f…` in 13 places, `install-opencode@0e8d83a7…` at `orchestrate_poll.yml:461` and `review_autofix.yml:2256`).
  - Validate runs pinned to `promote-candidate` or to a consumer commit therefore run whatever `setup-runtime` is at `stable`. This is a version-skew and supply-chain inconsistency.
- **Recommended fix:** Load it from the already-checked-out support source, as `validate.yml:821,829` do (`uses: ./.codex-workflow-src/.github/actions/setup-runtime`), or pin it to a SHA.

**SEC-003: a boolean input is interpolated directly into the run body**
- **File:** `.github/workflows/sync_ai_labels.yml:125`
- **Severity:** Low · **Category:** `security`
- **Description:** `if [ "${{ inputs.dry_run }}" = "true" ]`. It is safe because the input is `type: boolean` (lines 11, 23). The same file passes it through `env: DRY_RUN` at 170 and 259.
- **Recommended fix:** Use `DRY_RUN` in the step's `env:`. This also removes `${{ }}` from the body, so the expression limit no longer applies to it.

**Security sweep results (no finding):**
- Every untrusted event field (`issue.title` and `.body`, `comment.body`, `pull_request.title`, `.body` and `.head.ref`) is passed only through `env:` in all 22 places found.
- The only `inputs.*` value expanded directly in a run body is SEC-003.

### Section 2: GitHub API Call Redundancy Audit

**API-001: `evaluate_sync_superseded_by_main` runs twice in one call path**
- **File:** `scripts/orchestrate_poll_process.sh:12997-13042`
- **Severity:** Medium · **Category:** `api-redundancy`
- **Description:**
  - When `.sync.status == "superseded-by-main"`, line 12998 evaluates. If the result is `SYNC_SUPERSEDED_BY_MAIN=false` and confident, the state is reset to `active` (13025-13034) and execution continues. Line 13042 then calls the function again with identical `(integration_branch, default_branch)`.
  - Each evaluation costs N per-issue timeline GraphQL calls (9260-9273 through `gh_issue_timeline_with_cross_refs`, `gh_helpers.sh:1718`), plus M `_fetch_pr_json` REST calls (9280, `2220-2224`), plus M paginated `pulls/{n}/files` calls (9293).
- **Call counts:** 2×(N + 2M + extra pages) today, N + 2M + extra pages after the fix.
- **Recommended fix:** Set a function-local flag after the first evaluation and skip the call at 13042 when it is set and the result was confident. Inference: nothing between the two calls changes the inputs.

**BATCH-001: per-issue and per-PR calls in the superseded-by-main check**
- **File:** `scripts/orchestrate_poll_process.sh:9260-9307`
- **Severity:** Medium · **Category:** `api-batching`
- **Description:** Two sequential loops: a timeline query per issue (9262), then two REST calls per PR (9280, 9293). There is no cycle cache.
- **Call counts:** N + 2M today (for example 8 issues and 8 PRs gives 24 or more), ⌈N/25⌉ + ⌈M/25⌉ after batching (2 in the same example). PRs with more than 100 files fall back to REST.
- **Recommended fix:**
  - Extend the 25-per-chunk aliased pattern of `_fetch_issue_labels_batch_graphql` (`:4349`, chunking at `:124-126`) with `timelineItems(itemTypes:[CROSS_REFERENCED_EVENT])`.
  - Extend `_fetch_linked_pr_status_graphql` (`:18587`) with `state mergedAt files(first:100){nodes{path} pageInfo{hasNextPage}}`.
  - If a batch fails, mark it unconfident and keep the current per-item behaviour (§14).

**BATCH-002: 3–4 REST reads per follower run on every push to `main` and every hour**
- **File:** `scripts/promote_candidate_follow.sh:148-184,210`
- **Severity:** Low · **Category:** `api-batching`
- **Description:**
  - The follower reads the branch (148), the tag ref (168), the main ref (181), and the tag object when the tag is annotated (210).
  - The expected 404 on the tag ref is what produces the recurring `GH_API_RETRY outcome=permanent … tags/promote-cycle-frozen` noise in the GH API Call Audit table.
- **Call counts:** 3–4 today, 2 after the change. The window had 23 runs.
- **Recommended fix:**
  - Keep the REST `branches/promote-candidate` read, because its `protected` flag reflects rulesets.
  - Replace the other reads with one GraphQL query: `repository{tag:ref(qualifiedName:"refs/tags/promote-cycle-frozen"){target{oid … on Tag{message target{oid}}}} main:ref(qualifiedName:"refs/heads/main"){target{oid}}}`. A missing tag comes back as `null` rather than a 404.
  - [NEEDS VERIFICATION: that GraphQL `branchProtectionRule` does not cover rulesets, which is why the REST branch read stays]

**API-002: per-hop calls in the no-op ancestor-chain walk**
- **File:** `.github/workflows/implement.yml:5546-5559`
- **Severity:** Low · **Category:** `api-batching`
- **Description:** Each hop makes 2 calls: the issue body (5547) and the paginated comments (5551-5552). The walk is bounded by `IMPL_NOOP_ANCESTRY_THRESHOLD` (default 2).
- **Call counts:** 2×threshold plus pagination today (4 or more), 1 per hop after the change (2).
- **Recommended fix:** One GraphQL query per hop: `issue(number:$n){body comments(last:100){nodes{body}}}`. Keep the fail-open `break` behaviour.

**API-003: polling loop makes 4–5 REST calls per tick**
- **File:** `.github/workflows/test-and-mark-stable.yml:1403-1452`
- **Severity:** Low · **Category:** `api-batching`
- **Description:**
  - Each `POLL_INTERVAL` tick calls: PR by head (1409), issue labels (1429), the runs list (1433), jobs (1446) and branch head commits (1451), for the whole implement phase.
  - Line 1498 also re-queries the PR with raw `gh api`, which bypasses `gh_api_safe_print` (see CONSIST-002).
- **Call counts:** 5 per tick today, 3 after the change.
- **Recommended fix:** One GraphQL query, `repository{issue(number:N){labels(first:50){nodes{name}}} pullRequests(headRefName:"ai/issue-N",states:OPEN,first:1){nodes{number}} ref(qualifiedName:"refs/heads/ai/issue-N"){target{oid}}}`, plus the existing runs and jobs REST calls.

**API-004: an idempotent cancel POST is denied retries**
- **File:** `.github/workflows/cancel_on_pr_close.yml:364-380`
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:**
  - The loop makes one `POST …/runs/{id}/cancel` call per run. That is unavoidable, because there is no batch cancel endpoint.
  - `gh_retry` treats `-X POST` as a non-idempotent create and makes a single attempt (`gh_helpers.sh:1067-1069`), so a transient 5xx leaves the run uncancelled until the next scheduled tick.
- **Call counts:** Unchanged at 1 per run.
- **Recommended fix:** Use `gh_retry --idempotent gh api -X POST "${cancel_url}"`. Cancelling is idempotent, and the flag is supported at `gh_helpers.sh:1065`.

**Note (no new ID):** `check-failure-triage-historical.yml:75-123` makes up to 5 calls per run, including downloading the whole job log at line 123 just to check it exists. Per Speed item 1, the fix is to remove the workflow, not to optimise it.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001: the "Bootstrap GitHub API retry helper" block exists in 5 near-identical copies**
- **Files:** `implement.yml:133-181`, `review_autofix.yml:331-380`, `validate.yml:122-167`, `security-audit.yml:77-122`, `orchestrate_clarify_respond.yml:87-132` (end lines are the `GH_API_RETRY_HELPERS=` echo lines)
- **Severity:** Medium · **Category:** `duplication`
- **Description:**
  - Each copy is about 48 lines with the same logic: pick the ref (stable, main or a pinned SHA), git-fetch the helper with an `http.extraHeader` token, retry 3 times, then write `GITHUB_ENV`.
  - The block has to run before checkout, so it cannot live in `scripts/`.
  - BUG-005 exists because 7 workflows never received a copy.
- **Recommended fix:**
  - Create a new composite action, `.github/actions/gh-api-retry-bootstrap/action.yml`, with inputs `token` and `workflow_job_json` and output `helpers_path`.
  - Reference it SHA-pinned, as the install-codex pattern does: `uses: shubhodeep1/coding-workflows/.github/actions/gh-api-retry-bootstrap@<sha>`.
  - Update the 5 existing callers and add it to the 7 workflows listed in BUG-005.

**DUP-002: the support-ref resolver is repeated 16 times across 10 workflows**
- **Files:**
  - `cancel_on_pr_close.yml:32-67`
  - `orchestrate.yml:205,379`
  - `implement.yml:447,752,1331,7005`
  - `workflow_failure_heal.yml:109`
  - `check_failure_triage.yml:314`
  - `orchestrate_poll.yml:126,522`
  - `orchestrate_clarify_respond.yml:396`
  - `clarify.yml:384,2352`
  - `plan.yml:351,3161`
- **Severity:** Medium · **Category:** `duplication`
- **Description:** Each copy has the same three parts: the `promote_candidate_support_ref="${{ github.sha }}"` branch, the protected-branch read, and the consumer `@<sha>` pin regex. Only the workflow file name in the regex differs.
- **Recommended fix:**
  - Fold this into the DUP-001 action as a second output (`support_ref`), with an input `workflow_file` for the regex.
  - Each caller becomes one `uses:` step with `id:`, and later steps read `steps.<id>.outputs.support_ref`.
  - Keep the `SCRIPT_REF` `GITHUB_ENV` name (§10).

**DUP-003: phase-label swap logic exists in three divergent forms**
- **Files:** `scripts/label_helpers.sh:205-269`, `scripts/review_rb_judge.sh:991-1023` (`_resilient_phase_swap`), and inline in `implement.yml:5441-5447`, `5575-5581` and `5627-5630`
- **Severity:** Medium · **Category:** `duplication`
- **Description:**
  - Three implementations of "remove the phase labels, add the target", with different semantics: a PUT replace, a DELETE-per-label path with terminal-label reconciliation, and a combined `gh issue edit`.
  - BUG-001, BUG-002 and BUG-004 come from these differences.
- **Recommended fix:**
  - Make `set_issue_phase_label_resilient <issue> <target> [repo] [--respect-terminal]` in `label_helpers.sh` the only owner, using the additive approach from BUG-002 and the terminal reconciliation from `review_rb_judge.sh:997-1009`.
  - Point `review_rb_judge.sh` and the three implement.yml sites at it.

**DUP-004: Telegram PR, run and issue message assembly is repeated in every notify step**
- **Files:** `review_autofix.yml:4897-4913`, `4956-4961`, `5051-5052`, `5204-5209`, `5499-5525` (plus 4155-4166); `workflow-log-analysis.yml:769-776`, `1606-1627`, `2229-2235`, `2822-2828`
- **Severity:** Low · **Category:** `duplication`
- **Description:** The `PR_URL`, `RUN_URL` and `Issue:` lines and the `LINKED_ISSUES_JSON` jq lookup are rebuilt in each step.
- **Recommended fix:**
  - Add `tg_build_pr_context_msg <title> <pr_number> <linked_issues_json>` to `scripts/tg_helpers.sh`. It should build the message from `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY` and `GITHUB_RUN_ID`, with no `${{ }}`.
  - Callers become `MSG="$(tg_build_pr_context_msg "PR processed: #${PR_NUMBER}" "${PR_NUMBER}" "${LINKED_ISSUES_JSON:-[]}")"`.

**DUP-005: inline Telegram `curl` duplicates `tg_helpers.sh`**
- **Files:** `implement.yml:5508-5512`, `test-and-mark-stable.yml:7773-7776`
- **Severity:** Low · **Category:** `duplication`
- **Description:** The justification given at `implement.yml:5496-5498` is that `tg_helpers.sh` may have been cleaned up. That same step already sources `gh_helpers.sh` from `${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}` (5405).
- **Recommended fix:** Stage `tg_helpers.sh` into `IMPLEMENT_STAGED_SUPPORT_RUN_DIR` too, and call `tg_send_msg "${TG_MSG}" CRITICAL`.

**DUP-006: `frozen_at` parsing of the freeze tag uses two different regexes**
- **Files:** `scripts/promote_candidate_follow.sh:213` and `scripts/promote_main_cycle.sh:615`
- **Severity:** Low · **Category:** `duplication`
- **Description:**
  - The follower accepts `frozen_at=` anywhere, preceded by a non-word character.
  - The cycle requires `^frozen_at=` at the start.
  - The writer is `promote_main_cycle.sh:595`. A future message format change could make one reader see "unparsable, so frozen forever" while the other sees "stale". `orchestrate_poll_process.sh:13729` reads the same tag object. [NEEDS VERIFICATION: whether that third site parses `frozen_at`]
- **Recommended fix:** Add one `promote_freeze_tag_frozen_at <message>` function, for example in `gh_helpers.sh` or a new `scripts/promote_helpers.sh`, and use it in all readers.

### Section 4: Expression Size Limit Risk Assessment

**Context.**
- `tests/test_workflow_file_size_limit.py:31-32,66-98` already fails CI for any `${{ }}`-bearing run body of 18,000 characters or more. While CI is green, no High-risk (18K+) block can exist.
- The findings below are Medium-band candidates (15K–18K), based on estimated counts.
- The workflow file limit for this repo is **512,000 bytes** (repo-measured, test line 26), with a CI guard at 480,000 (lines 27, 44-59). That supersedes the generic 1 MB / 800 KB figures, and no workflow can reach 800 KB while the guard holds.

**EXPR-001: the "Phase 3b" body in `test-and-mark-stable.yml`**
- **File:** `.github/workflows/test-and-mark-stable.yml:1269-1529`
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:**
  - The body is 260 lines, with several 150–280-character lines (1326-1334, 1398, 1504).
  - Estimated size: about 14,000–17,000 characters. Headroom is about 4,000–7,000 to the 21,000 limit and about 1,000–4,000 to the CI guard. [NEEDS VERIFICATION: exact `len(run)`]
  - It has a single interpolation, at 1307. This file already promotes values to `env:` for exactly this reason (comments at 1851 and 2683).
- **Recommended fix:** Remove line 1307 and add the value to the step's `env:`. The body then contains no expression at all.
  ```yaml
          IMPL_CREATED_AFTER: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}
  ```

**EXPR-002: the "Handle no-op implementation" body in `implement.yml`**
- **File:** `.github/workflows/implement.yml:5403-5630`
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:**
  - The body is 227 lines, with three embedded comment templates (5430-5437, 5470-5484, 5564-5571) of 270–330-character lines that tend to grow.
  - Estimated size: about 13,000–16,000 characters, so about 5,000–8,000 of headroom. [NEEDS VERIFICATION]
  - The only interpolations are `github.server_url`, `github.repository` and `github.run_id` (5408, 5410, 5613, 5621, 5623, 5627, 5629).
- **Recommended fix:**
  - Replace those interpolations with `${GITHUB_SERVER_URL}`, `${GITHUB_REPOSITORY}` and `${GITHUB_RUN_ID}`, which removes the limit from this body.
  - Preferably also move the body to `scripts/implement_handle_noop.sh`, following the existing `scripts/implement_handle_guard_block.sh` pattern, which shrinks `implement.yml` too.

**EXPR-003: the "Send Telegram notification" body in `test-and-mark-stable.yml`**
- **File:** `.github/workflows/test-and-mark-stable.yml:7556-7776`
- **Severity:** Low · **Category:** `expression-limit`
- **Description:**
  - The body is 220 lines with more than 20 interpolations (7564-7584, 7613-7656, 7769). About 60 of the lines are comments (7713-7745).
  - Estimated size: about 10,000–12,000 characters, so about 9,000–11,000 of headroom.
  - It is below the Medium band but grows quickly with comments.
- **Recommended fix:** Move the `needs.*.result` and `needs.*.outputs.*` values into `env:` (as `RELEASE_ENABLED` already is at 7552), and use `GITHUB_*` variables for the URL.

**EXPR-004: the CI guard does not cover composite actions and has no early warning**
- **File:** `tests/test_workflow_file_size_limit.py:35-36,66-98`
- **Severity:** Low · **Category:** `expression-limit`
- **Description:**
  - `_workflow_files()` globs only `.github/workflows/*.yml`. The 6 composite actions under `.github/actions/*/action.yml` are never checked, although their `run:` steps fall under the same limit.
  - The test has no 15,000-character warning tier, so blocks drift silently toward the 18K hard fail.
- **Recommended fix:** Add the action files to the scan (walking `runs.steps[].run`), and print a non-failing list of bodies at or above 15,000 characters with their length.

**EXPR-005: workflow file-size headroom is not visible**
- **Files:** `.github/workflows/test-and-mark-stable.yml` (7,776 lines), `implement.yml` (7,068 lines), `review_autofix.yml` (5,749 lines)
- **Severity:** Low · **Category:** `expression-limit`
- **Description:**
  - I could not measure byte sizes here.
  - Inference: at about 55 bytes per line, `test-and-mark-stable.yml` would be about 430 KB, which is near the 480,000 guard. [NEEDS VERIFICATION]
  - `review_autofix.yml` already went over the limit once (540,537 bytes, see §24).
- **Recommended fix:** Have `test_every_workflow_file_is_below_size_guard` also print each file's headroom. EXPR-002 and EXPR-003 are the cheapest byte reductions available in the two largest files.

**`if:` conditions:** the largest one sampled, `review_autofix.yml:4884`, is about 560 characters. None comes close to the limit.

### Section 5: Cross-Cutting Concerns

**DEAD-001: `gh_rate_limit_breaker_tripped` has no callers**
- **File:** `scripts/gh_helpers.sh:336-341`
- **Severity:** Low · **Category:** `dead-code`
- **Description:**
  - Nothing in `scripts/` or `.github/workflows/` calls it. It appears only in `README.md` and `analysis/`.
  - The file's own comment (315-318) says the only consumer, in orchestrate_poll, was removed.
  - [NEEDS VERIFICATION: whether README presents it as a public API for consumer repos]
- **Recommended fix:** If it is not a documented consumer API, remove it and its README mention. Otherwise add a `# public consumer API` note.

**DEAD-002: unreachable `merged` branch in `gh_review_pr_state`**
- **File:** `scripts/gh_helpers.sh:91,108`
- **Severity:** Low · **Category:** `dead-code`
- **Description:** The function accepts `merged` from REST `pulls/{n}` `.state`, but that field only ever returns `open` or `closed`.
- **Recommended fix:** Drop `merged` from both patterns, or read `.merged_at` if callers need to tell merged apart from closed.

**CONSIST-001: run URLs are built with `${{ }}` instead of the default environment variables**
- **Files:** 51 occurrences in 13 workflows (`review_autofix.yml` 12, `implement.yml` 9, `workflow-log-analysis.yml` 8, `plan.yml` 7, …), against only 3 that use `${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}`
- **Severity:** Low · **Category:** `consistency`
- **Description:** Each `${{ }}` use makes an otherwise plain run body subject to the 21K expression limit (EXPR-002, EXPR-003).
- **Recommended fix:** Replace them mechanically with the default `GITHUB_*` variables, which are always present on the runner. Callers in DUP-004 can use the helper instead.

**CONSIST-002: raw `gh api` in loops that otherwise use wrappers**
- **File:** `.github/workflows/test-and-mark-stable.yml:1498,7645`
- **Severity:** Low · **Category:** `consistency`
- **Description:** The same loop uses `gh_api_safe_print` at 1409, 1429 and 1433, but 1498 and 7645 call `gh api` directly, so they bypass retry and rate-limit handling.
- **Recommended fix:** Use `gh_api_safe_print` at 1498 and `gh_retry gh api` at 7645.

**SHELL-001: unquoted word-splitting `for` loops (SC2086)**
- **File:** `scripts/orchestrate_poll_process.sh:4897,25122,25470,25508,25551`
- **Severity:** Low · **Category:** `shellcheck`
- **Description:**
  - Examples: `for pw_inum in ${PRIOR_NON_TERMINAL}`, `for inum in ${ISSUE_NUMS}`, `for validation_candidate_pr in ${validation_merged_pr_candidates}`.
  - The values come from jq and are numeric today. A corrupted state value containing `*` would glob-expand against the working directory.
- **Recommended fix:** Use `mapfile -t _arr <<< "${ISSUE_NUMS}"` with `for inum in "${_arr[@]}"`, or put `set -f` around the loops.

**SHELL-002: notify steps run without `set -euo pipefail`**
- **File:** `.github/workflows/review_autofix.yml:4897-4913,4956,5499-5525`
- **Severity:** Low · **Category:** `shellcheck`
- **Description:** These steps rely on GitHub's default `bash -e` only, so there is no `-u` or `pipefail`. That differs from the convention in nearby steps, and a typo in a variable name expands silently to empty.
- **Recommended fix:** Add `set -euo pipefail`, keeping the existing `|| true` guards on the jq lookups (for example 4903 and 5506).

**DEBT-001: `heal-pr-reconcile` does a full-history checkout before knowing whether there is any work**
- **File:** `.github/workflows/internal-cancel-on-pr-close.yml:45-54`
- **Severity:** Low · **Category:** `tech-debt`
- **Description:**
  - Every PR close runs a `fetch-depth: 0` checkout of `main` before `scripts/workflow_failure_heal_pr_reconcile.sh` finds out whether any heal PRs exist.
  - The script needs history, because it runs `git merge` at line 189 after its own fetches at 159-168.
  - An earlier cycle (`analysis/workflow-optimization-2026-10-22.md`, Speed item 3) proposed `fetch-depth: 1`. That would break those merges.
- **Recommended fix:**
  - Add a cheap pre-step that lists the heal PRs stacked on `SOURCE_HEAD_REF` using the API already available, and skip the checkout when there are none.
  - Alternatively, keep full history but add `filter: blob:none` to the checkout. [NEEDS VERIFICATION: that partial clone works with the script's worktree merges]

**TODO/FIXME/HACK markers:** none. There are 0 matches in `scripts/*.sh`, `scripts/*.py` and `.github/workflows/*.yml`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 12 | BUG-002, BUG-003, BUG-004, BUG-005, SEC-002, API-001, BATCH-001, DUP-001, DUP-002, DUP-003, EXPR-001, EXPR-002 |
| Low | 20 | BUG-006, SEC-001, SEC-003, BATCH-002, API-002, API-003, API-004, DUP-004, DUP-005, DUP-006, EXPR-003, EXPR-004, EXPR-005, DEAD-001, DEAD-002, CONSIST-001, CONSIST-002, SHELL-001, SHELL-002, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 2 (`scripts/label_helpers.sh`, `scripts/review_rb_judge.sh`) | Small |
| API call optimization | 5 (`scripts/orchestrate_poll_process.sh`, `scripts/promote_candidate_follow.sh`, `implement.yml`, `test-and-mark-stable.yml`, `cancel_on_pr_close.yml`) | Medium |
| Code modularization | about 16 (new `.github/actions/gh-api-retry-bootstrap/action.yml`, 12 workflows using the bootstrap or resolver, `label_helpers.sh`, `review_rb_judge.sh`, `tg_helpers.sh`) | Large |
| Expression size reduction | 3 (`test-and-mark-stable.yml`, `implement.yml`, `tests/test_workflow_file_size_limit.py`) | Small |
| Medium/Low fixes | about 12 (`implement.yml`, `cancel_on_pr_close.yml`, `validate.yml`, `sync_ai_labels.yml`, `review_autofix.yml`, `internal-cancel-on-pr-close.yml`, `gh_helpers.sh`, `orchestrate_poll_process.sh`, plus the 7 resolver workflows in BUG-005) | Medium |