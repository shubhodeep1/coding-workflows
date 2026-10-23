## Executive Summary

- **Most of the elapsed time in the PR pipeline is spent waiting for a runner, not running code.** I could only read the 36 gpt-6-luna run summaries in `recent_runs`; the full logs were unavailable. In those 36 runs, about **6,675 s of 9,674 s (~69%)** was spent waiting for a runner, logged as "All GitHub-hosted runners with label [ubuntu-latest] are busy". The worst cases:
  - run 39913143337 (review_autofix `post-merge-validate-dispatch`) waited **~17 min** for **8 s** of work.
  - run 39914323316 (issue_pr_status) waited **~12m52s** for ~10 s of work.

  Cutting runner demand, rather than speeding up steps, is the biggest latency lever. Impact: high. Confidence: medium, because the timings come from sampled summaries.
- **The two longest workflow families are long because of their own runner load plus queueing.** CI has p50 **2,872 s** and p95 **3,923 s** over 68 runs. review_autofix has p50 **3,033 s** and p95 **4,938 s** over 71 runs.
  - Every CI job is capped at `timeout-minutes: 20` (`.github/workflows/ci.yml` lines 40, 511, 950, 1264, 1536, 2075). CI runs that last 41–87 minutes (e.g. 39892201530 = 5,248 s, 39892179017 = 4,785 s) must therefore be mostly queue time. This is an inference.
  - `ci.yml` has no `concurrency:` block (its `on:` covers `pull_request` on `**` and `push`). Superseded PR heads keep holding runners.

  Impact: high. Confidence: medium.
- **Reliability is good overall but CI is the weak point.** 3 failures in 1,000 runs (0.3%), all in `CI` (3 of 68 runs = **4.4%**). All three are test-step failures:
  - two in matrix group `orchestrate-poll (2)`: run 39906818009, step "Isolated agent container tests", and run 39896162205, step "Orchestrate poll process unit tests";
  - one in `static-checks` / "Validation self-test unit tests": run 39912672392.

  Root cause is unknown: the logs directory was not mounted and the deep-dive logs were unavailable. Impact: medium. Confidence: high on counts, low on cause.
- **LLM spend is concentrated in review_autofix, and caching works.** That family used **77.06M OpenRouter tokens** over 68 calls in 10 telemetry runs (~1.13M tokens per call):
  - 62.63M cache-read, 10.40M uncached prompt, 3.49M cache-write and 0.55M completion tokens;
  - `cache_hit_rate` **0.8185**, ranging per run from 0.771 (39892824594) to 0.861 (39895143801);
  - zero `BREAK_GLASS` and zero `CONTEXT_BUDGET_WARN` lines.

  Impact: medium. Confidence: high.
- **Semble is healthy and contributes very little prompt text.** 7 queries, 104,105 bytes (about 0.25% of prompt tokens), 51 sources, 0 runtime fallbacks, 0 failed or unused bootstraps. However, bootstrap costs **~42 s per review run** (294,436 ms in 7 bootstraps). All 24 `SEMBLE_FALLBACK` lines are `context=contract-test` emitted by CI tests, not live fallbacks. Serena is disabled (`SERENA_ENABLED: false`) and produced no telemetry. Confidence: high.
- **The memory/lineage step is slow and records stale state.** `finalize-task` steps take **90–130 s**: about 130 s in 39913942424, about 90 s in 39913173380 with `push_attempts: 3`, and about 98 s in 39913143310. Lineage still shows implement runs 39886692299, 39892880145 and 39893245600 as `in_progress` after their PRs merged. 39892880145 and 39893245600 actually succeeded (2,598 s and 2,336 s). Impact: medium. Confidence: medium.
- **Two one-shot workflows keep using runners after their work is done.** `pr6576-shard-followup.yml` logs `PR6576_FOLLOWUP outcome=skip reason=marker_present issue=7072` (runs 39914472554 and 39913611955). Each run still sets up a `self-test` job and waited up to ~6.5 min for a runner. `check-failure-triage-historical.yml` logs `HISTORICAL_TRIAGE skip reason=already_filed issue=7300` (39914472661, 39913612143). Both are already in `docs/scripts-pending-removal.md` (lines 212 and 320). Impact: low–medium. Confidence: high.

## Speed Optimizations

Ranked by expected reduction in wall-clock time.

1. **Cancel superseded PR-head CI runs (critical path).**
   - **Evidence:** CI p50 is 2,872 s while every job's timeout is 20 min. There are 68 CI runs, each with at least 9 job slots: 5 single jobs plus the 4-way `orchestrate-poll` matrix (`poll-group: [0, 1, 2, 3]`, ci.yml 2073–2074). Queue time dominates the sampled summaries.
   - **Root cause:** no workflow-level `concurrency` in `ci.yml`. Every `synchronize` keeps the previous head's matrix running.
   - **Change:** apply only to `pull_request`, so `push` to main, stable and `orchestrator/project-*` is untouched, and gate it with a repo variable:
     ```yaml
     concurrency:
       group: ci-${{ github.event_name == 'pull_request' && format('pr-{0}', github.event.pull_request.number) || github.run_id }}
       cancel-in-progress: ${{ github.event_name == 'pull_request' && vars.CI_CANCEL_SUPERSEDED != 'false' }}
     ```
   - **Expected saving:** for each superseded push, about 9 job slots freed. I estimate a 20–40% cut in queue time for every other workflow during burst windows (inference).
   - **Risk:** medium. `ci_cancelled_rerun.py` already classifies `superseded_head` as skip (sweep 39913910600: `CI_CANCELLED_RERUN ... superseded_head`). Check that the merge train and autofix never wait on CI for a non-tip SHA before turning this on.

2. **Deduplicate the lightweight PR checks.**
   - **Evidence:** within 17 s, PR #7934 got two readiness runs (39913148835 and 39913177291, which queued 4m32s and ~7 min), two lint runs (39913148966 and 39913177442, which queued 78 s and 5.5 min) and two review gates (39913149107 and 39913177461). Real work in each was 1–6 s. Over the window there were 41 readiness runs and 39 lint runs, all successful.
   - **Root cause:** `integration-pr-readiness.yml` triggers on `[opened, synchronize, reopened, ready_for_review, labeled, unlabeled]` and `lint-pr-body-auto-close.yml` on `[opened, edited, synchronize, reopened, ready_for_review]`. Neither has a `concurrency` group.
   - **Change:** add `concurrency: { group: <workflow>-${{ github.event.pull_request.number }}, cancel-in-progress: true }` to both. Both are idempotent status checks, so the newest run is authoritative.
   - **Expected saving:** about 30–50% fewer runner pickups for these two families (inference from the paired runs seen).
   - **Risk:** low. A cancelled run briefly shows as "cancelled" until the newer run reports.

3. **Avoid the full-history fetch in `heal-pr-reconcile` when it has nothing to do.**
   - **Evidence:** `.github/workflows/internal-cancel-on-pr-close.yml:50` uses `fetch-depth: 0`. In 39913173390 the fetch took about 80 s. Runs 39913942436, 39913611548 and 39913143345 fetched about 100 `[new tag]` refs. In every sampled run the outcome was `WORKFLOW_HEAL_PR_RECONCILE noop reason=no_heal_issue`.
   - **Change:** add a first step with `fetch-depth: 1` that runs the reconcile script's heal-PR lookup in `--list-only` mode. Run the full fetch and reconcile only when that lookup finds a heal PR.
   - **Expected saving:** about 30–80 s of runner time per closed PR.
   - **Risk:** low. The reconcile step is already `continue-on-error: true`.

4. **Retire the one-shot workflows whose work is done.**
   - **Evidence:** `pr6576-shard-followup.yml` waited 397 s in 39914472554 and 67 s in 39913611955 despite `marker_present`. The `check-failure-triage-historical.yml` guard waited 148 s in 39914472661 and 444 s in 39913612143.
   - **Change:** run the registry preflight checks (`docs/scripts-pending-removal.md` lines 219–220 and 326–328). If they pass, remove both workflows and their registry entries in the same PR.
   - **Saving:** two to three runner pickups per push to main.
   - **Risk:** low. Both are already registered for removal.

5. **Speed up the memory finalize step (micro-optimization).** It runs 90–130 s in issue_pr_status. Details under AI Memory Health.

## Cost Optimizations

Ranked by expected token or dollar savings.

1. **Raise the review_autofix cache hit rate from 0.82 toward the best run's 0.86.**
   - **Evidence:** 10.40M uncached prompt tokens and 3.49M cache-write tokens in 68 calls. 39892824594 hit 0.771 while 39895143801 hit 0.861.
   - **Likely cause (inference):** run-specific content such as PR diff, Semble block or findings placed before stable rubric text in some phases.
   - **Change:** emit one `PROMPT_PREFIX_HASH phase=<p> stable_bytes=<n> dynamic_bytes=<n>` line per call so the variance can be attributed (see Prompt Cache section). Then move dynamic blocks after the static prefix.
   - **Expected saving:** each 0.01 of hit-rate gain moves about 0.76M tokens from uncached to cache-read per window of this size.
   - **Quality risk:** none. The change only reorders content.

2. **Cut wasted review cycles on PRs that are still changing.**
   - **Evidence:** 71 review_autofix runs. The sweep (39913910600) found all 6 candidate PRs (#7940, #7933, #7924, #7921, #7919, #7911) with `skipped_active=6`, meaning review runs stay active for a long time (p50 3,033 s). PR #7934 got two review-gate runs 17 s apart.
   - **Change:** make sure the review job's concurrency group cancels when a newer head arrives. I did not verify the group in `review_autofix.yml`, so this needs checking. Also confirm the head gate (`scripts/review_head_gate.sh`) skips superseded SHAs before any model call.
   - **Expected saving:** each avoided full review saves about 7.7M tokens (77.06M / 10 telemetry runs).
   - **Risk:** low if limited to superseded heads.

3. **Keep Semble as is.** It adds ~14.9 KB per query (7 queries, 104,105 bytes), about 0.25% of prompt tokens, with 51 sources and 0 `static_dup_bytes`. It is not inflating prompts. The only cost is bootstrap time, about 42 s per run, which is not a token cost.

4. **Serena:** no data (`SERENA_ENABLED: false` in 39913177461, 39913149107 and 39913143337). Nothing to evaluate.

5. **Model and reasoning level.** 39914886957 shows `MODEL_EDITOR: openai/gpt-6-sol` with `MODEL_REASONING_EFFORT: high`, but the triage was skipped (`base_pending`) before any model call. With no per-phase usage split I cannot recommend a model change. See the Metrics Appendix gaps.

## Reliability Improvements

Ranked by expected reduction in failure or rerun rate.

1. **CI `orchestrate-poll (2)` failures (2 of 3 failures).**
   - **Evidence:** run 39906818009 (3,043 s) failed in "Isolated agent container tests (codex_isolated_exec.sh)". Run 39896162205 (2,406 s) failed in "Orchestrate poll process unit tests". The isolated-container step runs only in group 2 (`if: strategy.job-index == 2`, ci.yml:2422). Group 2 therefore does a quarter of the poller tests plus the container suite under the same `timeout-minutes: 20`. The comment at ci.yml 2064–2068 says each group takes about 5 minutes when warm and that cold runners are slower.
   - **Root cause category:** unknown, because the logs were not mounted. Two hypotheses, both inference: a group-2 timeout or resource squeeze, or a flaky test.
   - **Diagnostic logging to add (smallest safe change):** at the end of each test step in the `orchestrate-poll` and `static-checks` jobs, emit one line:
     ```bash
     echo "CI_STEP_TELEMETRY job=${GITHUB_JOB} group=${CI_POLL_TEST_GROUP_INDEX:-na} step=<slug> rc=${rc} elapsed_s=${SECONDS} timeout_s=1200 failed_nodeids=$(grep -c '^FAILED' "$log" || true)"
     ```
     Also have pytest print `-rf` failed node IDs (they already go to stdout) so the collector can cluster them. This tells timeout failures (elapsed near 1,200 s) apart from assertion failures without needing the full logs.
   - **Expected impact:** makes the root cause visible within one week. If the cause is the timeout, the fix is to move the container suite to the least-loaded group, which would remove about 2 failures per 68 CI runs.
   - **Rollback:** logging only. The `|| true` keeps it fail-open.

2. **`static-checks` / "Validation self-test unit tests" (run 39912672392, 1,287 s).**
   - The step runs four `python3 tests/...` scripts in sequence under `set -euo pipefail` (ci.yml 466–472). The first failure stops the rest, so the failing script name is the key signal.
   - **Change:** print a `VALIDATION_SELFTEST script=<name> rc=<rc>` line around each script invocation.
   - **Impact:** faster triage. Risk: none.

3. **The collector cannot download logs for the slowest review runs.**
   - **Evidence:** `errors[]` shows 39901481285 failing with "stream error: stream ID 1; CANCEL" and 39894124757 failing with "HTTP 403". These are the #1 and #10 slowest review_autofix runs (6,696 s and 4,697 s), so the two largest outliers have no telemetry.
   - **Change:** in the collector, retry stream-cancel errors once with backoff. Log 403 as `LOG_FETCH_FAIL reason=forbidden run_id=...` and count it, rather than silently dropping cost telemetry.
   - **Impact:** fills the cost data gap for outliers. Fails open.

4. **Lineage left as `in_progress` after merge.**
   - **Evidence:** runs 39913942424, 39913143310 and 39913173380 show the implement runs (39886692299, 39892880145, 39893245600) still `in_progress` at `finalize-task` time, although slow_runs records 39892880145 and 39893245600 as `success`.
   - **Root cause (inference):** the implement run's terminal lineage event is never written, or it is overwritten.
   - **Change:** in `finalize-task`, when `final_state=merged`, mark any run entry still `in_progress` as `superseded_by_finalize` and emit `stale_runs_closed=<n>` in `AI_MEMORY_TELEMETRY`.
   - **Impact:** lineage stays correct for poller and stall detection.

5. **Noisy `##[error]` on a successful run.**
   - **Evidence:** 39914886957 (check_failure_triage, success) logged "Can't find 'action.yml' ... under '.codex-workflow-src/.github/actions/install-claude'" during post-job cleanup. The local action's checkout had been removed before cleanup ran.
   - **Change:** do not delete `.codex-workflow-src` inside the job, or move the deletion into a final step that runs after all `uses: ./...` post hooks.
   - **Impact:** removes a false error that triage tooling may pick up.

6. **BREAK_GLASS / CONTEXT_BUDGET_WARN:** 0 and 0 in all 112 runs with telemetry. There is no policy or prompt-size pressure.

7. **Semble and Serena fallbacks:** 24 `SEMBLE_FALLBACK` lines, all `contract-test` (4 or 8 per CI run, e.g. 39906818009, 39912672392, 39892201530). This is expected test output, not a broken rollout. Runtime fallbacks: 0. Serena probes: 0 ok, 0 failed, 0 skipped, because it is disabled.

## AI Memory Health

`AI_MEMORY_TELEMETRY` lines were found only in run summaries, all `op=finalize-task`, in 5 issue_pr_status runs:

| Run | issue | ok | did_push | push_attempts | fail_open | reason | step time |
|---|---|---|---|---|---|---|---|
| 39914323316 | – | true | – | – | true | no_linked_issues | – |
| 39913611559 | – | true | – | – | true | no_linked_issues | – |
| 39913942424 | 7914 | true | true | 1 | – | merged | ~130 s gap |
| 39913173380 | 7928 | true | true | **3** | – | merged | ~90 s |
| 39913143310 | 7929 | true | true | 1 | – | merged | ~98 s gap |

- **`retrieve` operations: none observed.** Hit rate, `estimated_tokens` versus budget, and the `keyword_method` mix (`llm`/`plain`/`none`) cannot be computed. No retrieve lines were in the sampled excerpts of the 10 review_autofix telemetry runs either, though those excerpts were not available to me. Next step: confirm that implement and review_autofix emit `retrieve` telemetry. If they do, make sure the collector's summary prompt keeps those lines in `log_summary`.
- **Push contention:** 39913173380 needed 3 push attempts. It finalized #7928 at 06:44–06:46 while 39913143310 finalized #7929 at 06:43–06:45; both are in project 7925. This points to concurrent pushes to the memory branch (inference). Watch for `push_attempts>1` rising.
- **Latency:** 90–130 s per finalize is long for one commit and push. Add `elapsed_ms` per sub-phase (`fetch`, `commit`, `push`) to the `finalize-task` telemetry JSON so the time can be attributed.
- **No `enabled: false` entries.** The `fail_open: true` entries are correct behavior for PRs with no linked issue.

## GH API Call Audit

- **Budget:** `GH_PAT_BUDGET` lines show low use:
  - sweep 39913910600: `used_in_job=2 remaining=3533`;
  - `post-merge-validate-dispatch` 39913143337: `used_in_job=0 remaining=4979`.
  - No HTTP 429 or secondary-rate-limit lines appear in any of the 36 summaries.
- **Duplicate per-PR calls:** each lint run makes one paginated `gh api repos/.../pulls/<n>/commits?per_page=100` call (39913948023, 39913286184, 39913177442, 39915126220, 39913148966). PR #7934 had two lint runs, so the call was duplicated. The concurrency fix under Speed #2 removes these duplicates, roughly 30–50% of lint API calls (inference).
- **Stale payload:** in 39913148966 the step logged "Collected 8 commit message(s)" while the env had `PR_COMMIT_COUNT: 7`. The event payload was older than the live API data, a `synchronize` race. The lint uses the live API result, so correctness holds. Using the collected count for the 250-commit warning would avoid trusting the stale value.
- **Historical triage guard:** "a few `gh api` calls (PR, run, job log probe) and a paginated issue read" on every push (39913612143, 39914472661), repeated although `already_filed`. Removing the workflow (Speed #4) removes these calls.
- **Scheduled cancel cleanup (39914955191):** "preserved 9 active run(s) without pull-request linkage", including runs queued since 2026-08-19 (32215723558) and 2026-09-13 (34748435167, 34748435180). Each scheduled run reads these again. Cancelling queued runs older than 7 days, regardless of PR linkage, stops the repeated reads and clears ghost queue entries. Fail-open: skip on any API error.
- **Repo API rules (§14):** none of the observed hotspots is a per-item loop. Volume is driven by duplicate triggers, not unbatched calls.

## Prompt Cache & Memory System

- **`cache_hit_rate` = 0.8185** overall for review_autofix. Per sampled run:

  | Run | Hit rate | Calls |
  |---|---|---|
  | 39895143801 | 0.861 | 6 |
  | 39892167550 | 0.844 | 10 |
  | 39892120483 | 0.844 | 10 |
  | 39907176577 | 0.802 | 22 |
  | 39894927559 | 0.789 | 10 |
  | 39892824594 | 0.771 | 10 |

- **Cache-write share:** 3.49M write tokens against 62.63M read tokens (about 5.6%). Prefixes are mostly reused. The 22-call run (39907176577) has the most writes (1.11M), which suggests more phase-specific prefixes per extra call (inference).
- **Likely cause of fragmentation (inference):** the runs with the lowest hit rates also have the most Semble sources (39894927559: 11 sources, 0.789). This is consistent with the Semble block landing inside the cacheable prefix. It is weak correlation over n=6, not a measured effect.
- **Recommendation:** add a per-call `PROMPT_PREFIX_HASH phase=<p> stable_bytes=<n> dynamic_bytes=<n> semble_offset=<n>` line. Then keep the Semble/targeted-context block after the static rubric and system text.
  - **Expected impact:** +2–4 points of hit rate, about 1.5–3M fewer uncached prompt tokens per window like this one. This is an estimate.
- **Context budget:** `CONTEXT_BUDGET_WARN` = 0, so prompt growth is not eroding cache value.
- **Memory retrieval:** no `retrieve` telemetry was observed (see AI Memory Health).

## Orchestrator Health

- **Active projects:** #7913 (integration PR #7922) and #7925 (integration PR #7934).
  - 39913948100: `10/10 sub-issues on #7913 still unchecked`.
  - 39913148835 and 39913177291: `4/5 sub-issues on #7925 still unchecked`. Meanwhile sub-PRs #7937 (issue #7928) and #7938 (issue #7929) merged into `orchestrator/project-7925` about 06:42.

  The readiness check reads the checklist before the poller ticks the boxes. That is expected lag, but it produces repeated readiness runs. **Indicator to track:** time from sub-PR merge to checkbox ticked.
- **Wave progression is working:** `finalize-task` reached `final_state=merged` for #7914, #7928 and #7929. Alerts were correctly skipped with "Orchestrator-managed issue — skipping PR merged alert (poller handles completion)".
- **Comment fan-out:** each `issue_comment: created` starts four workflows: `internal-clarify.yml`, `internal-plan.yml`, `internal-implement.yml` and `internal-orchestrate-clarify-respond.yml`. Examples are 39915127168/242/280/332 at 07:01:31 and 39914951850/853/857/906 at 06:59:59. That is 122 + 123 + 124 + 124 runs, nearly all `skipped`. Some skipped runs still took 7–11 s (39914951850 at 11 s, 39913927023 at 11 s, 39913291909 at 10 s), so a gate job ran. Low cost per run, but about 490 runs of noise in a 1,000-run window. Nearly all of these are skipped, so this is noise, not a fault.
  - **Smallest mitigation:** a cheap job-level `if:` that drops comments whose body has no command prefix. This assumes the gates key on comment text; I did not verify that.
  - **Indicator:** skipped-run share per comment.
- **Stalled or ghost runs:** 8 queued runs and 1 in-progress run preserved by the scheduled cleanup, the oldest from 2026-08-19 (39914955191). See GH API Call Audit.
- **Stability:** `PROMOTE_CANDIDATE_FOLLOW outcome=frozen protected=true` (39914472449, 39913612140) and `PROMOTE_CYCLE_FREEZE_MAX_SECS: 172800` mean the promote cycle is frozen by design. The forward-merge to main succeeded (39914322355), and the auto-release dispatched `test-and-mark-stable.yml` (39914112288, `AUTO_RELEASE_DISPATCHED sha=918953ab…`).
- **Conflict-heal and clarification loops:** no signals in the sample (all `orchestrate_clarify_respond` runs were skipped). The `orchestrate_poll` family had 10 of 29 runs cancelled. The cause is not visible in the summaries; an `ORCH_POLL_CANCEL reason=` line at cancellation is the collection step to add.

## Pipeline Flow Bottlenecks

| Stage | Queueing | Compute | Retry | Merge/conflict |
|---|---|---|---|---|
| clarify / plan / implement dispatch | negligible (all skipped, 0–11 s) | – | – | – |
| implement (actual) | unknown | 2,336–2,598 s (39893245600, 39892880145) | 0 retries | – |
| CI | **dominant** (inferred from 20-min job cap vs 2,872 s p50) | ≤20 min per job | 0 retries; 3 failures | – |
| review/autofix | high (gate 39913149107 waited 94 s; dispatch 39913143337 waited ~17 min) | p50 3,033 s; ~42 s Semble bootstrap | 0 retries | sweep: 6/6 `active_run` |
| post-merge (status, cancel, readiness, lint) | **dominant**: 70–772 s waits for 1–10 s of work | 90–130 s memory finalize | push_attempts=3 once | forward-merge OK |

Fixes ordered by end-to-end impact:

1. Cancel superseded CI runs (Speed #1).
2. Add concurrency to readiness and lint (Speed #2).
3. Retire the two one-shot workflows (Speed #4).
4. Avoid the `fetch-depth: 0` fetch in heal-reconcile (Speed #3).
5. Instrument and speed up memory finalize.

Items 1–4 all free runner slots, which is the shared bottleneck for every later stage.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - hosted-runner saturation (~69% of sampled wall time);
  - CI and review_autofix runs lasting 47–112 minutes (max 6,696 s in 39901481285);
  - 90–130 s memory finalize.
- **Top failure modes:**
  - `orchestrate-poll (2)` test steps (39906818009, 39896162205);
  - `static-checks` validation self-test (39912672392);
  - collector log-fetch failures (39901481285, 39894124757).
- **Highest-cost drivers:** review_autofix, 77.06M tokens over 68 calls in 10 telemetry runs (~7.7M per run). The 22-call run 39907176577 alone used 23.6M.
- **Top 3 actions:**
  1. A pull_request-scoped `concurrency` with `cancel-in-progress` in `ci.yml`, gated by a variable, plus `concurrency` in `integration-pr-readiness.yml` and `lint-pr-body-auto-close.yml`.
  2. `CI_STEP_TELEMETRY` and `VALIDATION_SELFTEST` lines in the failing CI steps to separate timeout failures from assertion failures in `orchestrate-poll (2)`.
  3. Run the removal-registry preflights and remove `pr6576-shard-followup.yml` and `check-failure-triage-historical.yml` if they pass.

## Metrics Appendix

**Run counts and outcomes (window: 1,000 runs, `success_sample_rate` 0.07, `insufficient_data` false)**

| Scope | Runs | Success | Failure | Cancelled | Other/skipped | Fail rate | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| repo total | 1000 | 310 | 3 | 13 | 674 | 0.3% | 1 | 3,348 |
| ci | 68 | 65 | 3 | 0 | 0 | 4.4% | 2,872 | 3,923 |
| review_autofix | 71 | 69 | 0 | 1 | 1 | 0% | 3,033 | 4,938 |
| orchestrate_poll | 29 | 19 | 0 | 10 | 0 | 0% | 679 | 1,871 |
| integration_pr_readiness | 41 | 41 | 0 | 0 | 0 | 0% | 229 | 1,012 |
| lint_pr_body_auto_close | 39 | 39 | 0 | 0 | 0 | 0% | 304 | 770 |
| cancel_on_pr_close | 27 | 25 | 0 | 2 | 0 | 0% | 361 | 1,128 |
| issue_pr_status | 13 | 13 | 0 | 0 | 0 | 0% | 319 | 1,230 |
| promote_candidate_follow | 11 | 11 | 0 | 0 | 0 | 0% | 140 | 817 |
| check_failure_triage | 81 | 6 | 0 | 0 | 75 | 0% | 1 | 449 |
| check_failure_triage_historical | 7 | 7 | 0 | 0 | 0 | 0% | 324 | 750 |
| clarify / plan / implement / orch_clarify_respond | 122 / 123 / 124 / 124 | 0 / 0 / 3 / 0 | 0 | 0 | 122 / 123 / 121 / 124 | 0% | 1 | 9–10 |
| workflow_failure_heal / _intake | 24 / 89 | 0 / 5 | 0 | 0 | 24 / 84 | 0% | 1 / 1 | 8.5 / 17.6 |

**Tokens and cache (all OpenRouter, review_autofix only; codex_calls = 0)**

| Metric | Value |
|---|---:|
| or_calls (usage available / unavailable) | 68 (68 / 0) |
| or_prompt_tokens (uncached) | 10,398,706 |
| or_cache_read_tokens | 62,627,584 |
| or_cache_write_tokens | 3,488,367 |
| or_completion_tokens | 549,350 |
| or_total_tokens | 77,063,017 |
| cache_hit_rate | 0.818504 |
| wall_clock_p50_ms / p99_ms (repo) | 10,000 / 5,230,900 (n=107) |
| wall_clock_p50_ms / p99_ms (review_autofix) | 4,755,500 / 6,057,730 (n=10) |
| break_glass_count | 0 |
| context_budget_warn_count | 0 |

**Semble**

| Metric | Value |
|---|---:|
| query_calls | 7 (all review_autofix) |
| logged bytes | 104,105 (~14.9 KB per query) |
| sources_total | 51 |
| static_dup_bytes / echo_lines_dropped | 0 / 0 |
| bootstraps (failed / unused) | 7 (0 / 0) |
| bootstrap_ms_total (avg) | 294,436 (~42.1 s) |
| fallbacks (contract-test / runtime) | 24 (24 / 0), all in `ci` |

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | queries | response_bytes | fallbacks |
|---|---:|---:|---:|---:|---:|---:|
| serena (disabled: `SERENA_ENABLED: false`) | 0 | 0 | 0 | 0 | 0 | 0 |

Other MCP servers observed: none.

**GH API and sampled queue time**

| Signal | Value |
|---|---|
| GH_PAT_BUDGET 39913910600 | used_in_job=2, remaining=3533 |
| GH_PAT_BUDGET 39913143337 | used_in_job=0, remaining=4979 |
| HTTP 429 / secondary limits | 0 seen |
| Sampled queue wait (36 summarized runs) | ~6,675 s of ~9,674 s (~69%) |
| Max single queue wait | ~1,027 s (39913143337) |
| Log fetch failures | 2 (39901481285 stream CANCEL; 39894124757 HTTP 403) |

**Data gaps and next collection steps**

- The full logs directory (`/home/runner/work/_temp/workflow-log-output`) was not mounted in this run. The CI failure root causes and the review-phase token split are therefore unverified.
- No `queue_wait_seconds` field per run. Derive it in the collector as job `started_at` minus run `created_at`, from the jobs data already fetched for `failure_point`.
- No per-phase token split (reviewer, aggregator, editor) and no USD cost.
- No `AI_MEMORY_TELEMETRY op=retrieve` lines.
- No telemetry for the 2 slowest review runs, whose log fetches failed.
- The cause of the 10 cancelled orchestrate_poll runs is unknown.