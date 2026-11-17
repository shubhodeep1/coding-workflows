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
## Deep Audit — Workflows & Scripts (2026-10-25)

Scope: this pass goes deep rather than wide. Files read in full or at length: `scripts/gh_helpers.sh`, `scripts/label_helpers.sh`, `scripts/tg_helpers.sh`, `scripts/workflow_failure_heal_pr_reconcile.sh`, `scripts/implement_handle_guard_block.sh`, the error handling in `scripts/ai_memory.py` and `scripts/ai_memory_lib.py`, the poll loops in `scripts/orchestrate_poll_process.sh`, `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/internal-cancel-on-pr-close.yml`, `.github/workflows/sync_ai_labels.yml`, `.github/workflows/pr6576-shard-followup.yml`, and parts of `.github/workflows/implement.yml` and `.github/workflows/test-and-mark-stable.yml`.

Three items are already covered earlier in this report and are not raised again here:
- the full-history checkout in `heal-pr-reconcile` (Executive Summary and Speed item 3);
- the full clone in `_clone_for_memory_branch` (Speed item 2);
- the "Clarify loop guard unit tests" step that cannot be diagnosed (Reliability item 1).

Several findings below were already raised in earlier reports under `analysis/` and are still in the code. Each one is marked "recurring".

### Section 1: Bug & Correctness Sweep

**BUG-001**: `scripts/label_helpers.sh:172-214`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - `ensure_label_exists` sends `gh label create` through `gh_retry` (lines 192-196).
  - `_is_gh_permanent_failure` (`scripts/gh_helpers.sh:192-195`) has no pattern for gh's "already exists" error, so `gh_retry` treats it as transient. It makes 5 attempts with 1+2+4+8 = 15s of backoff (`gh_helpers.sh:1220-1227`) and logs `::error::gh command failed after 5 attempts` (line 1236) before the helper's own grep at line 206 accepts the result.
  - The repo already documents this: `scripts/implement_handle_guard_block.sh:30-33` says "gh_retry does not treat it as permanent" and works around it with a raw first attempt.
  - The process-local cache (`label_helpers.sh:155-162`, `183-187`) only covers repeat calls in the same shell. Every new process pays again. There are 168 `ensure_label_exists` call sites across 27 files.
  - Recurring: raised in `analysis/workflow-optimization-2026-10-17-3.md`.
- **Recommended fix:** use the `guard_block_ensure_label` shape (`implement_handle_guard_block.sh:35-66`):
  1. One raw `gh label create ... 2>"${err}"`.
  2. On a match for `already[ _-]*exists`, cache and return 0.
  3. Use `gh_retry` only for other failures.

  Do not add "already exists" to `_is_gh_permanent_failure`, because that would change behaviour for every caller.

**BUG-002**: `scripts/tg_helpers.sh:380-441` (`tg_cleanup_phase_msgs`) and `scripts/tg_helpers.sh:449-510` (`tg_cleanup_msgs`)
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - Both loops read `comments?per_page=100&page=${page}`, DELETE the matching comments (lines 430-434 and 501-505), then move on to `page+1` using the count from before the deletes (lines 438-439).
  - Each delete moves later comments one page earlier, so up to *k* tracking comments from the old page 2 are never visited. Those comments and their Telegram messages are left behind.
  - It matters on busy issues. The header at lines 194-197 cites 447 `tg_cleanup` comments on one issue.
  - Recurring: BUG-003 in `analysis/workflow-optimization-2026-10-21.md`.
- **Recommended fix:** collect every matching `(id, body)` first using `gh_retry gh api --paginate --slurp ".../comments?per_page=100"` (the pattern at `orchestrate_poll_process.sh:58`), then delete. Alternatively, re-read the same `page` after any page that had deletions.

**BUG-003**: `scripts/tg_helpers.sh:227-276` and `scripts/tg_helpers.sh:296-344`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - `tg_store_msg_id` and `tg_store_phase_msg_id` read the newest tracking comment, append `,${msg_id}` with `sed` (lines 261-262 and 330-331), and PATCH the whole body (lines 263-267 and 332-336). This is a read-modify-write with no compare-and-swap.
  - Two jobs on the same issue (for example clarify and the poller) can each PATCH, and the second discards the first job's message ID, so that Telegram message is never cleaned up.
  - A failed read always creates a new tracking comment (lines 241-250 and 310-318).
  - Recurring: BUG-004 in `analysis/workflow-optimization-2026-10-21.md`.
- **Recommended fix:** post one comment per message (marker `<!-- tg_cleanup:<id> -->`). Comment creates do not conflict, and cleanup already deletes every matching comment. If one comment per issue must stay, re-read after the PATCH and re-apply any IDs that were lost.

**BUG-004**: `.github/workflows/cancel_on_pr_close.yml:155-189` and `.github/workflows/cancel_on_pr_close.yml:391-427`
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - `_cpr_fetch_active_runs` pipes `gh_retry gh api --paginate ...` into `jq -s` under `set -euo pipefail` (line 146). If the runs list fails for good, the "Cancel queued/in-progress runs" step fails.
  - The next step, "Release merge-queued PRs (merge train)" (line 391), has no `if:`, so it is skipped. That step is documented as fail-open (lines 423-427).
  - The PR-state GraphQL batch, by contrast, fails open on purpose (lines 228-237).
  - The 5-minute schedule (`internal-cancel-on-pr-close.yml:6-7`) acts as a backstop, so the impact is a delay (inference).
- **Recommended fix:** add `if: ${{ !cancelled() }}` to the merge-train release step so it runs even when the cancel step fails. Optionally, wrap the two runs-list calls so that a failure logs `::warning::` and exits 0, matching the GraphQL batch's fail-open contract.

**BUG-005**: `.github/workflows/cancel_on_pr_close.yml:14-16` and `.github/workflows/internal-cancel-on-pr-close.yml:6-7`
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - The job's concurrency group is `cancel-on-pr-close-${{ github.repository }}` with `cancel-in-progress: false`, and the caller also runs every 5 minutes.
  - GitHub keeps at most one *pending* run per concurrency group. A newer pending run (a schedule tick or another PR close) cancels an older pending PR-close run. That drops the PR-specific branch cleanup and the immediate merge-train release.
  - The scheduled sweep recovers both later. The Metrics Appendix shows 1 of 35 `cancel_on_pr_close` runs cancelled, which fits this pattern (inference). [NEEDS VERIFICATION]
- **Recommended fix:** use separate groups for each trigger: `cancel-on-pr-close-${{ github.repository }}-${{ github.event_name == 'schedule' && 'sweep' || github.event.pull_request.number }}`. Cancel API calls are idempotent, so running in parallel is safe.

**SEC-001**: `scripts/ai_memory.py:1269-1283` (`cmd_actions_runs_cache_get`)
- **Severity:** Medium
- **Category:** `security`
- **Description:**
  - This is the only one of 16 `except MemoryGitError as exc` handlers in `ai_memory.py` that does not pass the text through `_sanitize_git_error` (lines 76-79). It prints `AI_MEMORY_ERROR: {exc}` to stderr (line 1281) and puts the raw text in stdout JSON (lines 1279 and 1282).
  - The exception comes from `read_memory_root_from_branch` (`scripts/ai_memory_lib.py:3484-3488`). That function embeds git's stderr after `_resolve_origin_url` has put `GH_TOKEN` into the clone URL (`ai_memory_lib.py:3202-3204`, `3161-3173`).
  - Whether git repeats the credential in its error text depends on the git version (inference). Actions masks registered secrets in logs, but the stdout JSON may be saved by callers. [NEEDS VERIFICATION]
- **Recommended fix:** use `error_text = _sanitize_git_error(str(exc))` at line 1270 and print `error_text` instead of `exc` at line 1281. Also consider a regression test that greps every `except MemoryGitError` handler for `_sanitize_git_error`.

**SEC-002**: `.github/workflows/implement.yml:1978-1991`
- **Severity:** Low
- **Category:** `security`
- **Description:**
  - The "Log checkout ref" step writes `${{ steps.baseline_refctx.outputs.branch }}` and `.status` directly into the shell body.
  - The branch value comes from the issue body. Today it is validated against `ai/reissue-baseline/pr-(\d+)-([0-9a-f]{12})-\d+-\d+` (line 1187) and checked against GitHub (lines 1205-1306) before the step emits it, so it cannot be exploited now.
  - The safety depends on a regex in another step. The same step already uses `env:` for other values (lines 1972-1974).
- **Recommended fix:** add `BASELINE_REFCTX_BRANCH: ${{ steps.baseline_refctx.outputs.branch }}`, `BASELINE_REFCTX_STATUS` and `CHECKOUT_BASELINE_OUTCOME` to the step's `env:` and refer to them as `"${BASELINE_REFCTX_BRANCH}"`. This is the "promoted to env" pattern at `test-and-mark-stable.yml:1995`.

### Section 2: GitHub API Call Redundancy Audit

**API-001**: `scripts/gh_helpers.sh:1464-1517` (`curl_gh_api`)
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:**
  - Any status outside 2xx, other than a rate limit, is retried with exponential backoff (lines 1503-1509). That includes permanent 401, 404 and 422 responses. `gh_retry` stops early on these through `_is_gh_permanent_failure` (line 1203).
  - The header comment (lines 1454-1455) still says the helper "sleeps 30 s", but the code uses `_parse_reset_header` (line 1498).
  - Callers are in `scripts/tg_helpers.sh` (lines 207, 214, 398 and 467). A deleted issue costs 5 calls and about 15s per lookup.
  - Recurring: API-002 in `analysis/workflow-optimization-2026-10-07-4.md`.
- **Calls:** 5 → 1 on a permanent 4xx.
- **Recommended fix:** in the non-rate-limit branch, return 1 at once for `http_code` in `400|401|404|422`. Keep 403 retryable, as `_is_gh_permanent_failure` (lines 184-186) already does deliberately. Correct the header comment.

**API-002**: `.github/workflows/cancel_on_pr_close.yml:364-380`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:**
  - `gh_retry gh api -X POST .../actions/runs/<id>/cancel` is treated as a non-idempotent create. `_gh_api_args_unsafe_post` returns true for any POST that is not GraphQL (`gh_helpers.sh:721-741`), so `gh_retry` makes only one attempt (lines 1171-1173).
  - Cancel is idempotent. A transient 5xx leaves the run going, and the log calls it "likely already completed" (lines 376-378).
  - The per-run loop cannot be batched: the API has no bulk cancel.
- **Calls:** 1 → up to 5 on transient errors only. On success, 1 → 1.
- **Recommended fix:** use `gh_retry --idempotent gh api -X POST "${cancel_url}"`. The flag is supported at `gh_helpers.sh:1169`.

**API-003**: `scripts/orchestrate_poll_process.sh:46-58` (`replay_failed_reclarify_commands`)
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:**
  - For each queued issue the function makes one `issues/<n>` read (line 55) and one paginated comments read (line 58).
  - The issue read only checks `state == open`, that it is not a PR, and that it has the `ai:reclarify-requeue` label (lines 56-57). The `gh issue list` at lines 46-47 seconds earlier already filters on the open state and that label.
  - Only N > 0 ticks are affected, but they cost 1 + 1 + pages calls per issue. [NEEDS VERIFICATION: whether the "live issue read" in the comment at lines 39-41 is meant as a TOCTOU guard.]
- **Calls:** 2N + pages → N + pages.
- **Recommended fix:** if a separate live read is not needed, extend the listing to `--json number,state,labels` and drop line 55. The paginated comments read must stay, because "incomplete comment history never authorizes replay" (line 59). For that reason `_fetch_blocked_issue_routing_graphql` (`comments(last: 20)`, line 128) cannot be reused.

**API-004**: `scripts/tg_helpers.sh:204-218` (`_tg_newest_comments_json`)
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:**
  - Each tracked Telegram message costs 2 reads (issue for its comment count, then the last comments page) plus one write (lines 263-274).
  - The two reads also race: a comment posted between them can leave the "last page" holding only new comments, so the marker is missed and a duplicate tracking comment is created (inference).
- **Calls:** 2 → 1 read per tracked message.
- **Recommended fix:** use one GraphQL query `repository{issue(number:$n){comments(last:100){nodes{databaseId body}}}}`, the same `comments(last: N)` shape as `_fetch_blocked_issue_routing_graphql` (`orchestrate_poll_process.sh:128`). It returns the newest page atomically.

**BATCH-001**: `scripts/orchestrate_poll_process.sh:29283-29297`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:**
  - After review-blocked handling, the poller re-reads labels for every wave issue with one REST call each, `gh_retry _safe_gh_jq "repos/.../issues/${inum}/labels"` (line 29290), and builds `LABELS_JSON` by string concatenation (lines 29294-29296).
  - The same cycle already batches this exact set through `_fetch_issue_labels_batch_graphql` (defined at line 4460, used at line 26994, 25 issues per call, misses fall back to REST at lines 27003-27010).
  - A transient failure turns into `[]` (no labels) for that issue rather than a cache miss, which hides label-based status for the cycle.
- **Calls:** N → ceil(N/25), plus REST only for misses.
- **Recommended fix:** replace the loop with `LABELS_JSON="$(_fetch_issue_labels_batch_graphql "${_wave_issue_nums_json}")"`, followed by the existing REST backfill for missing keys. That backfill is the same loop as lines 27003-27010 (see DUP-002).

**BATCH-002**: `scripts/orchestrate_poll_process.sh:7095-7111` and `scripts/orchestrate_poll_process.sh:29425-29441`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** both paths make one `issues/<n> --jq .state` REST call per blocker fix-up issue inside a `while read` loop.
- **Calls:** B → ceil(B/25).
- **Recommended fix:** batch with aliased `issue(number:){state}` GraphQL. Extend `_fetch_candidate_issue_details_graphql` (line 19677), which already returns the state field used for `ISSUE_STATES_JSON` (comment at lines 26985-26988), or add a small helper modelled on `_fetch_blocked_issue_routing_graphql` (lines 115-131). Missing keys stay "unknown", which keeps today's fail-safe defer (lines 7106-7109 and 29436-29439).

**BATCH-003**: `scripts/orchestrate_poll_process.sh:2262-2266` (`_fetch_pr_json`), called in loops at `5008-5013`, `5319-5321`, `9390-9391`, `20398-20399` and `27042-27044`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:**
  - Each candidate PR costs one `pulls/<n>` REST call.
  - The comment at lines 5010-5012 shows the §14 audit was done for one site, but five loops share the pattern.
  - The callers read REST fields (`.merged_at`, `.state`, `.body`, head and base refs), so a GraphQL replacement needs a REST-shaped adapter. [NEEDS VERIFICATION per call site]
- **Calls:** P → ceil(P/25) per loop.
- **Recommended fix:** add `_fetch_pr_json_batch_graphql <numbers_json>`. It should alias `pullRequest(number:){number state mergedAt body headRefName baseRefName headRefOid}`, map the results to `{state, merged_at, body, head:{ref,sha}, base:{ref}}`, and fall back to `_fetch_pr_json` for missing keys. Document the batching contract as at lines 102-110.

**BATCH-004**: `scripts/workflow_failure_heal_pr_reconcile.sh:239-247`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:**
  - Each matching heal issue costs one `pulls?head=<owner>:ai/issue-<n>` call. This is documented in the budget comment at lines 29-32.
  - It is usually N = 1, so the saving is small.
- **Calls:** N → 1.
- **Recommended fix:** one aliased GraphQL `repository{h<n>:pullRequests(headRefName:"ai/issue-<n>",states:OPEN,first:10){nodes{number headRefName headRefOid baseRefName}}}` across all heal issues. Act only when N ≥ 2, to keep the simple path.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001**: `scripts/orchestrate_poll_process.sh:7094-7117` and `scripts/orchestrate_poll_process.sh:29420-29449`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** the blocker-state tally is the same in both places: an open/closed/unknown `case`, counters, a `#N=state` summary, and a defer reason of "blocker status lookup incomplete" or "blocker fix-up issue(s) still open". Only the variable prefixes differ.
- **Recommended fix:** add `_tally_blocker_issue_states <blockers_json>` in `orchestrate_poll_process.sh`. It sets `BLOCKER_TALLY_OPEN`, `BLOCKER_TALLY_UNKNOWN`, `BLOCKER_TALLY_SUMMARY` and `BLOCKER_TALLY_DEFER_REASON` (new, non-clashing names), and both call sites map them to their existing variables. Combine this with BATCH-002.

**DUP-002**: `scripts/orchestrate_poll_process.sh:27003-27010`, `29300-29310` and `29698-29706`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** three copies of "for each number missing from `LABELS_JSON`, run a REST labels read, then merge it in with `jq '. + {($key): $labels}'`".
- **Recommended fix:** add `_labels_json_backfill_missing <numbers…>`, which reads and updates the global `LABELS_JSON`. Use it at all three sites and in BATCH-001.

**DUP-003**: the "Resolve workflow support ref", checkout, fallback and ensure block, repeated across 14 workflows. Examples: `.github/workflows/cancel_on_pr_close.yml:25-104` and `.github/workflows/sync_ai_labels.yml:37-92`.
- **Severity:** Medium
- **Category:** `duplication`
- **Description:**
  - There are 29 `SUPPORT_REF_WORKFLOW_JOB_JSON:` blocks across 14 workflow files and 12 "Checkout workflow support source fallback" steps across 10 files.
  - The copies have already drifted:
    - `cancel_on_pr_close.yml:36-53` resolves a protected `promote-candidate` tip; `sync_ai_labels.yml:43-44` always uses `github.sha`.
    - The fallback checkout has `continue-on-error: true` at `cancel_on_pr_close.yml:82` but not at `sync_ai_labels.yml:70-78`, so the clearer `::error::` in "Ensure workflow support source checkout" (`sync_ai_labels.yml:80-86`) cannot be reached there.
  - The body cannot move into `scripts/`, because it runs before the support source is checked out.
- **Recommended fix:** extend `tests/test_workflow_support_ref_follows_pin.py` (not reviewed in this pass) so it requires every copy to be identical after normalising the workflow filename, with an explicit allowlist for the promote-candidate variant. Bring the fallback `continue-on-error` into line. A remote composite action is not an option, because its `uses:` ref cannot be dynamic. [NEEDS VERIFICATION]

**DUP-004**: the shim `type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }`, which appears 83 times in 44 files (for example `scripts/workflow_failure_heal_pr_reconcile.sh:55`, `.github/workflows/cancel_on_pr_close.yml:148`, `.github/workflows/plan.yml` with 14 copies)
- **Severity:** Low
- **Category:** `duplication`
- **Description:** when `gh_helpers.sh` fails to source, each copy silently drops to no retry and no rate-limit handling. Nothing in the log shows the fallback happened.
- **Recommended fix:** add a single `scripts/gh_helpers_shim.sh` with `_gh_retry_shim_install()`, which defines the passthrough and prints one `::warning::gh_retry unavailable; running without retry` line. Script callers that already source from `${script_dir}` can use it. Inline workflow copies stay where the shim cannot be staged.

**DUP-005**: `.github/workflows/implement.yml:6609`, `6705` and `6835` (plus the inverse at `5379`)
- **Severity:** Low
- **Category:** `duplication`
- **Description:** the same six-term guard predicate (`steps.preflight_destructive_guard.outputs.*` and `steps.commit_changes.outputs.*` for `destructive_commit_blocked`, `scope_violation_blocked` and `staged_support_rebase_conflict`) is copied into at least three `if:` conditions. Adding a seventh guard output means editing every copy.
- **Recommended fix:** add one `if: always()` step after `commit_changes` that emits `guard_blocked=true|false`. Replace the copies with `steps.<id>.outputs.guard_blocked == 'false'` (or `== 'true'`).

**DUP-006**: `.github/workflows/test-and-mark-stable.yml:881-1020`, `1035-1312` and `1342-1695`
- **Severity:** Low
- **Category:** `duplication`
- **Description:**
  - The Phase 1, 2 and 3b wait loops each re-implement inactivity timeouts, `RATE_LIMIT_BACKOFF`, page-aware run lookups (`fetch_plan_runs_page_json` at lines 1072-1074, `scoped_implement_runs_complete` at line 1396) and status output.
  - All of them wrap `find_latest_scoped_run_field` from `scripts/comprehensive_test_and_release_gh_api.sh`.
- **Recommended fix:** move them into `scripts/stable_e2e_step_wait_phase.sh <phase> <created_after>`, following the existing `scripts/stable_e2e_step_wait_review.sh` pattern. This also clears EXPR-001 and EXPR-002.

**DUP-007**: `scripts/tg_helpers.sh:227-276` vs `296-344`, and `380-441` vs `449-510`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** the store pair and the cleanup pair are near-identical, differing only in the marker prefix and the jq selector. BUG-002 and BUG-003 therefore have to be fixed twice.
- **Recommended fix:** add `_tg_store_marker_id <issue> <marker_prefix> <msg_id>` and `_tg_cleanup_by_marker <issue> <jq_select_expr> <sed_extract_expr>`. The four public functions become thin wrappers and keep their current names and signatures (§10).

### Section 4: Expression Size Limit Risk Assessment

Context:
- The in-repo guard `tests/test_workflow_file_size_limit.py` (lines 26-32 and 66-98, run at `ci.yml:1375`) fails CI for any `run:` body that contains `${{` and reaches 18,000 characters. It also fails any workflow file at or above 480,000 bytes.
- The repo's real file-size limit is GitHub's 512,000 bytes (§24), not the 1 MB in the audit template. No workflow can exceed 800 KB while the guard holds.
- Byte counts could not be measured in this sandbox (no shell). The estimates below are inferences from line counts. The YAML block-scalar indentation (10 spaces per line) is stripped before GitHub measures the body.

**EXPR-001**: `.github/workflows/test-and-mark-stable.yml:1350-1695` ("Phase 3b: Wait for PR creation")
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:**
  - The body is about 345 lines. One inline interpolation, `IMPL_CREATED_AFTER="${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}"` at line 1388, makes the whole body count as one expression.
  - Estimated at 15,000-19,000 characters, from about 345 lines at roughly 45-55 characters after indentation stripping. CI passing implies it is below 18,000, so the likely headroom is 3,000-6,000 characters before the 21,000 limit. [NEEDS VERIFICATION: run `python3 tests/test_workflow_file_size_limit.py` with a lowered threshold.]
- **Recommended fix:** move the expression into the step's `env:` (`IMPL_CREATED_AFTER_INPUT: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}`) and set `IMPL_CREATED_AFTER="${IMPL_CREATED_AFTER_INPUT}"`. The body then contains no `${{` and is outside the limit, as already done at lines 1995 and 2094. Longer term, extract it under DUP-006.

**EXPR-002**: `.github/workflows/test-and-mark-stable.yml:1043-1312` ("Phase 2: Wait for plan to complete")
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:**
  - The body is about 268 lines, with `${{ steps.create-issue.outputs.created_after }}` inlined three times (lines 1061, 1073 and 1091).
  - Estimated at 12,000-15,000 characters, leaving about 6,000-9,000 of headroom. [NEEDS VERIFICATION]
  - The same pattern appears in Phase 1 (line 929, about 140 lines) and in "Verify all phases passed" (lines 3380-3506, many `${{ steps.*.outputs.status }}` reads). Both are estimated below 10,000 characters.
- **Recommended fix:** add `CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` to each step's `env:` and use `"${CREATED_AFTER}"`. For the verify step, move each `steps.*.outputs.status` into `env:`.

**EXPR-003**: `tests/test_workflow_file_size_limit.py:66-98`
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:**
  - The guard fails only at 18,000 characters. There is no early warning at 15,000.
  - It only scans `run:`. `if:` and `with:` values are not measured. The longest `if:` conditions sampled are about 500-600 characters (`review_autofix.yml:4191`, `implement.yml:5379`), so there is no current risk.
- **Recommended fix:** in `main()`, print a non-failing `WARN: <file> job=<id> step=<name> length=<n>` for interpolated `run:` bodies of 15,000 characters or more. Also measure `if:` strings, flagging those of 2,000 characters or more.

**EXPR-004**: `.github/workflows/implement.yml` (7,180 lines, the largest workflow), and `.github/workflows/implement.yml:1037-1310`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:**
  - The 480,000-byte guard implies `implement.yml` averages under about 66.8 bytes per line. `review_autofix.yml` was about 87 bytes per line when it hit 505,283 bytes (#4327, cited in the test docstring). So `implement.yml` is plausibly within 10% of the guard (inference). [NEEDS VERIFICATION: `wc -c .github/workflows/implement.yml`]
  - The `baseline_refctx` step holds about 275 lines of inline Python (`python3 - <<'PY'`, lines 1039-1310). It has no `${{` in the body because all inputs come through `env:`, which makes it a clean candidate to extract.
- **Recommended fix:** move the Python verbatim into `scripts/implement_baseline_refctx.py`. Keep the step's `name:`, `id:`, `env:` and outputs, per §24. This frees an estimated 12-15 KB.

### Section 5: Cross-Cutting Concerns

TODO, FIXME and HACK markers: none in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml` (searched for `TODO|FIXME|HACK|XXX:`).

**DEAD-001**: `.github/workflows/pr6576-shard-followup.yml:14-23`, `40-105`, and `docs/scripts-pending-removal.md:331-340`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:**
  - This single-use workflow runs on every push to `main`. Each run starts a `self-test` job (pip install plus pytest, lines 40-64) and a `locate` job that only checks the marker (lines 92-97), even after the follow-up is done.
  - The registry entry still says "PR number assigned when the implementation PR opens" (line 333).
  - Whether the marker has been posted on #7072 cannot be checked offline. [NEEDS VERIFICATION]
- **Recommended fix:** run the registry preflight checks (lines 337-339). If they pass, remove the workflow, `scripts/pr6576_shard_followup.py`, `tests/test_pr6576_shard_followup.py` and the registry entry in one PR (§20.F). If not, add `paths:` to the `push` trigger, or move `self-test` to `pull_request` only, and fill in the PR number.

**DEAD-002**: `scripts/gh_helpers.sh:336-341` (`gh_rate_limit_breaker_tripped`)
- **Severity:** Low
- **Category:** `dead-code`
- **Description:**
  - No caller in `scripts/` or `.github/workflows/`. The comment at lines 315-318 notes that its only consumer was removed.
  - `README.md:3744` documents it as a public helper for consumers, so deleting it would be a breaking change (§10).
  - Recurring: DEAD-001 in `analysis/workflow-optimization-2026-10-25.md`.
- **Recommended fix:** keep it. State in the README that it is a consumer-facing API with no in-repo callers, so future audits stop re-raising it.

**CONSIST-001**: `scripts/tg_helpers.sh:244-248`, `263-267`, `270-274`, `312-316`, `332-336`, `338-342`, `430-434` and `501-505`
- **Severity:** Low
- **Category:** `consistency`
- **Description:**
  - Reads go through `curl_gh_api`. Every write (POST, PATCH, DELETE) is raw `curl -s ... || true`, with no `-f`, no HTTP status check, no retry and no breaker accounting. A 403 or 5xx is indistinguishable from success.
  - The fallback at lines 26-29 turns `curl_gh_api` into plain `curl` when `gh_helpers.sh` is absent.
- **Recommended fix:**
  - Route PATCH and DELETE (both idempotent) through `curl_gh_api`.
  - Keep POST to a single attempt (`curl_gh_api` has no unsafe-POST guard, unlike `gh_retry` at `gh_helpers.sh:1171-1173`), but capture `-w '%{http_code}'` and log `::warning::` when the status is outside 2xx.

**CONSIST-002**: `.github/workflows/implement.yml:1205-1229` and `.github/workflows/implement.yml:1256-1275`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** `baseline_refctx` calls `gh pr view` and `gh api .../git/matching-refs/...` as raw subprocesses with no retry. A transient 5xx drops the baseline override (`lookup-failed` or `ref-lookup-failed`) for the whole implement run. This fails closed and is safe, but it differs from the rest of the pipeline.
- **Recommended fix:** when moving this code under EXPR-004, wrap both calls in a bounded retry (3 attempts, exponential backoff) that retries only on non-4xx failures, matching `gh_retry`. Alternatively, reuse `scripts/gh_api_retry.py`, referenced at `gh_helpers.sh:751-752`. [NEEDS VERIFICATION: interface not reviewed]

**CONSIST-003**: `.github/workflows/sync_ai_labels.yml:70-78`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** the fallback checkout has no `continue-on-error: true`, unlike `cancel_on_pr_close.yml:80-89`. A fallback failure fails the job at the checkout step, and the "Ensure workflow support source checkout" diagnostic (lines 80-86) never runs.
- **Recommended fix:** add `continue-on-error: true` to the fallback step, as covered under DUP-003.

**SHELL-001**: `scripts/tg_helpers.sh:331` and `scripts/tg_helpers.sh:420`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:**
  - `${phase}` goes into `sed` regexes unescaped (`s/<!-- tg_phase:${phase}:\(...\)/.../`), and `tg_cleanup_msgs` only matches phases of the form `phase:[a-z_]*` (line 490).
  - All current callers pass the literals `clarify` or `plan` (`clarify.yml:2117`, `plan.yml:1289`, `plan.yml:2558`, `plan.yml:2990`, `implement.yml:2553`, `orchestrate_clarify_respond.yml:1296`), so nothing is broken today.
  - A future phase containing `/`, `.`, a digit or `-` would break the store, or never be cleaned by `tg_cleanup_msgs`.
- **Recommended fix:** at the top of `tg_store_phase_msg_id` and `tg_cleanup_phase_msgs`, add the guard below, which matches the line 490 contract.

  ```bash
  [[ "${phase}" =~ ^[a-z_]+$ ]] || { echo "::warning::invalid tg phase '${phase}'" >&2; return 0; }
  ```

**DEBT-001**: recurring findings that remain unfixed in `scripts/tg_helpers.sh`, `scripts/gh_helpers.sh` and `scripts/label_helpers.sh`
- **Severity:** Low
- **Category:** `tech-debt`
- **Description:** BUG-001, BUG-002, BUG-003 and API-001 were each raised in earlier reports (`analysis/workflow-optimization-2026-10-17-3.md`, `-10-21.md` and `-10-07-4.md`), and the cited code is unchanged. Re-auditing them each day costs reviewer time without moving them forward.
- **Recommended fix:** have the API-redundancy and log-analysis pipeline open one follow-up issue per recurring finding ID, with the file and line range and this report's fix. Downstream audits can then cite the issue number instead of re-describing the finding.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 9 | BUG-001, BUG-002, BUG-003, SEC-001, API-001, BATCH-001, DUP-003, EXPR-001, EXPR-004 |
| Low | 24 | BUG-004, BUG-005, SEC-002, API-002, API-003, API-004, BATCH-002, BATCH-003, BATCH-004, DUP-001, DUP-002, DUP-004, DUP-005, DUP-006, DUP-007, EXPR-002, EXPR-003, DEAD-001, DEAD-002, CONSIST-001, CONSIST-002, CONSIST-003, SHELL-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 0 (no Critical or High findings) | Small |
| API call optimization | 5 (`scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/tg_helpers.sh`, `scripts/workflow_failure_heal_pr_reconcile.sh`, `.github/workflows/cancel_on_pr_close.yml`) | Medium |
| Code modularization | About 18 (the 14 workflows with support-ref blocks, `scripts/orchestrate_poll_process.sh`, `scripts/tg_helpers.sh`, the new `scripts/stable_e2e_step_wait_phase.sh`, `tests/test_workflow_support_ref_follows_pin.py`) | Large |
| Expression size reduction | 4 (`.github/workflows/test-and-mark-stable.yml`, `.github/workflows/implement.yml`, the new `scripts/implement_baseline_refctx.py`, `tests/test_workflow_file_size_limit.py`) | Small |
| Medium/Low fixes | About 11 (`scripts/label_helpers.sh`, `scripts/tg_helpers.sh`, `scripts/ai_memory.py`, `scripts/gh_helpers.sh`, `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/implement.yml`, `.github/workflows/sync_ai_labels.yml`, the `pr6576-shard-followup` workflow, script and test, `docs/scripts-pending-removal.md`, `README.md`) | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-25)

### Safety Tag Legend
`SAFE_TO_MERGE` means the downstream implement stage can apply the fix without further review. Every SAFE_TO_MERGE precondition was confirmed by reading the code: same endpoint and filters, same function, no mutation in between, same error handling, no concurrency boundary, cache contract preserved. `NEEDS_VERIFICATION` means the fix is probably correct but depends on facts that static reading cannot settle; the listed checks must pass before anyone acts. `RISKY_SKIP` means at least one hard trigger applies: a retry or backoff loop, an auth or permission probe, pagination, a rate-limit-sensitive path, `orchestrate_poll_process.sh` or a judge, code that explicitly defends against races, or log keys that other code greps. These are listed for visibility only and must not be auto-implemented. This pass found no SAFE_TO_MERGE items.

### Consolidation Candidates (MERGE-###)

**MERGE-001**: auto-merge label gate and PR-metadata read can share one `pulls/{n}` response
- **Tag:** `RISKY_SKIP`
- **Files:**
  - `scripts/review_enable_auto_merge.sh:154-170`: paginated labels read.
  - `scripts/review_enable_auto_merge.sh:221-228`: full PR metadata read.
  - `scripts/review_enable_auto_merge.sh:91-109` (`reviewed_head_is_current_for_labels`): head-only PR read at line 100, reached from the e2e branch at line 158.
- **Endpoints:** `GET /repos/{o}/{r}/issues/{n}/labels?per_page=100` (`--paginate`) and `GET /repos/{o}/{r}/pulls/{n}`.
- **Calls:**
  - Normal path: 1+P label pages plus 1 PR read, where P is usually 0. That is 2 calls, and 1 after the change.
  - e2e path: 1 labels read plus 1 head read, so 2 calls, and 1 after the change.
  - `ENABLE_AUTO_MERGE != true` path: unchanged at 1.
- **Evidence:** the pull-request object carries `labels`, `head.sha`, `head.ref` and `base.ref`. The script reads the labels on their own first, then the full PR object:
  ```bash
  if PR_LABELS_RAW="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/labels?per_page=100" --jq '.[].name' 2>"${_label_err_file}")"; then
  ...
  if ! _ORCH_PR_META_JSON="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" 2>"${_orch_pr_meta_err_file}")"; then
  ```
  The comment at lines 98-99 shows the author already avoided one duplicate. The remaining one is the labels read, which comes before the metadata read.
- **Proposed fix:**
  1. Move the `_ORCH_PR_META_JSON` fetch above the e2e guard.
  2. Derive `PR_LABELS_RAW` from `.labels[].name` in that response.
  3. On the e2e path, compare `.head.sha` from the same response against `INITIAL_HEAD_SHA` instead of calling `reviewed_head_is_current_for_labels`.
  4. Keep the paginated REST labels read as a fallback when the label array has 100 or more entries.
- **Safety rationale:** the labels call uses `--paginate`, which is a RISKY_SKIP trigger. The comments at lines 144-153 record a past page-2 miss (Copilot review #3 on PR #1823). This is also a fail-closed gate that protects against the e2e auto-merge race.
- **Downstream signal:** do not auto-implement. A human must first confirm that `GET /pulls/{n}` `.labels` is never truncated for PRs with many labels; the GitHub docs do not say. They must also confirm that the reorder keeps both fail-closed exits (lines 165-169 and 221-225), with their current `::warning::` text, which operators may grep.

**MERGE-002**: phase-label swap reads labels with a separate GET even though the label write responses return the label set
- **Tag:** `RISKY_SKIP`
- **Files:**
  - `scripts/label_helpers.sh:240-312` (`set_issue_phase_label_resilient`): GET at 243-244, POST at 283-284, DELETEs at 298-299.
  - `scripts/review_rb_judge.sh:954-1023` (`_resilient_phase_swap`): GET at 959-960, POST at 986-987, DELETEs at 993, reconciliation GET at 997-998.
- **Endpoints:** `GET /issues/{n}/labels` (`--paginate`), `POST /issues/{n}/labels`, `DELETE /issues/{n}/labels/{name}`.
- **Calls:**
  - `set_issue_phase_label_resilient`: 2+D, where D is the number of stale phase labels deleted. With the change, 1+D: the POST response replaces the GET.
  - `_resilient_phase_swap` with a non-terminal target: 3+D. With the change, 2+D when D ≥ 1: the last DELETE response replaces the reconciliation GET. The first GET has to stay, because it decides the terminal-label no-op before any write (lines 977-984).
- **Evidence:** the [GitHub labels REST docs](https://docs.github.com/en/rest/issues/labels) show both `POST /issues/{n}/labels` and `DELETE /issues/{n}/labels/{name}` returning a label array. The older docs say DELETE returns the labels left on the issue. Neither page says outright that the POST response holds every label on the issue.
- **Proposed fix:**
  - In `set_issue_phase_label_resilient`, POST first, compute `_stale` from the POST response, and fall back to today's GET only when that response cannot be parsed.
  - In `_resilient_phase_swap`, read terminal precedence from the last successful DELETE response, and keep the GET when D = 0 or a DELETE failed.
- **Safety rationale:** both GETs use `--paginate`. The rb-judge reconciliation GET exists to resolve a documented race between the terminal `ai:merged` label and the ready label (comments at lines 968-976, incident tele-funtoken-msg-scoring#4379). Both are RISKY_SKIP triggers.
- **Downstream signal:** do not auto-implement. Manual review needs two things:
  1. A live probe showing that the POST and DELETE responses return the full current label set, including on issues with more than 30 labels.
  2. Confirmation that the "Never downgrade a terminal phase" guarantee still holds when the reconciliation read comes from a DELETE response, whose snapshot is taken before the DELETE commits.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001**: heal reconcile sends a blind `DELETE ai:merge-queued` even though the label state is already on disk
- **Tag:** `NEEDS_VERIFICATION`
- **Files:**
  - `scripts/workflow_failure_heal_pr_reconcile.sh:243-247`: the `pulls?head=` list is written to `PULLS_FILE`.
  - `scripts/workflow_failure_heal_pr_reconcile.sh:274`: the loop's jq extracts only number, head ref, head sha and base ref.
  - `scripts/workflow_failure_heal_pr_reconcile.sh:117-136` (`_close_heal`): the unconditional DELETE at line 128.
- **Endpoints:** `GET /repos/{o}/{r}/pulls?state=open&head=…` (already made) and `DELETE /repos/{o}/{r}/issues/{pr}/labels/ai:merge-queued`.
- **Calls:** 5 writes per closed heal PR (comment, PATCH, DELETE, comment, PATCH). With the change, 4 when the label is absent, which is presumably the usual case. 5 when it is present.
- **Evidence:**
  ```bash
  gh_retry gh api --method DELETE "repos/${REPO}/issues/${heal_pr}/labels/ai:merge-queued" >/dev/null 2>&1 || true
  ```
  Each pull object in `PULLS_FILE` already carries `labels[]`, but line 274 drops them. When the label is absent, the DELETE returns 404, which `gh_retry` classifies as permanent (`gh_helpers.sh:194`), so it costs one call per close with nothing to show for it.
- **Proposed fix:**
  1. Add a 5th TSV column at line 274: `((.labels // []) | map(.name) | index("ai:merge-queued") != null)`.
  2. Pass it to `_close_heal` as a new 5th argument. Use a unique local name such as `heal_has_merge_queued`.
  3. Skip the line-128 DELETE only when that value is `false`.
  4. Keep the DELETE when the value is empty or unknown.
- **Safety rationale:** the error behaviour is unchanged because line 128 is already `|| true`. However, the `PULLS_FILE` snapshot is taken before `_retarget_heal`'s git work, which can take minutes, so a label added concurrently by the merge train would be missed. That is why this cannot be SAFE_TO_MERGE.
- **Downstream signal:** before implementing, check two things:
  1. Confirm in `scripts/review_merge_train.sh` whether `release` (run from `cancel_on_pr_close.yml:419-427`) clears `ai:merge-queued` from closed PRs. If it does, the stale-snapshot gap is covered there.
  2. Grep `scripts/` and `.github/workflows/` for anything that adds `ai:merge-queued` to a PR whose head is `ai/issue-*` while its heal issue is open. If nothing does, the snapshot is sufficient.

**REUSE-002**: the terminal `fix` branch of the review-blocked judge re-reads PR state shortly after the post-LLM guard read
- **Tag:** `RISKY_SKIP`
- **Files:**
  - `scripts/review_rb_judge.sh:1918-1940`: the guard read `_guard_pr_meta` at line 1927, which is unset at line 1940.
  - `scripts/review_rb_judge.sh:2330-2345`: `fix)` with `IS_FINAL=true`, which reads `.state` again at line 2345.
- **Endpoint:** `GET /repos/{o}/{r}/pulls/{n}`.
- **Calls:** 2 reads on this path. With the change, 1, by keeping `.state` from the line-1927 response.
- **Evidence:** the line-1927 response already contains `.state`. Between 1927 and 2345 the script makes no PR state change of its own. It only posts a comment (lines 2030 onward) and runs the head-SHA checks (lines 1978-1998).
- **Proposed fix:** before the `unset` at line 1940, store `RB_GUARD_PR_STATE` (a new, non-clashing name) from `_guard_pr_meta`. At line 2345, use it in place of the new read, falling back to the live read when it is empty.
- **Safety rationale:** this is a judge path. The guard read exists explicitly to defend against an asynchronous auto-merge completing while the judge runs (comment at lines 1918-1923), so it is a RISKY_SKIP trigger. The saving is also tiny: one call, on retry-exhausted `fix` verdicts only.
- **Downstream signal:** do not auto-implement. A reviewer must decide whether the seconds between line 1927 and line 2345 (comment post, `rb_security_severity_block`) are a race window worth a fresh read. The `merge)` branch's polling read at lines 2223-2244 is a retry loop and must stay untouched in any case.

### Dead Calls (DEAD-API-###)
No findings. These were checked and found to be consumed:
- the `.comments` count read in `scripts/tg_helpers.sh:207-213`;
- the heal-issue list in `scripts/workflow_failure_heal_pr_reconcile.sh:96-102`;
- `default_branch` in `scripts/unblock_judge.sh:352`, which is used at line 358;
- the variable/secret probes in `scripts/unblock_judge.sh:308-309`, a permission probe that is used at line 310;
- the `user` identity reads in `scripts/unblock_judge.sh:1345` and `scripts/orchestrate_poll_process.sh:51`, both auth probes and both consumed.

### Cross-References to Deep Audit Section
- API-001: RISKY_SKIP — the fix changes the retry/backoff loop in `curl_gh_api` (`gh_helpers.sh:1479-1512`). Confirmed: the non-rate-limit branch at lines 1503-1509 retries every non-2xx status, and the header at lines 1454-1455 still says "sleeps 30 s". New detail: line 1483 maps curl transport failures to `000`, which must stay retryable, so the early return should cover only `400|401|404|422`.
- API-002: NEEDS_VERIFICATION — new detail. `gh_retry --idempotent` (`gh_helpers.sh:1169`) makes the cancel POST retryable. But HTTP 409 is not in `_is_gh_permanent_failure` (`gh_helpers.sh:194`), and cancelling a run that has already completed returns 409 "Cannot cancel a workflow run that is completed" ([observed 409 in technote-space/auto-cancel-redundant-workflow#68](https://github.com/technote-space/auto-cancel-redundant-workflow/issues/68); GitHub also returns it for orphaned queued runs, see [community #210234](https://github.com/orgs/community/discussions/210234)). With the flag alone, every snapshot-stale run would cost 5 attempts, about 15s of backoff, and a `::error::` line (lines 1220-1236). Before shipping, either treat `HTTP 409` as non-retryable at the call site (`cancel_on_pr_close.yml:370-379`), or confirm the stderr text and add a call-site-local check. Do not widen `_is_gh_permanent_failure` for all callers.
- API-003: RISKY_SKIP — this is in `orchestrate_poll_process.sh`. The header at lines 37-41 calls it a deliberate "live issue read" under a trust-gated replay design. It has also been raised in at least 5 earlier reports (for example `analysis/workflow-optimization-2026-10-16.md:502` and `-10-23-5.md:438`), so per DEBT-001 it needs a tracking issue, not another re-audit.
- API-004: RISKY_SKIP — the code addresses a computed last page (`per_page=100&page=${last_page}`, `tg_helpers.sh:213-217`), which counts as manual pagination. New details for any later manual implementation:
  - Callers use the REST numeric `.id` for the PATCH (`tg_helpers.sh:253-256`, `266`, `321-326`, `335`). A GraphQL replacement must map `databaseId`→`id` and return `[{id, body}]`.
  - The change moves this load from the REST core bucket to the GraphQL points bucket.
- BATCH-001: RISKY_SKIP — this is in `orchestrate_poll_process.sh`. New details:
  - `_fetch_issue_labels_batch_graphql` is a live fetch, not a cache (`4447-4459`), so the "Re-read labels since we may have changed them" intent (line 29289) is kept.
  - The existing `_wave_issue_nums_json` (`26980-26982`) is built before review-blocked handling, and reissues can change wave membership. The input must therefore be rebuilt from `${ISSUE_NUMS}` plus the state-file read at line 29301, not reused.
  - Today a failed per-issue read becomes `[]` (line 29290). The batch-plus-backfill version must keep that same `|| echo '[]'` fallback, so `orchestrate_lib.py check-wave-status` sees the same input.
- BATCH-002: RISKY_SKIP — this is in `orchestrate_poll_process.sh` (`7095-7111`, `29425-29441`), and its unknown-state handling is a fail-safe defer.
- BATCH-003: RISKY_SKIP — this is in `orchestrate_poll_process.sh`, and the fix needs a REST-shaped adapter at five call sites with different field needs.
- BATCH-004: NEEDS_VERIFICATION — new safety detail. REST `pulls?head=<owner>:ai/issue-<n>` (`workflow_failure_heal_pr_reconcile.sh:243`) restricts results to this owner's branch. GraphQL `pullRequests(headRefName:)` does **not** filter by the head repository owner, so a fork PR with the same branch name would match. The aliased query must also select `headRepository{nameWithOwner}`, and drop non-matching nodes before line 250. N is usually 1, so the benefit is minimal.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 3 | REUSE-001, API-002 (cross-ref), BATCH-004 (cross-ref) |
| RISKY_SKIP | 9 | MERGE-001, MERGE-002, REUSE-002, API-001 (cross-ref), API-003 (cross-ref), API-004 (cross-ref), BATCH-001 (cross-ref), BATCH-002 (cross-ref), BATCH-003 (cross-ref) |

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.