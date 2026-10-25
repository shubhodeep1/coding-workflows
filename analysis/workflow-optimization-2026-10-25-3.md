## Executive Summary

**Data scope.** The full-logs directory `/home/runner/work/_temp/workflow-log-output` was not mounted in this sandbox. It does not exist, so the `errors/`, `slow/` and `recent/` folders could not be read. All run-level evidence below comes from `analysis_context.json`: the cost telemetry and the gpt-6-luna `log_summary` fields. Code-level claims were checked against the repository. `insufficient_data=false`. The window is 1000 runs in one repo, `shubhodeep1/coding-workflows`, on 2026-10-25 from about 13:10 to 18:42 UTC.

- **Three review runs account for every logged model token.** Review/autofix runs 40204106165, 40197189973 and 40197313802 add up to exactly 36,313,289 OpenRouter tokens over 34 calls. Run 40204106165 alone used 27.06M tokens in 12 calls, about 2.26M per call, which is 74.5% of the window. Two of these three runs logged `CONTEXT_BUDGET_WARN`. Capping prompt re-expansion in review calls is the largest cost lever: an estimated 20–40% of review tokens. *Confidence: high on the numbers, medium on the cause.*
- **The AI-memory finalize step spends about 99s on a full repo clone.** Run 40219084559 logged `clone_ms=99402` out of 219s, because `scripts/ai_memory_lib.py:3211` clones the whole repo only to check out the `ai-memory` branch. Run 40219226696 shows a similar 136s gap (inference: same cause). Cloning only the memory branch should save an estimated 60–90s per merged PR. *Confidence: high on the evidence, medium on the size of the saving.*
- **CI is the only source of failures, and the failing step cannot be diagnosed.** Both failures (40206780460 and 40209629177) are in `tests-heal-plan-and-validation` / "Clarify loop guard unit tests". That step (`.github/workflows/ci.yml:1223-1241`) runs 16 test scripts one after another under `set -e` and prints nothing to say which file is running. Adding per-file start/result lines is cheap, and it lets the failure-heal and triage tooling target the right file on the first attempt. *Confidence: high.*
- **The `heal-pr-reconcile` job fetches full history on every PR close, even when it does nothing.** `internal-cancel-on-pr-close.yml:50` uses `fetch-depth: 0`. The script then exits at the `no_heal_issue` check (`workflow_failure_heal_pr_reconcile.sh:104`), before its own targeted fetches at lines 159 and 165. In runs 40219084575 and 40219226407, about 100–120s of the 132s and 115s runtimes went to fetching tags and history. Fixing this saves an estimated 100s of runner time per PR close. *Confidence: high.*
- **A no-op follow-up issue burned about 32 minutes of AI pipeline time.** The unblock judge on #7995 (40218766312) returned `accept_with_followup` with `paths=none`. Issue #8154 ("Follow-up to #7995") then went through clarify (547s), plan (800s) and implement (583s), and implement closed it as a success-no-op (40220171004). The judge-to-#8154 link is an inference from timing and title. Gating follow-up creation on `paths=none` would avoid about 1,930s of AI compute per occurrence. *Confidence: medium.*
- **Telemetry blind spots hide the biggest consumers:**
  - 6 of 34 OpenRouter calls (17.6%) reported no usage.
  - The aggregate `cache_hit_rate` is null.
  - `GH_PAT_BUDGET used_in_job=unknown` appears on long jobs.
  - Poller, plan and review logs lose 450k–1.36M characters to truncation.

  *Confidence: high.*
- **Three queued runs have been stuck for 6–10 weeks.** Every scheduled cancel-cleanup run warns about runs 34748435167, 34748435180 and 32215723558 and keeps them (seen in 40218931293, 40220833083 and 40222059916). The impact is low, but the warning repeats on every run. *Confidence: high.*

## Speed Optimizations

Ranked by expected latency reduction.

1. **CI burst tail (critical path, root cause unconfirmed).**
   - **Evidence:**
     - CI p50 is 883s, but p95 is 3507s.
     - Runs created between 13:18 and 13:34 UTC took 3000–3900s: 40196997178 (3892s), 40197231753 (3609s), 40197487357 (3473s), 40197650736 (3397s), 40198222187 (3241s) and 40197235036 (3093s).
     - Runs at the same time of day later in the afternoon took about 840–880s (40220485194, 40219226058).
     - Review/autofix in the same burst also ran long: 40197189973 (4427s) and 40197481394 (3596s).
   - **Root cause (inference):** runner-pool contention during a dispatch burst, not slower tests. Run 40219226058 logged about 2.5 minutes of runner wait even outside the burst.
   - **Change:** add a job-start line (`CI_JOB_START queued_ms=<started_at−created_at>`) in a first step of each CI job so queue time can be separated from compute time. Then consider a `concurrency` group keyed on the PR head for CI on `pull_request`, so superseded pushes are cancelled. Check first that the existing CI does not already rely on running every push.
   - **Estimated saving:** if contention is confirmed, cutting superseded runs could take 30–50 minutes off the p95 tail.
   - **Risk:** low for the logging line, medium for concurrency cancellation.

2. **AI-memory finalize clone (on the post-merge path).**
   - **Evidence:** run 40219084559 `clone_ms=99402` vs `push_ms=4243` and `write_ms=2452`; run 40219226696 had a 136s gap before finalize.
   - **Root cause:** `_clone_for_memory_branch` (`scripts/ai_memory_lib.py:3208-3224`) runs a full `git clone --no-tags` of the default branch and only afterwards fetches `refs/heads/<memory_branch>`.
   - **Change:** when `ls-remote` finds the branch, clone only that branch. Keep the existing full-clone path as the fallback when the branch is missing. Add a `clone_mode` field to the returned telemetry dict (additive).
     ```python
     probe = _run_git(temp_dir.parent, ["ls-remote", "--heads", origin_url, memory_branch], check=False)
     if probe.returncode == 0 and probe.stdout.strip():
         _run_git(temp_dir.parent, ["clone", "--no-tags", "--quiet", "--single-branch",
                                    "--branch", memory_branch, origin_url, str(temp_dir)])
         return temp_dir  # HEAD already on memory_branch tracking origin
     # else: existing full-clone + checkout -B path unchanged
     ```
   - **Estimated saving:** 60–90s per `finalize-task`. Each issue-PR status sync (9 runs in the window) runs one.
   - **Risk:** low to medium. The push-retry rebase only touches the memory branch, but `test_ai_memory_push_retry_backoff.py` and `test_ai_memory_git_env_isolation.py` must still pass.

3. **`heal-pr-reconcile` full-history checkout (off the critical path; runner minutes only).**
   - **Evidence:** runs 40219084575 and 40219226407 ended in `WORKFLOW_HEAL_PR_RECONCILE noop reason=no_heal_issue`. About 2 minutes went to a checkout that fetched every `v1.x` tag.
   - **Change:** set `fetch-depth: 1` at `internal-cancel-on-pr-close.yml:50`. In `workflow_failure_heal_pr_reconcile.sh`, after the no-op return at line 104 and before line 159, add `git fetch --quiet --unshallow --no-tags origin || true` so the active path keeps full history.
   - **Estimated saving:** about 100s per PR close (2 of 2 sampled closes were no-ops).
   - **Risk:** low on the no-op path, medium on the active path (test the rebase flow).

4. **Semble bootstrap inside review.**
   - **Evidence:** 5 bootstraps took 220,720 ms in total, about 44s each. Run 40219382352 spent 95,306 ms of its 968s on 2 bootstraps (about 10%). No bootstrap failed and none went unused.
   - **Change:** keep `SEMBLE_BOOTSTRAP_MODE=lazy`, and investigate why one review run bootstrapped twice. Adding a `reason=` field to the second `SEMBLE_BOOTSTRAP` line would show whether the index was rebuilt for the same head.
   - **Estimated saving:** about 45s per review that currently bootstraps twice. This is a micro-optimization.

5. **Slow orchestrate-poll CI tests (micro-optimization).**
   - **Evidence:** in 40220485194 the `orchestrate-poll (0)` suite took about 9 of 14 minutes. The slowest tests each took 13.6–22.0s, led by `test_integration_stale_alert_window_clears_when_branch_catches_up` at 21,970 ms.
   - **Change:** add a shard to the existing matrix, or mark the top 10 tests (at least 13s each) as their own shard.
   - **Estimated saving:** 2–4 minutes of CI wall time.
   - **Risk:** low.

## Cost Optimizations

Ranked by expected token savings.

1. **Prompt re-expansion across review calls.**
   - **Evidence:**

     | Run | Calls | Prompt tokens | Cache read | Cache write | Completion | Total | `CONTEXT_BUDGET_WARN` |
     |---|---|---|---|---|---|---|---|
     | 40204106165 | 12 | 2,524,520 | 23,860,113 | 571,844 | 107,852 | 27,064,147 | 1 |
     | 40197189973 | 12 | 1,990,161 | 2,993,859 | 554,414 | 57,503 | 5,595,812 | 1 |
     | 40197313802 | 10 | 1,333,635 | 1,886,402 | 418,484 | 14,809 | 3,653,330 | 0 |

     Completion tokens are 0.5% of the total, a ratio of about 200 input tokens to 1 output token.
   - **Root cause (inference):** each reviewer, aggregator or editor call re-sends the full PR plus repository context, and the context grows with each round. The budget warnings show prompts are near 70% of the model's context window (`DEFAULT_CONTEXT_BUDGET_WARN_RATIO = 0.7`, `scripts/cost_audit.py:207`).
   - **Change:** first add one line per call so the growth can be measured, for example `REVIEW_CALL_TELEMETRY role=<reviewer|aggregator|editor> round=<n> prompt_bytes=<n> diff_bytes=<n> context_bytes=<n>`. A new prefix must be registered through an `agents.d/` fragment under "Stable log prefixes (contractual)", per §25. Then cap the attached repository context per round, for example by reusing `TARGETED_FILE_CONTEXT_MAX_BYTES=102400`, which is already set in the poller env.
   - **Estimated saving:** 20–40% of review tokens. In a run like 40204106165 that is about 5–10M tokens.
   - **Quality risk:** medium. Capping context can hide relevant files, so roll out behind a flag that defaults to the current behaviour.

2. **Cache fragmentation in the smaller review runs.**
   - **Evidence:** the cache-read share is 88.5% in 40204106165 but only 54.1% in 40197189973 and 51.8% in 40197313802 (the collector-reported `cache_hit_rate=0.518453` matches the formula `read/(prompt+read+write)`). In the two smaller runs, cache writes are 418k–554k tokens.
   - **Root cause (inference):** with only 10–12 calls, the varying part of each prompt sits early enough to break the shared prefix.
   - **Change:** keep the system prompt, rubric and static repository context at the start of the prompt, and put round-specific findings and the diff at the end. Confirm the result with the per-call telemetry from item 1.
   - **Estimated saving:** raising smaller runs from about 52% to about 75% cache reads would save an estimated 0.5–1M uncached prompt tokens per run.
   - **Quality risk:** none.

3. **The no-op follow-up loop (avoidable rerun).**
   - **Evidence:**
     - The `#7995` judge logged `verdict=accept_with_followup` and `UNBLOCK_TARGET_REPO_GUARD ... paths=none`.
     - The `/answer` on the #7995 plan says the fix is already in the checked-out code (40218877169).
     - #8154 then ran clarify (547s), plan (800s) and implement (583s), and ended in "Codex success-no-op flag detected; closing #8154".
   - **Change:** when the guard reports `paths=none`, record the follow-up as a comment instead of opening a new issue, or emit `UNBLOCK_FOLLOWUP skip reason=no_paths` and let the clarify phase do a cheap no-op check.
   - **Estimated saving:** about 1,930s of high-reasoning model time per occurrence. Token counts are not visible because those logs were truncated.
   - **Quality risk:** low.

4. **Semble context volume.**
   - **Evidence:** 5 queries returned 70,239 logged bytes from 38 sources, about 14KB per query. `static_dup_bytes_total=0`, so Semble is not duplicating static context.
   - **Assessment:** about 17.5k tokens of Semble context against about 36M review tokens, which is 0.05%. Semble is not adding noise, and it is too small to explain the prompt growth. The data cannot show whether it replaces broader file attachment.
   - **Change:** log `context_bytes_without_semble` alongside each query so the replacement effect can be measured.

5. **Serena.** `SERENA_ENABLED=false` in every summary that reports config, and every Serena counter is 0. There is nothing to optimize.

6. **Model selection.**
   - **Evidence:** the `UNBLOCK_JUDGE` run (40218766312) had `UNBLOCK_JUDGE_MODEL=openai/gpt-6-sol`, but its usage was reported as `claude-opus-5-5`: $0.1868, 125,154 cache-read tokens against 13,688 cache-write tokens.
   - **Assessment:** the cost is trivial and the cache works well. The inference is that the engine resolved to Claude, which makes the model variable misleading.
   - **Change:** emit `AI_ENGINE_RESOLVED role=UNBLOCK_JUDGE engine=… model=…` next to the existing `CLAUDE_POOL run` line.

## Reliability Improvements

Ranked by expected reduction in failures and reruns.

1. **The "Clarify loop guard unit tests" step cannot be diagnosed.**
   - **Evidence:** both CI failures in the window (40206780460 at 15:24 and 40209629177 at 16:00) are in this step. The review/autofix runs created at the same timestamps (40206780589 and 40209629189) succeeded, and later CI runs passed, so the inference is that autofix fixed a PR-level regression.
   - **Root cause category:** observability gap. The 16 scripts run one after another under `set -euo pipefail`, so the first failing script aborts the rest and nothing says which one it was.
   - **Fix:** keep the step's `name:` and the script order, and wrap each invocation:
     ```bash
     set -euo pipefail
     run_t() { local f="$1"; shift; local s=$SECONDS
       echo "CI_TEST_FILE start file=${f}"
       if "$@"; then echo "CI_TEST_FILE result=pass file=${f} secs=$((SECONDS-s))"
       else rc=$?; echo "::error::CI_TEST_FILE result=fail file=${f} rc=${rc} secs=$((SECONDS-s))"; exit "${rc}"; fi; }
     run_t tests/test_phase_wrapper_predicate_contract.py env PYTHONDONTWRITEBYTECODE=1 python3 tests/test_phase_wrapper_predicate_contract.py
     # ...same for the remaining 15 lines
     ```
     The new `CI_TEST_FILE` prefix needs an `agents.d/` fragment (§25). Check the `ci.yml` size against the 480,000-byte guard (§24).
   - **Expected impact:** the triage and failure-heal tooling can target the failing file directly, saving an estimated one repair round per CI failure.
   - **Rollback:** the change only adds output, and the exit code is unchanged.

2. **OpenRouter calls with no usage data (17.6%).**
   - **Evidence:** `or_usage_unavailable_calls=6` of 34, three in 40204106165 and three in 40197189973. The token totals in this report are therefore lower bounds.
   - **Fix:** when usage is missing, log the response's generation id and status (no content) so the call can be looked up later.
   - **Impact:** keeps cost accounting from undercounting. Keep the missing-usage path fail-open.

3. **`CONTEXT_BUDGET_WARN` pressure.**
   - **Evidence:** 2 warnings, one each in 40204106165 and 40197189973. Both runs were in the slow set (3383s and 4427s). `BREAK_GLASS_count=0` everywhere, so there is no sign of policy or rubric pressure.
   - **Assessment:** this is prompt-size risk, not rubric risk. A model with a smaller context window, or one more round, could overflow.
   - **Fix:** same as Cost item 1. Also log `round=` in the warning line through a new field. Fields in `CONTEXT_BUDGET_WARN_RE` are positional with a terminal `(?:\s|$)`, so a trailing field parses safely. Verify with `tests/` before shipping.

4. **Semble fallbacks are healthy.**
   - **Evidence:** all 56 `SEMBLE_FALLBACK` lines are counted as contract-test fallbacks, at 4 or 8 per CI run (for example 40219084771 and 40220485194). `semble_runtime_fallbacks=0` and `semble_bootstraps_failed=0`.
   - **Assessment:** these are fixture output from the test suite, not a broken rollout. One gap: the poller runs (40220809286 and 40218947492) show `SEMBLE_ENABLED=true` with `SEMBLE_AVAILABLE=false` and no `SEMBLE_*` line, so it is silently unavailable.
   - **Fix:** emit one `SEMBLE_BOOTSTRAP outcome=skipped reason=unavailable target=orchestrate_poll` line, so it is clear whether the poller is meant to have Semble.

5. **Stale queued runs.**
   - **Evidence:** runs 34748435167 and 34748435180 (since 2026-09-13) and 32215723558 (since 2026-08-19) are reported as "preserved … without pull-request linkage" on every scheduled cleanup.
   - **Fix:** behind a new variable defaulting to off (§8), cancel unlinked `queued` runs older than 14 days. Log each one as a structured line with run id and age in days.
   - **Impact:** removes a recurring warning and frees stuck concurrency slots.
   - **Rollback:** turn the variable off.

6. **Cosmetic git warning.** `##[warning]The process '/usr/bin/git' failed with exit code 1` ("git-submodule cannot be used without a working tree") appears in 40220171004 and 40219382352. It is harmless, but it adds noise to warning-based triage. Fix the post-job submodule cleanup for `.codex-workflow-src`, for example by setting `submodules: false` on that checkout or adjusting the clean option. Impact is low.

## AI Memory Health

- **Telemetry found:** 2 `AI_MEMORY_TELEMETRY` lines, both `op=finalize-task`:
  - Issue 8150 (run 40219084559): `ok=true`, `did_push=true`, `push_attempts=2`, `clone_ms=99402`, `push_ms=4243`, `write_ms=2452`.
  - Issue 8151 (run 40219226696): `ok=true`, `did_push=true`, `push_attempts=1`.
- **Not found:**
  - No `retrieve`, `record-candidate`, `record-run-event`, `promote`, `compact` or `processed-command-*` lines appear in any summary. The retrieve hit rate, `estimated_tokens` against budget, and the `keyword_method` split (llm/plain/none) **cannot be computed**.
  - No `fail_open: true` or `enabled: false` entries were seen.
- **Push contention:** one retry in two pushes (8150) is within the jittered-backoff design (`ai_memory_lib.py:3227-3238`), so this is healthy.
- **Lineage gap:** the lineage record for 8150 still lists implement run 40203856619 as `in_progress` after merge. This may be a stale lineage state.
- **Data gap:** the plan, clarify and implement logs, where retrieves would happen, lost 201k–914k characters to truncation (40218877169, 40219255277, 40220171004).
- **Recommendation:** emit a one-line `AI_MEMORY_TELEMETRY` summary (count of retrieves, records selected, tokens) in a final step of each phase, so it survives middle truncation. Also check that `retrieve` is emitted at all, by grepping one untruncated plan log.
- **Speed fix:** the `clone_ms` fix from Speed item 2 is the main memory-system win.

## GH API Call Audit

- **No rate-limit events:** no HTTP 429, secondary-limit or `GH_API_RETRY` retryable events appear in any summary. The only `GH_API_RETRY` lines are expected permanent 404s on `git/ref/tags/promote-cycle-frozen` (40221414488 and 40219084780), one call per run.
- **Measured per-job usage (`GH_PAT_BUDGET used_in_job`):**
  - Review autofix sweep: 6 calls (40222043768) and 8 calls (40219183073).
  - `post-merge-validate-dispatch`: 7 calls (40219084696).
  - Auto-close lint: one paginated call (40220485180).

  All are small and need no change.
- **Shared PAT drain rate (inference):**
  - In the window with `reset=1792952091`, `remaining` fell 3381, then 3352, then 3293 between 17:59 and 18:01, so about 1,700 calls had already been used that hour.
  - In the window with `reset=1792955813`, it fell from 4972 (about 18:19) to 4561 (18:36): about 411 calls in roughly 17 minutes, or about 1,450 per hour.

  That is about 30–35% of the hourly budget. The largest consumer cannot be identified, because the poller logs, the most likely candidate, lost about 873k characters to truncation (40218947492).
- **`used_in_job=unknown`:**
  - **Evidence:** `codex-agent` in 40219382352, and `post-merge-force-poll` and `post-merge-validate-dispatch` in 40219226400.
  - **Cause:** `scripts/gh_helpers.sh:62-73` leaves the value unknown when the start snapshot is missing, the reset window changed during the job, or `remaining` went up.
  - **Recommendation:** emit a *separate* line with the reason, for example `GH_PAT_BUDGET_DETAIL job=… used_reason=no_start_snapshot|window_rollover|refill`. Do not append a field to the existing line: the collector regex `PAT_BUDGET_LINE` (`scripts/collect_workflow_logs.py:109-112`) ends with `used_in_job=(...)$`, so any extra field would stop those lines from parsing (§10).
- **Poller hotspot visibility:**
  - Make sure the poller prints its per-cycle `GH_PAT_BUDGET phase=end` and its call-count summary in the last step, so the lines survive middle truncation.
  - Per §14, before adding calls, keep using `_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql` for per-item data.
  - No unbatched loop can be proven from the available evidence. This is a data gap, not a finding.
- **Estimated reduction:** none can be measured yet. The first step is to make the hotspot visible.

## Prompt Cache & Memory System

- **Cache hit rate:**
  - The collector's aggregate `cache_hit_rate` is null. Computed from the same formula the per-run value uses, the window-level rate for review/autofix is 28,740,374 / (5,848,316 + 28,740,374 + 1,544,742) = **79.5%**. That figure is skewed by 40204106165 at 88.5%.
  - The median across the three runs with usage data is about 54%.
  - **Collector fix:** compute the aggregate the same way in `cost_audit.py`, where `cache_hit_rate` is currently not in `AGGREGATABLE_COST_FIELDS`. Derive it from the summed token fields instead of leaving it null.
- **Fragmentation:** cache writes are 554k and 418k tokens on the smaller runs, which points to the shared prefix being broken by early dynamic content (inference). Re-order the prompt so static content comes first (Cost item 2).
- **Fail-open:** `OPENROUTER_PROMPT_CACHE_DISABLED=false` in the clarify and poller configs. No cache-failure lines were seen.
- **Claude path:** the unblock judge read 125,154 cached tokens and wrote 13,688 (about 90% reads). The npm cache hit on `claude-cli-npm-Linux-…` in 40220171004, 40219382352 and 40218766312, taking 196–203 ms.
- **Budget pressure:** the 2 `CONTEXT_BUDGET_WARN` lines both fall in the highest-token runs. Prompt growth is close to the point where the cache cannot help, because each call still pays for the uncached tail and output.
- **Expected impact:** prefix ordering plus capped context gives an estimated 15–30% fewer uncached prompt tokens and about 5–10% lower review latency (inference).

## Orchestrator Health

- **Poller:**
  - 51 runs: 30 succeeded and 21 were cancelled (41%).
  - p50 is 514s, p95 is 1324s, and the slowest was 2270s (40197468603).
  - The cancellations are probably concurrency supersession (inference), since there are no failures.
  - **Indicator to track:** the cancelled-run share and per-cycle duration, with an alert if p50 goes above 900s.
- **Clarify and plan loops:** clarify-respond had 140 of 141 runs skipped. Clarify, plan and implement had 142, 132 and 128 skipped runs. This is fan-out from comment events, not a loop: each skipped run took 1–11s. No clarification loop is visible.
- **No-op wave:** #8154's path from judge to clarify, plan, implement and no-op close (see Cost item 3) is the clearest wasted-work pattern in the window.
- **Conflict healing:**
  - Forward-merge 40219226048 hit a conflict on `.github/workflows/plan.yml` and opened PR #8155.
  - Review 40219382352 (968s, `AUTOFIX_EDITS_PUSHED=true`) and CI 40220485194 then succeeded.
  - Per conflict this costs about 30 minutes of review plus CI. Only one was seen, so the frequency is unknown.
  - **Indicator to track:** `STATUS=conflict` count per week from the forward-merge runs.
- **Stuck or terminal states:**
  - The 3 stale queued runs (Reliability item 5).
  - The lineage record still showing `in_progress` for 40203856619.
- **Observability:** the poller has `EVENTS_JSONL_ENABLED=false` and `UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED=false`, and its logs are truncated by about 873k characters. As a result, wave progression, deferrals and retries in the conflict-heal path cannot be judged from this window. This is the main gap.
- **Smallest safe fix:** print one end-of-cycle summary line from the poller, for example `ORCH_POLL_SUMMARY candidates= dispatched= deferred= heal_retries= gh_calls=` (register the prefix through `agents.d/`).

## Pipeline Flow Bottlenecks

The #8154 chain (17:56 to 18:22) is the best-observed end-to-end path:

| Stage | Run | Duration | Overhead type |
|---|---|---|---|
| Judge (#7995) | 40218766312 | 89s | compute (34s model, 14s sandbox pull) |
| Clarify | 40218876082 | 547s | compute + runner wait |
| Plan | 40219255277 | 800s | compute (high reasoning) |
| Implement | 40220171004 | 583s | compute, ended in no-op |
| Review/autofix (other PRs) | p50 1045s, p95 3207s | — | compute, dominated by tokens |
| CI | p50 883s, p95 3507s | — | queueing in bursts, then the orchestrate-poll suite |
| Post-merge status sync | 40219084559 | 219s | memory clone (99s) |
| PR close reconcile | 40219084575 | 132s | full-history checkout (~120s) |

Fixes ordered by end-to-end impact:

1. **Review prompt size**, which drives compute and tokens (Cost item 1).
2. **Separating CI queue time from compute**, then de-duplicating superseded runs (Speed item 1).
3. **Gating no-op follow-ups**, which removes whole clarify, plan and implement chains (Cost item 3).
4. **Post-merge clone and checkout trims** (Speed items 2 and 3). These are off the critical path but save 3–4 runner-minutes per merged PR.

Skipped-run fan-out (699 of 1000 runs ended as "other" or skipped) adds runner-start noise but takes 1–11s each. It is low priority.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - Review/autofix wall time: p50 1045s, slowest 4427s.
  - The CI burst tail: 3000–3900s from 13:18 to 13:34.
  - The 8–12 minute poller cycle.
- **Top failure modes:**
  - CI "Clarify loop guard unit tests": 2 of 36 CI runs (5.6%), no per-file diagnostics.
  - Stale queued runs.
  - Missing usage data on 17.6% of OpenRouter calls.
- **Highest-cost drivers:** review/autofix run 40204106165 (27.06M tokens). Two `CONTEXT_BUDGET_WARN` runs. The no-op chain for #8154 (about 32 minutes of AI compute).
- **Top 3 actions:**
  1. Add per-file `CI_TEST_FILE` markers to the failing CI step (`ci.yml:1223-1241`).
  2. Add per-call review prompt telemetry, then cap context and put static content first in the prompt.
  3. Clone only the memory branch in `_clone_for_memory_branch` (`ai_memory_lib.py:3211`) and use a shallow `heal-pr-reconcile` checkout (`internal-cancel-on-pr-close.yml:50`).

## Metrics Appendix

**Run outcomes (window: 1000 runs)**

| Scope | Runs | Success | Failure | Cancelled | Other/skipped | Failure rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| All | 1000 | 267 | 2 | 32 | 699 | 0.2% | 1 | 1170 |
| ci | 36 | 34 | 2 | 0 | 0 | 5.6% | 883 | 3507 |
| review_autofix | 48 | 44 | 0 | 2 | 2 | 0% | 1045 | 3207 |
| orchestrate_poll | 51 | 30 | 0 | 21 | 0 | 0% | 514 | 1324 |
| clarify | 146 | 4 | 0 | 0 | 142 | 0% | 1 | 10 |
| plan | 141 | 9 | 0 | 0 | 132 | 0% | 1 | 28 |
| implement | 141 | 5 | 0 | 8 | 128 | 0% | 1 | 23 |
| orchestrate_clarify_respond | 141 | 1 | 0 | 0 | 140 | 0% | 1 | 9 |
| workflow_failure_heal / intake | 59 / 58 | 0 / 7 | 0 | 0 | 59 / 51 | 0% | 1 / 1 | 7 / 17.5 |
| cancel_on_pr_close | 35 | 34 | 0 | 1 | 0 | 0% | 30 | 522 |
| validate | 2 | 2 | 0 | 0 | 0 | 0% | 923.5 | 951 |
| memory_maintenance | 1 | 1 | 0 | 0 | 0 | 0% | 1243 | 1243 |

**Tokens and cache (OpenRouter; all from review_autofix)**

| Metric | Value |
|---|---|
| or_calls (usage available / unavailable) | 34 (28 / 6) |
| prompt / completion tokens | 5,848,316 / 180,164 |
| cache read / cache write tokens | 28,740,374 / 1,544,742 |
| total tokens | 36,313,289 |
| `cache_hit_rate` (collector aggregate) | null; computed 79.5%; per run 88.5% / 54.1% / 51.8% |
| codex_tokens_used / codex_calls | 0 / 0 |
| Claude (unblock judge 40218766312) | $0.1868; 125,154 cache read; 13,688 cache write; 2,609 output |

**Review telemetry**

| Metric | Value |
|---|---|
| `wall_clock_p50_ms` (repo) | 7,000 |
| `wall_clock_p99_ms` (repo) | 3,860,870 |
| review_autofix wall_clock p50 / p99 ms | 968,000 / 4,360,520 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 2 (40204106165: 1, 40197189973: 1) |

**GH API**

| Signal | Value |
|---|---|
| HTTP 429 / secondary-limit events | 0 observed |
| `GH_API_RETRY` | 2 permanent 404s (`promote-cycle-frozen` ref probe) |
| Measured `used_in_job` | 6, 8 (sweep); 7 (validate-dispatch) |
| `used_in_job=unknown` | 3 jobs (40219382352 codex-agent; 40219226400 ×2) |
| Estimated shared-PAT drain | ~1,450 calls/hour (inference) |

**Semble**

| Metric | Value |
|---|---|
| query_calls / logged bytes | 5 / 70,239 (~14,048 per query) |
| sources_total / static_dup_bytes | 38 / 0 |
| bootstraps (failed / unused) / total ms | 5 (0 / 0) / 220,720 (avg 44,144) |
| fallbacks (contract-test / runtime) | 56 (56 / 0), all in the ci family |
| echo_lines_dropped | 0 |

**Serena and MCP availability**

| Target | query_calls | response_bytes | tool_calls | fallbacks | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|---|---|---|---|
| serena (all) | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

Other MCP servers observed: none.

**AI memory**

| op | Count | Notes |
|---|---|---|
| finalize-task | 2 | ok=true ×2; push_attempts 2 and 1; clone_ms 99,402 (one sample) |
| retrieve and other ops | 0 observed | Logs truncated, so this is a data gap |

**Data gaps**

- The `workflow-log-output` directory was not mounted.
- Logs were truncated by 47k–1.36M characters in most AI-phase runs.
- The cache-hit-rate aggregate is null.
- 6 OpenRouter calls had no usage.
- No retrieve telemetry was found.
- Poller per-cycle API counts are not visible.

**Actions taken:** read-only analysis. No files were edited.

**Assumptions:**
- The per-run `cache_hit_rate` formula is `read/(prompt+read+write)`. This was verified against run 40197313802.
- The judge-to-#8154 link and the CI runner-contention cause are inferences.

**Missing context:** the full run logs. The `log_summary` fields and the repository code were used instead.