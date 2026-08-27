## Executive Summary

- **CI is systematically timing out:** 23/32 runs (71.9%) were cancelled near the 30-minute job limit; 22 stopped in `lint / Orchestrate poll process unit tests`. Sharding plus a temporary 45-minute ceiling should eliminate most cancellations and save 8–15 minutes of critical-path latency. **Confidence: high.**
- **Review/autofix dominates operations:** it consumed 75.4% of recorded wall time; CI plus review/autofix account for 93.1%. Seven deep-dived successful reviews averaged 105 minutes of execution. **Confidence: high.**
- **Reviewer configuration is unnecessarily expensive:** each iteration launches six models twice, and pass 1 runs at `xhigh` despite repository documentation specifying `medium`. Changing only pass 1 to `medium` should save roughly 15–30 minutes and 20–35% of reviewer cost while retaining the `xhigh` second pass. **Confidence: high.**
- **The consolidator rollout is broken but masked:** all 7 sampled slow reviews failed immediately with `Not inside a trusted directory and --skip-git-repo-check was not specified`, then continued fail-open. **Impact:** lost synthesis, parser coverage, and lessons-learned generation. **Confidence: high.**
- **Editor retries are systemic:** all 7 sampled reviews succeeded only on attempt 3; 12 attempts failed manifest validation and 2 were stall-killed. Fixing the output contract could save 10–18 minutes per normal review and more than 30 minutes in stall cases. **Confidence: high.**
- **Cost and API observability are incomplete:** OpenRouter reports 143 calls but no token/cache values, `cache_hit_rate` is unavailable, no `GH_API_CALL` telemetry exists, and failed-run folders contain metadata without step logs. **Confidence: high.**

## Speed Optimizations

### 1. Critical path: shard the orchestrator-poll CI tests

- **Evidence:** 23/32 CI runs were cancelled; 22 failed at `Orchestrate poll process unit tests`, generally after 1,817–1,839 seconds. Examples: runs `33070953400` and `33060444929`. Successful runs still took 1,211–1,743 seconds, including `33060427213` and `33059180763`.
- **Root cause:** `.github/workflows/ci.yml` places the large remaining test suite in one 30-minute `lint` job.
- **Exact change:** split the derived test list into 2–3 deterministic matrix shards, retain a final aggregate `CI` check, and temporarily raise the current job ceiling to 45 minutes during rollout.
- **Diagnostic logging:** emit `TEST_TIMING name=... elapsed_ms=... outcome=... shard=...` and a heartbeat with completed/remaining counts.
- **Estimated savings:** 8–15 minutes p95; removes approximately 11.6 hours of cancelled CI wall time in this window.
- **Risk:** medium for sharding; low for the temporary timeout increase.

### 2. Critical path: run reviewer pass 1 at medium reasoning

- **Evidence:** the 7 slow reviews spent an average 4,317 seconds in reviewer passes. `scripts/review_run_reviewers.sh` launches pass 1 with `xhigh`, while `probably_unnecessary_but_read_if_stuck.md` documents pass 1 as `medium`.
- **Root cause:** hard-coded reasoning drift; six reviewers run twice at high cost.
- **Exact change:** introduce `REVIEWER_PASS1_REASONING`, defaulting to `medium`; leave pass 2 at the existing scheduled `xhigh`.
- **Estimated savings:** 15–30 minutes and 20–35% of reviewer spend per full review.
- **Risk:** low-to-medium because the deep second pass remains unchanged.

### 3. Critical path: repair editor manifest retries

- **Evidence:** all 7 sampled runs succeeded on attempt 3. Runs `32978817666`, `32980421457`, `32987735882`, `33004184186`, `33041351113`, and `33056159542` each had two manifest-validation failures. Run `33015375913` had two stall-killed attempts.
- **Root cause:** inferred output-contract mismatch; the same retry pattern across unrelated PRs is unlikely to be PR-specific.
- **Exact change:** inject an exact machine-generated manifest skeleton, report missing entries to the retry prompt, and reuse valid worktree edits while retrying only summary repair.
- **Diagnostic logging:** `EDITOR_VALIDATION_FAILURE attempt=... reason=... missing_count=... output_bytes=... worktree_changed=...`.
- **Estimated savings:** 10–18 minutes normally; approximately 36 minutes for run `33015375913`.
- **Risk:** medium; preserve existing final validation and fail-open behavior.

### 4. Critical path: suppress same-head queued review duplicates

- **Evidence:** review runs waited 2,123 seconds (`33004184186`) and 6,186 seconds (`33041351113`) between job evaluation and runner request; runner pickup was effectively immediate. Runs `32980399079`, `33015359985`, and `33056143324` were cancelled before the first codex step after 6,276–7,051 seconds.
- **Root cause:** overlapping wrapper/direct dispatches serialize in `pr-autofix-<PR>` with `cancel-in-progress=false`.
- **Exact change:** keep running jobs protected, but suppress dispatch when the same PR and head SHA already has a running or queued review. Cancel only stale queued duplicates, never active work.
- **Diagnostic logging:** emit concurrency key, head SHA, active owner run ID, queue age, and suppression reason.
- **Estimated savings:** up to 35–103 minutes of queue delay on affected runs.
- **Risk:** low-to-medium.

**Micro-optimization note:** Semble consumed only about 6.8 seconds across 12 deep-dive queries; it is not a meaningful latency target.

## Cost Optimizations

### 1. Investigate the implement token outlier before changing models

- **Evidence:** implement run `33039973788` used 1,326,191 Codex tokens across 13 calls—96.75% of all 1,370,765 recorded Codex tokens.
- **Root cause:** unknown because this token outlier was not archived for deep inspection.
- **Exact change:** archive top-token runs independently of top-duration runs; emit prompt tokens, completion tokens, role, model, reasoning, phase, and stable-prefix hash per call.
- **Estimated savings:** potentially 0.66–1.06 million tokens if repeated context or runaway retries account for 50–80% of the outlier.
- **Quality risk:** do not downgrade the implement model until the per-call breakdown identifies the expensive stage.

### 2. Reduce pass-1 reasoning, not reviewer coverage

- **Evidence:** 12 reviewer invocations per full iteration; sampled reviews logged 13–16 OpenRouter calls after retries.
- **Exact change:** use medium reasoning for pass 1 and retain all six models plus `xhigh` pass 2.
- **Estimated savings:** 20–35% of reviewer token/dollar cost.
- **Quality risk:** lower than reducing roster size because diversity and deep review remain intact.

### 3. Eliminate failed editor attempts

- **Evidence:** 14 of 21 sampled editor attempts failed before attempt 3.
- **Exact change:** repair manifest generation and use a summary-only continuation after valid code edits.
- **Estimated savings:** approximately 40–65% of editor spend in affected runs.
- **Quality risk:** low if existing validation remains authoritative.

### 4. Preserve Semble; instrument Serena before expanding it

- **Semble:** 22 queries returned 196,762 logged bytes, with no runtime fallback. Deep queries averaged under one second. It appears to be selecting bounded context rather than causing prompt expansion.
- **Serena:** 8 query summaries represented 180 tool calls, but reported zero response bytes and zero milliseconds. In run `33056159542`, the visible calls were only `chmod` and `write_text`, so Serena did not replace expensive discovery work there.
- **Exact change:** record per-tool success/error, response bytes, duration, files touched, and whether the call replaced a shell/read operation.
- **Estimated savings:** unknown until instrumentation distinguishes productive edits from noisy round trips.
- **Quality risk:** none for logging-only changes.

### 5. Stop treating unavailable OpenRouter metrics as zeros

- **Evidence:** 143 calls were collected, but every prompt/completion/cache total is zero or `na`; dollar cost cannot be calculated.
- **Exact change:** emit `usage_available=false reason=...` instead of numerical zero and capture provider request IDs plus usage-source status.
- **Estimated savings:** enables reliable prioritization; direct savings not currently measurable.
- **Quality risk:** none.

## Reliability Improvements

### 1. Prevent CI’s deterministic 30-minute cancellation

- **Failure evidence:** 71.9% CI cancellation rate; 22 runs share the same terminal step.
- **Category:** timeout/capacity.
- **Fix:** temporary 45-minute ceiling, then matrix sharding and per-test timeout diagnostics.
- **Expected impact:** likely raises CI completion from 28.1% to above 90% for this workload.
- **Rollback:** revert the matrix while retaining the higher timeout and timing markers.

### 2. Fix the consolidator trust failure

- **Failure evidence:** 7/7 deep reviews produced zero consolidator bytes and the identical trust error.
- **Category:** configuration rollout.
- **Fix:** add `--skip-git-repo-check` to the synthesis-only consolidator command or execute from a verified trusted worktree. Add a preflight `git rev-parse` result marker.
- **Expected impact:** restores consolidation, parser coverage, and lessons generation.
- **Fail-open:** retain current reviewer-bundle passthrough if consolidation still fails.
- **Rollback:** disable `REVIEW_CONSOLIDATOR_ENABLED` explicitly rather than silently running a broken stage.

### 3. Carry reviewer rate-limit state across passes

- **Failure evidence:** every deep review had a retryable reviewer failure. Of 17 advances, 16 involved `minimax/minimax-m3`; two runs ultimately used its fallback model.
- **Category:** provider capacity.
- **Fix:** when pass 1 rate-limits a slot, start pass 2 with the successful cheaper-reasoning or fallback selection rather than retrying the known-saturated primary.
- **Expected impact:** removes repeated 5–10 minute retry episodes.
- **Rollback:** clear the same-run health hint; keep existing retry ladder.

### 4. Make failed workflows diagnosable

- **Failure evidence:** runs `32984390950` and `32985679705` have no job, step, or log excerpts. Startup failures `32984400029` and `32985626080` were not archived under `errors/`.
- **Category:** collector coverage.
- **Fix:** archive startup failures; query the jobs API even when log download fails; record `log_download_status`, API error, workflow validation annotations, and job conclusions.
- **Expected impact:** converts four opaque failures into actionable classifications.
- **Fail-open:** always write metadata, even if every API request fails.

### 5. Add structured MCP availability probes

- **Evidence:** all 35 Semble fallbacks were correctly classified as contract-test fallbacks, not runtime failures. However run `33071397954` reported `SEMBLE_AVAILABLE:false` without a structured probe/fallback, and all probe counters remain zero.
- **Fix:** emit `SEMBLE_PROBE` and `SERENA_PROBE` whenever availability is decided, including `target`, `result`, `reason`, and elapsed time.
- **Impact:** distinguishes unavailable optional tooling from a masked broken deployment.

No `BREAK_GLASS` or `CONTEXT_BUDGET_WARN` events were observed.

## AI Memory Health

- **Retrieval:** 7/7 deep-dive retrieves selected records, for a 100% hit rate.
- **Budget:** average estimated context was 1,383 of 1,400 tokens—98.8% utilization.
- **Selection:** every retrieve used `keyword_method=llm` and selected exactly 30 records, suggesting the result cap is consistently saturated.
- **Push health:** 3/14 `record-run-event` operations required two push attempts; mean was 1.21 attempts.
- **Gaps:** 6 sampled runs emitted `write_lessons_learned count=0`; the broken consolidator is a likely contributor. Run `33070949342` also logged a fail-open `force-tick-put` failure.
- **Recommendation:** after repairing the consolidator, log candidate count, selected-score range, age distribution, duplicate suppression, and push failure reason. Alert only after repeated fail-open pushes.
- No sampled `retrieve` had `enabled=false`, zero records, or `fail_open=true`. No meaningful `promote`, `compact`, or `finalize-task` coverage was present.

## GH API Call Audit

No structured GH API call-count telemetry was found. Model-provider rate limits above must not be confused with GitHub API rate limits.

| Hotspot | Evidence | Recommendation | Estimated reduction |
|---|---|---|---:|
| Per-reviewer PR-state watchdog | `review_run_reviewers.sh` checks PR state from each parallel reviewer; six reviewers run per pass | One parent monitor writes shared PR state for child watchers | Up to 83% for this endpoint |
| Gate metadata/files lookups | `review_autofix.yml` performs direct PR, commit, GraphQL, and paginated file calls | Wrap all through `gh_helpers.sh`; persist PR/file JSON for reuse | 1+ calls per fallback path |
| Check-run polling | Same check-run snapshot may be fetched repeatedly for up to 300 seconds | Emit snapshot hash/page count and reuse unchanged payloads | 20–60% during stable waits |
| Overlapping dispatch checks | Sweep checks both `internal-review.yml` and `review_autofix.yml` | Build one cached active-run index per sweep | O(PR×workflow) to O(workflow pages) |

This aligns with `agents.md`, which requires cycle-local caches and forbids re-fetching cached orchestrator state per iteration.

**Required logging addition:**

`GH_API_CALL method=GET endpoint_template=repos/:owner/:repo/pulls/:number logical_attempt=1 pages=1 status=200 elapsed_ms=... retry_reason=none rate_remaining=...`

Also emit a job-end `GH_API_SUMMARY`. Endpoint values must be sanitized, and GraphQL bodies must never be logged.

## Prompt Cache & Memory System

- `cache_hit_rate` is unavailable across the entire window.
- OpenRouter cache reads/writes are zero because usage fields were logged as `na`, not because cache misses were proven.
- All 42 reviewer slot summaries across the 7 deep runs reported zero cache-read tokens. Seven retries attempted reuse, but none recorded a read.
- Editor retries deliberately append an epoch/nonce, guaranteeing cache fragmentation. Keep this safety mechanism until manifest compliance is fixed; then limit nonce busting to refusals or repeated invalid cached output.
- Dependency-cache hits such as run `33069893455` are healthy but unrelated to prompt caching. Plan run `33071734461` reported concurrent cache creation, indicating dependency-cache contention.
- Copilot reviews built prompts of 50,334 tokens (`33067152986`) and 65,022 tokens (`33069878980`). No context-budget warning fired, so these were below the configured threshold—not necessarily small.
- **Recommendation:** log stable-prefix hash, dynamic-section token counts, cache eligibility, provider usage availability, and miss reason. Keep static instructions first and move timestamps, run IDs, and retry diagnostics to the tail.

## Orchestrator Health

- `orchestrate_poll` completed 23/23 runs successfully, with p50 246 seconds and p95 374 seconds.
- The recent poll runs `33058970573` and `33071397954` spent almost their full 236–269 seconds in the poll step; this is stable rather than a failure spike.
- Run `33061939791` found queued candidates aged 1,136–1,141 minutes but skipped them as `active_run`. This shows the sweep cannot distinguish healthy active work from stale concurrency backlog.
- Clarify, plan, implement, and clarify-response produced 335 skipped runs overall. These appear mostly to be healthy gating, but skip reasons are not consistently aggregated.
- **Smallest safe mitigation:** add `ORCHESTRATOR_QUEUE_STATE` with active run ID, status, head SHA, queue age, and concurrency key; emit `WORKFLOW_SKIP reason=...` for every gated workflow.
- Track: phase age, same-head queued count, pending-to-running delay, recovery action, judge fingerprint repeats, and merge-deferral count.

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Priority |
|---|---|---|---:|
| Clarify | Occasional model work | Successful run `33071664674` took 118s; most runs skipped | Low |
| Plan | High reasoning and cache contention | Run `33071734461` took 291s; maximum 705s | Medium |
| Implement | Token expansion | Run `33039973788`: 1.326M tokens, 1,005s | High cost |
| Review | Two full six-model passes | Deep sample averaged 72 minutes | Highest |
| Autofix editor | Two failed retries before success | 7/7 succeeded only on attempt 3 | Highest |
| CI | Single 30-minute test job | 23/32 cancelled | Highest reliability |
| Orchestrator poll | Fixed polling work | p50 246s, 23/23 success | Medium |
| Merge/conflict | Additional resolver work | Run `33041351113` resolved a conflict and pushed successfully | Situational |

Queueing, reviewer compute, editor retries, and CI timeouts—not Semble or runner pickup—dominate end-to-end latency.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix p95 6,750 seconds; CI p50 1,818 seconds; same-PR concurrency waits up to 6,186 seconds.
- **Top failure modes:** 23 CI cancellations, 21 review cancellations, broken consolidator, systematic editor retries, and opaque failed/startup runs.
- **Highest-cost drivers:** implement run `33039973788`; twelve-model-call two-pass reviews; repeated editor attempts.
- **Top actions:**
  1. Shard orchestrator-poll tests and temporarily raise CI timeout.
  2. Fix consolidator trust handling and editor manifest compliance.
  3. Set reviewer pass 1 to medium and add same-head dispatch suppression.

## Metrics Appendix

### Run outcomes

| Outcome | Count | Rate |
|---|---:|---:|
| Total | 609 | 100% |
| Success | 226 | 37.11% |
| Failure | 2 | 0.33% |
| Startup failure | 2 | 0.33% |
| Cancelled | 44 | 7.22% |
| Skipped | 335 | 55.01% |

Overall duration: p50 **8s**, p95 **4,719s**. Cancelled runs represent **33.76%** of recorded wall time.

### Key workflow families

| Family | Runs | Success | Failure/startup | Cancelled | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|
| CI | 32 | 9 | 0 | 23 | 1,818s | 1,827s |
| Review/autofix | 104 | 80 | 3 | 21 | 35s | 6,750s |
| Copilot reviewer | 32 | 31 | 1 | 0 | 206s | 321s |
| Orchestrate poll | 23 | 23 | 0 | 0 | 246s | 374s |
| Clarify | 87 | 2 | 0 | 0 | 1s | 10s |
| Plan | 84 | 2 | 0 | 0 | 1s | 11s |
| Implement | 85 | 1 | 0 | 0 | 1s | 11s |

### Cost and review telemetry

| Metric | Value |
|---|---:|
| Runs with log telemetry | 115/609 (18.88%) |
| Codex calls | 39 |
| Codex tokens | 1,370,765 |
| OpenRouter calls | 143 |
| OpenRouter tokens | Unavailable (`na`) |
| Cache read/write tokens | Unavailable |
| `cache_hit_rate` | Unavailable |
| `wall_clock_p50_ms` | 9,000 |
| `wall_clock_p99_ms` | 7,790,240 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |

### MCP telemetry

| System/target | Queries | Tool calls | Bytes | Fallbacks | Probe OK/failed/skipped |
|---|---:|---:|---:|---:|---:|
| Semble, all | 22 | — | 196,762 | 35 contract-test; 0 runtime | 0 / 0 / 0 |
| Semble reviewer-context, deep sample | 7 | — | 79,922 | 0 | 0 / 0 / 0 |
| Semble overflow, deep sample | 4 | — | 32,391 | 0 | 0 / 0 / 0 |
| Semble conflict-resolver-context | 1 | — | 9,286 | 0 | 0 / 0 / 0 |
| Serena review/autofix | 6 | 36 | 0 | 0 | 0 / 0 / 0 |
| Serena implement | 2 | 144 | 0 | 0 | 0 / 0 / 0 |

Other MCP servers observed: **none**.

### GH API telemetry

| Metric | Value |
|---|---|
| Structured call count | Not emitted |
| Per-endpoint counts | Not emitted |
| Retry totals | Not emitted |
| Rate-limit events | None visible in sampled logs; global coverage incomplete |
| Required next step | Instrument `gh_helpers.sh` and direct gate calls |

## Deep Audit — Workflows & Scripts (2026-08-27)

### Section 1: Bug & Correctness Sweep

All scoped Bash, Python, and workflow YAML files passed syntax parsing. Previously documented CI timeout, consolidator, editor-retry, and reviewer-cost findings are not duplicated here.

#### BUG-001 — Phase-label replacement can erase concurrent labels
- **ID:** `BUG-001`
- **File path:** `scripts/label_helpers.sh:166-216`; callers include `.github/workflows/issue_pr_status.yml:430-458` and `.github/workflows/review_autofix.yml:5111-5116,6657-6661`
- **Severity:** High
- **Category tag:** `bug`
- **Description:** `set_issue_phase_label_resilient` reads all labels, computes a replacement list, then uses `PUT /labels`. Any label added between the GET and PUT—including control labels such as `force-review` or orchestrator metadata—is silently removed.
- **Recommended fix:** Replace the whole-list PUT with selective `--remove-label`/`--add-label` arguments, following `set_issue_phase_label` in `scripts/orchestrate_poll_process.sh:2453-2498`. Preserve unrelated labels without a read-modify-replace operation.

#### BUG-002 — Failed label reads create contradictory phase states
- **ID:** `BUG-002`
- **File path:** `scripts/label_helpers.sh:182-202`
- **Severity:** Medium
- **Category tag:** `bug`
- **Description:** When the current-label GET or local `jq` transformation fails, the helper POSTs the target label without removing the previous phase label and returns success. This violates phase-group exclusivity and can leave states such as `ai:done` plus `ai:review-blocked`.
- **Recommended fix:** On an unconfirmed current-label snapshot, perform no mutation and return a distinct retryable status. Let the existing poller reconciliation repair the phase on the next cycle.

#### SEC-001 — PAT is persisted in model-accessible Git configuration
- **ID:** `SEC-001`
- **File path:** `.github/workflows/clarify.yml:149-158`; `.github/workflows/plan.yml:217-226`; `.github/workflows/implement.yml:786-796`; `.github/workflows/orchestrate.yml:85-94`; `.github/workflows/review_autofix.yml:1441-1450`; `.github/workflows/validate.yml:182-191`; `.github/workflows/orchestrate_poll.yml:201-210`
- **Severity:** High
- **Category tag:** `security`
- **Description:** These steps embed `GH_PAT` into `remote.origin.url`, after which Codex runs with repository access—for example `clarify.yml:754-945`, `orchestrate.yml:613-723`, and `validate.yml:796-820`. **Inference:** an untrusted prompt can cause the model to read `.git/config`, exposing the credential outside GitHub’s log masking.
- **Recommended fix:** Set checkout `persist-credentials: false`, keep a credential-free origin, and authenticate individual Git operations through a temporary header/askpass helper. Reuse the scrub-before-Codex pattern in `orchestrate_clarify_respond.yml:696-702,1103-1108`.

#### SEC-002 — Check-run text is interpolated into shell source before the fork guard
- **ID:** `SEC-002`
- **File path:** `.github/workflows/check_failure_triage.yml:96-145,288-295`; `.github/workflows/internal-check-failure-triage.yml:13-27`
- **Severity:** High
- **Category tag:** `security`
- **Description:** `inputs.check_name` is inserted directly into double-quoted shell commands. Shell metacharacters such as command substitutions become executable syntax after Actions interpolation. The logging step runs before the fork-origin guard while job secrets are configured. Exploitability depends on who can create the associated check-run name. `[NEEDS VERIFICATION]`
- **Recommended fix:** Pass every input through step `env`, log only quoted shell variables, and validate `pr_number`, `check_run_id`, conclusion, and SHA before API use. Apply the same treatment to the failure-notification step.

### Section 2: GitHub API Call Redundancy Audit

The prior report’s reviewer-watchdog, gate metadata/files, check-run polling, and sweep indexing hotspots are not reissued as new IDs.

#### API-001 — Final PR metadata is fetched twice
- **ID:** `API-001`
- **File path:** `scripts/orchestrate_poll_process.sh:7358-7364`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** The same `GET /pulls/{final_pr}` endpoint is called separately for `.state` and `.merged_at`. Current count: **2** calls; proposed count: **1**.
- **Recommended fix:** Fetch one JSON object and derive both fields locally, following the existing `_fetch_pr_json` plus `_jq_field` pattern.

#### BATCH-001 — Standalone discovery makes seven label-list calls
- **ID:** `BATCH-001`
- **File path:** `scripts/orchestrate_poll_process.sh:11652-11682`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** The standalone recovery sweep runs one `gh issue list` for each of seven phase labels. Current count: **7** logical calls per sweep; proposed count: **1** normal-path GraphQL request.
- **Recommended fix:** Extend `_fetch_standalone_marker_issues_graphql` with seven aliased searches and merge their issue numbers. Retain paginated REST only when an alias reports `hasNextPage`.

#### BATCH-002 — Post-judge label refresh reverts to per-issue REST
- **ID:** `BATCH-002`
- **File path:** `scripts/orchestrate_poll_process.sh:16475-16513`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** After review-blocked handling, every current or replacement issue’s labels are fetched individually. Current count: **U** calls for U unique issues; proposed count: **ceil(U/25)**.
- **Recommended fix:** Build one unique issue-number array and call `_fetch_issue_labels_batch_graphql`, retaining per-issue REST only for missing aliases.

#### BATCH-003 — Blocker status is fetched inside a nested loop
- **ID:** `BATCH-003`
- **File path:** `scripts/orchestrate_poll_process.sh:16554-16640`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** Every parsed post-Codex blocker receives an individual issue-state lookup. Current count: **B** calls for B blockers; proposed count: **ceil(U/25)** for U unique blockers across the cycle. Restructuring requires collecting blocker IDs before decisions. `[NEEDS VERIFICATION]`
- **Recommended fix:** Batch blocker states through `_fetch_candidate_issue_details_graphql`, then read `.state` from a cycle-local map.

#### API-002 — Failed-review detection queries three workflows serially
- **ID:** `API-002`
- **File path:** `scripts/orchestrate_poll_process.sh:9891-9920`
- **Severity:** Medium
- **Category tag:** `api-redundancy`
- **Description:** Each stalled PR can issue one branch-scoped `gh run list` for each of three workflow files. Current count: **1–3** calls; proposed count: **1**, or **0** when the completed-run cache is conclusive.
- **Recommended fix:** Extend `_direct_inflight_review_run_on_branch` into a branch-run snapshot helper returning active and completed review-family runs, then filter workflow names locally. Consult `_load_actions_runs_cached` first.

#### API-003 — Feature sweep refetches each behind PR for its head SHA
- **ID:** `API-003`
- **File path:** `scripts/orchestrate_poll_process.sh:15195-15239`
- **Severity:** Medium
- **Category tag:** `api-redundancy`
- **Description:** The initial PR listing already supplies each candidate’s metadata, but every behind PR triggers another GET for `.head.sha`. Current count: **1+B** for B behind PRs; proposed count: **1**. Availability of `headRefOid` must be confirmed against the pinned `gh` version. `[NEEDS VERIFICATION]`
- **Recommended fix:** Add `headRefOid` to the existing `gh pr list --json` fields and use it as `expected_head_sha`.

#### API-004 — Fork guard retries permanent failures as though transient
- **ID:** `API-004`
- **File path:** `.github/workflows/check_failure_triage.yml:117-145`
- **Severity:** Medium
- **Category tag:** `api-redundancy`
- **Description:** The guard retries every error three times with fixed 2/4-second sleeps, including malformed input, 401/403, and 404 responses. Current count: **1–3** unconditional attempts; proposed count: **1 logical** `gh_retry` call, with retries restricted to transient failures.
- **Recommended fix:** Stage and source `gh_helpers.sh`, validate the PR number first, and delegate retry classification and reset-aware backoff to `gh_retry`.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Stall-state parser is copied five times
- **ID:** `DUP-001`
- **File path:** `scripts/watchdog_helpers.sh:186-201`; `scripts/review_conflict_resolve.sh:173-187`; `scripts/review_rb_judge.sh:115-129`; `scripts/review_run_reviewers.sh:299-313`; `scripts/self_heal_validation.sh:143-158`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** Five byte-equivalent implementations of `read_codex_stall_guard_state` exist.
- **Recommended fix:** Keep `read_codex_stall_guard_state <status_file>` solely in `watchdog_helpers.sh`; source it from the four callers and retain a minimal fail-open fallback only when the helper is unavailable.

#### DUP-002 — Context-budget warning logic is copied three times
- **ID:** `DUP-002`
- **File path:** `scripts/review_apply_fixes.sh:103-141`; `scripts/review_rb_judge.sh:203-241`; `scripts/review_run_reviewers.sh:44-82`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** The same Python import, environment setup, argument validation, and output logic appears in three review stages.
- **Recommended fix:** Add `scripts/prompt_budget_helpers.sh` owning `emit_context_budget_warn_for_prompt <phase> <prompt_path> <model>`, then source it from all three scripts.

#### DUP-003 — Integration-ref bootstrap is repeated across workflows
- **ID:** `DUP-003`
- **File path:** `.github/workflows/clarify.yml:57-130`; `.github/workflows/implement.yml:318-392`; `.github/workflows/orchestrate_clarify_respond.yml:110-184`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** Three approximately 70-line blocks independently clone the workflow source, fetch a preferred ref, redact clone errors, locate `resolve_integration_ref.sh`, and emit the result.
- **Recommended fix:** Create a composite action with inputs `issue_number`, `repository`, and `token`, and output `ref`. Update all three workflows to call that action.

#### DUP-004 — Prompt path resolution is duplicated
- **ID:** `DUP-004`
- **File path:** `scripts/assemble_prompt.sh:12-41,72-93`; `scripts/render_prompt.sh:12-41,138-159`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** `resolve_prompt_file` and `resolve_assembly_source_path` are duplicated between the two prompt entrypoints.
- **Recommended fix:** Move both functions into `scripts/prompt_path_helpers.sh` with signatures `resolve_prompt_file <path>` and `resolve_assembly_source_path <path>`.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Implement support-staging block exceeds the medium-risk threshold
- **ID:** `EXPR-001`
- **File path:** `.github/workflows/implement.yml:851-1153`
- **Severity:** Medium
- **Category tag:** `expression-limit`
- **Description:** The interpolated run body is approximately **16,339 characters**, leaving **4,661 characters** before the 21,000-character limit. It contains three `${{ }}` interpolations and is at approximately 77.8% of the limit.
- **Recommended fix:** Extend `scripts/stage_workflow_support.sh` with an implement-specific manifest and replace this inline staging implementation with one script invocation.

#### EXPR-002 — Validation embeds a growth-sensitive support manifest
- **ID:** `EXPR-002`
- **File path:** `.github/workflows/validate.yml:202-424`
- **Severity:** Low
- **Category tag:** `expression-limit`
- **Description:** The interpolated run body is approximately **13,096 characters**, leaving **7,904 characters**. It is below the 15,000-character threshold but embeds a large JSON manifest heredoc whose asset lists will grow over time.
- **Recommended fix:** Move the manifest into a checked-in JSON file or a named manifest inside `stage_workflow_support.sh`, then pass only its path from the workflow.

No workflow exceeds 800 KB. The largest, `review_autofix.yml`, is approximately 453,544 characters.

### Section 5: Cross-Cutting Concerns

No `TODO`, `FIXME`, or `HACK` markers were found in the scoped files. Repository-wide ShellCheck found no validated SC2086, SC2046, or SC2006 defects.

#### DEAD-001 — Current floor classifications are stored but never consumed
- **ID:** `DEAD-001`
- **File path:** `scripts/review_issue_ledger.sh:862-920`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `CURRENT_FLOOR` is declared and populated for each issue but never read.
- **Recommended fix:** Remove the array and assignment, or add a corresponding `FINAL_FLOOR` field and persist it if floor classification is intended to remain part of ledger output.

#### DEAD-002 — Two reviewer raw-artifact aliases are unused
- **ID:** `DEAD-002`
- **File path:** `scripts/review_run_reviewers.sh:578-586`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE` and `RAW_REVIEWER_SYMBOL_DIFF_SUMMARY_FILE` are assigned but never referenced; filtered replacements are generated from other inputs.
- **Recommended fix:** Remove both aliases after confirming no external sourced caller relies on dynamically scoped variables.

#### DEAD-003 — Branch-rebuild diagnostic outputs are never observed
- **ID:** `DEAD-003`
- **File path:** `scripts/orchestrate_poll_process.sh:6311-6396`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `BRANCH_REBUILD_SKIP_REASON` and `BRANCH_REBUILD_LAST_REBUILD_AT` are assigned across threshold and cooldown branches but never read or logged.
- **Recommended fix:** Emit them in a structured `BRANCH_REBUILD_SKIP` diagnostic when `_check_branch_rebuild_threshold` returns false, or remove the assignments.

#### SHELL-001 — Reused `cmd` identifier prevents clean ShellCheck
- **ID:** `SHELL-001`
- **File path:** `scripts/codex_thread_reuse.sh:492-505,806-856`
- **Severity:** Low
- **Category tag:** `shellcheck`
- **Description:** ShellCheck reports SC2178/SC2128 because `cmd` is an array in `codex_thread_reuse_run_once` and a scalar subcommand in `codex_thread_reuse_main`. Runtime behavior is protected by function-local scope, but the warning obscures future genuine array mistakes.
- **Recommended fix:** Add a narrowly scoped ShellCheck suppression documenting function-local isolation, preserving the existing identifier contract.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BUG-001, SEC-001, SEC-002 |
| Medium | 9 | BUG-002, API-002, API-003, API-004, BATCH-001, BATCH-002, BATCH-003, DUP-003, EXPR-001 |
| Low | 9 | API-001, DUP-001, DUP-002, DUP-004, EXPR-002, DEAD-001, DEAD-002, DEAD-003, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 9–11 | Large |
| API call optimization | 2 | Medium |
| Code modularization | 12–14 | Large |
| Expression size reduction | 3–4 | Medium |
| Medium/Low fixes | 6–9 | Medium |
