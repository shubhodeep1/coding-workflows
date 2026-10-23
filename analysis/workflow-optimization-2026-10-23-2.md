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
## Deep Audit — Workflows & Scripts (2026-10-23)

Scope and method: this pass favours depth over breadth. It covered the PR-check and close-path workflows (`integration-pr-readiness.yml`, `lint-pr-body-auto-close.yml`, `internal-cancel-on-pr-close.yml`, `cancel_on_pr_close.yml`), the dispatch wrappers (`internal-review.yml`, `review_rb_judge_dispatch.yml`, `unblock_judge_dispatch.yml`), `promote-candidate-follow.yml`, `review_autofix_sweep.yml`, and the largest interpolated steps of `workflow-log-analysis.yml`, `test-and-mark-stable.yml` and `implement.yml`. On the script side it covered the shared retry and label helpers (`scripts/gh_helpers.sh`, `scripts/label_helpers.sh`) and several scripts with API loops (`workflow_failure_heal_pr_reconcile.sh`, `workflow_failure_heal_intake.sh`, `workflow_failure_heal_report.sh`, `check_failure_triage.sh`, `check_external_branch_advance.sh`, `promote_candidate_follow.sh`, `unblock_judge.sh`, `review_freshness_ci_failed.sh`). Several items are already covered by the existing report sections and are cross-referenced, not repeated: the missing `concurrency` on `ci.yml` and on the readiness and lint workflows (Speed #1/#2), the full-history fetch in heal-reconcile (Speed #3), the one-shot workflows (Speed #4), the stale `PR_COMMIT_COUNT` (GH API Call Audit), and the ghost queued runs (GH API Call Audit). Character counts below are estimates from line counts and sampled line lengths. No byte-exact measurement was possible in this pass.

### Section 1: Bug & Correctness Sweep

**BUG-001 — The release gate's rate-limit guard never fires, and failed reads keep resetting the inactivity timer**
- **File:** `.github/workflows/test-and-mark-stable.yml:1078-1117` (Phase 2 loop). The same guard appears at `:887`, `:1082-1084`, `:1413-1416`, `:2205` and `:3783`, with `RATE_LIMIT_BACKOFF=0` set at `:815`, `:969`, `:1283`, `:1903` and `:3633`.
- **Severity:** Medium
- **Category:** bug
- **Description:**
  - **The guard is dead code.** `gh_api_safe` (`scripts/comprehensive_test_and_release_gh_api.sh:97-132`) is the only code that raises `RATE_LIMIT_BACKOFF`, and on success it resets it to 0 (`:115`). Every poll calls it through command substitution, e.g. `LABELS=$(gh_api_safe_print "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" ... || echo "")` at `:1078`. Command substitution runs in a subshell, so the change never reaches the step shell. The guard `if [ "$RATE_LIMIT_BACKOFF" -gt 0 ] && [ -z "$LABELS" ]; then continue; fi` (`:1082-1084`) therefore never fires.
  - **Failed reads look like activity.** After 4 failed attempts the read degrades to `LABELS=""` (and `COMMENTS_JSON="[]"` at `:1086`). This changes `CUR_STATE` (`:1107`), and `:1110-1116` then resets `LAST_ACTIVITY`. The next successful read changes the state back and resets it again. Intermittent API failures can therefore stretch the `PLAN_PHASE_TIMEOUT` inactivity limit until the job's `timeout-minutes`.
  - **Latent tight loop.** If the guard were ever made reachable, the `:1082` copy does `continue` without sleeping (the `:1413` copy sleeps first), which would make it a tight loop.
- **Recommended fix:**
  - Call `gh_api_safe` in the step shell (not `gh_api_safe_print` inside `$(...)`) and read `GH_API_SAFE_OUTPUT`.
  - When the read fails, skip the state comparison, run the existing idle check `fail_plan_confirm_retry_if_idle` (`:1034-1041`), sleep `POLL_INTERVAL`, and `continue`.
  - Apply the same change to all five copies, or fix it once via the helper proposed in DUP-006.

```bash
if gh_api_safe "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '[.labels[].name] | join(",")'
then
	LABELS="${GH_API_SAFE_OUTPUT}"
else
	fail_plan_confirm_retry_if_idle
	sleep "$POLL_INTERVAL"
	continue
fi
```

**BUG-002 — A no-op implement run can end without `ai:implementation-failed`, because the comment is posted before the label**
- **File:** `.github/workflows/implement.yml:5616-5633`
- **Severity:** Medium
- **Category:** bug
- **Description:**
  - **Order of operations.** The step runs under `set -euo pipefail` (`:5407`). It first posts the comment with `gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` (`:5616-5617`) and only afterwards applies `ai:implementation-failed` (`:5630-5633`).
  - **No retry on the comment.** `gh_retry` treats a call carrying fields with no explicit method as a single-attempt unsafe POST (`scripts/gh_helpers.sh:678-741`, `:1067-1069`). One transient error therefore aborts the step before the label is applied.
  - **Where the issue ends up.** The `failure()` handler "Comment on issue failure" (`:6713-6788`) then moves `ai:implementing` back to `ai:awaiting-approval` (`:6775-6780`). The issue sits in the approval state rather than `ai:implementation-failed`, which the step comment at `:5612` says is the orchestrator's re-processing signal. Stall recovery can then post `/approved` again on the same no-op plan. That last consequence is an inference.
- **Recommended fix:**
  - Move the label block (`:5619-5633`) above the comment, so the state change lands first.
  - Make the comment fail-open: `gh_retry gh api ... -f body="${noop_body}" >/dev/null || echo "::warning::IMPLEMENT_NOOP comment_failed issue=${ISSUE_NUMBER}"`.

**SEC-001 — The two required PR checks run gate scripts from the PR's own merge ref**
- **File:**
  - `.github/workflows/integration-pr-readiness.yml:51-54` and `:73-79`
  - `.github/workflows/lint-pr-body-auto-close.yml:49-56` and `:100-115`
- **Severity:** Medium
- **Category:** security
- **Description:**
  - Both workflows run on `pull_request` and use the default `actions/checkout`, which checks out the PR merge ref. They then execute `scripts/check_integration_pr_readiness.py` and `scripts/lint_pr_body_auto_close.py` from that checkout. The readiness job has `statuses: write` (`:39`) and posts its own `orchestrator/integration-pr-not-ready` status.
  - A PR that changes either script, including an AI sub-PR merged into an `orchestrator/project-*` integration branch, is therefore judged by its own copy of the gate. A regression in the script turns the check green for the very PR that introduced it.
  - Integrity rule: these gates exist because of the #2734 and #6664 incidents (header comments `:1-22` and `:1-24`).
  - Caveat: a hostile PR could also edit the workflow file itself, so this fix guards against accidental self-validation, not a determined attacker [NEEDS VERIFICATION of branch-protection and ruleset coverage].
- **Recommended fix:**
  - Add a second, credential-less checkout of the trusted script, mirroring `review_autofix_sweep.yml:144-149` ("Check out trusted CI recovery helper"):
    ```yaml
    with:
      ref: ${{ github.event.repository.default_branch }}
      path: .trusted-gate
      sparse-checkout: scripts
      persist-credentials: false
    ```
  - Invoke `python3 .trusted-gate/scripts/<script>.py`.
  - Keep the PR checkout only for nothing at all, or drop it: both scripts read only the event payload and the API.

**BUG-003 — Scheduled cleanup ticks can replace a PR-close cleanup run that is still pending**
- **File:** `.github/workflows/cancel_on_pr_close.yml:14-16`, triggered by `.github/workflows/internal-cancel-on-pr-close.yml:3-7`
- **Severity:** Low
- **Category:** bug
- **Description:**
  - The job uses one repository-wide group, `cancel-on-pr-close-${{ github.repository }}` with `cancel-in-progress: false`. The caller fires on every `pull_request: closed` and every 5 minutes (`cron: "*/5 * * * *"`).
  - GitHub keeps at most one pending run per group and cancels the older pending one when a newer run queues. A PR-close run that is waiting behind a running tick is therefore cancelled by the next scheduled or close event. Its per-PR cancel and its merge-train release (`:391-427`) are skipped until the next scheduled sweep.
  - The schedule path backstops both, so the impact is a delay of up to about 5 minutes, plus "cancelled" runs in the metrics. The report's appendix shows `cancel_on_pr_close` with 2 cancelled runs; attributing them to this is an inference.
- **Recommended fix:** give PR-close runs their own group, e.g. `group: cancel-on-pr-close-${{ github.repository }}-${{ github.event_name == 'schedule' && 'schedule' || github.event.pull_request.number }}`. First check that `scripts/review_merge_train.sh release` tolerates two concurrent invocations [NEEDS VERIFICATION]. If it does not, keep the shared group only around the merge-train step by splitting it into a separate job.

**BUG-004 — The candidate follower can fast-forward `promote-candidate` after a cycle has frozen it**
- **File:**
  - `scripts/promote_candidate_follow.sh:168-179` (tag read) and `:233-242` (PATCH)
  - `.github/workflows/promote-candidate-follow.yml:23-25`
- **Severity:** Low
- **Category:** bug
- **Description:**
  - The follower reads `git/ref/tags/promote-cycle-frozen` and later PATCHes `refs/heads/promote-candidate` to the `main` tip it read earlier. Its concurrency group (`promote-candidate-follow-…`) is separate from the promote cycle in `scripts/promote_main_cycle.sh`, which creates the tag (`PROMOTE_CYCLE_PIN_TAG`, `:149`) and fast-forwards the candidate.
  - If the cycle freezes at M1 between the follower's tag read and its PATCH, the follower moves the candidate past the frozen commit (a fast-forward, so `force=false` accepts it).
  - agents.md (line 1661) documents that the gate then fails with `PROMOTE_CYCLE_FAILED reason=pin_head_mismatch`. The race wastes a cycle rather than promoting untested code.
- **Recommended fix:** on the advance path only, re-read `git/ref/tags/promote-cycle-frozen` right before the PATCH at `:237`, and emit `outcome=frozen` if the tag now exists. This is one extra read; update the §14 budget comment at `:22-25`.

**BUG-005 — A transient failure in the sweep's open-PR listing aborts the whole tick with no END line**
- **File:** `.github/workflows/review_autofix_sweep.yml:213` and `:228-237`
- **Severity:** Low
- **Category:** bug
- **Description:**
  - The open-PR snapshot is a raw `GH_TOKEN="${READ_TOKEN}" gh api --paginate ... | jq -s ...` under `set -euo pipefail`. Any transient error exits the step without writing `${RUNNER_TEMP}/ci-cancelled-pr-snapshot.json` (`:239`) and without the `AUTOFIX_SWEEP_END` line the log collector keys on.
  - The same step already sources and uses `gh_api_retry` for the active-run snapshot (`:216-220`, `:340`).
- **Recommended fix:**
  - Fetch with `gh_api_retry --paginate -X GET "repos/${REPOSITORY}/pulls" -f state=open -f per_page=100` into a file.
  - On a non-zero exit, emit `::warning::AUTOFIX_SWEEP_SKIPPED reason=pr_list_unavailable` plus the `AUTOFIX_SWEEP_END` line and `exit 0`, mirroring `:286-289`.

### Section 2: GitHub API Call Redundancy Audit

**API-001 — The sweep re-reads the repository just to get `default_branch`, which the PR list already returns**
- **File:** `.github/workflows/review_autofix_sweep.yml:285` (redundant read); data already fetched at `:228-237`
- **Severity:** Low
- **Category:** api-redundancy
- **Description:**
  - Every tick that has at least one candidate calls `gh api "repos/${REPOSITORY}" --jq .default_branch`.
  - Each item of the `pulls` list response already carries `base.repo.default_branch`, but the projection at `:230-236` drops it.
  - The sweep runs every 30 minutes (header comment `:140`), so this is about 48 avoidable calls per day.
- **Recommended fix:**
  - Add `default_branch: (.base.repo.default_branch // "")` to the projection.
  - Set `sweep_default_branch="$(printf '%s' "${prs_json}" | jq -r '[.[].default_branch | select(. != "")][0] // ""')"`.
  - Keep the current validation and skip at `:286-290`.
- **Calls per tick:** 1 → 0.
- **Pattern:** reuse the same-step cache, per §15 "Cycle-local caches".

**BATCH-001 — The active-run snapshot makes 6 paginated listings per tick where 3 would do**
- **File:** `.github/workflows/review_autofix_sweep.yml:337-348` and `:437-439`
- **Severity:** Low
- **Category:** api-batching
- **Description:**
  - `snapshot_active_review_runs` runs once for each of `internal-review.yml` and `review_autofix.yml`. Each run lists `actions/workflows/<wf>/runs` once per status (`queued`, `in_progress`, `pending`), so a tick makes 6 paginated calls.
  - The reducer already parses `.path` (`:380`), so it could filter by workflow file itself.
- **Recommended fix:**
  - Query `repos/${REPOSITORY}/actions/runs?status=<s>` once per status: 3 calls in total.
  - Split the result by `.path` (`.github/workflows/internal-review.yml` vs `.github/workflows/review_autofix.yml`) in the existing jq reducer.
  - Keep the `active_snapshot_incomplete` fail-closed handling per status.
- **Calls per tick:** 6+ → 3+. [NEEDS VERIFICATION]: the repo-wide queued list also contains other workflows' runs, so the page count per status may grow during bursts.
- **Pattern:** the `_cpr_fetch_active_runs` repo-wide status listing in `cancel_on_pr_close.yml:155-189`.

**API-002 — Two retry helpers retry permanent 4xx errors**
- **File:**
  - `scripts/gh_helpers.sh:1271-1335` (`gh_api_json_to_file`)
  - `scripts/gh_helpers.sh:1350-1403` (`curl_gh_api`)
  - Callers: `scripts/workflow_failure_heal_intake.sh:480`, `:499`, `:506`; `scripts/workflow_failure_heal_report.sh:235`; `scripts/check_failure_triage.sh:462`; `scripts/tg_helpers.sh:207`, `:214`, `:398`, `:467`
- **Severity:** Medium
- **Category:** api-redundancy
- **Description:**
  - `gh_retry` (`:1099-1106`) and `gh_retry_to_file` (`:1184-1192`) stop at once on `_is_gh_permanent_failure` (404/400/401/422, `:192-195`). `gh_api_json_to_file` lacks that check: its non-rate-limit branch (`:1313-1322`) always backs off and retries. `curl_gh_api` retries every non-2xx status that is not a rate limit (`:1389-1395`).
  - Several callers probe resources that are legitimately absent:
    - `pulls/${ISSUE_NUMBER}` for an issue number (`workflow_failure_heal_intake.sh:499`, `workflow_failure_heal_report.sh:235`);
    - a `compare/${HEAD_SHA}...` after a force-push (`:506`).
  - Each such probe costs 5 calls and about 15 s of sleep (1+2+4+8). This is a retry on a non-transient error.
- **Recommended fix:**
  - In the `else` branch of `gh_api_json_to_file`, add the same `if _is_gh_permanent_failure "${stderr_content}"; then ...; : > "${outfile}"; return 1; fi` that `gh_retry_to_file` has.
  - In `curl_gh_api`, return 1 at once for `400|401|404|422`.
- **Calls per permanent failure:** 5 → 1.

**BATCH-002 — Heal-PR reconcile makes one PR lookup per heal issue**
- **File:** `scripts/workflow_failure_heal_pr_reconcile.sh:239-247` (documented in the header at `:29-31`)
- **Severity:** Low
- **Category:** api-batching
- **Description:** for N matching heal issues the loop makes N `GET repos/${REPO}/pulls?state=open&head=<owner>:ai/issue-<n>` calls inside the `for heal_issue` loop.
- **Recommended fix:**
  - Build one aliased GraphQL query, `h<n>: repository(owner:,name:){ pullRequests(headRefName:"ai/issue-<n>", states:OPEN, first:10){ nodes{ number headRefName headRefOid baseRefName } } }` per issue, in batches of 50, as `_cpr_fetch_pr_state_batches` does in `cancel_on_pr_close.yml:196-256`.
  - Keep the per-issue fail-open (a missing alias means `heal_pr_lookup_failed`).
- **Calls:** N → ⌈N/50⌉. Typical N is 0–2, so savings are small. Cross-ref: report Speed #3 (`fetch-depth: 0` on the same job).

**BATCH-003 — Commit-identity checks make one REST call per commit**
- **File:** `scripts/check_external_branch_advance.sh:180-202`
- **Severity:** Low
- **Category:** api-batching
- **Description:** each self-subject SHA costs one `gh_retry gh api "repos/${REPOSITORY}/commits/${sha}"`. The script's own comment (`:175-177`) accepts this because there is "no existing batched helper".
- **Recommended fix:**
  - Use one GraphQL query with an alias per SHA: `c<i>: object(oid:"<sha>"){ ... on Commit { author{ user{ login } } committer{ user{ login } } } }`.
  - Keep "any alias missing" equivalent to `ADVANCE=unknown`.
  - [NEEDS VERIFICATION]: confirm that GraphQL `Commit.author.user.login` matches REST `.author.login` attribution for squash and bot commits.
- **Calls:** N → 1.

**BATCH-004 — Each release-gate poll tick makes two REST reads that one GraphQL query could replace**
- **File:** `.github/workflows/test-and-mark-stable.yml:1078-1089` (Phase 2; Phases 1, 3b and 6 use the same pattern)
- **Severity:** Low
- **Category:** api-batching
- **Description:** every `POLL_INTERVAL` the loop reads `issues/${ISSUE_NUMBER}` (labels) and `issues/${ISSUE_NUMBER}/comments?per_page=100&direction=desc` (count and latest `updated_at`), plus one runs page (`:1102`).
- **Recommended fix:**
  - Replace the two issue reads with one query: `repository{ issue(number:){ labels(first:100){ nodes{ name } } comments(last:100){ totalCount nodes{ updatedAt } } } }`.
  - `totalCount` changes whenever the old count would, so it still works as the activity signal.
- **Calls per tick:** 3 → 2. Release-gate only.

**API-003 — The scheduled close-cleanup re-lists all active runs every 5 minutes**
- **File:**
  - `.github/workflows/internal-cancel-on-pr-close.yml:6-7`
  - `.github/workflows/cancel_on_pr_close.yml:258-316` (schedule path) and `:155-189` (listing)
- **Severity:** Medium
- **Category:** api-redundancy
- **Description:**
  - Each `*/5` tick makes at least 2 paginated repo-wide `actions/runs` listings (`status=queued` and `status=in_progress`, `event=pull_request`) plus ⌈N/50⌉ GraphQL calls, and takes one runner pickup.
  - That is at least 576 REST calls and 288 job pickups per day for a backstop. The `pull_request: closed` path already handles the normal case.
  - Cross-ref: the report's GH API Call Audit found that each tick re-reads the same 9 preserved unlinked runs, and that runner pickups are the main bottleneck (Executive Summary).
- **Recommended fix:**
  - Reduce the backstop cron to `*/15`: at least 192 REST calls per day, a 3× cut.
  - Combine with the report's proposal to cancel queued runs older than 7 days, so the unlinked set stops being re-read.
  - [NEEDS VERIFICATION]: confirm that a backstop latency of up to 15 minutes is acceptable for merge-train release in core-profile repos (`:392-394`).

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001 — The "Resolve workflow support ref" step is copied 20 times across 12 workflows**
- **File:**
  - Example: `.github/workflows/cancel_on_pr_close.yml:25-67`
  - Same body in `orchestrate_poll.yml:118-159` and in `sync_ai_labels.yml`, `orchestrate.yml` (2), `implement.yml` (5), `workflow_failure_heal.yml`, `orchestrate_clarify_respond.yml`, `clarify.yml` (2), `review_autofix.yml`, `check_failure_triage.yml`, `memory_maintenance.yml` and `plan.yml` (2)
- **Severity:** Medium
- **Category:** duplication
- **Description:**
  - Each copy (about 43 lines) repeats the same logic: the promote-candidate protected-branch read, the 40-hex pin regex, `SCRIPT_REF` written to `GITHUB_ENV`, and the inline `gh_api_retry` shim.
  - Only the workflow filename in the two regexes differs. That is about 850 duplicated lines in total.
  - The step runs before any support checkout, so it cannot simply `source` a script.
- **Recommended fix:**
  - Keep the step inline, since its trust model needs it before checkout, but generate every copy from one template. Add a contract test that asserts byte-identical bodies modulo the filename token.
  - `tests/test_workflow_support_ref_follows_pin.py` already parses these steps; extend it.
  - Alternative [NEEDS VERIFICATION]: a composite action referenced at an immutable SHA, which release tooling bumps the way it does consumer wrapper pins (agents.md "Immutable consumer wrapper pins").

**DUP-002 — `workflow-log-analysis.yml` repeats its agent-pass scaffolding 3–4 times**
- **File:** `.github/workflows/workflow-log-analysis.yml`
  - Failure emitters: `:1100-1193` (`emit_log_analysis_phase_failure`), `:1890-1966` (`emit_deep_audit_failure`), and the API-redundancy copy around `:2490-2560`.
  - Engine and retry loops: `:1329-1398`, `:2024-2088`, plus the API-redundancy copy.
  - `_wla_label_create`: `:1079` and `:1878`.
  - Setup steps (Resolve AI engine, Install Claude Code CLI, Cache/Install/Persist/Restore Codex CLI, Create Codex config, Setup uv, Install semble, Build semble index), once per job: `:251-384`, `:828-995`, `:1667-1857`, `:2274-2456`.
- **Severity:** Medium
- **Category:** duplication
- **Description:**
  - The `AI_PHASE_FAILURE_V1` payload builder, the comment, the `emit_event` calls and the label application are near-identical. They differ only in `failed_step_name` and the heading text.
  - The `wla_engine_ready` / `claude_run_selected` / codex-fallback / exit-80 loop is copied verbatim.
  - The copies are also why these steps are large and interpolated (see EXPR-001/002).
- **Recommended fix:**
  - Create `scripts/workflow_log_analysis_pass.sh` with:
    - `wla_emit_phase_failure <failed_step_name> <failure_mode> <attempt_count> <failure_summary> <heading>`, which reads `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_WORKFLOW` and `GITHUB_SERVER_URL` from the runner environment;
    - `wla_run_agent_pass <role> <heartbeat_phase> <prompt_file> <out_file> [include_dir]`.
  - Move the shared setup into a local composite action, `.github/actions/wla-agent-setup`. This workflow runs only in this repository (schedule and dispatch at `:4`, `:36`), so `uses: ./...` resolves after checkout.
  - Callers: the four jobs listed above.

**DUP-003 — `gh_helpers.sh` contains five separate retry engines**
- **File:**
  - `scripts/gh_helpers.sh:910-1030` (`gh_api_retry`), `:1060-1138` (`gh_retry`), `:1149-1222` (`gh_retry_to_file`), `:1271-1335` (`gh_api_json_to_file`), `:1350-1403` (`curl_gh_api`)
  - A sixth in `scripts/comprehensive_test_and_release_gh_api.sh:97-160` (`gh_api_safe`)
- **Severity:** Low
- **Category:** duplication
- **Description:** the helpers have drifted apart:
  - permanent-failure short-circuit: `gh_retry` and `gh_retry_to_file` have it; `gh_api_json_to_file` and `curl_gh_api` do not (API-002);
  - unsafe-POST single attempt: absent in `curl_gh_api`;
  - status classification: exists only in `gh_api_retry`.
- **Recommended fix:**
  - Factor out `_gh_retry_core <out_mode:stdout|file> <outfile|-> <validate_json:0|1> -- <cmd…>`, implementing permanent-failure detection, rate-limit wait, unsafe-POST capping and backoff once.
  - Keep `gh_retry`, `gh_retry_to_file` and `gh_api_json_to_file` as thin wrappers (§10: no renames).
  - Long term, have `gh_api_safe` delegate to `gh_api_retry`.

**DUP-004 — Fallback shims silently remove retries in 43 files**
- **File:**
  - 81 shim lines of the form `type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }` across 31 scripts and 12 workflows.
  - Full fallback re-implementations: `scripts/check_failure_triage.sh:207-212` and `:213-226` (a copy of `_safe_gh_jq`), `scripts/workflow_failure_heal_intake.sh:171-176`, `scripts/workflow_failure_heal_report.sh:56-61`.
- **Severity:** Low
- **Category:** duplication
- **Description:**
  - These scripts `source scripts/gh_helpers.sh` relative to the working directory, then fall back to a no-retry pass-through. A run from another directory silently loses all retry and rate-limit handling.
  - `gh_api_json_to_file`'s fallback also drops JSON validation.
- **Recommended fix:**
  - Resolve helpers from the script's own directory, as `scripts/workflow_failure_heal_pr_reconcile.sh:50-54` does.
  - For scripts whose staging is guaranteed (`REQUIRED_BOOTSTRAP_SCRIPTS` in `scripts/stage_workflow_support.sh`), fail closed when `gh_helpers.sh` is missing, as `scripts/promote_candidate_follow.sh:60-65` does.
  - Keep shims only in workflow steps that run before support staging.

**DUP-005 — 44 ad-hoc `gh label create` calls bypass `ensure_label_exists`**
- **File:**
  - Canonical helper: `scripts/label_helpers.sh:163-197` (`ensure_label_exists`).
  - Ad-hoc calls: `scripts/implement_handle_guard_block.sh` (10), `.github/workflows/implement.yml` (8, e.g. `:5626`, `:6735`, `:6776`), `.github/workflows/review_autofix.yml` (4), `scripts/review_rb_judge.sh` (4), `.github/workflows/workflow-log-analysis.yml` (3, via `_wla_label_create` at `:1079` and `:1878`), and 8 more files.
- **Severity:** Low
- **Category:** duplication
- **Description:** each call site hand-rolls "already exists" detection, and they differ. Some use `2>/dev/null || true`, which also hides permission errors. `_wla_label_create` does its own stderr match and then retries.
- **Recommended fix:**
  - Extend `ensure_label_exists <label> [repo] [color] [description]`, backward compatible, with the catalog values remaining the defaults.
  - Route callers through it. Where a caller's copy of `label_helpers.sh` may be absent, keep a one-line fallback only.

**DUP-006 — The release-gate wait loops are re-implemented in each phase**
- **File:**
  - `.github/workflows/test-and-mark-stable.yml`: Phase 1 `:800-940`, Phase 2 `:954-1229`, Phase 3b `:1261-1552`, Phase 4 `:1821-2603`, Phase 6 `:3474-3897`
  - Also `.github/workflows/comprehensive-test-and-release.yml:55` and `:288`
- **Severity:** Low
- **Category:** duplication
- **Description:** each phase re-implements the `LAST_ACTIVITY`/`PREV_STATE`/`CUR_STATE` inactivity timer, the dead `RATE_LIMIT_BACKOFF` guard (BUG-001), and a `capture_run_id` retry wrapper.
- **Recommended fix:**
  - Add `phase_activity_tick <state_string>` (returns 0 on change and resets `LAST_ACTIVITY`) and `capture_scoped_run_id <name_re> <created_after> <title> [max_pages]` to `scripts/comprehensive_test_and_release_gh_api.sh`, which every phase already sources (`:977`, `:1296`).
  - Replace the per-phase copies. This fixes BUG-001 once and also shrinks EXPR-003.

### Section 4: Expression Size Limit Risk Assessment

**EXPR-001 — "Run workflow log analysis" is the largest interpolated run body found**
- **File:** `.github/workflows/workflow-log-analysis.yml:1068-1401`
- **Severity:** High (a conservative call; the estimate straddles the 18,000-character threshold)
- **Category:** expression-limit
- **Description:**
  - The body (about 333 lines) contains `${{ github.run_id }}`, `${{ github.run_attempt }}`, `${{ github.workflow }}`, `${{ github.server_url }}` and `${{ github.repository }}` at `:1113-1115`, `:1128`, `:1134-1136` and `:1173-1175`. The whole body is therefore one template expression.
  - It contains several very long lines (`:1334` is about 370 characters; `:1255`, `:1260-1264`, `:1312-1313` are about 150–240) and keeps growing with each engine and Semble change.
  - **Estimated size:** about 16,000–19,500 characters (76–93% of 21,000); headroom about 1,500–5,000 [NEEDS VERIFICATION with a byte count].
- **Recommended fix (smallest):**
  - Replace every `${{ github.* }}` in the body with the runner's default variables (`${GITHUB_RUN_ID}`, `${GITHUB_RUN_ATTEMPT}`, `${GITHUB_WORKFLOW}`, `${GITHUB_SERVER_URL}`, `${GITHUB_REPOSITORY}`). The body then has no expressions and is exempt from the limit.
  - Follow up by extracting it to `scripts/` per DUP-002.

**EXPR-002 — The deep-audit and API-redundancy pass steps are borderline watch items**
- **File:** `.github/workflows/workflow-log-analysis.yml:1869-2123` ("Run deep audit pass") and `:2457-2722` ("Run API redundancy pass")
- **Severity:** Low
- **Category:** expression-limit
- **Description:**
  - Both bodies interpolate `${{ github.* }}` (deep audit: `:1901-1903`, `:1915`, `:1921-1923`, `:1954-1956`; API redundancy: `:2501-2503`, `:2515`, `:2521-2523`, `:2554-2556`).
  - They carry growing inline contracts (the `required_headings` array at `:2096-2105`).
  - **Estimated size:** about 12,500–15,000 characters each; headroom about 6,000–8,500 [NEEDS VERIFICATION].
- **Recommended fix:** same as EXPR-001: switch to the runner variables now, and move the bodies into the shared `scripts/workflow_log_analysis_pass.sh` (DUP-002).

**EXPR-003 — Release-gate Phase 2 and Phase 3b**
- **File:** `.github/workflows/test-and-mark-stable.yml:962-1229` (Phase 2) and `:1269-~1550` (Phase 3b)
- **Severity:** Medium
- **Category:** expression-limit
- **Description:**
  - Phase 2 interpolates `${{ steps.create-issue.outputs.created_after }}` three times (`:980`, `:992`, `:1010`). Phase 3b interpolates `${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}` once (`:1307`).
  - Both bodies are long and heavily commented, at about 267 and 281 lines.
  - **Estimated size:** Phase 2 about 14,000–17,000 characters; Phase 3b about 15,000–18,000 characters; headroom about 3,000–7,000 [NEEDS VERIFICATION].
  - The file already uses the fix elsewhere: `:1851` and `:2683` say "Promoted to env so the run: script body contains no ${{ }}".
- **Recommended fix:**
  - Add `CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` (Phase 2) and `IMPL_CREATED_AFTER: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}` (Phase 3b) to each step's `env:`.
  - Reference `${CREATED_AFTER}` and `${IMPL_CREATED_AFTER}` in the bodies. That removes all interpolation from both steps.

**EXPR-004 — `implement.yml` "Handle no-op implementation" is a watch item**
- **File:** `.github/workflows/implement.yml:5406-5633`
- **Severity:** Low
- **Category:** expression-limit
- **Description:**
  - The body (about 227 lines, including a multi-line Markdown comment template at `:5596-5614`) interpolates `${{ github.server_url }}`, `${{ github.repository }}` and `${{ github.run_id }}` at `:5411`, `:5413`, `:5616`, `:5624`, `:5626`, `:5630` and `:5632`.
  - **Estimated size:** about 11,000–13,500 characters; headroom about 7,500–10,000.
- **Recommended fix:** `REPO` is already assigned at `:5413`. Set `REPO="${GITHUB_REPOSITORY}"` and `RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"`, and use `${REPO}` in the later calls.

**Other checks:**
- **Large bodies already clean.** "Run Codex implementation" (`implement.yml:2795-4106`, about 1,300 lines) interpolates only in its `env:` block (`:2807-2832`). "Attempt post-Codex syntax repair" (`:4639-5170`) and "Preflight destructive-commit guard" (`:4216-4627`) do the same, so none of them is subject to the limit.
- **Not measured in this pass:** the large interpolated steps of `plan.yml` (67 interpolated body lines), `clarify.yml` (54), `review_autofix.yml` (57), `update_workflows.yml` (45), `validate.yml` (42) and `orchestrate.yml` (39) [NEEDS VERIFICATION].
- **Workflow file size:** no workflow can exceed 800 KB while CI passes, because `tests/test_workflow_file_size_limit.py` fails at 480,000 bytes (agents.md "Workflow file size limit"). The largest by line count are `test-and-mark-stable.yml` (7,776 lines), `implement.yml` (7,136) and `review_autofix.yml` (5,749). Their byte sizes were not measured here; the `budget-watch` job warns at `CI_BUDGET_WORKFLOW_WARN_BYTES` (440,000) [NEEDS VERIFICATION with `wc -c`].

### Section 5: Cross-Cutting Concerns

**DEAD-001 — The `GH_API_RETRY_HELPERS` source lines in 8 workflows always read `/dev/null`**
- **File:**
  - `.github/workflows/cancel_on_pr_close.yml:43`
  - `orchestrate.yml:214`, `:388`
  - `workflow_failure_heal.yml:118`
  - `clarify.yml:393`, `:2361`
  - `check_failure_triage.yml:323`
  - `orchestrate_poll.yml:135`, `:531`
  - `plan.yml:360`, `:3170`
- **Severity:** Low
- **Category:** dead-code
- **Description:**
  - Only `validate.yml:167`, `implement.yml:178`, `security-audit.yml:122`, `orchestrate_clarify_respond.yml:132` and `review_autofix.yml:376` ever export `GH_API_RETRY_HELPERS`.
  - In the 8 files listed, `source "${GH_API_RETRY_HELPERS:-/dev/null}"` is a no-op, so the promote-candidate branch read always uses the inline shim with no retry (`cancel_on_pr_close.yml:44`, `orchestrate_poll.yml:136`). A failed read silently keeps `github.sha`.
- **Recommended fix:** either drop the dead `source` line in those files (the shim is the real behaviour), or export the variable from the "Bootstrap GitHub API retry helper" pattern used in `implement.yml:133-181` before the resolve step. Fold this into DUP-001's template.

**CONSIST-001 — `curl_gh_api` lacks the safety guards the other helpers have**
- **File:** `scripts/gh_helpers.sh:1350-1403`, compared with `:1060-1138`
- **Severity:** Low
- **Category:** consistency
- **Description:**
  - `curl_gh_api` retries any non-2xx status up to 5 times and does not apply the unsafe-POST single-attempt rule from issue #6634 (`gh_retry` at `:1062-1069`).
  - Current callers (`scripts/tg_helpers.sh:207`, `:214`, `:398`, `:467`) are all GETs, so there is no duplicate-write bug today. A future POST through it would be retried after a timeout.
  - The doc comment at `:1340-1341` ("sleeps 30 s") is also stale: the code uses `_sleep_until_reset`.
- **Recommended fix:** detect `-X POST`, `--request POST` or `--data*` in the arguments and cap at 1 attempt. Add the permanent-status short-circuit (API-002) and correct the comment.

**CONSIST-002 — Two dispatch wrappers bypass the promote-candidate pin**
- **File:**
  - `.github/workflows/review_rb_judge_dispatch.yml:46` (`review_autofix.yml@main`)
  - `.github/workflows/unblock_judge_dispatch.yml:32` (`unblock_judge.yml@main`)
  - Compared with every `internal-*.yml` wrapper, e.g. `internal-review.yml:56` (`@promote-candidate`)
- **Severity:** Medium
- **Category:** consistency
- **Description:**
  - agents.md (lines 1661–1663) states that the internal wrappers call their reusable workflows at `@promote-candidate`, so pipeline runs execute the frozen candidate during a pinned promote cycle.
  - The poller-dispatched review-blocked judge and unblock judge run `main`'s `review_autofix.yml` and `unblock_judge.yml`. During a frozen cycle they can execute commits newer than the candidate under test.
  - `promote_main_cycle.sh` only checks `internal-*.yml` (`:526`), so these wrappers are outside its pin check.
  - [NEEDS VERIFICATION]: whether `@main` is deliberate, e.g. so judges pick up hotfixes during a long freeze.
- **Recommended fix:**
  - If not deliberate, pin both to `@promote-candidate`.
  - Either way, extend the wrapper check in `scripts/promote_main_cycle.sh:526` to list these two files explicitly, so the choice is enforced rather than implicit.

**CONSIST-003 — Raw `gh` calls sit next to retry helpers in the same step**
- **File:**
  - `.github/workflows/review_autofix_sweep.yml:228` and `:285` (raw), compared with `:340` (`gh_api_retry`)
  - `.github/workflows/workflow-log-analysis.yml:1164-1167` and `:1948` (raw `gh issue comment`; `:1948` uses a bare `|| true` with no warning)
  - `.github/workflows/test-and-mark-stable.yml:1143` (raw `gh api`), compared with `gh_api_safe_print` everywhere else in the step
- **Severity:** Low
- **Category:** consistency
- **Description:**
  - Transient errors on these calls either abort the step (BUG-005) or lose state silently. For example, the `AI_PHASE_FAILURE_V1` marker comment on the tracking issue is dropped with no log line at `:1948`.
  - The `:1143` re-check treats a failed read as "no labels", which can push a still-running plan into `plan_failed` after the active-run walk.
- **Recommended fix:**
  - Route each call through the helper already loaded in that step: `gh_api_retry`, or `GH_RETRY_IDEMPOTENT=false gh_retry` for comment creates.
  - On fail-open paths, emit a `::warning::` with a stable prefix.

**CONSIST-004 — Run bodies use `${{ github.* }}` where runner environment variables exist**
- **File:**
  - `.github/workflows/cancel_on_pr_close.yml:33-34`
  - `.github/workflows/orchestrate_poll.yml:125-126`
  - `.github/workflows/implement.yml:5411-5413`, `:5616-5632`, `:6721`, `:6735-6787`
  - `.github/workflows/workflow-log-analysis.yml` (see EXPR-001/002)
- **Severity:** Low
- **Category:** consistency
- **Description:**
  - `${{ github.repository }}`, `${{ github.sha }}`, `${{ github.run_id }}` and `${{ github.server_url }}` are spliced into shell. The values are not attacker-controlled, so this is not an injection risk.
  - Each one turns its whole run body into a template expression, which feeds Section 4 risk. Most of the repository already uses `${GITHUB_REPOSITORY}` (e.g. `lint-pr-body-auto-close.yml:85`, `:115`).
- **Recommended fix:** mechanically replace them with `${GITHUB_REPOSITORY}`, `${GITHUB_SHA}`, `${GITHUB_RUN_ID}` and `${GITHUB_SERVER_URL}`. Add a lint in `tests/` that rejects `${{ github.(repository|sha|run_id|run_attempt|server_url|workflow) }}` inside `run:` bodies.

**SHELL-001 — Loop over a space-joined string instead of an array (SC2086)**
- **File:** `scripts/check_external_branch_advance.sh:141`, `:153` and `:180`
- **Severity:** Low
- **Category:** shellcheck
- **Description:** `self_subject_shas+="${sha} "` followed by `for sha in ${self_subject_shas}; do` relies on unquoted word-splitting. It is safe today because the values are git SHAs from `git rev-list`, but shellcheck flags it and it breaks under a changed `IFS`.
- **Recommended fix:** use an array: `self_subject_shas=()`, then `self_subject_shas+=("${sha}")`, then `for sha in "${self_subject_shas[@]}"; do`.

**SHELL-002 — Useless `cat` in a pipeline (SC2002)**
- **File:** `.github/workflows/review_autofix_sweep.yml:350`
- **Severity:** Low
- **Category:** shellcheck
- **Description:** `cat "${snap_raw}" | jq -c -s ...` adds a process and an extra pipeline stage under `pipefail`.
- **Recommended fix:** `jq -c -s --argjson cutoff ... '…' "${snap_raw}"`.

**DEBT-001 — Mixed indentation in one script**
- **File:** `scripts/implement_handle_guard_block.sh:38-57` (tabs), compared with `:97-99`, `:208-210` and `:368-369` (2-space)
- **Severity:** Low
- **Category:** tech-debt
- **Description:** the file mixes tab-indented function bodies with 2-space-indented top-level blocks, against the CLAUDE.md §9 code style (tabs).
- **Recommended fix:** re-indent the space-indented blocks with tabs the next time the file is touched for a functional change. Do not make a style-only PR (§5).

**TODO/FIXME/HACK markers:** a repository-wide search of `scripts/*.sh`, `scripts/*.py` and `.github/workflows/*.yml` for `TODO|FIXME|HACK|XXX` found none.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | EXPR-001 |
| Medium | 9 | BUG-001, BUG-002, SEC-001, API-002, API-003, DUP-001, DUP-002, EXPR-003, CONSIST-002 |
| Low | 21 | BUG-003, BUG-004, BUG-005, API-001, BATCH-001, BATCH-002, BATCH-003, BATCH-004, DUP-003, DUP-004, DUP-005, DUP-006, EXPR-002, EXPR-004, DEAD-001, CONSIST-001, CONSIST-003, CONSIST-004, SHELL-001, SHELL-002, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | None at Critical/High. Medium bug and security items: `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/implement.yml`, `.github/workflows/integration-pr-readiness.yml`, `.github/workflows/lint-pr-body-auto-close.yml` | Small |
| API call optimization | `scripts/gh_helpers.sh`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/internal-cancel-on-pr-close.yml`, `scripts/workflow_failure_heal_pr_reconcile.sh`, `scripts/check_external_branch_advance.sh`, `.github/workflows/test-and-mark-stable.yml` | Medium |
| Code modularization | `.github/workflows/workflow-log-analysis.yml` plus a new `scripts/workflow_log_analysis_pass.sh` and a local composite action, 12 workflows carrying the support-ref step, `scripts/gh_helpers.sh`, `scripts/label_helpers.sh` and its 14 ad-hoc callers, `scripts/comprehensive_test_and_release_gh_api.sh` | Large |
| Expression size reduction | `.github/workflows/workflow-log-analysis.yml`, `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/implement.yml` | Small |
| Medium/Low fixes | `.github/workflows/cancel_on_pr_close.yml`, `scripts/promote_candidate_follow.sh`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/review_rb_judge_dispatch.yml`, `.github/workflows/unblock_judge_dispatch.yml`, `scripts/promote_main_cycle.sh`, 8 workflows with the dead `GH_API_RETRY_HELPERS` source, `scripts/check_external_branch_advance.sh`, `scripts/implement_handle_guard_block.sh` | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-23)

### Safety Tag Legend
- `SAFE_TO_MERGE`: the calls hit the same endpoint (or a strict subset of its fields) with the same filters, pagination and token, in the same function or step, with no mutation in between. Merging them changes no error handling, crosses no concurrency boundary and keeps every documented cache contract. The implement stage may act on these directly.
- `NEEDS_VERIFICATION`: the saving looks real, but reading the code alone cannot prove one or more of those conditions. Each such finding lists the checks that must pass first.
- `RISKY_SKIP`: the call sits in a retry, backoff or poll loop, an identity or permission check, a paginated listing, a rate-limit-sensitive path, or code that defends against races. These are listed so they are visible, but must not be changed automatically.

### Consolidation Candidates (MERGE-###)

**MERGE-001 — The auto-close lint makes one `gh issue view` call per referenced issue; one aliased GraphQL query per repository would do**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Calls involved:**
  - `scripts/lint_pr_body_auto_close.py:143-178` (`_fetch_issue_labels_gh`)
  - the loop in `lint()` at `scripts/lint_pr_body_auto_close.py:257-268`
  - invoked from `.github/workflows/lint-pr-body-auto-close.yml:100-115`
- **Current call count:** N, the number of distinct `(repo, issue)` pairs referenced by auto-close keywords in the PR body plus every commit message.
- **Proposed call count:** ⌈N/50⌉ per distinct referenced repository. Usually 1.
- **Endpoints:** `gh issue view --json labels`, which is GraphQL `repository.issue.labels`, once per issue → one aliased GraphQL query, `repository(owner,name){ i<N>: issueOrPullRequest(number:N){ ... on Issue{labels(first:100){nodes{name}}} ... on PullRequest{labels(first:100){nodes{name}}} } }`.
- **Evidence:** the cache only removes repeats of the same issue. Distinct issues still cost one subprocess and one API call each:
  ```python
  # Cache per-(repo, issue) lookups so a body with multiple references to the
  # same issue makes one gh call, not N (CLAUDE.md §15).
  issue_label_cache: dict[tuple[str, int], list[str]] = {}
  ...
  		labels = label_lookup(lookup_repo, issue)
  ```
  Since the workflow started linting every commit message for PRs into the default branch (`lint-pr-body-auto-close.yml:71-98`), N grows with the commit count.
- **Proposed fix:**
  - Add a batch pre-fetch, `_fetch_issue_labels_gh_batch(repo, numbers) -> dict[int, list[str] | None]`, modelled on the aliased-query pattern of `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`.
  - Call it once per `lookup_repo` before the loop in `lint()` and seed `issue_label_cache` from it.
  - Keep `label_lookup` as the per-issue fallback for any alias that is missing or errored, so the injectable test hook at `:239-246` keeps working.
- **Safety rationale:** failure semantics change. Today one failed lookup is one `None` (exit 2 unless `--fail-open-on-lookup-error`). A GraphQL batch can return partial `errors` together with `data`, so the error-to-caller mapping is not provably identical, which breaks the "same error-handling semantics" condition.
- **Downstream signal (verification steps):**
  1. Confirm what `gh issue view <n>` returns today when `<n>` is a pull request number, and make sure the batch's `issueOrPullRequest` handling reproduces it exactly.
  2. Confirm that a per-alias GraphQL error (for example, an inaccessible cross-repo issue) maps to `None` for that issue only, and that the lint's exit code stays 2 in that case.
  3. Confirm that `labels(first:100)` matches what `gh issue view --json labels` returns for issues with more than 100 labels, or accept and document the bound.

**MERGE-002 — The candidate follower's three REST reads could be one GraphQL query, but the reads guard the freeze tag**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:**
  - `scripts/promote_candidate_follow.sh:148` (`repos/${REPO}/branches/promote-candidate`)
  - `scripts/promote_candidate_follow.sh:168` (`repos/${REPO}/git/ref/tags/promote-cycle-frozen`)
  - `scripts/promote_candidate_follow.sh:181` (`repos/${REPO}/git/ref/heads/main`)
  - plus the conditional annotated-tag read at `:210` (`repos/${REPO}/git/tags/${TAG_OBJECT_SHA}`)
- **Current call count:** 3 reads per run, 4 when an annotated tag exists (budget documented at `:22-25`).
- **Proposed call count:** 1 GraphQL query: `ref(qualifiedName:"refs/heads/promote-candidate"){target{oid}}`, `ref(qualifiedName:"refs/tags/promote-cycle-frozen"){target{__typename oid ... on Tag{message}}}`, `ref(qualifiedName:"refs/heads/main"){target{oid}}`.
- **Evidence:** all three reads run back to back in step 1 (`:145-184`). Only scalar fields are used: `commit sha` and `protected` (`:156-161`), `object type/sha` (`:177-178`) and `object sha` (`:183`).
- **Proposed fix:** none for automatic implementation. The data shape is mergeable, but see the rationale.
- **Safety rationale:** this code defends against an upstream race (the promote-cycle freeze). It fails closed on a REST 404 versus other statuses via `follow_is_not_found` and `GH_API_RETRY_LAST_STATUS` (`:116-119`, `:173-174`); GraphQL reports a missing ref as `null`, not 404. The REST `branches/<b>.protected` flag has no verified GraphQL equivalent that also covers rulesets. The finding also runs against Deep Audit BUG-004, which adds a second tag read right before the PATCH.
- **Downstream signal:** do not auto-implement. A human must first decide BUG-004 (the tag re-read before the PATCH), then confirm a ruleset-aware GraphQL substitute for `protected`, then confirm that `null` versus error handling keeps the fail-closed `tag_read_failed` path.

**MERGE-003 — `plan_split_dispatch.sh` lists the issue's comments up to three times**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:**
  - `scripts/plan_split_dispatch.sh:102-105` (dedupe pre-check)
  - `:231-232` (only when posting the marker fails)
  - `:251-252` (reservation re-read after the marker POST)
  - all three are `GET repos/${REPO}/issues/${ISSUE_NUMBER}/comments?per_page=100 --paginate`
- **Current call count:** 2 paginated listings on the success path, 2 on the marker-failure path.
- **Proposed call count:** none proposed.
- **Evidence:** the pre-check projects `{login, body}`; the later reads project `{id, login, body}`. The pre-check could carry `id` too, but the later reads exist to see comments posted after the pre-check:
  ```bash
  # The marker is the reservation: two plan runs racing past the check above
  # both post one, and only the run whose marker is the first since the last
  # release marker dispatches.
  ```
  (`:247-250`).
- **Safety rationale:** the calls are paginated, and the second read is an explicit defence against a concurrent plan run, made after a write (the marker POST at `:222`). Merging it with the pre-check would remove that race protection.
- **Downstream signal:** do not auto-implement. Only a human may widen the `:102` projection to include `id`, for log parity. The two post-write reads must stay as they are.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001 — `plan_split_dispatch.sh` reads the repository to get `default_branch`, which plan.yml already has from the event payload**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Calls involved:**
  - `scripts/plan_split_dispatch.sh:127-128` (`GET repos/${REPO}` with `--jq '.default_branch // ""'`), consumed only at `:272` (`gh workflow run "${wrapper}" --ref "${default_branch}"`)
  - caller: `.github/workflows/plan.yml:2829-2836` ("Auto-approve clear plan" step, env at `:2593-2603`)
- **Current call count:** 1 per plan split.
- **Proposed call count:** 0 when the event payload has the field; 1 as a fallback.
- **Endpoint:** `GET /repos/{owner}/{repo}`.
- **Evidence:**
  - plan.yml already uses the payload value in other steps: `PLAN_DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}` (`plan.yml:922`), and `ref: ${{ steps.refctx.outputs.ref || github.event.repository.default_branch }}` (`:313`, `:321`, `:1336`).
  - The library wrapper `internal-plan.yml` triggers only on `issue_comment` (`internal-plan.yml:4`), whose payload carries `repository`.
  - The repository already has this exact pattern: `update_workflows.yml:1341-1344` (`BASE="${DEFAULT_BRANCH:-}"`, with the API read only when it is empty).
- **Proposed fix:**
  - Add `PLAN_SPLIT_DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}` to the Auto-approve step's `env:` (`plan.yml:2593-2603`). Put it in `env:`, not the `run:` body, so the expression-size guard noted at `:2601-2602` still holds.
  - In `plan_split_dispatch.sh:127`, use `default_branch="${PLAN_SPLIT_DEFAULT_BRANCH:-}"` and call the existing `gh_retry gh api "repos/${REPO}"` read only when it is empty or fails the `^[A-Za-z0-9._/-]+$` check. This gives the new variable a default in the helper itself (§8).
- **Safety rationale:** the endpoint differs (event payload versus REST), so the "same endpoint" condition is not met, and plan.yml is a reusable workflow whose consumer callers may use event types this pass did not read.
- **Downstream signal (verification steps):**
  1. List every caller of `plan.yml` (`internal-plan.yml`, the consumer templates under `workflow-templates/`, and any `workflow_dispatch` path). Confirm each event type carries `github.event.repository.default_branch`; the API fallback covers any that do not.
  2. Confirm with `wc -c` that the edit keeps `plan.yml` well under the 480,000-byte guard (§24).
  3. Confirm `tests/` has no contract test that pins the PLAN_SPLIT API budget.

**REUSE-002 — The plan job reads the pipeline login (`gh api user`) up to three times**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:**
  - `.github/workflows/plan.yml:995` ("Close superseded CI-repair issue" step, `GH_TOKEN: secrets.GH_PAT` at `:917`)
  - `.github/workflows/plan.yml:2757` or `:2792` (`plan_gate_login` in "Auto-approve clear plan", the same `secrets.GH_PAT` at `:2594`)
  - `scripts/plan_split_dispatch.sh:98` (`pipeline_login`, called from the same Auto-approve step at `plan.yml:2833-2836`)
- **Current call count:** 1–3 per plan run, depending on which branches run.
- **Proposed call count:** 1–2. Passing `plan_gate_login` into the script's environment in the same shell would save the `:98` read whenever `:2757` or `:2792` already resolved it.
- **Endpoint:** `GET /user`.
- **Evidence:** all three reads use the same token and return the same immutable login. `plan_split_dispatch.sh` is launched as a child of the shell that may already hold `plan_gate_login` (`plan.yml:2833-2836`).
- **Safety rationale:** this is an identity check that binds trust. `:98` decides which account's marker comments count for dedupe (`:106-111`), and `:995` "binds trust to the pipeline account". Reusing across steps through `GITHUB_ENV` would also cross the model-run step ("Run Codex planning", `plan.yml:1664`), which could write to that file.
- **Downstream signal:** do not auto-implement. If pursued, limit it to the same-step path (a `PLAN_SPLIT_PIPELINE_LOGIN="${plan_gate_login}"` prefix on the `bash scripts/plan_split_dispatch.sh` invocation, with `:98` as the fallback). A security reviewer must confirm that nothing between `:2757`/`:2792` and `:2836` can change `plan_gate_login`. Never pass the login through `GITHUB_ENV`.

**REUSE-003 — The CI-cancelled rerun helper lists open PRs again after the sweep step already did**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:**
  - `.github/workflows/review_autofix_sweep.yml:228-239` (paginated `GET repos/${REPOSITORY}/pulls?state=open`, written to `${RUNNER_TEMP}/ci-cancelled-pr-snapshot.json`)
  - `scripts/ci_cancelled_rerun.py:117-145` (`current_pr_heads`: `gh api --paginate --slurp repos/{repo}/pulls?state=open&per_page=100`), run by the same job at `review_autofix_sweep.yml:590-598`
- **Current call count:** 2 paginated listings of the same endpoint per sweep tick.
- **Proposed call count:** none proposed. The refresh is deliberate.
- **Evidence:** the helper explains why it re-lists:
  ```python
  # The sweep's earlier /pulls listing predates review dispatch; its
  # review-run cache has no live PR heads. Refresh once for all PRs,
  # not per PR; a failed/partial page cannot authorize a rerun.
  ```
- **Safety rationale:** the listing is paginated, deliberately fresh, and gates an Actions write (`rerun-failed-jobs` at `:168`). A stale head could authorize a rerun on a superseded SHA. Several steps run between the two reads, including review dispatches and the backfill.
- **Downstream signal:** do not auto-implement. Keep both reads. A human could reconsider only if the two steps were merged so that no dispatch happens between the reads, and that is out of scope here.

Audited and already compliant (no finding):
- The backfill passes its login to the autofix reporter through `AUTOFIX_FAILURE_MARKER_AUTHOR="${LOGIN}"` (`scripts/workflow_failure_heal_non_retryable_backfill.sh:404`), and the reporter uses it before its own `gh api user` fallback (`scripts/workflow_failure_heal_autofix_report.sh:216-228`).
- The default-branch read is memoised per process in `scripts/review_merge_train.sh:1137-1149` (`MT_DEFAULT_BRANCH`) and per shell in `scripts/gh_helpers.sh:1910-1931` (`_autofix_review_default_branch_prime`).
- The second `gh api user` in `scripts/workflow_failure_heal_report.sh:210` runs only when the shared read at `:172` returned nothing.

### Dead Calls (DEAD-API-###)

No findings. In every call this pass read, the result is used later (for example, `protected` at `promote_candidate_follow.sh:158-162` feeds `follow_emit`, and `default_branch` at `plan_split_dispatch.sh:127` feeds `:272`). No dead call could be proven from static reading.

### Cross-References to Deep Audit Section
- API-001: NEEDS_VERIFICATION — the fix replaces `repos/{repo}` with a field from another endpoint (`pulls[].base.repo.default_branch`), so the same-endpoint condition fails. The call guards #6629 provenance with "no fallback" (`review_autofix_sweep.yml:278-290`). Two things to confirm: the projection must read the field before the `draft == false` filter or document that an all-draft tick exits at `:273` first; and the event payload is not a safe substitute, since `:147` already falls back with `|| github.ref_name` on scheduled ticks.
- BATCH-001: RISKY_SKIP — the listings are paginated and defend against the #3895 pending-run duplicate-dispatch race (`:322-330`, plus `group_by(.id)` across sequential status fetches at `:399-402`). A repo-wide listing also changes how many pages are read during bursts.
- API-002: RISKY_SKIP — the fix changes the retry loop itself (`gh_helpers.sh:1271-1335`, `:1350-1403`). A human must check each caller's 404 handling, for example `_report_auth_reject issue_not_found` at `workflow_failure_heal_intake.sh:496`, and the `tg_helpers.sh` GETs.
- BATCH-002: NEEDS_VERIFICATION — GraphQL `pullRequests(headRefName:)` does not scope to the head owner the way the REST `head=<owner>:ai/issue-<n>` filter does (`workflow_failure_heal_pr_reconcile.sh:239-247`). The batch must also select `headRepositoryOwner{login}` and filter on it, or fork PRs with the same branch name will match.
- BATCH-003: NEEDS_VERIFICATION — agree; GraphQL `Commit.author.user` versus REST `.author.login` attribution for squash and bot commits must be confirmed first.
- BATCH-004: RISKY_SKIP — this is a poll loop with a rate-limit backoff guard (`test-and-mark-stable.yml:1078-1117`). BUG-001 must land first, and the change needs a human-reviewed release-gate run.
- API-003: NEEDS_VERIFICATION — this is a schedule-frequency change, not a merge. The listings are paginated (`cancel_on_pr_close.yml:167-186`), and a merge-train release latency of up to 15 minutes needs owner sign-off. Coordinate with BUG-003's group split.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | MERGE-001, REUSE-001 |
| RISKY_SKIP | 4 | MERGE-002, MERGE-003, REUSE-002, REUSE-003 |

Counts cover only the findings raised in this pass. The Deep Audit items reviewed above are tagged under Cross-References: API-001, BATCH-002, BATCH-003 and API-003 as NEEDS_VERIFICATION; BATCH-001, API-002 and BATCH-004 as RISKY_SKIP.

### Implement-Stage Handoff
No SAFE_TO_MERGE findings in this pass.