## Executive Summary

- **Fix telemetry counting first.** The collector reports 56.25M OpenRouter tokens in the deep-dive archive, but whole-job-only recalculation finds 42.90M: **13.35M reported tokens are duplicates**, not additional billed usage. Run 38252166135 alone is counted as 20 calls instead of 10. **Impact:** accurate cost decisions; **confidence: high**.
- **A failed child blocked the release test for 5,292 seconds.** Test & Mark Stable run 38253656098 failed while watching validation-refresh run 38253831133. That child reported 10 red consumer results—which the runner classifies as non-blocking—but also one checkout error following a GraphQL failure. A bounded retry of that read could avoid a full test rerun on this path, potentially **up to 88 minutes**; success is not guaranteed. **Confidence: medium**.
- **Closed PRs still reach failing review steps.** Review runs 38257610792 and 38261607088 marked PRs #7267 and #7266 closed, then failed at “Checkout PR head branch” because the workspace was unavailable. The former also spent 300 seconds waiting for checks after the close warning. **Impact:** prevent two observed failures and avoid up to five minutes of needless wait per affected run; **confidence: high**.
- **Queueing and merge-train work need separate timing.** Readiness run 38271033352 waited about 150 seconds for a runner; CI run 38268305673 waited about 85 seconds. Two scheduled close-cleanup runs spent about 192 and 218 seconds in merge-train release scans. **Impact:** expose which delays are avoidable before changing dispatch behavior; **confidence: medium**.
- **MCP failures do not indicate a broken rollout in this sample.** All 52 reported Semble fallbacks are contract-test events; runtime fallbacks are zero. Serena has no queries or probes, and run 38268306664 reports it disabled. **Impact:** avoid an unnecessary rollback; **confidence: high for observed logs, low for unsampled runs**.

## Speed Optimizations

Ranked by potential end-to-end latency reduction:

| Priority | Evidence and root cause | Exact change and diagnostic logging | Estimated saving; risk |
|---|---|---|---|
| 1 — release critical path | Run 38253656098 took **5,292s** and failed on child 38253831133. The child took **1,288s** and reported `processed=13 green=2 red=10 error=1`; its error was a checkout GraphQL failure. | Retry only transient, read-only checkout/default-branch requests with a small bounded backoff. Log `VALIDATION_STAGE repository=… stage=checkout attempt=… status=… elapsed_ms=…`, then retain the existing error gate if retries fail. Do **not** turn red self-tests green. | Potentially avoids one **5,292s** rerun on this failure path; **low implementation risk**, conditional benefit. |
| 2 — review critical path | Runs 38257610792 and 38261607088 detected closed PRs before an unconditional checkout step. Run 38257610792 subsequently logged a **300s** check-run wait. | Guard checkout, check-wait, and memory retrieval on the already-established `PR_CLOSED` state; retain the current failure for an *open* PR with a missing workspace. Log `REVIEW_PRECHECK pr=… state=… action=skip_closed|continue|fail_missing_workspace elapsed_ms=…`. | Up to **300s of observed wait** on run 38257610792, plus downstream work; **low risk** if unknown PR state never counts as closed. |
| 3 — runner queue, not compute | Run 38271033352 spent about **150s** waiting for `ubuntu-latest`; CI run 38268305673 waited about **85s**. Run-level `run_started_at` does not expose those job waits. | Record job queued-to-runner and execution milliseconds separately using already-fetched job data. Test whether redundant, identical-head events can be coalesced **without changing required-check behavior**. | At most the measured wait for a genuinely redundant job; **medium risk** for dispatch changes, negligible risk for logging. |
| 4 — instrument before optimizing | Cancel-on-close runs 38270862087 and 38268250577 spent **192s** and **218s** in “Release merge-queued PRs”; one examined 29 PRs and released zero. Code already caches per-PR file lists and deliberately scans active-run statuses twice. | Add `MERGE_TRAIN_RELEASE_TIMING` with `listing_ms`, `files_ms`, `blockers_ms`, `comments_ms`, `api_calls_by_category`, and cache hits. Optimize the measured dominant category; preserve the safety double-pass. | **No defensible saving yet**; the observed step is the opportunity ceiling. Logging is low risk. |

Semble’s reported nine bootstraps total 322 seconds and have zero reported unused bootstraps. Even before correcting duplicates, that is a micro-optimization beside multi-thousand-second review runs; retain lazy bootstrap.

## Cost Optimizations

**Dollar savings cannot be estimated:** the input has no comparable prices or verified billed totals. The token figures below are collector-reported unless identified as whole-job-only.

1. **Measure review passes before changing model selection.** Review/autofix accounts for **63,636,796 of 63,683,625 reported OpenRouter tokens**. In run 38252166135, five reviewer models made 10 *distinct* calls: `pass1` used 4,111,263 tokens and `review` used 1,192,309. The configured editor model in its logs is `openai/gpt-6-sol`; available aggregates do not establish per-call reasoning level or quality contribution. Log `model`, `reasoning_effort`, `phase`, `call`, distinct call ID, and accepted findings. Compare a tightly scoped one-pass trial against judge and finding outcomes **before** removing calls. **Opportunity:** the measured `review` calls are 1,192,309 tokens in that run; **quality risk high**, so production savings are presently **zero**.
2. **Bound prompt growth without removing rubric or relevant files.** Review runs 38246098799 and 38252203873 logged prompts of **165,632** and **188,772** tokens against a 200,000-token window; both exceeded the 140,000 warning threshold. Log component token counts and stable-prefix hashes, then trim only demonstrated redundant context while preserving review requirements. **Conditional ceiling to reach the warning threshold:** 25,632 and 48,772 tokens respectively, not a measured achievable saving; **quality risk medium** for content removal, low for byte-equivalent prefix reordering.
3. **Avoid work after a confirmed PR close.** The two failed review runs still retrieved 26 memory records each, estimating **1,391** and **1,400** context tokens; neither started agents. Move retrieval behind the closed-PR guard. **Saving:** two retrievals and 2,791 prepared context tokens in these runs; **billed savings unknown**, quality risk negligible for confirmed-closed PRs.
4. **Prevent avoidable release reruns.** Failed release-test run 38253656098 recorded **46,829** OpenRouter tokens. A successful transient-read retry could avoid that amount on one equivalent rerun; it cannot be counted as saved until the release completes successfully.
5. **Repair cost accounting—not as a cost-cutting claim.** The deep-dive collector counts run 38252166135 twice (**10,607,144 versus 5,303,572** whole-job-only tokens) and run 38258839515 twice (**16,092,770 versus 8,046,385**). `scripts/collect_workflow_logs.py` compares full timestamped lines; parent-job and child-step copies differ by microseconds. Match the same event across ancestor/descendant logs using a producer event ID, or a narrowly bounded timestamp-normalized fallback with regression tests. **Actual token saving: zero; reporting correction: 13.35M tokens in this archive.**

Semble recorded **nine queries / 134,703 bytes / 76 sources**, with **2,502 static-duplicate bytes**; all queries target `reviewer-context`. A 14,948-byte, eight-source query appears in run 38252166135. This is evidence of targeted retrieval, **not proof of reduced model prompt tokens**: log bytes inserted into the prompt and a comparable non-Semble baseline before claiming savings. Serena recorded **zero calls and zero response bytes**; it cannot yet be credited with replacing model or tool work, nor blamed for noisy output.

## Reliability Improvements

1. **Keep validation mechanism errors distinct from consumer failures.** Run 38253831133 had **10 red consumer results** with self-test failures, including compose startup/build and health failures, plus **one checkout error**. Its parent, 38253656098, failed. Retry the transient checkout read, emit a per-stage failure category and final retry disposition, and continue to gate on genuine mechanism errors. **Expected impact:** may remove this one observed release-blocking error; **rollback:** restore the previous attempt limit, never suppress the final error.
2. **Honor confirmed closed-PR state end to end.** Both observed review/autofix failures, 38257610792 and 38261607088, reached “Review workspace is unavailable” after logging closed-PR state. Add the guarded skip and a terminal `REVIEW_DISPOSITION` containing PR state, skip reason, and failed step. **Expected impact:** eliminates this specific two-failure pattern; **fail-open boundary:** ambiguous state must not be treated as closed.
3. **Catch CI contract violations earlier while retaining CI.** CI failed **7/49** runs. Runs 38246118726 and 38268027882 rejected direct additions to `agents.md`; run 38245816108 found an unwired test; 38245692461 failed the disabled-publishing contract; 38246083359 deferred plan approval because its helper was unavailable. Run the same guards in pre-submit validation and emit `CI_FAILURE_SUMMARY job=… test=… category=…` rather than only an exit code. Put new documentation in `agents.d/`, per the repository rule. **Expected impact:** earlier diagnosis, not a guaranteed failure-rate reduction; final CI remains authoritative.
4. **Retry the artifact download, not the lint decision.** CI run 38250387138 failed installing actionlint when `curl` returned **504**. Add a small bounded download retry with status/attempt logging, then verify the pinned artifact before executing; still fail if unavailable. **Expected impact:** addresses one observed transient-install failure; rollback is the prior single attempt.
5. **Preserve unknown outcomes as unknown.** Poll run 38246066105 has **no job or step failure point** because its log archive returned **404**. Log archive-fetch attempts, HTTP status, age and jobs-fallback outcome; do not classify it as an orchestrator-code defect from the missing archive alone.

**Policy and MCP signals:** `BREAK_GLASS` is **0**. The **two** `CONTEXT_BUDGET_WARN` events are review prompt-size risk, not evidence of a policy exception. Semble’s **52 reported fallbacks** are all `target=overflow`, `context=contract-test`; **zero runtime fallbacks** were reported. CI run 38268027882 illustrates duplicate job/step copies: eight reported fallback lines represent four whole-job events. No Serena availability probe or runtime fallback was observed, so the safe mitigation is coverage logging—not rollout rollback.

## AI Memory Health

In **11 unique deep-dive review runs**, all **11 retrieves selected records** (**100% observed hit rate**), selecting 26 records each. Estimated context averaged **1,394/1,400 budget tokens** (99.6%); `keyword_method` was **llm: 11, plain: 0, none: 0**. No observed retrieve selected zero records, had `enabled=false`, or logged `fail_open=true`. The sample also has 22 `record-run-event`, five `record-candidate`, and three `write_lessons_learned` events. Of 27 entries reporting push attempts, **five took two attempts** and none took more; log retry *reason and elapsed time* to detect worsening contention.

Run 38261607088 retrieved memory after PR #7266 had been marked closed. Guard that operation as above. No `finalize-task`, `promote`, `compact`, or processed-command telemetry appears in these selected review logs; verify emission in the workflows that perform those operations rather than treating their absence here as a failure.

## GH API Call Audit

**Call totals, per-endpoint frequencies, and rate-limit-event counts are not supplied.** Review deep dives emit `GH_PAT_BUDGET used_in_job=unknown` (for example, run 38252166135); no measured `GH_API_RETRY` or 429 event was found in the inspected job logs. A **404** occurred fetching run 38246066105’s archive, and validation-refresh run 38253831133 logged a GraphQL checkout error. Neither establishes rate-limit saturation.

| Workflow / step | Supported pattern | Safe action and expected call reduction |
|---|---|---|
| Cancel on PR Close / merge-train release | Runs 38270862087 and 38268250577 spent 192s and 218s scanning; one examined 29 PRs. `scripts/review_merge_train.sh` already lists open PRs once, caches file lists per distinct PR, and normally reads five active-run statuses twice for safety. | Log calls/pages, latency, cache hits, and outcome by endpoint category. **No verified redundant call to remove yet; estimated reduction: 0 until measured.** If a repeated metadata lookup is found across 29 PRs, reuse the existing PR-list row—**up to 28 calls per repeated lookup**, conditional, without weakening the two-pass check. |
| Cancel on PR Close / scheduled cleanup | Run summaries 38270862087 and 38268250577 each say **three unlinked active runs were preserved**. Code fetches queued and in-progress runs separately and batches PR states in groups of 50. | Keep both bounded status filters and fail-open preservation. Log pages, batch count, unresolved PRs, and preserved-run reason; **no supported reduction** from combining calls. |
| Validation Refresh / checkout | Run 38253831133 has one observed GraphQL error after processing 13 repos. | Log read endpoint *category*, attempts and final status; retry only transient reads. Call count may rise by at most the configured retries on this path, while avoiding a full workflow rerun if successful. |

These recommendations follow `CLAUDE.md` §15: extend existing fetches, use cycle-local caches, and fail open safely on cache misses. Do not introduce a per-PR API call merely to improve logging; count calls at the existing helper. Log rate-limit resource, remaining budget and retry wait **without request bodies or credentials**.

## Prompt Cache & Memory System

The assembled sample reports **47,625,161 cache-read**, **3,987,540 cache-write**, and **11,383,691 prompt tokens** across 108 reported OpenRouter calls. These totals need the deduplication repair. Aggregate `cache_hit_rate` is **null**, including where usage is unavailable for four calls; do not convert null to zero. Four review runs have reported rates **61.9%, 62.2%, 75.7%, and 84.6%**; their simple mean is **71.1%**, not a population estimate. Release-test run 38253656098 is **0%** on 10 calls.

Unstable prefixes or dynamic noise *could* fragment cache entries, but this archive contains no prefix hashes or miss reasons to establish either cause. Emit `CACHE_PREFIX` with a non-content hash, static-prefix version, model, breakpoint status, prompt-component token counts, and hit/miss reason. Keep invariant instructions before variable run data where output remains equivalent; compare cache-read tokens and latency on matched workloads. That is a **low-risk measurement and ordering change**, with token and latency gains unquantified until replay. The two review budget warnings warrant measuring which component expanded, not silently truncating the rubric. Memory’s 11/11 observed hits are positive, but the near-full 1,400-token allocation leaves little headroom; measure whether selected records contribute to outcomes before raising it.

## Orchestrator Health

- **Progression is not inferable from dispatch counts alone.** Clarify had **146 skipped/153 runs**, plan **138/143**, implement **129 other plus eight cancelled/143**, and clarify-respond **140 skipped/142**. Recent run IDs 38271378709 (clarify), 38271378699 (plan), and 38271378781 (implement) all skipped. Log one eligibility/disposition record per dispatch with issue, wave, guard reason and terminal state; do not call skips successful phase completions.
- **Poll and merge state deserve correlation IDs.** Orchestrate-poll had **9 successes, 5 cancellations and 1 failure/15 runs**; the failure, 38246066105, lacks logs. Review runs for PR #7044—38246151160, 38252233393 and 38258869453—each succeeded but took **4,307s, 4,217s and 4,385s**. Log PR head, iteration, triggering change, deferral reason, conflict-heal attempt and judge disposition to separate intentional new cycles from churn.
- **Do not mistake negative-test output for live conflict regressions.** CI orchestrate-poll test logs intentionally emit fingerprint-verification errors; the named CI failing tests and checkout failures are the operational evidence. Track live `conflict_heal_attempts`, `judge_dispatches`, `stuck_age_ms` and terminal reasons separately. A workflow-heal intake summary for run 38268901859 reports `scope_guard_disabled`; record that as a guarded disposition, not a successful heal.

## Pipeline Flow Bottlenecks

| Stage | Observed bottleneck | Type | Next action, ordered by end-to-end impact |
|---|---|---|---|
| Clarify → plan → implement | Predominantly skipped dispatches; successful implement runs can still take **2,227s** (38260483740). | Eligibility versus compute not separated | Emit guard reason and active phase timers before altering dispatch frequency. |
| Review/autofix | **70 runs**, p50 **987s**, p95 **4,266.5s**; two failures after PR close and high multi-model token use. | Compute plus avoidable terminal-path work | Fix closed-PR gating first; then measure distinct model passes and review iterations. |
| Validate → release test | Child validation-refresh failed after **1,288s**; parent release test failed after **5,292s**. | Child failure / potential rerun | Retry transient checkout reads and report child outcome categories at the parent. |
| Orchestrate / merge | Poll p50 **968s**; merge-train scans took **192–218s**, with no per-category timing. | Polling and merge/conflict overhead unknown | Add scan-stage and live conflict/judge disposition timers; retain existing safety checks. |
| Runner acquisition | Readiness run 38271033352 waited about **150s**; CI run 38268305673 about **85s**. | Queueing | Publish job-level queue p50/p95 separately from execution; test only safe duplicate-dispatch coalescing. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** review/autofix p95 **4,266.5s**, CI p50 **904s**, and queue waits up to **150s** in the cited sample. **Top failure modes:** seven CI contract/install failures, two closed-PR review failures, and a validation child error that blocked one release test; the poll failure has no archive. **Highest-cost driver:** review/autofix contributes **63.64M/63.68M reported** OpenRouter tokens, but duplicate logs inflate that figure.

**Top three actions:**
1. Repair parent/step event deduplication and emit explicit cost-coverage and call IDs; this removes **13.35M falsely reported deep-dive tokens**, with no claimed bill reduction.
2. Guard review work after confirmed PR close; address the **two observed failures** and the **300s** needless wait.
3. Add bounded retry and per-stage diagnostics to validation checkout; preserve consumer-red versus mechanism-error distinctions to protect release gating.

## Metrics Appendix

**Scope:** one repository, 1,000 runs; archive generated **October 12, 2026, 15:40 UTC**. Assembled context reports telemetry on **120 runs**; mounted `summary.json` reports **22 deep-dive runs**. They are different coverage sets. The archive has one missing failed-run log (38246066105). Percentiles describe runs, not individual steps.

| Population | Runs | Success | Failure | Cancelled | Skipped | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| All | 1,000 | 264 (26.4%) | 12 (1.2%) | 14 | 710 | 2s / 1,252s |
| Success or failure only, derived | 276 | 264 (95.7%) | 12 (4.3%) | — | — | ≈231s / ≈2,499s |
| CI | 49 | 42 | 7 (14.3%) | 0 | 0 | 904s / 1,359.8s |
| Review/autofix | 70 | 65 | 2 (2.9% of all review runs) | 1 | 2 | 987s / 4,266.5s |
| Orchestrate-poll | 15 | 9 | 1 | 5 | 0 | 968s / 1,800.7s |

| Assembled-context telemetry, **reported, not dedup-corrected** | Value |
|---|---:|
| OpenRouter calls; usage available / unavailable | 108; 104 / 4 |
| OpenRouter total; prompt; completion tokens | 63,683,625; 11,383,691; 730,059 |
| Cache read / write tokens | 47,625,161 / 3,987,540 |
| `cache_hit_rate` aggregate | null; four sampled review values 61.9%–84.6%, one release value 0% |
| Codex calls / tokens | 2 / 4,052 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 2,000 / 5,272,740; 119 samples—includes skips |
| `break_glass_count` / `context_budget_warn_count` | 0 / 2; warnings in review runs 38246098799 and 38252203873 |
| Deep-dive duplicate correction, whole-job-only | 88 → 66 OpenRouter calls; 56,248,627 → **42,898,670** tokens |

| MCP server and target | Reported queries / bytes | Fallbacks | Bootstrap / contribution | Probe OK / failed / skipped |
|---|---:|---:|---|---:|
| Semble `reviewer-context` | 9 / 134,703 | 0 | 9 bootstraps, 321,997ms; 76 sources, 2,502 static-duplicate bytes | 0 / 0 / 0; no probe evidence |
| Semble `overflow` | 0 / 0 | 52, all contract-test; **0 runtime** | No runtime contribution measured | 0 / 0 / 0; no probe evidence |
| Serena, no target observed | 0 / 0 response bytes; 0 tool calls | 0 | Per-tool breakdown: none observed | 0 / 0 / 0; availability unknown |

At least **two** reported Semble queries, **29,404 bytes**, two bootstraps and four CI fallback counts are verified parent/step duplicates in the mounted archive; do not use reported MCP counts as independent availability trials. **Other MCP servers observed:** none in inspected logs.

| GH API signal | Measured value / gap |
|---|---|
| Per-workflow calls, retries and rate-limit events | **Not collected**; PAT `used_in_job=unknown` in reviewed deep dives |
| Missing archive | One **HTTP 404**: collector fetch for run 38246066105 |
| Validation checkout | One observed GraphQL error: run 38253831133 |
| Merge-train scan | 29 PRs examined, zero released in run 38270862087; **actual API calls unmeasured** |
| Existing batching | Cancel-on-close uses two status-filtered run listings and PR-state GraphQL batches of up to 50; merge-train caches file lists within a scan |

The next collection should retain job queue timestamps, per-stage timers, API call-category counters, cost-event IDs and log-source coverage alongside the existing run ID and job/step identifiers.

## Deep Audit — Workflows & Scripts (2026-10-12)

### Section 1: Bug & Correctness Sweep

Audited the repository’s 55 `.github/workflows/*.yml` files and 216 top-level `scripts/*.sh` and `scripts/*.py` files. The existing report already covers the closed-PR review failure and validation checkout error; they are not repeated here.

- **BUG-001** — **File:** `.github/workflows/integration-pr-readiness.yml:25-32`; `scripts/orchestrate_poll_process.sh:3962-3997`. **Severity:** High. **Category:** `bug`. **Description:** The readiness workflow runs on PR events, not tracking-issue edits. The poller refreshes the status when *its rendered body* changes or its stored refresh hash differs. **Inference:** a human can change checked boxes on the live tracking issue after a success status without either path reposting failure on the unchanged PR head. [NEEDS VERIFICATION] **Recommended fix:** handle `issues.edited` for verified tracking issues, resolve their open integration PR, and recompute the head-bound status from live issue and PR data; also reconcile live-body changes during poll ticks.
- **BUG-002** — **File:** `.github/workflows/integration-pr-readiness.yml:32-79`; `scripts/check_integration_pr_readiness.py:186-186,219-259`. **Severity:** High. **Category:** `bug`. **Description:** The job writes PR labels from its event payload to a file, and the script uses that snapshot to authorize the override. There is no per-PR concurrency guard. **Inference:** overlapping labeled/unlabeled jobs can finish out of order, leaving a success status from an older override snapshot after removal. [NEEDS VERIFICATION] **Recommended fix:** fetch live PR labels and verify the current head immediately before posting; serialize status writers per PR without treating an older event payload as authority.
- **BUG-003** — **File:** `scripts/review_merge_train.sh:871-887,992-1000,1410-1415`. **Severity:** Medium. **Category:** `bug`. **Description:** `_mt_upsert_comment` turns failed PATCH and POST calls into success with `|| true`. Its release caller’s failure warning therefore cannot report those failed writes; the gate likewise proceeds without knowing whether its queue marker was posted. **Recommended fix:** return the write’s failure status and have both callers log a structured warning while preserving their current fail-open queue/release behavior.
- **BUG-004** — **File:** `.github/workflows/test-and-mark-stable.yml:779-780,887-896`. **Severity:** Medium. **Category:** `bug`. **Description:** Under `pipefail`, `echo "$LAST_BOT_COMMENT" | grep -q` can evaluate false even when a multiline comment starts with `failed`: `grep -q` exits after the match and the writer receives SIGPIPE. A read-only reproduction with a large multiline value produced writer status 141 and grep status 0. That can delay recognition of a Clarify failure until timeout. **Recommended fix:** match the captured string with Bash `[[ ... =~ ... ]]`, or use a consumer that reads the complete input; preserve the existing two-stage failure classification.

No independently verified secret disclosure or title/body-to-shell injection finding emerged from this static pass.

### Section 2: GitHub API Call Redundancy Audit

Counts below are **code-path estimates**, not measured calls from workflow logs. Pagination and retries can add calls. Keep the merge train’s documented two-pass active-run safety check (`scripts/review_merge_train.sh:83-106`) unchanged.

- **API-001** — **File:** `scripts/review_merge_train.sh:798-805,868-886`. **Severity:** Medium. **Category:** `api-redundancy`. **Description:** For an existing marker, `_mt_find_marker_comment` paginates comments but returns only ID/time; `_mt_upsert_comment` then fetches that comment again solely for `.body`. This is distinct from the PR-list caching opportunity in the existing report. **Current → proposed:** **P paginated comment-list calls + 1 body GET → P calls** per existing-marker upsert. **Recommended fix:** extend `_mt_find_marker_comment` to return the selected body with its ID/time, with complete-page validation. Reuse its local result as `_mt_pr_meta_prefetch` already reuses cycle-local metadata; retain a targeted GET fallback if that result is unavailable.
- **BATCH-001** — **File:** `scripts/orchestrate_poll_process.sh:42-60,100-142`. **Severity:** Medium. **Category:** `api-batching`. **Description:** `replay_failed_reclarify_commands` makes an issue GET and a fully paginated comments GET inside its queued-issue loop. The adjacent `_fetch_blocked_issue_routing_graphql` demonstrates 25-issue aliased batches, but cannot be reused unchanged because replay checks older marker history. **Current → proposed:** **2N reads + three initial commands → `ceil(N/25)` GraphQL reads + three initial commands** when all N histories fit the verified batch; retain paginated REST fallback for incomplete histories. The initial issue-list command may itself paginate. **Recommended fix:** add a replay-specific aliased prefetch with comment `pageInfo` completeness checks, then fall back per incomplete issue. Preserve author, timestamp, and dedup checks before any POST or label removal. [NEEDS VERIFICATION]
- **BATCH-002** — **File:** `scripts/validation_refresh_runner.py:246-274,361-380`. **Severity:** Medium. **Category:** `api-batching`. **Description:** Every repository passes through a separate `gh repo view --json defaultBranchRef` before clone. **Current → proposed:** **N reads → `ceil(N/25)` aliased GraphQL reads**, plus individual legacy reads only for missing or unverified results. **Recommended fix:** prefetch validated registry entries once per cycle, extending the alias-and-keyed-cache pattern of `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh:17094-17124`; retain `_resolve_default_branch` as the per-repo fallback and its diagnostic.
- **BATCH-003** — **File:** `scripts/files_touched_scope_guard.py:239-258,275-307`. **Severity:** Low. **Category:** `api-batching`. **Description:** Origin-chain verification reads each discovered issue separately, up to `ORIGIN_MAX_ISSUES = 8` (`scripts/files_touched_scope_guard.py:116-117`), with at most two attempts each. **Current → proposed:** **N–2N reads → one aliased read per known breadth-first layer** when a layer’s results are complete; a linear chain still needs one read per layer. **Recommended fix:** consider an issue-node prefetch patterned on `_fetch_candidate_issue_details_graphql`, retaining the existing REST fallback and fail-closed verdict on incomplete identity, repository, author, body, or labels. Establish GraphQL/REST field parity before using it for an authority decision. [NEEDS VERIFICATION]
- **API-002** — **File:** `scripts/review_security_hold_sweep.py:309-329`. **Severity:** Medium. **Category:** `api-redundancy`. **Description:** After dispatch, any failed marker-comment POST is retried after two seconds, including a permanent error or an ambiguous response to a successful create. **Current → proposed:** **two POSTs → one POST** for permanent failures; an ambiguous result needs a marker read and a second POST *only if absence is verified*. **Recommended fix:** classify failures using the non-idempotent POST rule in `scripts/gh_api_retry.py:19-28`, and reconcile the existing head/cycle marker before a conditional retry. This may increase calls on ambiguous outcomes but avoids blind duplicate creates. [NEEDS VERIFICATION]

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — **File:** `scripts/watchdog_helpers.sh:186-201`; `scripts/review_rb_judge.sh:329-343`; `scripts/self_heal_validation.sh:143-158`; `scripts/review_run_reviewers.sh:406-420`; `scripts/review_conflict_resolve.sh:286-300`. **Severity:** Low. **Category:** `duplication`. **Description:** Five copies implement the same stall-guard state parser. **Recommended fix:** keep `read_codex_stall_guard_state <status_file>` in `scripts/watchdog_helpers.sh`; update the four other callers to source their verified support copy before use, with compatibility handling for older staged support.
- **DUP-002** — **File:** `scripts/review_run_reviewers.sh:2052-2061`; `scripts/review_conflict_prepare.sh:1265-1274`; `scripts/review_apply_fixes.sh:1022-1031`. **Severity:** Low. **Category:** `duplication`. **Description:** Three identical `append_semble_query_section` implementations duplicate the file-existence and byte-cap behavior. **Recommended fix:** put `append_semble_query_section <label> <path> [max_bytes]` in existing `scripts/semble_helpers.sh`; source it from all three callers without changing their prompt order.
- **DUP-003** — **File:** `.github/workflows/implement.yml:122-152`; `.github/workflows/orchestrate_clarify_respond.yml:75-105`; `.github/workflows/review_autofix.yml:326-356`; `.github/workflows/security-audit.yml:68-98`; `.github/workflows/validate.yml:104-134`. **Severity:** Low. **Category:** `duplication`. **Description:** Five matching inline bootstraps fetch the trusted GitHub API retry helper. **Recommended fix:** consolidate them in a **trusted, pinned** composite action owning `bootstrap_gh_retry_helper <ref> <destination>` and the `GH_API_RETRY_HELPERS` export. Update the five steps, but never execute a helper from a PR-head checkout merely to remove duplication.
- **DUP-004** — **File:** `.github/workflows/mark-stable.yml:1310-1346`; `.github/workflows/test-and-mark-stable.yml:6984-7017`. **Severity:** Low. **Category:** `duplication`. **Description:** The two consumer-dispatch blocks nearly match, but one uses `gh_retry gh api` and the other raw `gh api`. Both parse and loop over the same registry. **Recommended fix:** add a shared release helper, `dispatch_stable_release <version> <release_sha> <consumer_file>`, called by both workflows; validate registry entries and use one deliberate, non-idempotent POST policy rather than blindly retrying dispatch creation.
- **DUP-005** — **File:** `scripts/render_prompt.sh:12-41,138-159`; `scripts/assemble_prompt.sh:12-41,72-93`. **Severity:** Low. **Category:** `duplication`. **Description:** Both entrypoints repeat prompt-file resolution and assembly-source selection. **Recommended fix:** move the existing `resolve_prompt_file <path>` and `resolve_assembly_source_path <path>` interfaces into a new `scripts/prompt_path_helpers.sh`, sourced by both scripts with their current `SCRIPT_DIR` resolution preserved.

No additional function was proven unreachable, and no `TODO`, `FIXME`, or `HACK` marker was found in the scoped files.

### Section 4: Expression Size Limit Risk Assessment

**Measurement boundary:** These are static character counts of complete `run: |` bodies containing `${{ }}`, including placeholder text—not measurable runtime-expanded values. They are the requested **run-block risk proxy**, not proof that any individual `${{ }}` expression is already invalid: the longest individual interpolation found in a run block was approximately 180 characters. The six blocks below warrant runner-level verification before claiming an existing rejection. Large bodies *without* `${{ }}` were excluded.

- **EXPR-001** — **File:** `.github/workflows/implement.yml:1092-1496`. **Severity:** High. **Category:** `expression-limit`. **Description:** “Stage workflow support files” is approximately **23,940 characters**, with three interpolations: **2,940 over** the 21,000-character proxy limit. [NEEDS VERIFICATION] **Recommended fix:** extract the body to a trusted `scripts/` step helper and pass the three expression values through `env:`.
- **EXPR-002** — **File:** `.github/workflows/update_workflows.yml:837-1257`. **Severity:** High. **Category:** `expression-limit`. **Description:** “Commit and push updates” is approximately **23,792 characters**, with nine interpolations: **2,792 over** the proxy limit. [NEEDS VERIFICATION] **Recommended fix:** extract the commit/push logic to a staged script; pass step outputs as environment variables rather than interpolating them in its body.
- **EXPR-003** — **File:** `.github/workflows/implement.yml:3712-4104`. **Severity:** High. **Category:** `expression-limit`. **Description:** “Preflight destructive-commit guard” is approximately **23,596 characters**, with one interpolation: **2,596 over** the proxy limit. [NEEDS VERIFICATION] **Recommended fix:** move this guard into a support script and pass `github.repository` through `env:`.
- **EXPR-004** — **File:** `.github/workflows/plan.yml:2107-2414`. **Severity:** High. **Category:** `expression-limit`. **Description:** “Auto-approve clear plan” is approximately **18,605 characters**, with five interpolations and **2,395 characters of proxy headroom**. [NEEDS VERIFICATION] **Recommended fix:** extract the approval and comment-handling body to a support script, passing repository identity via `env:`.
- **EXPR-005** — **File:** `.github/workflows/workflow-log-analysis.yml:915-1212`. **Severity:** Medium. **Category:** `expression-limit`. **Description:** “Run workflow log analysis” is approximately **16,044 characters**, with 12 interpolations and **4,956 characters of proxy headroom**. [NEEDS VERIFICATION] **Recommended fix:** move its runner logic into `scripts/` and supply run metadata through `env:`.
- **EXPR-006** — **File:** `.github/workflows/test-and-mark-stable.yml:1240-1501`. **Severity:** Medium. **Category:** `expression-limit`. **Description:** “Phase 3b: Wait for PR creation” is approximately **15,825 characters**, with one interpolation and **5,175 characters of proxy headroom**. [NEEDS VERIFICATION] **Recommended fix:** extract the polling body to a shared script, retaining the workflow step’s name, ID, conditions, and outputs.

The largest workflow is `.github/workflows/test-and-mark-stable.yml` at **418,666 bytes**: none exceeds the requested 800 KB warning threshold. It is also below the repository’s stricter 480,000-byte CI guard documented in `CLAUDE.md:1697-1728`. The longest measured `if:` scalar was about **1,224 characters**, not near the stated expression threshold.

### Section 5: Cross-Cutting Concerns

- **SHELL-001** — **File:** `.github/workflows/mark-stable.yml:1333-1345`; `.github/workflows/test-and-mark-stable.yml:7004-7016`. **Severity:** Low. **Category:** `shellcheck`. **Description:** Both release paths suppress registry parse errors with `|| true` and iterate `for REPO in $REPOS`. Word-splitting and glob expansion are intentional only if every JSON entry is a valid repository name; malformed data can instead silently skip dispatch or alter loop items. **Recommended fix:** validate the JSON array and each `owner/repo` string, then load entries with `mapfile` and iterate `"${repos[@]}"`; distinguish an empty registry from a parse failure.
- **SHELL-002** — **File:** `.github/workflows/test-and-mark-stable.yml:899-899,1191-1191,1491-1491,2464-2464,3804-3804`. **Severity:** Low. **Category:** `shellcheck`. **Description:** Five timeout tests use unquoted `$IDLE` and `$INACTIVITY_LIMIT` in `[ ... ]`. They appear numeric on the normal path, but the tests lack the quoting discipline used elsewhere and are not covered by inline ShellCheck in CI. **Recommended fix:** quote both operands or use validated arithmetic comparisons in each block.
- **DEBT-001** — **File:** `.github/workflows/ci.yml:301-314,403-425`. **Severity:** Medium. **Category:** `tech-debt`. **Description:** CI disables actionlint’s ShellCheck integration for every inline workflow `run:` block; its separate ShellCheck loop checks script files only and fails at `--severity=error`. Thus inline warning-level defects—including the release loops above—are not a CI gate. **Recommended fix:** add a staged warning-level lint gate for changed inline Bash blocks, or extract those bodies to checked scripts, while baselining existing warnings rather than abruptly failing unrelated workflows.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 6 | BUG-001, BUG-002, EXPR-001, EXPR-002, EXPR-003, EXPR-004 |
| Medium | 9 | BUG-003, BUG-004, API-001, API-002, BATCH-001, BATCH-002, EXPR-005, EXPR-006, DEBT-001 |
| Low | 8 | BATCH-003, DUP-001, DUP-002, DUP-003, DUP-004, DUP-005, SHELL-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 3 existing readiness workflow/script/poller files | Medium |
| API call optimization | 5 existing scripts, plus tests | Large |
| Code modularization | At least 12 existing workflows/scripts, plus shared helpers | Large |
| Expression size reduction | 5 workflows, plus extracted scripts | Large |
| Medium/Low fixes | 4 existing workflow/script files, plus tests | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-10-12)

### Safety Tag Legend

`SAFE_TO_MERGE` is authorized for implementation as written; `NEEDS_VERIFICATION` requires the stated checks first; `RISKY_SKIP` must not be auto-implemented because a safety-sensitive call is involved.

### Consolidation Candidates (MERGE-###)

- **MERGE-001 — NEEDS_VERIFICATION.** **Calls:** `scripts/review_collect_pr_metadata.sh:265-274` and `scripts/review_collect_pr_metadata.sh:308-329` (via `_fetch_linked_issue_bodies_graphql`, `scripts/review_collect_pr_metadata.sh:87-180`). **Current → proposed:** two GraphQL reads → one when the closing-reference read succeeds but returns no issues and PR text supplies fallback issue numbers; retain the second read when the first fails. **Endpoint:** `POST /graphql`. **Evidence:** the PR-text issue numbers are computed at `scripts/review_collect_pr_metadata.sh:246-258`, before the closing-reference query. An empty result currently triggers a second query for those already-known numbers. **Proposed fix:** extend the initial closing-reference query with aliases for at most 20 PR-text issue numbers, using the alias pattern in `_fetch_candidate_issue_details_graphql` (`scripts/orchestrate_poll_process.sh:17094-17124`). Update its response parsing and `_fetch_linked_issue_bodies_graphql`’s caller to use those aliases only on the existing empty-reference path; retain the separate fallback after a failed first read. **Safety rationale:** although both reads use the same GraphQL endpoint and token, combining their responses changes failure handling and the `LINKED_ISSUES_RAW_FILE.status` completeness contract, so identical error semantics are not yet proven. **Downstream signal:** Test empty, nonempty, failed, partial-alias and 50-reference responses, plus the 20-issue cap; verify unchanged `LINKED_ISSUES_JSON`, prompt context, status sidecar, and retry/fallback behavior before implementation.

### Redundant Re-Fetch (REUSE-###)

- **REUSE-001 — RISKY_SKIP.** **Calls:** `scripts/workflow_failure_heal_intake.sh:469-471` and `scripts/workflow_failure_heal_intake.sh:874-884`; the earlier result is consumed for provenance at `scripts/workflow_failure_heal_intake.sh:489-491`. **Current → proposed:** two `GET /user` reads → one for a verified `autofix_failure` with numeric `SOURCE_GEN` when `PROVENANCE_LOGIN` is nonempty; retain the later read on cache miss. **Endpoint:** `GET /user`. **Evidence:** the later lineage-author selection starts with `PHASE_COMMENT_AUTHOR`, but does not consult the already-fetched `PROVENANCE_LOGIN` before querying `/user` again. **Proposed fix:** initialize `HEAL_TRUSTED_AUTHOR` from `PHASE_COMMENT_AUTHOR`, then `PROVENANCE_LOGIN`, retaining the existing `/user` fallback when both are empty. **Safety rationale:** both reads participate in identity and provenance decisions—an explicit `RISKY_SKIP` trigger—even though the script does not change its token between them. **Downstream signal:** Do not auto-implement; manually review token identity, failed-first-read recovery, and lineage-author tests for phase, autofix, and workflow-run reports.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: RISKY_SKIP — its source comment read is paginated, and the gate can mutate queue state before upsert.
- BATCH-001: RISKY_SKIP — poller replay depends on complete paginated history and defends against duplicate-command races.
- BATCH-002: NEEDS_VERIFICATION — confirm GraphQL/default-branch field parity and preserve per-repository fallback diagnostics.
- BATCH-003: RISKY_SKIP — origin reads are in a two-attempt retry path and authorize a trust-chain verdict.
- API-002: RISKY_SKIP — the second non-idempotent POST is a retry after an ambiguous write outcome; reconciliation needs manual review.

### Summary Counts

*Counts cover net-new findings above; Deep Audit cross-references are excluded.*

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 1 | MERGE-001 |
| RISKY_SKIP | 1 | REUSE-001 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
