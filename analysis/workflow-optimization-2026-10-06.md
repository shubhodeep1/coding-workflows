## Executive Summary

- **Planning has a confirmed compatibility failure.** In five archived `plan / plan` runs—including 37385496261 and 37394219899—the staged `codex_stall_guard.sh` rejected `--engine` on all three attempts. A preflight against the *staged* script would prevent the identical retries and fail before further preparation. The five failed runs consumed **2,349 seconds combined**; not all of that time is recoverable. **Confidence: high.**
- **CI regressions are the largest observed failure cluster.** CI failed in **10/32 runs**; six failed at `Orchestrate poll process unit tests`. Runs 37390665575 and 37392114833 repeat judge-safety assertion failures. Fix the implementation or fixtures without relaxing the guards; this addresses up to six observed failing runs, subject to other tests passing. **Confidence: high.**
- **Review latency includes distinct wait problems.** Run 37387443320 waited **679 seconds for a runner**, then logged a **1,423-second gap** between agent eligibility evaluation and job start whose cause is not established. Its check-run collection also took **301 seconds** waiting for an in-progress check. Log these intervals separately before changing concurrency or polling. **Confidence: high for timings; low for the handoff cause.**
- **A late remote error wasted an expensive review.** Run 37387112300 failed after **2,870 seconds** at `Push all pending commits`: the remote rejected the push with an Internal Server Error, and the workflow did not retry it. It had logged **36.27 million usage tokens**. A bounded, ancestry-checked retry for an explicit transient server error could avoid a full rerun without weakening branch protection. **Confidence: high for the failure; medium for prospective savings.**
- **Telemetry is useful but incomplete.** The assembled window reports **199.36 million usage tokens**, **77.37% cache hit rate**, and **zero observed runtime Semble fallbacks**. It has no GH API request counts, dollar prices, or production Serena probes. Add endpoint-level and handoff diagnostics before claiming API savings or changing models. **Confidence: high.**

## Speed Optimizations

| Rank | Evidence and root cause | Exact change; estimated saving | Risk |
|---|---|---|---|
| **1 — critical path: runner and handoff wait** | Review 37387443320: `review / gate` waited **679s** after “Waiting for a runner”; CI 37386924253 has a poll job waiting **749s**. Separately, review 37387443320 has an unexplained **1,423s gate-to-agent eligibility/start gap**—do not count it as confirmed runner queue. | Log `job_eligible_at`, `job_defined_at`, `runner_started_at`, and supersession reason. Audit existing same-PR concurrency so *safely superseded* work can release capacity before model execution, while preserving runs with unpushed edits. **Potential:** up to the observed ~11-minute runner wait on similarly contended runs; handoff savings are unquantifiable until diagnosed. | **Medium:** unsafe cancellation could lose work. |
| **2 — critical path: pending-check wait** | Review 37387443320 spent **301s** in `Collect PR check-run failures`, logging seven sleeps totaling **296s** for one pending check; this was waiting, not demonstrated API slowness. | Emit `CHECK_WAIT` with fetch count, accumulated sleep, pending count, final status, and deadline result. Test whether any reviewer work is independent of final check context; overlap only that work, then refresh the check snapshot before an edit decision. **Potential:** at most **301s** in this run if independence is established. Never omit the final check. | **Medium:** stale CI context could affect fixes. |
| **3 — failed-run waste: planning preflight** | Five archived planning failures retried the same unsupported `--engine` argument three times, with **10s + 20s** backoff per run. The current checkout’s guard accepts the option; the run’s staged copy did not. | Before memory retrieval or model setup, validate the staged guard’s options and log its version/hash and selected engine. Classify option/usage exit 2 as non-retryable. **Saving on this failure mode:** **30s retry sleep per run**, plus later doomed preparation if the check runs early; no claimed improvement to successful planning time. | **Low.** |
| **4 — feedback, not a shortcut around CI** | CI 37392114833’s poll-test shards logged roughly **374–411s** each; several judge tests failed. | Run the relevant judge-safety contract tests as an early, separate signal while retaining full required CI. **Impact:** earlier failure identification; mandatory full-CI wall-time saving is **not established**. | **Low** if required checks remain unchanged. |

Semble calls are a micro-optimization here: observed query timings are around **0.4–0.6s** per marker, far below the waits and model steps above.

## Cost Optimizations

1. **Avoid expensive late-stage reruns first.** Review 37387112300 logged **24 OpenRouter usage calls** and **36.27M total usage tokens** before its remote-500 push failure. Add the safe push retry described below and log whether a subsequent run actually repeated the review. **Potential saving:** up to one repeated review *if* this failure causes a full rerun; neither rerun occurrence nor dollar cost is established. **Quality risk:** none from preserving the existing review and push ancestry checks.

2. **Measure phase value before reducing model effort.** The assembled review telemetry is **208 calls**, **35.73M prompt**, **1.53M completion**, **9.04M cache-write**, and **153.06M cache-read tokens**. Run 37387112300 alone used 24 calls; archived planning logs configure high reasoning effort, while summarized run 37395114759 names a lighter model for materiality/summarization. Add per-phase `model`, `reasoning_effort`, usage, elapsed time, and accepted-outcome counters; trial a cheaper setting only for a demonstrated low-risk phase. **Savings:** unknown without per-model prices and phase attribution; **quality risk:** high for judge, security, and editing decisions, so do not downgrade those blindly.

3. **Reduce repeated uncached context without hiding evidence.** Overall `cache_hit_rate` is **77.37%**, but slow reviews 37381317564 and 37387443320 show **56.70%** and **62.10%** respectively. Reuse a stable instruction prefix and cycle-local issue/PR facts; place run-specific noise after that prefix and select only relevant memory/context. As a *scenario, not a forecast*, a 10% reduction in the measured **35.73M prompt-token** component equals about **3.57M tokens** in this window. **Quality risk:** medium unless required evidence and rubric text remain intact.

4. **Evaluate context tools on outcomes, not byte counts alone.** The assembled data logs **27 `SEMBLE_QUERY` markers / 317,088 `bytes=`**, with no runtime fallback. Archived `review / codex-agent / Run reviewer models` queries commonly log ~13KB for `target=reviewer-context`; `overflow` and `conflict-resolver-context` also occur. This is targeted context, but no no-Semble prompt baseline proves reduced expansion. Log query purpose, injected bytes, subsequent prompt delta, and useful-result status; cap only demonstrated low-value output. Serena has **zero queries, tool calls, response bytes, and probes**; summarized review 37395114759 says it was disabled. Do not claim Serena replaced downstream work or recommend a rollout on this evidence. **Savings and quality effect:** unknown pending comparison.

5. **Record cancellation cost.** Seven review runs were cancelled; 37386869805 lasted **2,387s**, but has no usage record here. Record cancellation phase and usage accrued before considering an earlier safe supersession gate. **Potential saving:** unquantifiable; preserve in-progress edits.

## Reliability Improvements

| Rank | Failure evidence; category | Smallest safe fix and diagnostic addition | Expected impact; rollback/fail-open |
|---|---|---|---|
| **1** | Five inspected `plan / plan` failures, including 37394219899: `codex_stall_guard.sh: unknown option: --engine`, followed by `no_transcript` and three identical attempts. **Staged-interface mismatch**, not proven model-capacity failure. | Check the staged guard and wrapper together before invocation; emit `PLANNING_GUARD_PRECHECK` with run, staged-script hash/version, engine, supported-option result, and exit category. Do not retry usage exit 2 or switch models for it. | Prevents this confirmed five-run failure mode. Roll back the preflight if it misclassifies an option; retain the existing invocation contract. |
| **2** | CI **10/32 failures**: six poll-test steps, including repeated `test_review_blocked_merged_fix_target_rejects_unrelated_followup` failures reporting `head_repo_mismatch` in 37390665575 and 37392114833. Security tests in 37384706088/37386834838 rejected `python3 -c`; 37386924253’s push-parser tests expected a guarded destination but got none. **Contract/security regressions**; some `##[error]` lines elsewhere are intentional negative-test output. | Preserve the guards. Emit a concise `CI_TEST_FAILURE` containing test ID, assertion category, expected/actual *safe identifiers*, and fixture version; fix code or fixture based on that distinction. | Could clear the observed CI failures once their assertions pass; do not fail-open or remove security tests. |
| **3** | Review 37387112300: pre-push check reported a fresh base; remote then returned an Internal Server Error and the workflow’s non-fast-forward-only retry classifier stopped. **Transient remote-service error**, distinct from merge conflict. | For an explicit server-error rejection, re-fetch the branch, check whether the commit already landed and whether an external tip advanced, then make at most a few backoff retries **without force-push**. Emit `REVIEW_PUSH_ATTEMPT` with attempt, rejection class, ancestry result, and outcome—never credentials or raw remote output. | Could prevent recurrence of this one observed late failure. Keep hard failure for ambiguous results, auth errors, or unsafe ancestry; feature-flag the new retry for rollback. |
| **4** | Assembled telemetry: **60 `SEMBLE_FALLBACK`**, all **contract-test** fallbacks, `target=overflow`; **0 runtime** fallbacks. No Serena query or probe is recorded. | Keep the tested fail-open behavior and separately log runtime availability, probe result, target, and fallback reason. Do **not** treat test fixtures’ deliberately missing Semble binary as a broken production rollout or zero Serena probes as proof of availability. | Better detection of a masked rollout; no failure-rate reduction can yet be estimated. |

`break_glass_count=0` and `context_budget_warn_count=0` in the covered logs: there is **no observed policy/rubric-pressure or model-window warning** to quantify. CI tests of prompt character/byte caps are test evidence, not production `CONTEXT_BUDGET_WARN` events. Continue emitting both markers and report their coverage denominator.

## AI Memory Health

- Across archived logs, **21 distinct retrieve payloads** (deduplicated by run and JSON payload) all selected records: **21/21 positive**, none with zero records. Mean `estimated_tokens` was **1,172** against mean budget **1,305**; keyword methods were **`llm` 21, `plain` 0, `none` 0**. Planning averaged **931/1,200** tokens; reviewer retrieval averaged **1,390/1,400**, leaving little *memory-allocation* headroom. Run 37387112300’s reviewer retrieve selected 24 records at 1,379/1,400. Preserve relevant selections but log which categories fill the budget; this should reduce low-value context without asserting model-window pressure.
- No archived retrieve reports `enabled:false`, `fail_open:true`, or a zero-record miss. A **separate summarized** `issue_pr_status` run, 37395114695, reports `finalize-task` with `fail_open:true`, `reason=no_linked_issues`; that is not evidence of a failed retrieval. Summarized run 37395836573 reports a successful merged-task finalization. Keep operation-specific `reason` and `ok` fields so these outcomes remain distinguishable.
- **Sixteen distinct archived memory events** report more than one push attempt while ultimately reporting success. In review 37387443320, `Record review run start in memory` took approximately **115s** and reported **10 push attempts**. Add `push_duration_ms`, retry reason category, and contention outcome to `AI_MEMORY_TELEMETRY`; investigate that run before changing fail-open behavior. No `promote` or `compact` event was found in the archived subset—verify emission during those operations rather than inferring they never occur.

## GH API Call Audit

**Request counts, status distributions, rate-limit remaining, and retry totals were not collected.** No selected runtime log establishes rate-limit exhaustion. Instrument existing wrappers locally with `GH_API_CALL` fields for workflow/job/step, **endpoint template** (not full URL or token), method, pages, attempts, HTTP class, elapsed milliseconds, and remaining limit; aggregate without adding a diagnostic API request.

| Observed path | What can be said now | Safe call-count action and rate-limit effect |
|---|---|---|
| Review 37387443320, `Collect PR check-run failures` | Seven logged sleeps imply **at least eight fetch invocations** if the loop reached another snapshot; actual HTTP requests can exceed invocations through pagination/retries. `scripts/collect_pr_check_runs_context.py` already uses `gh api --paginate --slurp`, 100 per page, and backs off. | Keep polling while status changes matter; log fetch/page/retry counts and reuse its written snapshot downstream. **Measured redundant calls: none established; current reduction estimate: unknown.** Avoid an unmeasured tighter loop that increases shared-limit risk. |
| Non-orchestrator readiness run 37396067386 | Its **11s** success reported readiness not applicable. The script uses the event head ref and must still **POST a neutral required-check status**; it does not need a tracking-issue lookup on this path. | Retain the status POST. Skipping this apparently no-op workflow would break its required-check contract; **safe demonstrated call saving: zero**. |
| Production `orchestrate_poll` | **9/9 runs succeeded**, but none has collected log telemetry here; per-item call counts cannot be audited. | Add batch size, cycle-cache hits/misses, fallback calls, and endpoint-template counters. Follow this repo’s `CLAUDE.md §15`: reuse cycle-local facts and aliased GraphQL batches where supported, fail open to the smallest safe legacy read. If a measured \(N\)-item loop is found, batching to size \(B\) saves \(N-\lceil N/B\rceil\) calls; **\(N\) and current savings are unknown**. |

## Prompt Cache & Memory System

The assembled review sample has **153.06M cache-read** and **9.04M cache-write tokens**, with a **77.37%** aggregate hit rate under the collector’s read/(prompt + write + read) definition. Run 37387112300 reached **85.39%** and still failed at push: cache performance does not cure late reliability failures. Runs 37381317564 (**56.70%**) and 37387443320 (**62.10%**) warrant prefix inspection, but their misses do not by themselves prove fragmentation.

Emit a privacy-safe stable-prefix hash, static/dynamic prefix byte counts, per-phase cache read/write/uncached tokens, and cache-disable/fail-open reason. Then move timestamps, run IDs, and other dynamic material *after* reusable instructions where semantics permit; retain the full safety rubric. Compare cache hit rate and accepted-review outcomes before rollout. **Token/latency gain is unmeasured** until prefix variance is captured; bounded prompt-reduction scenario is in Cost Optimizations. The memory retrieval results above are positive, but near-full reviewer budgets justify measuring selected-record usefulness rather than indiscriminately increasing memory injection.

## Orchestrator Health

- **Clarify-to-plan progressed, then failed safely:** plan 37394219899 claimed an `answer` command, later recorded `processed-command-complete` with `status=failed` and a `phase_failed` event after the guard error. This is evidence of failure bookkeeping, not a demonstrated stuck claim. Add `command_claim_age_ms` and terminal transition reason so abandoned claims can be distinguished; impact is faster diagnosis, with no state-machine change.
- **Many runs are intentionally gated, not failed:** **646/1,000** conclusions are `skipped`; `clarify` has 151/156 other/skipped and `implement` 149/152. Recent run 37396038221 is a one-second skipped clarify. Record skip reason from workflow event/job metadata at collection time—there is no running step in which to print a log. This would distinguish healthy filtering from stalled progression without launching extra jobs.
- **Wave, deferral, and judge-cycle health remains unmeasured.** All nine `orchestrate_poll` runs succeeded, but have zero parsed logs in this selection. Review 37390688134 did spend **81s** in a conflict-resolver/validate/commit step; that alone does not establish recurring conflict-heal retries. Add `ORCHESTRATOR_TRANSITION` with cycle/wave, prior/next state, deferral or judge reason, retry count, and time in state; monitor age of pending waves and terminal-state transitions. Preserve existing fail-closed merge and judge checks.

## Pipeline Flow Bottlenecks

| Stage | Observed bottleneck type | End-to-end priority |
|---|---|---|
| Clarify → plan | Most clarify runs skip quickly; **8/154 plan runs failed**, with five archived failures proving the staged-guard mismatch. **Retry/preparation overhead**, not established model latency. | **First:** preflight staged interfaces and stop non-retryable retries. |
| Implement → review/autofix | Implement is mostly gated; active review has **p50 564s, p95 3,163.6s**. Review 37387443320 combines **679s runner queue**, unexplained handoff, **301s check wait**, and **794s reviewer-model step**. | **Next:** separate queue/handoff/check/model timing; safely reduce superseded work, then test overlap only for independent tasks. |
| Validate/CI → orchestrate | CI **p50 869.5s, p95 1,441.75s**, with **10/32 failures**; selected poll shards take minutes. Production orchestrator poll **p50 557s** but lacks step evidence. **Compute plus runner contention and failing validations** are visible; wave retries are not. | Fix CI assertions and collect production poll transitions before changing cadence. |
| Merge/push | Review 37390688134’s resolver step took **81s**; review 37387112300 failed after **2,870s** on a remote push 500, not a recorded merge conflict. | Add bounded safe push recovery before optimizing the comparatively small observed resolver step. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** contended review/CI runners, the unclassified review gate-to-agent handoff, pending-check waits, and long review model steps. **Top failure modes:** staged planning-guard incompatibility (five confirmed runs), judge/security CI regressions (10 CI failures overall), and one late remote push rejection. **Highest-cost driver:** review/autofix’s **199.36M logged usage tokens** in the assembled telemetry; dollar cost and cancelled-run usage are unavailable.

**Prioritized actions:** (1) preflight the staged planning guard and classify usage errors as non-retryable; (2) fix the named CI assertions without weakening safeguards; (3) add ancestry-checked transient push recovery plus queue, API, and phase-timing diagnostics. These address demonstrated failure waste before riskier model or concurrency changes.

## Metrics Appendix

**Scope and coverage.** The archive’s 1,000 run rows span **October 5, 2026 22:15:13 UTC–October 6, 2026 00:50:15 UTC**. “Other” means skipped here; it is **not** failure. The assembled context reports telemetry for **125 runs**, whereas the archive’s `summary.json` reports **28** parsed runs; archive downloads are 28 successful, 12 empty, and 960 not selected. The assembled totals below include its wider supplied coverage; **do not add** archive totals to them.

| Workflow family | Runs | Success | Failure | Cancelled | Skipped | Failure/all runs | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| **All** | **1,000** | **328** | **19** | **7** | **646** | **1.9%** | **2s / 1,430s** |
| CI | 32 | 22 | 10 | 0 | 0 | 31.25% | 869.5s / 1,441.75s |
| Plan | 154 | 3 | 8 | 0 | 143 | 5.19% | 1s / 484.15s |
| Review/autofix | 187 | 179 | 1 | 7 | 0 | 0.53% | 564s / 3,163.6s |
| Clarify | 156 | 5 | 0 | 0 | 151 | 0% | 1s / 11s |
| Implement | 152 | 3 | 0 | 0 | 149 | 0% | 1s / 11s |
| Orchestrate poll | 9 | 9 | 0 | 0 | 0 | 0% | 557s / 685.2s |

| Assembled cost/review telemetry | Value | Coverage qualification |
|---|---:|---|
| OpenRouter calls; usage available/unavailable | 208; 208/0 | Logged calls, not all 1,000 runs |
| Prompt / completion / total usage tokens | 35,731,785 / 1,528,024 / 199,361,289 | Total includes cache-token accounting; components should not be re-added |
| Cache-write / cache-read tokens; `cache_hit_rate` | 9,044,590 / 153,059,453; **77.37%** | Aggregate over covered usage |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 7,000 / 3,680,700 | **123** sampled run clocks; many short skipped runs |
| Review-only `wall_clock_p50_ms` / `wall_clock_p99_ms` | 3,208,000 / 3,963,720 | **17** sampled review clocks |
| `break_glass_count` / `context_budget_warn_count` | **0 / 0** | Covered logs only |
| Codex calls / reported tokens | 12 / 6 | Not a substitute for missing per-model billing |
| GH API requests / retries / rate-limit events | **Not collected / not collected / not collected** | No zero-rate-limit claim |
| Archived job runner-wait proxy | p50 **18.5s**, p95 approximately **663s** | **120 selected job system logs**, not population-wide |

| MCP server/target | Assembled markers and logged bytes | Archived target detail and availability |
|---|---|---|
| Semble, all query targets | **27 `SEMBLE_QUERY`; 317,088 `bytes=`** (~11,744 bytes/marker) | Archived subset: reviewer-context **16 / 218,576 bytes**; overflow **5 / 40,326**; conflict-resolver-context **2 / 16,030**. Combined/per-step logs can repeat markers, so these are **logged occurrences, not proven distinct service requests**. |
| Semble fallback, `overflow` | **60 total; 60 `context=contract-test`; 0 runtime** | Deliberately missing-binary tests, not a production failure rate. |
| Serena, all targets/tools | **0 queries, 0 fallbacks, 0 response bytes, 0 tool calls, 0 query ms** | No per-tool breakdown exists because no tool was observed; availability untested. |

| MCP availability target observed in query/fallback evidence | `probe_ok` | `probe_failed` | `probe_skipped` |
|---|---:|---:|---:|
| Semble `reviewer-context` | 0 | 0 | 0 |
| Semble `overflow` | 0 | 0 | 0 |
| Semble `conflict-resolver-context` | 0 | 0 | 0 |
| Serena — no target observed | 0 | 0 | 0 |

**Other MCP servers observed:** none with an emitted runtime `<NAME>_QUERY`, `<NAME>_FALLBACK`, or `<NAME>_PROBE` event in the inspected logs. Zero probe rows mean **no probe evidence**, not confirmed availability.

## Deep Audit — Workflows & Scripts (2026-10-06)

### Section 1: Bug & Correctness Sweep

The existing report already covers the staged planning-guard failure, late review push rejection, pending-check wait, and CI regressions; those are not repeated here.

- **BUG-001** — `.github/workflows/integration-pr-readiness.yml:25-33`; `scripts/check_integration_pr_readiness.py:219-259`. **Severity:** High. **Category:** `bug`. **Description:** Readiness is posted as a commit status, but the workflow runs only on PR events. Its own comment acknowledges that editing the tracking issue does not trigger reevaluation. **Inference:** If a previously ready tracking issue gains an unchecked task without a PR event, its earlier success status remains on the same head. **Recommended fix:** On tracking-issue changes, locate the open integration PR for that issue and rerun the readiness calculation against its current head; retain the existing PR-event check and fail closed when the current head cannot be established.

- **SEC-001** — `.github/workflows/mark-stable.yml:44-52`; `.github/workflows/test-and-mark-stable.yml:162-179`; `.github/workflows/workflow-log-analysis.yml:1160-1174`. **Severity:** High. **Category:** `security`. **Description:** Each step inserts `${{ github.ref_name }}` directly into a double-quoted Bash assignment. Expression substitution happens before Bash parses the script, so a ref name containing shell substitution syntax could execute before the release workflows’ `stable` comparison or the analysis workflow’s push logic. Whether an actor can create and dispatch such a ref depends on repository permissions. **[NEEDS VERIFICATION]** **Recommended fix:** Pass the ref through a step `env:` value and assign from that environment variable in Bash; keep the existing branch and ref-type checks.

- **BUG-002** — `scripts/check_integration_pr_readiness.py:210-217`. **Severity:** Medium. **Category:** `bug`. **Description:** A branch matching `orchestrator/project-<N>` receives a success status when issue `N` lacks `ai:orchestrator-tracking`, without checking its task list. **Inference:** Accidental removal of that label can turn an incomplete project’s required status green; occurrence in live runs is unverified. **[NEEDS VERIFICATION]** **Recommended fix:** Fail closed for a matching integration branch with a missing tracking label, and reserve the documented override-label path at lines 221-229 for intentional exceptions.

- **BUG-003** — `scripts/review_merge_train.sh:285-301`, `scripts/review_merge_train.sh:689-713`. **Severity:** Medium. **Category:** `bug`. **Description:** `_mt_upsert_comment` suppresses PATCH and POST failures with `|| true`. The release caller’s warning branch therefore cannot detect either failure and can report a release without its audit comment. **Recommended fix:** Return the mutation’s status from `_mt_upsert_comment`; let callers explicitly choose a warning or fail-open outcome without representing an unsuccessful write as successful.

- **BUG-004** — `.github/workflows/review_autofix.yml:2055-2079`. **Severity:** Medium. **Category:** `bug`. **Description:** The deterministic-skip path requests `closingIssuesReferences(first: 50)` without `pageInfo`, then labels only returned issues. A PR closing more than 50 issues would leave later linked issues unlabelled; whether that occurs here is unverified. **[NEEDS VERIFICATION]** **Recommended fix:** Request `pageInfo` and paginate when necessary, or suppress the linked-issue label transition until the complete set is known.

- **SEC-002** — `scripts/gh_helpers.sh:484-515`; `scripts/review_merge_train.sh:395-401`. **Severity:** Medium. **Category:** `security`. **Description:** `gh_retry` prints its full argument list (`$*`) on permanent or exhausted failure. Callers can pass a comment body with PR-derived filenames as `-f body=…`, placing that body in failure logs; no credential exposure was established. **[NEEDS VERIFICATION]** **Recommended fix:** Log the command class, endpoint template, status category, and attempt count instead of arguments or body text; retain escaped, bounded diagnostics.

- **SEC-003** — `scripts/gh_helpers.sh:647-659`. **Severity:** Medium. **Category:** `security`. **Description:** When a successful API response is malformed JSON, `gh_api_json_to_file` prints its first 50 raw lines to workflow logs. Callers also use this helper for PR and issue reads, so a truncated response may expose body content in diagnostics. **Recommended fix:** Log byte count and parse-failure category, not response contents; keep the response in the temporary file for the bounded retry.

### Section 2: GitHub API Call Redundancy Audit

Counts below are logical calls before pagination and retries unless stated otherwise. Proposed batching retains the smallest safe legacy read on incomplete results.

- **API-001** — `scripts/gh_helpers.sh:91-94`, `scripts/gh_helpers.sh:484-511`, `scripts/gh_helpers.sh:647-679`, `scripts/gh_helpers.sh:719-748`. **Severity:** Medium. **Category:** `api-redundancy`. **Description:** Unlike `gh_retry`, `gh_api_json_to_file` does not classify permanent failures; `curl_gh_api` retries every non-rate-limit HTTP failure. The default five-attempt budget can repeat a permanent 404 or 422 five times, with a sleep even after the final attempt. **Current → proposed:** up to **5 → 1** requests per permanent failure, separately for either wrapper. **Recommended fix:** Apply the existing `_is_gh_permanent_failure` classification to the JSON helper; classify explicit non-retryable HTTP responses in the curl helper, while retaining rate-limit and transient backoff.

- **API-002** — `scripts/review_merge_train.sh:265-300`. **Severity:** Medium. **Category:** `api-redundancy`. **Description:** `_mt_find_marker_comment_id` lists comments but keeps only IDs; `_mt_upsert_comment` then fetches the selected comment’s body. **Current → proposed:** **2 → 1 GET** per existing marker, with any necessary PATCH unchanged. **Recommended fix:** Return the selected ID and body from the existing paginated listing and compare locally. Preserve a fresh-read fallback if concurrent comment edits make that snapshot unsuitable. **[NEEDS VERIFICATION]** This extends the cycle-local reuse pattern already used by `_MT_FILES_CACHE` in the same script.

- **BATCH-001** — `scripts/orchestrate_poll_process.sh:6250-6285`. **Severity:** Medium. **Category:** `api-batching`. **Description:** The advisory-follow-up loop performs one issue-state/labels GET for each of **N** unchecked follow-ups, then a comments GET for each of **K** blocked issues. **Current → proposed:** **N + K → ⌈N/25⌉ + K reads**; necessary comment POSTs remain unchanged. **Recommended fix:** Extend `_fetch_candidate_issue_details_graphql`’s 25-alias pattern at `scripts/orchestrate_poll_process.sh:14937-15008` to prefetch complete state and labels for these issue numbers. Fall back per missing or incomplete item; retain the full comments reads needed for durable-marker reconciliation.

- **BATCH-002** — `scripts/review_merge_train.sh:135-145`, `scripts/review_merge_train.sh:212-240`. **Severity:** Medium. **Category:** `api-batching`. **Description:** Blocker evaluation fetches files once for each distinct older PR. The default `MT_MAX_OLDER=20` permits up to 20 such paginated reads after the open-PR listing, despite the process-local file cache. **Current → proposed:** **1 + N → 1 + ⌈N/25⌉** listing/file requests for N eligible older PRs whose file connection is complete; retain per-PR REST fallback for additional file pages or failed aliases. **[NEEDS VERIFICATION]** **Recommended fix:** Prefetch aliased PR file connections before the blocker loop, then populate `_MT_FILES_CACHE` only from complete results, following `_fetch_linked_pr_status_graphql` at `scripts/orchestrate_poll_process.sh:15090-15120`.

- **BATCH-003** — `.github/workflows/review_autofix.yml:2055-2079`. **Severity:** Medium. **Category:** `api-batching`. **Description:** One GraphQL read yields linked issue numbers, followed by one label POST per issue. **Current → proposed:** **1 + N → 1 + ⌈N/25⌉** read/mutation requests for N linked issues, excluding the existing label-creation and PR-label writes. **[NEEDS VERIFICATION]** **Recommended fix:** Include issue node IDs and the label ID in the read, use bounded aliased `addLabelsToLabelable` mutations, and fall back to individual POSTs for missing IDs or failed aliases. First address **BUG-004** so an incomplete `first: 50` result cannot authorize a partial batch.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — `.github/workflows/mark-stable.yml:686-837`; `.github/workflows/test-and-mark-stable.yml:6090-6242`. **Severity:** Medium. **Category:** `duplication`. **Description:** Both release workflows contain the same 6,306-character tag-publication step, including `publish_tag_with_remote_verification`. Their release-creation steps are also duplicated. The *entire workflows* are not near-duplicates: one has five jobs and the other fourteen. **Recommended fix:** Move tag publication into `scripts/stable_release_helpers.sh` with `publish_tag_with_remote_verification <tag_ref> <immutable|moving>`; source the verified script in both release jobs while retaining each workflow’s existing gates and outputs.

- **DUP-002** — `.github/workflows/clarify.yml:217-235`; `.github/workflows/orchestrate_clarify_respond.yml:325-344`; `.github/workflows/orchestrate.yml:372-389`; `.github/workflows/implement.yml:994-1038`. **Severity:** Medium. **Category:** `duplication`. **Description:** These stages repeat the primary/fallback verified-source selection, required-file check, install, and fetched-file bookkeeping with different manifests. The review-specific `scripts/stage_workflow_support.sh:4-39` already centralizes a related operation but stages out of tree and cannot replace the implement ledger unchanged. **Recommended fix:** Add a verified-source bootstrap module exposing `stage_selected_support <primary> <fallback> <destination> <manifest> <required-names...>`; update the four callers while preserving their optional assets, install modes, and implement’s staged-overwrite ledger.

- **DUP-003** — `.github/workflows/review_autofix.yml:1669-1691`, `.github/workflows/review_autofix.yml:1902-1924`; `scripts/gh_helpers.sh:452-521`. **Severity:** Low. **Category:** `duplication`. **Description:** Two inline `gh_retry` implementations duplicate one another and omit the shared helper’s permanent-error and rate-limit handling. **Recommended fix:** Provide a small trusted bootstrap path to `gh_helpers.sh::gh_retry "$@"` for both jobs, pinned to the verified workflow-support commit—not the reviewed PR head. Preserve stdout buffering and the lightweight deterministic-skip job.

### Section 4: Expression Size Limit Risk Assessment

- **EXPR-001** — `.github/workflows/implement.yml:994-1363`. **Severity:** Medium. **Category:** `expression-limit`. **Description:** The expression-bearing “Stage workflow support files” `run:` body measures **17,588 characters**, leaving **3,412** before the specified 21,000-character limit and **412** before the 18,000-character high-risk threshold. It contains three `${{ }}` substitutions. **Recommended fix:** Extract the body to a verified `scripts/stage_implement_support.sh`, pass those three values through step `env:`, and preserve the self-repo overwrite ledger and file modes.

Across **52** workflows, the read-only YAML scan found **230** expression-bearing `run:` bodies; only the block above exceeds 15,000 characters. The largest inspected `if:` string is **697** characters. Expression-free `run:` blocks were excluded. No workflow exceeds the requested **800 KB** warning threshold. The repository documents a stricter **512,000-byte** workflow limit and **480,000-byte** CI guard in `CLAUDE.md:1644-1672`; the largest current file, `.github/workflows/review_autofix.yml`, is **439,318 bytes**, leaving **40,682 bytes** before that guard.

### Section 5: Cross-Cutting Concerns

- **DEAD-001** — `scripts/review_run_reviewers.sh:828-836`. **Severity:** Low. **Category:** `dead-code`. **Description:** `RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE` and `RAW_REVIEWER_SYMBOL_DIFF_SUMMARY_FILE` are assigned here but have no other references in the inspected workflows or scripts. **Recommended fix:** Remove these assignments after checking any external contract tests for their names; keep the adjacent aliases that are consumed.

- **SHELL-001** — `scripts/write_guard.sh:103-110`. **Severity:** Low. **Category:** `shellcheck`. **Description:** Shellcheck reports SC2295 on both unquoted root-path expansions inside `${write_guard_config_log_path#…}`. A root path containing pattern characters could produce an incorrect relative diagnostic path. **Recommended fix:** Quote the root-path portion within both parameter-expansion patterns and add a path-with-pattern-characters case to the guard’s tests.

Shellcheck also reported indirectly assigned poller threshold variables at `scripts/orchestrate_poll_process.sh:19685-19686`, `20221-20225`, and `20325-20326`; `printf -v` in `_integration_backpressure_effective_threshold` at `4453-4481` supplies them, so they are **not** findings. The inspected workflow and script files contained no `TODO`, `FIXME`, or `HACK` markers requiring a separate debt finding.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, SEC-001 |
| Medium | 13 | BUG-002, BUG-003, BUG-004, SEC-002, SEC-003, API-001, API-002, BATCH-001, BATCH-002, BATCH-003, DUP-001, DUP-002, EXPR-001 |
| Low | 3 | DUP-003, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 5 workflow/script files | Medium |
| API call optimization | At least 4 workflow/script files | Large |
| Code modularization | At least 7 existing files plus shared helpers | Large |
| Expression size reduction | `implement.yml` plus 1 extracted script | Medium |
| Medium/Low fixes | At least 6 workflow/script files, overlapping rows above | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-10-06)

### Safety Tag Legend

`SAFE_TO_MERGE` is ready to implement without further review; `NEEDS_VERIFICATION` requires the stated checks first; `RISKY_SKIP` must not be auto-implemented because it touches a protected API-call pattern or safety guard.

### Consolidation Candidates (MERGE-###)

- **MERGE-001 — RISKY_SKIP.** **Calls:** `scripts/review_enable_auto_merge.sh:102-115` and `scripts/review_enable_auto_merge.sh:153-190`; the early-exit head read is at `scripts/review_enable_auto_merge.sh:39-56`. **Current → proposed:** 2 → 1 logical GET on a path where the PR response can *provably* supply the complete label set; otherwise remain at 2. **Endpoints:** `GET /repos/{repo}/issues/{pr}/labels` and `GET /repos/{repo}/pulls/{pr}`. **Evidence:** the first call checks `e2e-smoke-test`; the later PR response supplies head metadata. The early-exit branch makes a separate head-freshness read because that later response is unreachable. **Proposed fix:** Only after proving label completeness, move the full PR read ahead of the label decision and use its labels and `.head.sha`; retain the paginated labels read whenever completeness is uncertain, along with both fail-closed exits. **Safety rationale:** the labels call uses `--paginate` specifically to catch a label beyond the first page, so replacing it with an unverified PR-embedded label list risks enabling auto-merge on an e2e PR. **Downstream signal:** Do not auto-implement; manually verify PR-response label completeness for large label sets and test second-page e2e labels, either read failing, and a moving head before changing the guard.

### Redundant Re-Fetch (REUSE-###)

No findings.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: RISKY_SKIP — Changing `gh_api_json_to_file` and `curl_gh_api` retry classification requires manual review of permanent-error and rate-limit behavior.
- API-002: RISKY_SKIP — The existing comment listing is paginated; manual review must preserve complete results and the release-claim freshness boundary.
- BATCH-001: RISKY_SKIP — The change is inside `orchestrate_poll_process.sh`; manually validate cycle-cache completeness and per-item fallbacks.
- BATCH-002: RISKY_SKIP — Replacing paginated PR-file reads requires manual verification of connection completeness and page-boundary fallback.
- BATCH-003: NEEDS_VERIFICATION — Resolve BUG-004’s `first: 50` limit, then verify label IDs, partial mutation errors, and individual-write fallback.

### Summary Counts

Counts cover **net-new findings**, not cross-references.

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 0 | — |
| RISKY_SKIP | 1 | MERGE-001 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
