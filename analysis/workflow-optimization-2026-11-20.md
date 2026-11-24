## Executive Summary

- **Data gaps first.** `shubhodeep1/coding-workflows` is in `coverage.repositories_truncated` (`page_limit_reached: true`), so only part of its runs are listed (1,000 rows). No repository failed. The full run-logs directory (`/home/runner/work/_temp/workflow-log-output`) could not be read from this analysis sandbox, even though the prompt says it is mounted. All evidence below comes from `analysis_context.json` (run rows and `log_summary`) and the repository code.
- **Top finding: when the Claude engine runs the log analysis, it never gets the run logs it is told are mounted (Reliability 1).** `.github/workflows/workflow-log-analysis.yml:1646-1647` calls `claude_run_selected LOG_ANALYSIS …` without `AI_ENGINE_INCLUDE_PATHS`. Only the codex path (`wla_codex_analyze`, line 1621) passes `--include "${RUN_LOGS_DIR}"`. `scripts/ai_engine.sh:568-573` already turns `AI_ENGINE_INCLUDE_PATHS` into `--include` mounts. This is why the 7 failures and the slow runs in this window, and the 2026-11-18 report before it, could not be diagnosed. The fix is a one-line environment prefix. Impact: high, since it restores root-cause evidence for every later report. Confidence: high.
- **CI final runs are cancelled mid-step in `orchestrate-poll (3)` (Speed 1).** Three CI runs (44648184781, 44649054723, 44671393907) were cancelled at the same step, `Pre-push affected-test gate tests`. #9391 pinned that step to poll-group 3, which runs under a 20-minute `timeout-minutes` (`ci.yml:3448, 3458`). Inference: at least the two push runs, which `cancel-in-progress` does not cancel while running, most likely hit the job timeout. Each costs a 20-minute runner slot plus a full CI re-run. Confidence: medium.
- **`GH_PAT_BUDGET used_in_job=` is `unknown` in 7 of the 10 visible lines (GH API 1).** That includes four `deterministic-skip-merge` jobs of about 49 s, where an hourly rate-limit window rollover is very unlikely. The end-step check requires `initial_reset == reset` exactly (`review_autofix.yml:1665`, `scripts/gh_helpers.sh:1008`). Past verdicts rely on this per-job counter, so most of the time it is not working. Confidence: medium.
- **Runner saturation sets the latency floor and has stalled the stable release.** `AUTO_RELEASE_SKIPPED reason=runner_queue_deep queued=240 max=100` (run 44689205697). The last stable gate (44490333814) failed 14 hours earlier, so no gate starts until the 24-hour backoff expires. Jobs routinely wait for runners: about 9 minutes for CI legs 3/8/9 in 44682881634, about 4m11s in 44690284974, about 3 minutes in 44686567575. Bounding the 24-leg `tests-system-python` fan-out is Speed 2. Confidence: medium.
- **Cost is healthy but concentrated in review.** 10 review runs with telemetry: 98 OpenRouter calls, 64,918,968 tokens (about 6.49M per run), `cache_hit_rate` 0.860874, up from 0.7346 in the 2026-11-18 report. `break_glass_count` and `context_budget_warn_count` are both 0. The cost driver is repeated review rounds per PR (Cost 1); its cause is unverified until Reliability 1 lands. Confidence: medium.
- **The poller has no job timeout (Orchestrator Health 1).** Poll run 44678573237 failed after 2,844 s with an empty log archive. The `poll` job (`orchestrate_poll.yml:104`) has no `timeout-minutes`, so a hung tick can hold the `ai-orchestrate-poll` concurrency group for up to the 360-minute default. Adding a 90-minute timeout is low-risk. Confidence: medium.

## Speed Optimizations

**1. Rebalance `orchestrate-poll` group 3: three CI runs were cancelled mid-step at `Pre-push affected-test gate tests`.** This is on the critical path of final CI.
- **Evidence:** Three runs, all with `failure_point` = `orchestrate-poll (3)` / `Pre-push affected-test gate tests`:
  - 44648184781 (`CI`, push, 3,929 s, cancelled)
  - 44671393907 (`CI`, push, 1,822 s, cancelled)
  - 44649054723 (`CI [pr:9699]`, final-CI dispatch, 4,644 s, cancelled)
- **Root cause (inference):**
  - Since #9391, group 3 pins this step on top of its slice of the poll tests, the single-file module step (#8472) and others (`ci.yml:3428-3449`). The job has `timeout-minutes: 20` (`ci.yml:3458`).
  - Push runs share `ci-push-<ref>` without cancel-in-progress (`ci.yml:94-96`), so a running push run is not cancelled by a newer push. A job timeout is the likely cause for 44648184781 and 44671393907.
  - The `[pr:9699]` run may instead have been superseded by a newer final run, because `ci-final-pr-<n>` does cancel in progress.
  - `budget-watch` only measures `main` pushes (`ci.yml:4787`), so timeouts on integration-branch and final-CI runs never reach an `ai:ci-budget` issue.
- **Exact change:**
  - First confirm the cancellation cause with Reliability 2 (job annotations: "exceeded the maximum execution time").
  - If confirmed, move `Pre-push affected-test gate tests` (`ci.yml:4053-4061`, `if: strategy.job-index == 3`) to the poll group with the most headroom. Change only the `job-index` value.
  - Do not raise the 20-minute timeout.
- **Estimated savings:** Each avoided timeout saves a 20-minute group-3 slot plus a full final-CI re-run. Under current queueing that re-run took about 30-65 minutes wall clock (CI p50 717.5 s, p95 3,939.5 s, plus queue). In this window that is up to 3 occurrences.
- **Risk:** Low. A step moves between matrix legs, and the `lint` aggregate still requires every group.

Finding check: new — no prior verdict mentions poll-group 3 or #9391 (the report's only pre-push entries, lines 1803/1990, concern the gate's existence); `ci.yml:3448-3458` still pins the step to group 3 under a 20-minute timeout.

**2. Bound the `tests-system-python` fan-out while the account queue is above the release gate's limit.** This trades micro-latency for slots.
- **Evidence:**
  - The matrix has 24 legs (`ci.yml:4262`), with no `max-parallel`.
  - In 44682881634, legs 3, 8 and 9 each waited about 9 minutes for a runner (legs 4: about 2 minutes, 5 and 7: about 10 s).
  - In 44686474928, leg 9 waited about 3m43s. Shards ran 443-591 s of tests (`744 passed … in 443.19s`, `772 passed … in 591.11s`).
  - The account queue was at `queued=240` against `AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS: 100` (44689205697).
- **Root cause:** Each final CI requests 24 system-python slots at once, plus 5 `orchestrate-poll` legs, 3 sandbox legs and the single jobs. Under §26 saturation those requests sit in the same queue as review, poll and plan jobs. Those jobs waited 2-4 minutes in this window (44687165331, 44686567575, 44682881025).
- **Exact change:** Add `max-parallel: 12` under `tests-system-python.strategy` (`ci.yml:4257`), with a comment citing this window. Shard count and per-shard runtime stay as they are.
- **Estimated savings:** When the queue is saturated, other workflows reclaim up to 12 concurrent slots per final CI. When runners are free, final CI gets slower by at most one shard wave (about 7-10 minutes).
- **Risk:** Medium. It slows final CI when idle, against §26 "keep delivery fast". Apply it only together with a repo-variable kill switch and revert if CI p50 (717.5 s) rises by more than 10 minutes.

Finding check: new — no prior verdict on system-python matrix width (the 2026-06-04 `CI-sharding` entry is listed as deferred, line 397); `ci.yml:4257-4262` has no `max-parallel`.

**3. Semble runs one bootstrap per query (about 47.7 s each).** This is a micro-optimization.
- **Evidence:**
  - Window-wide: 18 bootstraps for 18 queries, `semble_bootstrap_ms_total` 858,711 (47.7 s average), 0 failed and 0 unused.
  - Review runs with 2 queries also logged 2 bootstraps: 44684904856 (97,418 ms), 44684000308 (89,698 ms) and 44685676203 (69,616 ms).
  - Poll run 44685046725 logged 2 bootstraps (94,944 ms) for 11,768 query bytes.
- **Root cause:** Unknown.
  - `semble_ensure_ready` reuses a `state=ready` file keyed by `RUNTIME_DIR` and run ID (`scripts/semble_helpers.sh:13, 228, 250`).
  - A second bootstrap means the second pass uses a different `RUNTIME_DIR`, or the index is rebuilt on purpose after an autofix commit.
- **Exact change:** None until the logs are readable (Reliability 1). Then compare the `SEMBLE_BOOTSTRAP` lines of a 2-pass run. If the rebuild is unintentional, pin `SEMBLE_BOOTSTRAP_STATE_FILE` per job.
- **Estimated savings:** About 35-48 s per multi-pass review run (3 of 10 telemetry runs).
- **Risk:** Low.

Finding check: unverified — current code shows per-job state reuse; whether the second bootstrap is intentional cannot be read without the deep-dive logs (not mounted).

## Cost Optimizations

**1. Repeated review rounds per PR are the dominant token and runner cost.**
- **Evidence:**
  - Per-run tokens: 44643514627 11,470,236; 44684904856 9,360,934; 44624288724 9,029,201; 44656690022 7,621,768.
  - PR #9806 (forward-merge) had at least 3 review runs: 44619070114 (6,122 s, 3,858,254 tokens), 44662751557 (3,556 s) and 44675311847 (3,683 s).
  - PR #9699 had at least 4: 44650442666, 44650828570, 44658611420 and 44671662569.
  - PR #9711 had 3.
  - `REVIEW_SWEEP_ROUND_CAP outcome=already_escalated pr=#9675 rounds=13 max=10` (44688966052).
- **Root cause:** Not attributable. The `REVIEW_AUTOFIX_RUN_SUMMARY_V1` and `REVIEWER_ATTEMPT_RESULT` lines that would explain each re-run were cut from every `log_summary` (about 1.78M chars truncated), and the full logs were not readable.
- **Exact change:** Land Reliability 1. In the next window, tabulate re-review triggers per PR from those lines before changing any review policy.
- **Estimated savings:** Unknown. Each avoided round is about 3.2-4.7M tokens (8-call passes) and 17-100 minutes of runner time.
- **Quality risk:** None for the diagnostic step.

Finding check: unverified — root cause requires the deep-dive logs, which are not mounted in this run (see Reliability 1).

**2. `issue_comment` wrapper fan-out still produces mostly skipped runs.**
- **Evidence:** The four comment-triggered wrapper families (clarify, plan, implement, orchestrate_clarify_respond) produced 279 skipped runs out of 301 (92.7%). Every comment starts all four; for example at 06:29:35 runs 44688166102, 44688166255, 44688166181 and 44688166221 all skipped. Window-wide, 464 of 1,000 runs were skipped.
- **Root cause:** `internal-clarify.yml:6-7` triggers on every `issue_comment: created`, and GitHub cannot filter on comment body at the trigger level. Item 15 of the run-volume plan (signed job-token comments) is live (`AI_COMMENT_SIGNING_POST: true` in 44686481533), so the remaining skipped runs come from comments still posted with the PAT or by humans.
- **Exact change:** Do not build a router; it is a declared non-goal (`docs/plans/reduce-actions-run-volume-plan.md` D19). Instead, have the existing `workflow_failure_heal_intake` or log-analysis collector count skipped wrapper runs per triggering comment author. That identifies which PAT comment kinds still start wrappers after item 15, and they can be moved under D16.
- **Estimated savings:** The plan measured about 2,270 wasted runs a day before item 15 (D16). The remaining share is unmeasured. Skipped runs take no runner but load the queue and API (§26).
- **Quality risk:** None.

Finding check: new — the comment-router entry is recorded as deferred (`recommendation-processing-report.md:397`), not decided; the four-wrapper fan-out is still present in `internal-clarify.yml:3-7`.

## Reliability Improvements

**1. Mount the run-logs directory for Claude-engine log analysis.** This has the highest diagnostic leverage.
- **Failure evidence:**
  - This run was told logs are "Mounted read-only in the sandbox at this path", but the path does not exist in the sandbox.
  - The 2026-11-18 report hit the same gap.
  - Collect run 44684864406 uploaded `workflow-log-output` (Artifact 14606444292), so the artifact exists. The problem is the mount.
- **Root cause category:** Code defect.
  - `workflow-log-analysis.yml:1621` passes `${wla_run_logs_include_args[@]}` only to `codex_isolated_exec.sh` (the codex path).
  - The Claude path at lines 1646-1647 sets only `AI_ENGINE_MODEL_HINT` and `AI_ENGINE_EFFORT_HINT`.
  - `claude_run` mounts only the paths listed in `AI_ENGINE_INCLUDE_PATHS` (`scripts/ai_engine.sh:568-573`), and no workflow in this file sets that variable. Only `implement.yml:6763` and `orchestrate.yml:1023` do.
- **Exact fix:**
  ```bash
  AI_ENGINE_MODEL_HINT="${WORKFLOW_EDITOR_MODEL}" AI_ENGINE_EFFORT_HINT="" \
    AI_ENGINE_INCLUDE_PATHS="${RUN_LOGS_DIR}" \
    claude_run_selected LOG_ANALYSIS "${CODEX_PROMPT_FILE}" "${REPORT_FILE}" "${PWD}" -- wla_codex_analyze
  ```
  Also check the other `LOG_ANALYSIS` pass at line 3505 and apply the same prefix if its prompt names the logs directory.
- **Expected impact:** Restores untruncated evidence for all `errors/`, `slow/` and `recent/` runs. Six items in this report are `unverified` only for lack of it.
- **Rollback / fail-open:**
  - An empty `RUN_LOGS_DIR` (artifact missing) adds no mount, because the loop skips empty entries (line 570).
  - If the pool-overlap guard (`_ai_engine_pool_isolated`, line 526) rejects the path, the run fails closed with `reason=pool_overlap` and the next scheduled pass retries. `RUN_LOGS_DIR` is a sibling directory under `RUNNER_TEMP`, so this is not expected.
  - Revert by removing the one line.

Finding check: reopened — prior verdict "Restoring the full-log archive | rejected | … Its absence is runtime artifact state, not a code defect" (2026-10-08 ledger, line 2283); new evidence: the artifact is present and the defect is the missing `AI_ENGINE_INCLUDE_PATHS` on the Claude path (`workflow-log-analysis.yml:1646-1647`).

**2. Record the GitHub-side cause for empty-archive failures and mid-step cancellations.**
- **Failure evidence:**
  - Poll run 44678573237: `failure`, 2,844 s, `log_download_status: empty_archive`, `step_name: null`.
  - Three CI runs cancelled mid-step (Speed 1).
  - Cancelled poll ticks 44689722795 and 44684383953 (these are healthy coalescing).
- **Root cause category:** Observability gap. `scripts/collect_workflow_logs.py` hard-codes `WORKFLOW_VALIDATION_ANNOTATIONS_STATUS = "unavailable_from_existing_payloads"` (line 82) and never fetches check-run annotations. Those annotations carry "exceeded the maximum execution time", "runner has received a shutdown signal" and "lost communication with the server".
- **Exact fix:** In the collector, fetch `repos/{repo}/check-runs/{job_id}/annotations` once for the failing or cancelled job, only when the run has `diagnostic_failure_reason=empty_log_archive` or a cancelled job whose steps had started. Store a sanitized, capped `failure_annotation` field on the run row and fail open on any error.
- **Expected impact:** Separates timeout, runner loss and superseded cancellation. This confirms or refutes Speed 1 and explains 44678573237. Cost: 4 GH API calls in this window.
- **Rollback / fail-open:** The field is additive and a missing value is equivalent to today.

Finding check: new — the related "cancellation semantics telemetry" item is recorded as deferred, not decided (2026-08-29 ledger, line 1101); the collector still has no annotation fetch (`collect_workflow_logs.py:82`).

**3. Unattributed single failures (four failing runs).**
- **Evidence:**
  - PR #9442 failed three times at `review / codex-agent` / `Run Codex resolver, validate, stage, commit`: 44646017321 (1,489 s), 44650487981 (1,529 s) and 44657650075 (1,376 s). Each did one Semble query (about 10.6-10.8 KB) and logged 0 OpenRouter and 0 Codex usage.
  - CI failures: 44621001029 (`tests-promote-stall-and-review` / `Review autofix step-script contract and workflow file size guard tests`, PR #9806), 44645371836 (`tests-system-python (21)`, PR #9811) and 44669026517 (`orchestrate-poll (1)` / `Orchestrate poll process unit tests`, main).
- **Root cause:** Not readable.
  - The resolver already escalates its tier after `RESOLVER_ESCAPE_THRESHOLD_N` (default 5, `scripts/review_conflict_resolve.sh:2415`), and #9442's 3 failures are below that.
  - Main CI was green again by 44671664016 (04:02).
- **Exact fix:** None now. After Reliability 1, read the resolver's failure class for #9442, and the failing assertion of 44621001029, which may involve the 480,000-byte guard. Act only if #9442 reaches a 4th failure on the same head.
- **Expected impact:** Prevents roughly 25 minutes of runner time per repeated resolver failure.
- **Fail-open:** Not applicable.

Finding check: unverified — failing step logs were downloaded (`log_download_status: success`) but are not readable in this sandbox.

**4. Semble runtime fallbacks in implement.**
- **Evidence:** All 5 `semble_runtime_fallbacks` in the window come from implement run 44634099626 (4,259 s, success). That run recorded 0 bootstraps and 0 queries. The 32 contract-test fallbacks come from CI fixtures (4 or 8 per CI run) and are healthy.
- **Classification:** Rare fail-open behaviour (1 of 8 implement runs with telemetry), not a masked broken rollout. Review (16 queries) and poll (2 queries) show 0 fallbacks.
- **Exact fix:** None. Read `SEMBLE_FALLBACK target= reason=` (`scripts/semble_helpers.sh:240, 247, 292`) once logs are mounted. Escalate only if a second implement run repeats the pattern.
- **Expected impact:** Low.
- **Fail-open:** Already fails open.

Finding check: unverified — the per-target `reason=` values are in the unmounted logs.

`BREAK_GLASS` and `CONTEXT_BUDGET_WARN`: 0 and 0 across all 118 runs with telemetry, so there is no rubric pressure or prompt-size risk signal. `SERENA_*`: 0 everywhere, with `SERENA_ENABLED: false` in the review `log_summary` entries.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines appear in any `log_summary` in this window. Several summaries explicitly searched for them: 44686567575, 44683602278, 44685675954 and 44687165331. The deep-dive logs were not readable, so `retrieve` hit rate, `estimated_tokens` against budget, `keyword_method` distribution, `fail_open` and `enabled: false` entries, and push-retry counts cannot be computed. The emitter is known to exist (2026-10-08 ledger: "`AI_MEMORY_TELEMETRY` and `CONTEXT_BUDGET_WARN` are already emitted"). The absence is most likely truncation (about 1M-1.8M chars cut from poll and review logs), not a lack of emission.

**Action:** After Reliability 1, grep `errors/`, `slow/` and `recent/` for `"op":"retrieve"` and report the four metrics. If they are still absent from untruncated plan and implement logs, check emission at the `retrieve` call site.

Finding check: unverified — the telemetry sources (deep-dive logs) are not mounted in this run.

## GH API Call Audit

**1. Make `GH_PAT_BUDGET used_in_job=` measurable.** The per-job budget counter is mostly `unknown`.
- **Evidence:** 10 `GH_PAT_BUDGET phase=end` lines are visible:
  - Measured: `used_in_job=17` (44689029265), `60` (sweep 44688966052) and `0` (44682881025).
  - `unknown` (7): 44689038043, 44684908929, 44684900791 and 44681595970, all `deterministic-skip-merge` jobs of about 49 s; and 44685676203, 44684904856 and 44684000308 (`codex-agent`, 17-45 minutes).
  - The lowest `remaining` was 3,030 (05:46). No 429s or secondary limits appear anywhere.
- **Root cause:** The end steps require exact equality, `[ "${initial_reset:-}" = "${reset}" ]` (`review_autofix.yml:1665`, the same pattern at lines 830-837, 1066-1073 and 1166-1173; `scripts/gh_helpers.sh:1008`). A real hourly rollover explains the long `codex-agent` jobs. For four of four 49-s jobs a rollover is very unlikely; inference: the `reset` value jitters between calls, or the start snapshot was not written.
- **Exact change (additive, two parts):**
  - Emit `start_reset=<value>` on the end line so the mismatch becomes visible.
  - Accept a small difference:
  ```bash
  if [[ "${initial:-}" =~ ^[0-9]+$ ]] && [[ "${initial_reset:-}" =~ ^[0-9]+$ ]] \
     && [ $(( reset > initial_reset ? reset - initial_reset : initial_reset - reset )) -le 5 ] \
     && [ "${initial}" -ge "${remaining}" ]; then
    used=$((initial - remaining))
  fi
  ```
  Apply it in `gh_pat_budget` and in the four inline end steps. These are small edits; `review_autofix.yml` is about 376-383 KB per the ledger, well under the 480,000-byte guard.
- **Estimated impact:** Turns about 70% `unknown` into measured per-job usage. Past verdicts already rely on this counter (for example 2026-10-07-3: "`GH_PAT_BUDGET` … already reports per-job usage"). There is no call-count change and no added rate-limit risk.

Finding check: new — no prior verdict on the `unknown` rate; strict equality still at `review_autofix.yml:1665` and `scripts/gh_helpers.sh:1008`.

**2. Merge-train release time is not explained by its counted calls.**
- **Evidence:** Three runs logged `MERGE_TRAIN_RELEASE_SUMMARY examined=0 released=0 … file_list_fetches=0 active_listing_calls=0`:
  - 44688952636: `elapsed_ms=13575`, about 13.6 s of a 25 s run.
  - 44681234994: `elapsed_ms=18585`.
  - 44682880884: `elapsed_ms=6601`.
  - The same ticks logged per-base `MERGE_LEASE … mode=tick outcome=no_waiter reason=lease_free` for about 7-8 bases.
- **Root cause (inference):** The time goes to the per-base lease ticks and freshness reads, which `MERGE_TRAIN_RELEASE_SUMMARY` does not count.
- **Exact change:** Add one additive `lease_tick_calls=` counter to the existing summary line in `scripts/review_merge_train.sh`. This is the deferred "API time" half of the 2026-10-08 row. If ticks dominate, skip lease ticks for bases with no `ai:merge-lease-wait` PR, using the label search the sweep already performs.
- **Estimated impact:** Up to about 7 calls and 6-18 s per scheduled `cancel_on_pr_close` tick (22 runs in window). The rate-limit benefit is small.

Finding check: new — the 2026-10-08 row records this part as deferred ("API time separate from total `elapsed_ms=` … deferred to a later additive field", line 2262).

**Healthy patterns:**
- `pr-body-lint` uses one paginated commits call (44686480949, 46 commits).
- `promote-cycle-frozen` 404s are classified `outcome=permanent` and not retried (44687999121, 44682881658). Their `##[warning]` lines are cosmetic noise only.
- Scheduled close cleanup batches PR state 50 per GraphQL call (`cancel_on_pr_close.yml:213-278`).

## Prompt Cache & Memory System

- **`cache_hit_rate` is 0.860874** (cache read 55,171,138 / write 2,703,974 / uncached prompt 6,212,256), up from 0.734551 in the 2026-11-18 window. Per run it ranges from 0.637918 (44614210752) to 0.926951 (44643514627). `or_usage_unavailable_calls` is 0 of 98, so usage reporting is complete.
- **Fragmentation signal:** The low outlier 44614210752 wrote 159,355 cache tokens against 862,787 read, about 2.6x the write share of the high-hit runs (238,089 against 10,536,580 in 44643514627). One plausible cause (inference) is a cold first pass on a new PR, where no prefix was shared yet.
- **Semble as context reducer:** 18 queries, 239,477 bytes (13,304 B per query), 98 sources (5.4 per query), `static_dup_bytes` 0, echo lines dropped 0. Semble adds compact, non-duplicated context, about 0.2% of review prompt volume, so it is not a source of prompt bloat. Its cost is mainly bootstrap time (Speed 3), not tokens.
- `context_budget_warn_count` is 0, so there is no budget pressure eroding cache value.

**Recommendation:** After Reliability 1, compare `REVIEWER_PROMPT_COMPONENTS static_prefix_sha256` (`scripts/review_run_reviewers.sh`) across the passes of 44614210752. If the hash differs between passes on the same head, pin the varying component behind the dynamic suffix. Estimated impact: raising that run class to the window median (about 0.86) would save about 0.3M uncached tokens per affected run.

Finding check: unverified — whether the prefix hash varied cannot be read without the deep-dive logs.

## Orchestrator Health

**1. Add `timeout-minutes: 90` to the `poll` job.**
- **Evidence:**
  - 44678573237 failed after 2,844 s (about 47 minutes) with an empty log archive, while healthy ticks run p50 769 s and p95 1,369.5 s.
  - 32 of 71 poll runs were cancelled before their first step, for example 44682259162 (736 s queued, superseded by 44683602278) and 44684383953 (superseded by 44685046725). This is the intended `cancel-in-progress: false` coalescing.
- **Root cause:** The `poll` job (`orchestrate_poll.yml:104-111`) has no `timeout-minutes`, so GitHub's 360-minute default applies. One hung tick can block the `ai-orchestrate-poll` group for up to 24 cron slots (`internal-orchestrate-poll.yml:5`, `*/15`).
- **Exact change:** `timeout-minutes: 90` on the `poll` job, with a comment citing this run. That is about 4x p95 and above every observed healthy tick (max 1,603 s in the slow list, 44622784409).
- **Expected impact:** Worst-case orchestrator stall drops from 6 hours to 1.5 hours.
- **Risk:** Low to medium. A tick killed mid-resolution or mid-judge resumes on the next tick, which is the same recovery path as a runner loss. The 90-minute value leaves 3.9x headroom.

Finding check: new — no prior verdict on a poll job timeout (the only "orchestrate-poll … timeout-minutes" ledger hit, line 1707, concerns the CI test job); `orchestrate_poll.yml:104` still has none.

**Observable indicators (no new code):**
- **Merge-lease wait:** PR #9795 waited on holder #9705 at 05:28 (44681234994), 05:46 (44682880884) and 06:36 (44688952636). `behind_by` grew from 23 to 32, with overlap on `scripts/orchestrate_poll_process.sh`. The lease TTL is 240 minutes (`MERGE_LEASE_TTL_MINUTES`).
- **Round cap:** PR #9675 escalated at 13 of 10 rounds (44688966052).
- **Integration readiness:** #9396 has 5 of 6 sub-issues on #9394 unchecked (44686480949).
- **Clarify → plan flow:** Issue #9823 went from clarify (44686449741, 154 s) to plan (44686567575, 969 s, about 3 minutes of it runner wait) through `/answer [auto-answered-by-orchestrator]`.
- **Stable release:** Blocked by `runner_queue_deep` until the 24-hour backoff on 44490333814 expires.

## Pipeline Flow Bottlenecks

The bottlenecks below are ordered by end-to-end impact.

1. **Review/autofix compute.** `review_autofix` has p50 1,026 s, p95 4,188.2 s, and runs of 5,139-6,122 s on the slow list (44619070114, 44624288724, 44614769218, 44614210752, 44656690022) with only 6-8 model calls each. Model calls therefore do not explain the wall time. The dominant phase cannot be attributed until Reliability 1 lands (see Cost 1).
2. **Queueing across every family.** Runner waits of 2-4 minutes hit short jobs: plan (44686567575), poll (44687165331, 44683602278), post-merge dispatch (44682881025) and heal intake (44690284974, 4m11s of 263 s). For 103-s and 119-s jobs (44686480949, 44683121644), the wait is most of the runtime. CI legs waited up to about 9 minutes (44682881634). Fix order: Speed 2, then the run-volume plan items.
3. **Retry and re-run overhead.**
   - CI timeouts in poll-group 3 force full final-CI re-runs (Speed 1).
   - Repeated resolver failures on #9442 cost 3 × about 25 minutes (Reliability 3).
4. **Merge and conflict overhead.** Lease serialization on `scripts/orchestrate_poll_process.sh` holds #9795 behind #9705, and forward-merge PRs #9806 and #9820 carried conflicts (6 files on #9820). These are policy-intended waits; track them through the Orchestrator Health indicators.
5. **Clarify and plan** are not bottlenecks: compute is 154 s and about 13 minutes respectively, with runner waits on top.

Finding check: new — summary of Speed 1–2 (new) and Orchestrator Health 1 (new) above; no separate change proposed here.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - Review/autofix wall time (p50 1,026 s, p95 4,188 s).
  - Account-wide runner queue (240 queued; waits of 2-9 minutes).
  - Final CI (p50 717.5 s, p95 3,939.5 s).
- **Top failure modes:**
  - Resolver failures on PR #9442 (3).
  - CI cancellations at poll-group 3 (3).
  - Empty-archive poll failure 44678573237 (1).
  - Single CI test failures (3).
- **Highest-cost drivers:** 10 review runs with telemetry used 64,918,968 OpenRouter tokens (98 calls, cache hit 0.861). Repeated rounds on PRs #9806, #9699, #9711 and #9675 drive the total.
- **Top 3 prioritized actions:**
  1. Pass `AI_ENGINE_INCLUDE_PATHS="${RUN_LOGS_DIR}"` to the Claude `LOG_ANALYSIS` run (`workflow-log-analysis.yml:1646`).
     Finding check: reopened — 2026-10-08 ledger line 2283 rejected archive restoration as "not a code defect"; the defect is now located at `workflow-log-analysis.yml:1646-1647`.
  2. Rebalance the `Pre-push affected-test gate tests` step out of `orchestrate-poll` group 3 after confirming timeouts.
     Finding check: new — no prior verdict; `ci.yml:3448-3458` unchanged.
  3. Add `timeout-minutes: 90` to the `poll` job.
     Finding check: new — no prior verdict; `orchestrate_poll.yml:104` has no timeout.

## Metrics Appendix

**Run outcomes** (1,000 runs, truncated listing)

| Scope | Runs | Success | Failure | Cancelled | Skipped/other | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| Repo total | 1,000 | 478 | 7 (0.7%) | 51 (5.1%) | 464 (46.4%) | 36.0 | 2,684.0 |
| review_autofix | 165 | 146 | 3 | 14 | 2 | 1,026.0 | 4,188.2 |
| ci | 86 | 78 | 3 | 5 | 0 | 717.5 | 3,939.5 |
| orchestrate_poll | 71 | 38 | 1 | 32 | 0 | 769.0 | 1,369.5 |
| implement | 73 | 8 | 0 | 0 | 65 | 2.0 | 2,547.4 |
| plan | 74 | 7 | 0 | 0 | 67 | 1.0 | 602.8 |
| clarify | 80 | 6 | 0 | 0 | 74 | 1.0 | 165.6 |
| orchestrate_clarify_respond | 74 | 1 | 0 | 0 | 73 | 1.0 | 11.0 |
| workflow_failure_heal | 81 | 1 | 0 | 0 | 80 | 1.0 | 9.0 |
| workflow_failure_heal_intake | 77 | 34 | 0 | 0 | 43 | 10.0 | 282.8 |
| integration_pr_readiness | 44 | 20 | 0 | 0 | 24 | 3.0 | 380.8 |
| check_failure_triage | 39 | 3 | 0 | 0 | 36 | 2.0 | 384.2 |

Of the 536 runs that executed, 89.2% succeeded, 1.3% failed and 9.5% were cancelled.

**Tokens, cache and review telemetry**

| Metric | Value |
|---|---|
| `or_calls` (usage available / unavailable) | 98 (98 / 0) |
| Prompt / completion tokens | 6,212,256 / 833,871 |
| Cache read / write tokens | 55,171,138 / 2,703,974 |
| Total tokens | 64,918,968 (10 review runs; about 6.49M per run) |
| `cache_hit_rate` | 0.860874 (per run 0.637918–0.926951) |
| `codex_calls` / `codex_tokens_used` | 0 / 0 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (repo, 115 samples) | 25,000 / 5,663,180 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (`review_autofix`, 25 samples) | 1,034,000 / 6,027,440 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |

**Semble / Serena**

| Metric | Value |
|---|---|
| `semble_query_calls` / `semble_query_bytes` | 18 / 239,477 (13,304 B per query); review 16 / 227,709; poll 2 / 11,768 |
| `semble_sources_total` / `static_dup_bytes` / echo lines dropped | 98 / 0 / 0 |
| `semble_bootstraps` / failed / unused / total ms | 18 / 0 / 0 / 858,711 (about 47.7 s each) |
| `semble_fallbacks` (contract-test / runtime) | 37 (32 / 5); runtime all in implement run 44634099626 |
| Serena query / fallback / response bytes / tool calls / ms | 0 / 0 / 0 / 0 / 0 |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (all targets) | 0 | 0 | 0 |

Other MCP servers observed: none.

**GH API**

| Signal | Value |
|---|---|
| `GH_PAT_BUDGET` end lines visible | 10 (measured 3: 0 / 17 / 60; `unknown` 7) |
| Lowest `remaining` | 3,030 (44682881025) |
| HTTP 429 / secondary limit | 0 observed |
| `GH_API_RETRY` | 2 × `outcome=permanent status=404` (`promote-cycle-frozen`, not retried) |
| `MERGE_TRAIN_RELEASE_SUMMARY` elapsed with 0 counted calls | 13,575 / 18,585 / 6,601 ms |

**Findings check**

| Status | Count |
|---|---|
| new | 7 (Speed 1, Speed 2, Cost 2, Reliability 2, GH API 1, GH API 2, Orchestrator 1) |
| recurring | 0 |
| reopened | 1 (Reliability 1) |
| unverified | 7 (Speed 3, Cost 1, Reliability 3, Reliability 4, AI Memory Health, Prompt Cache, plus Pipeline Flow cross-references counted under their sources) |
| dropped: already fixed on this ref | 2 (Semble contract-test vs runtime fallback split, `scripts/cost_audit.py`; `cancelled_before_first_step` classification, `scripts/collect_workflow_logs.py`) |
| dropped: previously decided | 3 (poller `SEMBLE_AVAILABLE=false` under lazy mode, rejected 2026-10-07-3; runner-queue logging, rejected; `GH_API_CALL` per-endpoint counters, rejected) |

**Data gaps**
- `shubhodeep1/coding-workflows` is truncated (`page_limit_reached: true`).
- The run-logs directory was not readable in this sandbox (root cause in Reliability 1), so step-level evidence comes only from `log_summary`, which was truncated by about 0.04-1.8M chars per run.
- The processing report (`analysis/recommendation-processing-report.md`) was readable; its last ledger entry is `workflow-optimization-2026-10-08.md`. Later reports, including 2026-11-18, have no verdicts yet.
- `claude_*` cost fields are not exported in `analysis_context.json`, so engine cost for the #9442 resolver runs (0 OpenRouter and 0 Codex usage) is unknown.