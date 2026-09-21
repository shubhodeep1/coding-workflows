## Executive Summary

- **Review/autofix is the dominant bottleneck:** 30.01 of 47.62 runner-hours (63%). Full reviews took 5,047–7,175s and usually made 12 OpenRouter calls; run `35312976998` made 16. **Impact: 10–40 minutes/run; confidence: high.**
- **Concurrency creates hour-long duplicate waits:** runs `35324993978` and `35340561680` waited 5,062s and 5,509s before `codex-agent`; three long cancelled peers consumed 3.88 hours of wall-clock visibility. **Impact: up to 84–92 minutes per duplicate; confidence: high.**
- **CI is a single 107-step critical path:** 17/17 succeeded, but p50 was 1,931s and p95 1,993.6s. **Potential impact: 8–15 minutes through measured sharding; confidence: medium.**
- **Poller overhead is systemic:** 103 successful runs consumed 7.62 runner-hours at p50 270s. In runs `35355562803` and `35353300488`, memory start/end writes alone consumed 95–106s. **Potential impact: 30–60s/run; confidence: high.**
- **Token volume is concentrated in repeated review panels:** 98.70M total tokens across 113 calls. Numeric telemetry implies an 82.8% cache-read ratio, although aggregate `cache_hit_rate` is suppressed because three calls lacked usage. **Potential savings: 10–25%; confidence: medium.**
- **Operational health is nominal but degraded signals are masked:** zero hard failures, but eight cancellations, provider retry cascades, tolerated `gawk` failures, Git cleanup errors, and firewall warnings occurred. **Reliability impact: medium; confidence: high.**

## Speed Optimizations

1. **Prevent duplicate review jobs before reusable-workflow concurrency — critical-path queueing win**
   - **Evidence:** `35324993978` waited 5,061.5s and `35340561680` waited 5,509.4s before `codex-agent`. Three paired internal-review runs were cancelled after 3,603–6,598s. Productive continuation dispatches deliberately bypass the peer check in `.github/workflows/review_autofix.yml`.
   - **Root cause:** the direct continuation and `pull_request:synchronize` paths enter the same PR concurrency group; the redundant run waits behind the designated successor.
   - **Exact change:** add a lightweight, fail-open preflight in `.github/workflows/internal-review.yml` that skips a synchronize-triggered call when a same-head `review_autofix.yml` workflow-dispatch continuation is active.
   - **Logging addition:** `REVIEW_CONCURRENCY_DECISION pr=... head_sha=... peer_run_id=... queue_ms=... action=run|skip reason=...`.
   - **Savings:** eliminate up to 84–92 minutes of duplicate queueing and reduce active-run sweep blockage.
   - **Risk:** low-medium; on API uncertainty, run normally.

2. **Bound reviewer stragglers after quorum — critical-path compute win**
   - **Evidence:** run `35301629065` completed five first-pass reviewers by approximately 03:37 UTC, while `moonshotai/kimi-k3` completed at 04:14 UTC. Run `35312976998` spent retries on `stall_guard`, `server_error`, and `rate_limit`.
   - **Root cause:** every six-model pass waits for the slowest slot even after sufficient reviewer diversity may exist.
   - **Exact change:** shadow-test a five-of-six quorum, then permit straggler cancellation only after five valid results and consolidator schema validation.
   - **Logging addition:** `REVIEW_SLOT_TIMING pass=... slot=... attempt=... duration_ms=... outcome=...` and `REVIEW_QUORUM_DECISION succeeded=... pending=... action=wait|cancel`.
   - **Savings:** 10–37 minutes on observed straggler runs.
   - **Risk:** medium; preserve full-panel mode for high-risk paths and rollback via a variable.

3. **Reuse the poller’s memory checkout**
   - **Evidence:** `Record poll run start/end` took 48.8s + 46.0s in `35355562803` and 58.9s + 47.0s in `35353300488`.
   - **Root cause:** each event repeats Git branch preparation and push work.
   - **Exact change:** initialize one memory workspace per poll job and reuse it for both events while retaining separate durable pushes.
   - **Logging addition:** extend `AI_MEMORY_TELEMETRY` with `duration_ms`, `fetch_ms`, `commit_ms`, `push_ms`, and `conflict_retries`.
   - **Savings:** estimated 30–60s per 270–300s poll.
   - **Risk:** low if existing push-retry semantics remain unchanged.

4. **Shard the measured CI hotspot**
   - **Evidence:** `.github/workflows/ci.yml` has 107 named steps in one `lint` job; CI p50 is 32.2 minutes.
   - **Root cause:** independent static checks and large orchestrate-poll tests execute serially.
   - **Exact change:** first emit per-step/test-group timings, then split the orchestrate-poll module and static checks into deterministic parallel jobs.
   - **Logging addition:** `CI_GROUP_TIMING group=... duration_ms=... tests=... failures=...`.
   - **Savings:** estimated 8–15 minutes critical-path latency.
   - **Risk:** medium; additional setup runner-minutes and possible hidden ordering dependencies.

**Micro-optimization:** the 78 skipped/other runs consumed only 257s total. Tightening their triggers is lower priority.

## Cost Optimizations

1. **Adapt the second review pass**
   - **Evidence:** numeric slow reviews normally made exactly 12 calls—six first-pass and six second-pass. Aggregate usage was 98.70M tokens.
   - **Root cause:** every reviewer runs again regardless of first-pass agreement or contribution.
   - **Exact change:** in shadow mode, identify pass-one slots producing no unique actionable findings; skip only those slots in pass two after consolidator validation.
   - **Estimated savings:** 10–25% of review calls/tokens; up to 50% on a fully clean first pass.
   - **Quality risk:** medium; retain full pass two for workflow, security, migration, and low-consensus changes.

2. **Enable the existing reviewer health circuit breaker**
   - **Evidence:** `35312976998` made 16 calls versus the normal 12, with three usage-unavailable attempts, two failbacks and terminal `stall_guard`/`rate_limit` failures.
   - **Root cause:** `REVIEWER_CIRCUIT_BREAKER_ENABLED=0`, despite existing health thresholds.
   - **Exact change:** enable it with the existing three-failure/1,800s settings, initially for retryable failures only.
   - **Estimated savings:** 1–4 calls and 15–30 minutes during a provider incident.
   - **Quality risk:** low-medium; temporarily reduced model diversity, with automatic expiry.

3. **Stabilize cache prefixes**
   - **Evidence:** numeric cache ratios ranged from 49.8% (`35333337581`) to 90.1% (`35301629065`); aggregate numeric ratio was 82.8%.
   - **Root cause:** **inference:** dynamic PR metadata, memory and Semble results likely vary before or within the reusable prefix.
   - **Exact change:** place stable rubric/system text first; append timestamps, SHAs, comments and retrieved context after the cache breakpoint.
   - **Logging addition:** `CACHE_PREFIX phase=... model=... prefix_sha256=... stable_bytes=... dynamic_bytes=...`.
   - **Estimated savings:** raising the ratio to 87% would avoid roughly 4.1M uncached prompt tokens in this window.
   - **Quality risk:** low; prompt content remains unchanged.

4. **Keep Semble enabled; measure its avoided expansion**
   - **Evidence:** 23 queries returned 217,530 bytes, averaging 9.5KB; runtime fallbacks were zero. Deep-dive calls took only 350–656ms.
   - **Assessment:** Semble is adding small, targeted context rather than noisy bulk expansion.
   - **Exact change:** log `prompt_bytes_before`, `prompt_bytes_after`, and `candidate_bytes_avoided` per query.
   - **Savings:** not directly measurable without this baseline; current latency cost is negligible.

**Serena:** zero queries, probes or fallbacks were recorded, so no efficiency conclusion is possible.

## Reliability Improvements

1. **Stop retry cascades on unhealthy reviewer slots**
   - **Evidence:** `35312976998` exhausted Minimax through server/stall failures and Kimi through stall/rate-limit failures. `35325012757` recorded two non-retryable DeepSeek failures while succeeding overall.
   - **Root category:** provider/model health.
   - **Fix:** enable the circuit breaker and emit a final per-slot health summary.
   - **Expected impact:** prevent repeated degraded-model retries and reduce tail variance.
   - **Rollback/fail-open:** expire health state after 30 minutes; continue with remaining reviewers.

2. **Make continuation/synchronize ownership explicit**
   - **Evidence:** six review cancellations; three long cancelled jobs account for 3.88 hours. Two successful duplicates later waited 84–92 minutes before doing little or no model work.
   - **Root category:** orchestration/concurrency.
   - **Fix:** caller preflight plus `continuation_owner_run_id` telemetry.
   - **Expected impact:** remove duplicate pending jobs and active-run sweep suppression.
   - **Fail-open:** dispatch when peer detection is unavailable.

3. **Treat tolerated test failures as structured degradation**
   - **Evidence:** review runs `35338302845` and `35333315736` reported one failed test because `gawk` was unavailable, yet concluded success.
   - **Root category:** runner dependency gap.
   - **Fix:** install `gawk` in the review test environment or replace the test dependency with portable `awk`.
   - **Logging addition:** `SOFT_TEST_FAILURE suite=... test=... missing_binary=gawk policy=continue`.
   - **Expected impact:** eliminate false-red internal validation and make genuine tolerated failures auditable.
   - **Rollback:** retain current continue behavior until the dependency fix is verified.

4. **Remove recurring cleanup noise**
   - **Evidence:** `35333827334`, `35333337581`, and `35332266812` reported `git-submodule cannot be used without a working tree`; `35349897098` skipped unsafe `RUNTIME_DIR` cleanup and killed an orphan `gh` process.
   - **Root category:** lifecycle cleanup ordering.
   - **Fix:** do not remove/move the checkout before action post-steps; guard submodule cleanup with a worktree check.
   - **Logging addition:** `CLEANUP_SUMMARY worktree_valid=... runtime_dir_valid=... orphan_count=... result=...`.
   - **Expected impact:** fewer masked cleanup defects and orphan processes.
   - **Fail-open:** warnings must remain nonfatal.

5. **Emit explicit MCP availability probes**
   - **Evidence:** 29 Semble fallbacks were all contract-test fixtures; runtime fallbacks were zero. Poll run `35355562803` showed early `SEMBLE_AVAILABLE=false` but post-install availability was true. Serena emitted no probes.
   - **Fix:** emit post-install `SEMBLE_PROBE` and existing-schema `SERENA_PROBE`, then extend `scripts/cost_audit.py` for Semble probe counters.
   - **Expected impact:** distinguish harmless pre-install state from broken rollout.
   - **Rollback:** probe failure remains fail-open.

`BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts were both zero; there is no observed policy-pressure or context-window incident in this window.

## AI Memory Health

- **Retrieval hit rate:** 9/9 slow-run retrieves selected records—100%.
- **Budget:** average 1,379 estimated tokens versus 1,400 budget, or 98.5% utilization.
- **Selection:** eight retrieves selected 30 records; one selected 29.
- **Keyword method:** `llm` for all nine; no `plain` or `none`.
- **Health exceptions:** no zero-record, `fail_open:true`, or `enabled:false` retrieve events.
- **Push reliability:** 4 of 25 sampled push-bearing operations needed two attempts; none exceeded two.
- **Learning output:** seven `record-candidate` operations succeeded, while 12 `write_lessons_learned` operations produced zero records.

**Recommendation:** retain retrieval, but add `records_considered`, `records_truncated`, score distribution and operation timings. The 98.5% budget saturation leaves little headroom even though no context warnings occurred.

## GH API Call Audit

The repository broadly follows `CLAUDE.md` §15: extend existing calls, batch GraphQL lookups, cache per cycle, and fail open on misses.

- **Review sweep:** `.github/workflows/review_autofix_sweep.yml` correctly fetches PRs once and snapshots active runs once per workflow/status rather than per PR. With candidates, this is one PR-list call plus six status snapshots before pagination.
- **Merge train:** `scripts/review_merge_train.sh` caches one file-list fetch per distinct PR per release tick.
- **Poller:** `scripts/orchestrate_poll_process.sh` contains batch-size-25 GraphQL helpers and cycle-local caches for labels and linked PRs.
- **Observed rate limits:** no GitHub API rate-limit event was found. The `rate_limit` in `35312976998` was a model-provider failure, not GitHub.
- **Primary gap:** there are no runtime endpoint-family call counters, page counts or cache-hit metrics. The 75s `Process each tracking issue` step cannot therefore be divided into API, local compute or retry time.

**Recommended instrumentation in `scripts/gh_helpers.sh`:**

`GH_API_CALL helper=gh_retry endpoint_family=pulls method=GET attempt=1 duration_ms=... result=ok pages=1 cache=miss rate_limited=false`

Emit a final `GH_API_SUMMARY` grouped by endpoint family. Normalize endpoints and omit query values to avoid leaking issue content.

## Prompt Cache & Memory System

- Reported cache-read tokens: **80,966,807**; prompt tokens: **16,798,389**.
- Numeric cache ratio: **82.8%**. Aggregate `cache_hit_rate` remains N/A because three of 113 calls lacked usage.
- Cache-creation tokens were reported as zero; this does not prove that providers created no caches.
- Review logs consistently emitted `REVIEWER_CACHE status=supported prompt_reused=true`.
- No `CONTEXT_BUDGET_WARN` occurred, but AI-memory retrieval consumed 98.5% of its own budget.
- Semble context was compact and fast; no runtime fallback was observed.

**Priority changes:** stable prompt prefixes, cache-prefix hashes, and per-call cache attribution. Expected impact is approximately 4.1M fewer uncached prompt tokens if the numeric ratio reaches 87%.

## Orchestrator Health

- `orchestrate_poll` completed **103/103** runs successfully with p50 **270s** and p95 **296.7s**.
- Run `35355562803` processed tracking issue `#3965`; security-pass fix issue `#4113` remained in progress. The run completed without dispatch churn.
- Review sweeps repeatedly skipped PRs `#4119` and `#4120` because active review jobs existed—for example runs `35332461892`, `35335201493`, and `35337520497`. Deduplication is working, but hour-long review/concurrency tails delay cadence.
- Forward-merge run `35332198829` detected a conflict, opened PR `#4120`, and dispatched review. Run `35332266812` subsequently resolved a conflict and pushed edits—a positive recovery signal.
- Clarify, plan and clarify-response each produced 20 skipped/other runs; implement produced 18 skipped runs. Their combined cost is minor.

**Logging addition:** emit one `ORCH_ISSUE_DECISION` per tracking issue with state before/after, action, blocker and duration. Add `ORCH_STAGE_TIMING` around global prefetches: latest polls spent roughly 62 seconds before printing the first issue-processing line.

## Pipeline Flow Bottlenecks

| Stage | Dominant delay | Evidence | Priority fix |
|---|---|---|---|
| Clarify/plan | Broad-trigger skips | 20/20 other in each family | Deprioritize; only 257s across all skipped runs |
| Implement | Phase mismatch events | `35349975875` and `35349897098` skipped wrong phase | Log event origin and expected/current phase |
| Review/autofix queue | Duplicate continuation/synchronize jobs | 5,062s and 5,509s queue waits | Caller-level active-continuation preflight |
| Review/autofix compute | Six-model, two-pass panels and stragglers | 5,047–7,175s; 12–16 calls | Quorum/straggler policy and adaptive pass two |
| Validate/CI | One serial 107-step job | p50 1,931s | Timing instrumentation, then deterministic sharding |
| Orchestrate | Memory Git writes and silent prefetch | 95–106s memory writes; ~62s pre-issue gap | Reuse memory checkout and emit stage timings |
| Merge/conflict | Forward-merge conflict review | PR `#4120`, runs `35332198829` and `35332266812` | Existing recovery worked; retain and measure retries |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix consumes 63% of runner time; CI p50 is 32.2 minutes; poller p50 is 4.5 minutes.
- **Top failure modes:** duplicate concurrency waits, model stall/rate-limit cascades, tolerated missing-`gawk` tests, and cleanup warnings.
- **Highest cost driver:** 113 OpenRouter calls and 98.70M tokens, concentrated entirely in review/autofix.
- **Top three actions:**
  1. Suppress redundant synchronize runs when a continuation owns the PR head.
  2. Add reviewer slot/quorum timing and shadow an adaptive second pass.
  3. Instrument and optimize poller memory/API stages.

## Metrics Appendix

### Outcomes and duration

| Workflow family | Runs | Success | Failure | Cancelled | Other | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Total** | 342 | 256 | 0 | 8 | 78 | 17s | 3,630s |
| review_autofix | 86 | 80 | 0 | 6 | 0 | 9.5s | 6,361s |
| orchestrate_poll | 103 | 103 | 0 | 0 | 0 | 270s | 296.7s |
| ci | 17 | 17 | 0 | 0 | 0 | 1,931s | 1,993.6s |
| copilot_pull_request_reviewer | 14 | 14 | 0 | 0 | 0 | 203s | 404.4s |
| clarify | 20 | 0 | 0 | 0 | 20 | 1s | 11s |
| plan | 20 | 0 | 0 | 0 | 20 | 1s | 9s |
| implement | 20 | 2 | 0 | 0 | 18 | 1s | 12.1s |
| orchestrate_clarify_respond | 20 | 0 | 0 | 0 | 20 | 1s | 9.1s |
| validation_refresh | 1 | 1 | 0 | 0 | 0 | 1,188s | 1,188s |

Overall success was 74.85% of all runs and 96.97% of success-or-cancel terminal outcomes; hard failure rate was 0%.

### Token and cache telemetry

| Metric | Value |
|---|---:|
| OpenRouter calls | 113 |
| Usage available / unavailable | 110 / 3 |
| Prompt tokens | 16,798,389 |
| Completion tokens | 938,578 |
| Cache-read tokens | 80,966,807 |
| Cache-creation tokens reported | 0 |
| Total tokens | 98,698,605 |
| Numeric cache ratio | 82.8% |
| Reported aggregate `cache_hit_rate` | N/A |
| `wall_clock_p50_ms` | 13,000 |
| `wall_clock_p99_ms` | 7,011,440 |
| Wall-clock samples | 115 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |

### MCP telemetry

| System/target | Queries | Bytes | Fallbacks | Probe OK | Probe failed | Probe skipped |
|---|---:|---:|---:|---:|---:|---:|
| Semble aggregate | 23 | 217,530 | 29 contract-test / 0 runtime | N/A | N/A | N/A |
| Semble `reviewer-context`¹ | 7 | 89,370 | 0 | N/A | N/A | N/A |
| Semble `overflow`¹ | 7 | 53,278 | 0 | N/A | N/A | N/A |
| Serena, no target markers | 0 | 0 | 0 | 0 | 0 | 0 |

¹Target rows cover 14 of 23 queries from selected slow-run logs.

- Serena tool calls: **0**; response bytes: **0**; query time: **0ms**.
- Other MCP servers observed: **none**.

### GH API telemetry

| Metric | Result |
|---|---|
| Runtime call-count telemetry | Not emitted |
| GitHub rate-limit events observed | 0 |
| Sweep batching | One PR snapshot; active runs prefetched per workflow/status |
| Poller batching | GraphQL batches of 25 plus cycle-local caches |
| Main data gap | No endpoint, page-count, cache-hit or duration summary |

## Deep Audit — Workflows & Scripts (2026-09-18)

### Section 1: Bug & Correctness Sweep

All 46 workflows, 79 shell scripts, and 56 Python scripts passed YAML, `bash -n`, and Python AST parsing.

#### BUG-001 — Review-blocked actions are not bound to the judged PR head

- **ID:** BUG-001
- **File:** `scripts/review_rb_judge.sh:1647-1664,1967-2072,2509-2549`
- **Severity:** High
- **Category:** `bug`
- **Description:** The post-judge live PR fetch checks only whether the PR merged. It never compares the current `.head.sha` with `RB_JUDGED_HEAD_SHA`. An intervening push can therefore cause the no-op fix path to apply `ai:ready-to-merge`, or `close_and_reissue` to close and preserve a baseline from a head the judge never evaluated. The close occurs before the later `gh pr view` head lookup.
- **Recommended fix:** Retain `.head.sha` from `_guard_pr_meta`, compare it with `RB_JUDGED_HEAD_SHA` before posting or executing any action, and exit with a structured stale-head skip when they differ. Use `RB_JUDGED_HEAD_SHA` for the reissue baseline and remove the later redundant head lookup.

#### BUG-002 — Stale check-run failures can triage a newer PR head

- **ID:** BUG-002
- **File:** `.github/workflows/check_failure_triage.yml:64-145`; `scripts/check_failure_triage.sh:144-177,358-399`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** `head_sha` is syntax-validated, but neither PR fetch verifies that the live PR head still equals that SHA. Inference: a check for an older commit can complete after a newer push, causing an obsolete failure to open a fix issue against the current PR.
- **Recommended fix:** Compare `.head.sha` with `HEAD_SHA` in the lightweight derivation job and repeat the comparison immediately before issue creation. Log and skip stale events rather than filing an issue.

#### SEC-001 — Privileged review job executes a mutable third-party action tag

- **ID:** SEC-001
- **File:** `.github/workflows/review_autofix.yml:1637-1662`; `.github/workflows/internal-review.yml:30-33,53-66`
- **Severity:** Medium
- **Category:** `security`
- **Description:** `jlumbroso/free-disk-space@v1.3.1` is referenced by a movable tag and executes before later steps expose write credentials. A compromised or moved tag could persist code on the runner and intercept those credentials.
- **Recommended fix:** Pin the action to a reviewed 40-character commit SHA and configure automated dependency updates for the pin.

#### SEC-002 — PAT is interpolated directly into generated shell source

- **ID:** SEC-002
- **File:** `.github/workflows/review_autofix.yml:5961-6009`; `.github/workflows/implement.yml:4315-4327`
- **Severity:** Low
- **Category:** `security`
- **Description:** Both steps already export `GH_TOKEN`, but interpolate `${{ secrets.GH_PAT }}` directly into the `run:` script and Git remote URL. This unnecessarily places the secret in generated shell source and increases exposure if command or remote diagnostics are introduced. [NEEDS VERIFICATION]
- **Recommended fix:** Use `${GH_TOKEN}` and `${GITHUB_REPOSITORY}` from the step environment. Prefer a temporary credential header/helper over persisting the token in `.git/config`.

### Section 2: GitHub API Call Redundancy Audit

#### BATCH-001 — PR metadata collection performs five or six separable calls

- **ID:** BATCH-001
- **File:** `scripts/review_collect_pr_metadata.sh:209-225,251-269,464-477`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** The normal path separately fetches PR metadata, issue comments, review comments, closing issue references, and the diff: five logical calls, or six when top-level reviews are enabled. Pagination can increase the underlying request count.
- **Recommended fix:** Extend `gh_pr_with_all_comments` in `gh_helpers.sh` to return comment IDs/timestamps, top-level review state/body, and closing issue references. **Current:** 5–6 logical calls. **Proposed:** 2—one consolidated GraphQL call plus the separate diff representation, with existing REST pagination as fail-open fallback.

#### API-001 — Merge-train comment upsert re-fetches a listed comment

- **ID:** API-001
- **File:** `scripts/review_merge_train.sh:255-290`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** `_mt_find_marker_comment_id` lists all comments but returns only the ID. `_mt_upsert_comment` then fetches that comment again solely to compare its body.
- **Recommended fix:** Return `{id,body}` from the initial comments query and pass both into `_mt_upsert_comment`. **Current:** 2 reads per existing-marker upsert. **Proposed:** 1 read; write count unchanged. Extend the script’s existing cycle-local cache pattern.

#### BATCH-002 — Merge-train file inspection remains per-PR REST fan-out

- **ID:** BATCH-002
- **File:** `scripts/review_merge_train.sh:123-136,202-230`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** `_mt_blockers_for_into` invokes one paginated `/pulls/{n}/files` request per older PR. With `MERGE_TRAIN_MAX_OLDER_PRS=20`, a gate can issue up to 21 logical file-list calls when the current PR also needs fallback hydration. [NEEDS VERIFICATION]
- **Recommended fix:** Add an aliased GraphQL batch patterned after `_fetch_candidate_issue_details_graphql`, requesting each PR’s `files(first:100)`. **Current:** up to 20–21 calls. **Proposed:** 1 batch for the configured cap, with per-PR REST fallback only when `hasNextPage=true`.

#### API-002 — Default-branch metadata is fetched repeatedly per poll

- **ID:** API-002
- **File:** `scripts/orchestrate_poll_process.sh:16190-16206,16691-16728,20598-20601,21505-21518`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** Normal tracking-issue processing fetches repository `.default_branch` once per issue, while merge-conflict, judge, and standalone-sweep paths can fetch it again. For `T` normally processed tracking issues, the minimum is approximately `T+1` calls, with additional path-dependent reads.
- **Recommended fix:** Fetch repository metadata once before the tracking loop into `REPOSITORY_DEFAULT_BRANCH` and reuse it everywhere. **Current:** approximately `T+1` or more. **Proposed:** 1 call per poll job. Follow the existing `ACTIVE_WORKFLOW_ISSUES` cycle-cache pattern.

#### API-003 — Authentication failures can consume the full retry budget

- **ID:** API-003
- **File:** `scripts/gh_helpers.sh:75-94,439-493`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** `_is_gh_permanent_failure` handles 404, 422, and selected permission text but omits common HTTP 400/401 and generic non-rate-limit 403 responses. Those failures can be retried five times despite being non-transient.
- **Recommended fix:** Check `_is_gh_rate_limit` first, then classify 400/401 and non-rate-limit 403 as permanent. **Current:** up to 5 identical calls. **Proposed:** 1 call for permanent failures; retain the existing retry budget for transient failures.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Workflow-analysis runtime setup is repeated across four jobs

- **ID:** DUP-001
- **File:** `.github/workflows/workflow-log-analysis.yml:207-291,689-808,1376-1517,1887-2021`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** Python setup, Codex caching/install/restore, configuration, uv/Semble installation, and index construction are near-identical in weekly-retro, analyze, deep-audit, and API-redundancy jobs.
- **Recommended fix:** Create a local composite action backed by `scripts/workflow_analysis_runtime_helpers.sh`. Suggested function: `workflow_analysis_prepare_runtime <model> <reasoning> <semble_index_path>`. Update all four jobs to invoke it.

#### DUP-002 — Context-budget telemetry helper has four copies

- **ID:** DUP-002
- **File:** `scripts/review_apply_fixes.sh:164-202`; `scripts/review_consolidate.sh:268-307`; `scripts/review_rb_judge.sh:256-294`; `scripts/review_run_reviewers.sh:69-107`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** `emit_context_budget_warn_for_prompt` is duplicated almost byte-for-byte, including the embedded Python importer and fail-open behavior.
- **Recommended fix:** Move it to `scripts/review_prompt_helpers.sh` with signature `emit_context_budget_warn_for_prompt <phase> <prompt_path> <model>`, stage it once, and source it from all four callers.

#### DUP-003 — Support checkout and staging logic is independently maintained

- **ID:** DUP-003
- **File:** `.github/workflows/check_failure_triage.yml:213-303`; `.github/workflows/clarify.yml:167-354`; `.github/workflows/plan.yml:230-429`; `.github/workflows/orchestrate.yml:290-470`; `.github/workflows/orchestrate_clarify_respond.yml:226-415`; `.github/workflows/orchestrate_poll.yml:318-586`; `.github/workflows/implement.yml:865-1276`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** Each workflow separately resolves the source ref, performs primary/fallback/main checkouts, and stages overlapping helper sets. `review_autofix.yml` and `validate.yml` already centralize this through `stage_workflow_support.sh`.
- **Recommended fix:** Extend `scripts/stage_workflow_support.sh` with `stage_workflow_support <phase> --script-ref <ref> --manifest <path> --main-fallback`, using phase manifests for differences. Migrate the listed workflows incrementally.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Implement support-staging block exceeds the medium-risk threshold

- **ID:** EXPR-001
- **File:** `.github/workflows/implement.yml:920-1276`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** The interpolated `run:` scalar is approximately **16,985 characters**, leaving **4,015 characters** before GitHub’s 21,000-character limit. It contains three `${{ }}` interpolations and will fail workflow loading if future edits exceed the limit.
- **Recommended fix:** Bind `github.repository`, `PROMPT_PRELUDE_REFACTOR_ENABLED`, and `UNATTENDED_IDENTITY_REINJECT_ENABLED` through step `env:` values so the block contains no template interpolation, or migrate the block under DUP-003 to `stage_workflow_support.sh`.

No `if:` expression exceeded 739 characters. No workflow exceeded 800 KB; the largest was `review_autofix.yml` at approximately 497,695 characters.

### Section 5: Cross-Cutting Concerns

#### DEAD-001 — Branch-rebuild threshold outputs are assigned but never consumed

- **ID:** DEAD-001
- **File:** `scripts/orchestrate_poll_process.sh:8791-8871`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `BRANCH_REBUILD_SKIP_REASON` and `BRANCH_REBUILD_LAST_REBUILD_AT` receive multiple values, but no caller reads or logs them. Only `BRANCH_REBUILD_ESCALATED_ERROR` affects behavior.
- **Recommended fix:** Either emit the values through a structured `BRANCH_REBUILD_THRESHOLD_DECISION` log at the caller or remove the dead assignments.

#### DEAD-002 — Summary and smoke-test values are computed but discarded

- **ID:** DEAD-002
- **File:** `.github/workflows/orchestrate.yml:1273-1277`; `.github/workflows/test-and-mark-stable.yml:1140-1152`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `WAVE_COUNT` is assigned from the dependency-edge count and never rendered. `BLOB_CONTENT` is decoded from the contents API and never read.
- **Recommended fix:** Remove `BLOB_CONTENT`. Remove `WAVE_COUNT`, or calculate the actual wave count and add it to the summary table if that metric is intended.

#### CONSIST-001 — Failure-alert API lookup bypasses canonical helpers

- **ID:** CONSIST-001
- **File:** `.github/workflows/orchestrate_poll.yml:965-978`; `.github/workflows/forward-merge-stable-to-main.yml:483-497`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** Two duplicate failure-alert blocks use raw authenticated `curl` rather than `gh_retry`, `_safe_gh_jq`, or `curl_gh_api`. They therefore lack shared permanent-error handling, rate-limit telemetry, and backoff.
- **Recommended fix:** Add `gh_first_failed_step <repo> <run_id> <attempt>` to `scripts/gh_helpers.sh` and call it from both workflows. Normal call count remains one.

ShellCheck found no actionable SC2086, SC2046, SC2006, or SC2015 violations. Remaining warnings were intentional glob matching, dynamic sourcing, or dead assignments covered above.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 9 | BUG-002, SEC-001, BATCH-001, BATCH-002, API-002, API-003, DUP-001, DUP-003, EXPR-001 |
| Low | 6 | SEC-002, API-001, DUP-002, DEAD-001, DEAD-002, CONSIST-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 1 | Medium |
| API call optimization | 4 | Large |
| Code modularization | ≈16 | Large |
| Expression size reduction | 1–2 | Small |
| Medium/Low fixes | ≈8 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-09-18)

### Safety Tag Legend

`SAFE_TO_MERGE` is directly implementable; `NEEDS_VERIFICATION` requires specified checks; `RISKY_SKIP` must not be automated because retry, pagination, race-defense, or poller semantics are involved.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — Clarify fetches issue comments twice

- **Safety tag:** `RISKY_SKIP`
- **File:** `.github/workflows/clarify.yml:468,472-473`
- **Current call count:** 2 logical calls when semantic caching is enabled.
- **Proposed call count:** 1 logical paginated call.
- **Endpoint:** `GET /repos/{owner}/{repo}/issues/{issue_number}/comments`
- **Evidence:**
  ```bash
  gh_retry gh api ".../comments?...&per_page=50" > "${ISSUE_COMMENTS_FILE}"
  gh_retry gh api --paginate --slurp ".../comments?...&per_page=100"
  ```
  Both calls use ascending creation order; the second response contains the first call’s 50-comment subset.
- **Proposed fix:** In the `Fetch issue comments` step, capture one paginated response, derive `${ISSUE_COMMENTS_FILE}` from the first 50 comments, and derive `${THREAD_HISTORY_FILE}` from the complete array.
- **Safety rationale:** This is `RISKY_SKIP` because the second call uses pagination, and merging would make later-page failures affect the currently independent fatal prompt-context and fail-open semantic-cache paths.
- **Downstream signal:** Do not auto-implement; manually define whether a later-page failure remains cache-only degradation or becomes fatal before sharing the snapshot.

#### MERGE-002 — Three poller reissue paths fetch issue title and body separately

- **Safety tag:** `RISKY_SKIP`
- **Files:** `scripts/orchestrate_poll_process.sh:12720-12722`, `scripts/orchestrate_poll_process.sh:15142-15150`, `scripts/orchestrate_poll_process.sh:19724-19730,19998-20000`
- **Current call count:** 6 call sites total; 2 calls per entered reissue path.
- **Proposed call count:** 3 call sites total; 1 call per entered path.
- **Endpoint:** `GET /repos/{owner}/{repo}/issues/{issue_number}`
- **Evidence:**
  ```bash
  orig_title="$(... --jq '.title // ""' ...)"
  orig_body="$(... --jq '.body // ""' ...)"
  ```
  The same pattern appears in `execute_stall_recovery_action`, `run_standalone_stall_recovery`, and the top-level implementation-failed reissue loop.
- **Proposed fix:** Fetch `{title,body}` once at each site, then derive `orig_title`/`orig_body` or `IF_TITLE`/`IF_BODY` locally.
- **Safety rationale:** This is `RISKY_SKIP` because all calls occur inside `orchestrate_poll_process.sh`, including explicit stall-recovery paths that defend against upstream races.
- **Downstream signal:** Do not auto-implement; manually verify each reissue path’s behavior when only one of the two current calls would have failed.

#### MERGE-003 — Finalizer reads PR state and merged status through separate requests

- **Safety tag:** `RISKY_SKIP`
- **File:** `scripts/orchestrate_poll_process.sh:9842-9853`
- **Current call count:** 2 when `final_pr_json_snapshot` is unavailable.
- **Proposed call count:** 1.
- **Endpoint:** `GET /repos/{owner}/{repo}/pulls/{pull_number}`
- **Evidence:**
  ```bash
  existing_pr_state="$(... --jq '.state' ...)"
  existing_pr_merged="$(... --jq '.merged_at != null' ...)"
  ```
- **Proposed fix:** In `finalize_integration_merge_if_needed`, call existing `_fetch_pr_json "${final_pr}"` once and extract both fields using `_jq_field`.
- **Safety rationale:** Although the calls are adjacent with no intervening mutation, this is `RISKY_SKIP` because the final-merge logic resides in `orchestrate_poll_process.sh` and explicitly protects race-sensitive state transitions.
- **Downstream signal:** Do not auto-implement; manually run final-merge tests for open, merged, closed-unmerged, malformed-response, and API-failure payloads.

#### MERGE-004 — E2E stability check re-fetches full PR metadata after its second snapshot

- **Safety tag:** `RISKY_SKIP`
- **File:** `.github/workflows/test-and-mark-stable.yml:1073-1090,1092-1113`
- **Current call count:** 3 on the normal first-attempt stable path.
- **Proposed call count:** 2 on that path.
- **Endpoint:** `GET /repos/{owner}/{repo}/pulls/{pull_number}`
- **Evidence:**
  ```bash
  HEAD_A=$(gh api ".../pulls/${PR_NUMBER}" --jq '.head.sha // ""')
  HEAD_B=$(gh api ".../pulls/${PR_NUMBER}" --jq '.head.sha // ""')
  PR_META=$(gh api ".../pulls/${PR_NUMBER}")
  ```
- **Proposed fix:** Make the second stability read capture full JSON, derive `HEAD_B` from it, and reuse it as `PR_META` only after stability is confirmed; retain the third call as fallback otherwise.
- **Safety rationale:** This is `RISKY_SKIP` because the calls are inside a retry loop and the later fetch is an explicit defense against asynchronous merge and indexing races.
- **Downstream signal:** Do not auto-implement; manually prove that reusing the second snapshot cannot miss a close or merge occurring between stability confirmation and bait injection.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — Failure-state PR metadata is not persisted for the next step

- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:** `.github/workflows/review_autofix.yml:7195-7213`, `.github/workflows/review_autofix.yml:7215-7261`
- **Current call count:** 2 when `${PR_META_FILE}` is missing or unusable and linked-issue caches are empty.
- **Proposed call count:** 1 after a successful first fetch; retain 2 only when that fetch fails.
- **Endpoint:** `GET /repos/{owner}/{repo}/pulls/{pull_number}`
- **Evidence:**
  ```bash
  pr_meta="$(gh_retry _safe_gh_jq ".../pulls/${PR_NUMBER}" ...)"
  ```
  The immediately following step may fetch the same PR again for `.title` and `.body`.
- **Proposed fix:** In `Check PR state before failure alerts`, validate and atomically project successful `pr_meta` into `${PR_META_FILE}` using its existing compact schema. Keep the live call in `Mark linked issues review-blocked (workflow failure)` solely as a cache-miss fallback.
- **Safety rationale:** This is `NEEDS_VERIFICATION` because the calls cross workflow-step boundaries and therefore fail the same-step precondition for `SAFE_TO_MERGE`.
- **Downstream signal:** Verify missing/corrupt-cache success and failure cases, preserve the fallback fetch, and confirm that a PR body edit between the two steps need not be observed.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- BATCH-001: `RISKY_SKIP` — consolidation includes paginated comment/review calls and must preserve partial-page fallback behavior.
- API-001: `RISKY_SKIP` — the source comment-list call is paginated, triggering mandatory manual review.
- BATCH-002: `RISKY_SKIP` — both REST pagination and GraphQL’s first-100-files boundary require explicit overflow handling.
- API-002: `RISKY_SKIP` — every affected call resides in `orchestrate_poll_process.sh`.
- API-003: `RISKY_SKIP` — authentication and rate-limit classification changes affect the shared retry-sensitive helper.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| `SAFE_TO_MERGE` | 0 | — |
| `NEEDS_VERIFICATION` | 1 | REUSE-001 |
| `RISKY_SKIP` | 4 | MERGE-001, MERGE-002, MERGE-003, MERGE-004 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
