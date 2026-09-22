## Executive Summary

- **CI is systemically broken:** 27/35 runs failed (77.1%); 23 stopped at `Inventory parity`, four at `Script-workflow cross-reference`. Selected logs consistently report stale `scripts/helper.sh` references in `agents.md` or `implement.yml`. Moving these checks first and fixing the references would save approximately **8.4 runner-hours per comparable window**. **Impact: very high; confidence: high.**
- **Three review/autofix runs spent 3.14 hours and 46.0M token units before an editor configuration precondition failed:** runs `35680228793`, `35685250882`, and `35689046981` all ended with `OPENROUTER_API_KEY must be set for editor isolation`. **Impact: 46.0M avoidable tokens; confidence: high.**
- **Review latency is dominated by queueing and oversized panels:** review/autofix p50 was 1,005s and p95 4,504s; 14 runs were cancelled after accumulating 31,457 wall-seconds. Runs `35686664615` and `35689898407` each waited roughly 62–68 minutes for hosted runners. **Impact: 4–8 hours less waiting per window through stricter deduplication; confidence: medium.**
- **The stable-release cycle lost 110 minutes waiting for a smoke test whose only final failure was missing `cancel_on_pr_close` registration:** run `35672590166` polled for ten minutes, performing approximately 67 workflow-run list calls before returning `no_run`; parent `35672563468` then failed. **Impact: 8–9 minutes faster failure diagnosis plus lower API use; confidence: high.**
- **Caching and memory retrieval are positive but prompt volume remains extreme:** 112.8M cache-read tokens produced a derived weighted cache-hit rate of **77.42%**, while selected memory retrieves hit 10/10 times. However, three same-head failures still consumed 7.85M fresh prompt tokens. **Impact: high; confidence: high.**
- **Semble appears efficient, not noisy:** 28 queries returned 325,053 logged bytes; 60/61 fallbacks were intentional contract tests and the sole runtime fallback was `budget-exhausted`. Serena was disabled, so no rollout assessment is possible. **Impact: retain Semble; confidence: high.**

## Speed Optimizations

1. **Run parity checks before the full CI suite — critical path.**
   - **Evidence:** 23 failures at `Inventory parity`; four at `Script-workflow cross-reference`. Runs `35689898156` and `35689341722` ran about 17 minutes before reporting `agents.md:390: unexpected tracked-surface reference scripts/helper.sh`. Runs `35694071722`, `35693473219`, and `35691005682` ran 30–43 minutes before reporting `implement.yml: references scripts/helper.sh which does not exist`.
   - **Root cause:** cheap deterministic checks execute after hundreds of tests.
   - **Exact change:** place `tests/inventory_parity.py` and `scripts/check_workflow_script_refs.py` immediately after checkout; retain their later invocation temporarily as a non-blocking parity assertion.
   - **Estimated saving:** 15–43 minutes per affected run; about 8.4 hours across this window.
   - **Risk:** low.

2. **Validate editor isolation credentials before reviewer invocation — critical path.**
   - **Evidence:** runs `35680228793`, `35685250882`, and `35689046981` spent 2,887–4,054 seconds before the editor failed instantly.
   - **Root cause:** editor-only credential validation occurs after the reviewer panel and consolidator.
   - **Exact change:** add a preflight that creates and dry-runs the isolated editor launcher without exposing credentials to the model. Emit `EDITOR_PREFLIGHT result=... credential_source=... launcher=...`.
   - **Estimated saving:** 48–67 minutes on each recurrence.
   - **Risk:** low; fail closed before model calls.

3. **Collapse duplicate review queue entries — critical path.**
   - **Evidence:** review/autofix had 14 cancellations, including runs cancelled before their first step after 4,245–4,482 seconds. Sweep logs repeatedly show all candidates already active.
   - **Root cause:** PR events, sweeps, and recovery dispatches can enqueue overlapping same-PR/head work before cancellation becomes effective.
   - **Exact change:** key concurrency by `repository + PR + head_sha`; have sweeps prefetch queued/in-progress runs once and cancel older exact-head duplicates before dispatch.
   - **Estimated saving:** 4–8 hours of wall-clock queue occupancy per comparable window.
   - **Risk:** medium; force-judge and conflict-resolution dispatches must bypass deduplication.

4. **Batch orchestrator memory writes and reuse support checkouts — critical path.**
   - **Evidence:** poller run `35695640921` took 319s. Recording `poll_started` took 55s and `poll_completed` 52s; the primary checkout took 50s. Multiple support checkouts resolved the same SHA.
   - **Root cause:** two independent memory clone/commit/push cycles and repeated checkout setup.
   - **Exact change:** retain one memory worktree and flush start/end events together in the final `always()` step; when repository and support SHA match, stage support files from the primary checkout with checkout fallback on mismatch.
   - **Estimated saving:** 60–120s per poll run.
   - **Risk:** medium; hard runner termination may lose the unflushed start event.

5. **Separate event-registration timeout from execution timeout in release smoke — critical but infrequent.**
   - **Evidence:** run `35672590166` waited ten minutes without observing any new cancel workflow run.
   - **Exact change:** fail after 90 seconds if no run is registered; retain the longer timeout once a run ID exists. Dump the latest 20 candidate runs before failing.
   - **Estimated saving:** about 8.5 minutes on missing-webhook failures.
   - **Risk:** low-to-medium.

## Cost Optimizations

1. **Move editor preflight ahead of all model calls.**
   - **Avoidable spend:** three failures consumed 46,032,446 total token units: 7,853,659 prompt, 476,213 completion, and 37,702,958 cache-read tokens.
   - **Change:** credential/launcher preflight before the six-reviewer panel.
   - **Estimated saving:** the full 46.0M units for this incident pattern. Dollar value cannot be calculated without per-model prices.
   - **Quality risk:** none.

2. **Enable reviewer health circuit-breaking for persistent provider throttling.**
   - **Evidence:** Mistral produced 60 upstream rate-limit errors across ten sessions in runs `35657288328`, `35665803378`, and `35672480663`; every session retried six times. Logs show `REVIEWER_CIRCUIT_BREAKER_ENABLED=0`.
   - **Change:** open the circuit after one exhausted rate-limit session for 30 minutes, substituting the configured healthy fallback slot.
   - **Estimated saving:** repeated backoff and failed request overhead; likely several minutes per affected run.
   - **Quality risk:** low if panel quorum remains satisfied.

3. **Canary same-head and bot-self-trigger suppression.**
   - **Evidence:** the three editor failures reviewed the same head SHA and repeated large panels. Workflow comments explicitly identify self-trigger verification passes as pure repeat work, while the feature defaults off.
   - **Change:** enable the authenticated `[ai-autofix]` synchronize skip for one week, preserving workflow-dispatch, conflict-resolution, and force-review bypasses.
   - **Estimated saving:** potentially one full panel per successful autofix cycle; collection currently lacks a reliable eligible-run count.
   - **Quality risk:** medium; track regressions caught by the 30-minute recovery sweep.

4. **Reduce oversized review context before lowering model quality.**
   - **Evidence:** run `35665803378` emitted `CONTEXT_BUDGET_WARN` at 167,186/200,000 tokens (83.59%). Editor prompts in the three failures were 464–514KB.
   - **Change:** keep instructions and rubrics in a stable prefix; move volatile run metadata and diffs to the suffix; cap previous-review excerpts and use Semble overflow chunks instead of full files.
   - **Estimated saving:** 10–20% fresh prompt tokens on oversized reviews.
   - **Quality risk:** medium; never truncate cited findings or changed hunks.

5. **Retain Semble; do not add Serena until measured.**
   - Semble averaged roughly 11.6KB per successful query, with only one runtime fallback. This is low overhead relative to 32.9M fresh prompt tokens.
   - Serena had zero queries and probes because it was disabled; no savings claim is supportable.

## Reliability Improvements

1. **Fix and classify stale tracked-surface references.**
   - **Failure evidence:** 27 CI failures; sampled runs repeatedly identify `scripts/helper.sh`.
   - **Root-cause category:** repository inventory/configuration drift.
   - **Fix:** remove or replace stale references in both `agents.md` and `implement.yml`; add:
     `PARITY_FAILURE kind=missing_target source=<file> line=<n> target=<path> changed_in_pr=<bool>`.
   - **Expected impact:** eliminate the dominant failure class.
   - **Rollback:** none required; parity checks remain strict.

2. **Correct editor credential propagation.**
   - **Failure evidence:** three same-head editor failures with an identical missing-key message.
   - **Root-cause category:** secret isolation/plumbing regression.
   - **Fix:** pass the key only into the isolated launcher, not the model-facing environment; exercise this path in a no-network contract test.
   - **Expected impact:** remove 75% of observed review/autofix failures.
   - **Fail-open:** do not fail open into an unisolated editor; fail before reviewers instead.

3. **Make linked-issue metadata generation atomic and diagnosable.**
   - **Evidence:** run `35692710132` successfully built linked-issue context and a 324,491-byte PR diff, then failed because the linked-issue metadata digest could not be published.
   - **Fix:** write metadata to a temporary file, JSON-validate, rename atomically, retry generation once, then log bytes, record count, fallback mode, and digest status.
   - **Expected impact:** remove this early plumbing failure without weakening integrity checks.
   - **Rollback:** retain current fail-closed behavior after retry exhaustion.

4. **Instrument cancel-on-close event correlation.**
   - **Evidence:** `35672590166` closed PR `#4252` but observed no new workflow run.
   - **Fix:** on timeout log workflow enabled state, expected trigger, PR close timestamp/head SHA, pre-run ID, and the latest candidate runs with event/head SHA/status.
   - **Expected impact:** distinguish missing webhook, disabled workflow, filtering error, or indexing delay in one run.
   - **Fail-open:** release remains blocked.

5. **Treat Semble fallback telemetry correctly.**
   - **Evidence:** 61 fallbacks: 60 deliberate `context=contract-test` missing-binary cases and one runtime `budget-exhausted` fallback in `35665803378`.
   - **Assessment:** healthy rare fail-open behavior, not a broken rollout.
   - **Fix:** dashboards must exclude `context=contract-test` from availability alarms.

`BREAK_GLASS` count was zero. The single context-budget warning indicates prompt-size pressure, not rubric/policy override pressure.

## AI Memory Health

- Selected deep dives contained **60** memory telemetry records.
- **Retrieval:** 10/10 retrieves selected records (100% hit rate); average estimated usage was 1,385 of 1,400 tokens (98.9%); `keyword_method=llm` for all ten. No zero-result, disabled, or retrieve fail-open entries were found.
- **Writes:** five `record-run-event` and five `record-candidate` operations failed open. Three successful pushes needed two attempts; 21 needed one.
- **Learning extraction:** 15 `write_lessons_learned` operations all recorded `count=0`. This may be legitimate, but sustained zero yield should be measured.
- **Missing operations:** no `finalize-task`, `promote`, `compact`, or processed-command telemetry appeared in selected logs.

**Recommendation:** add `latency_ms`, `failure_class`, `git_stage`, `branch`, and `push_attempts` to every memory operation. Alert separately on retrieval failure and write fail-open; retrieval health is currently strong, while write reliability is weaker.

## GH API Call Audit

- **Collection gap:** no aggregate GitHub API call counter, endpoint breakdown, retry count, or rate-limit-remaining metric exists. High-confidence total API volume cannot be reported.
- **Positive hygiene:** review gating follows the repository’s §15 pattern by reusing one `/pulls/{n}` response for state, labels, additions/deletions, mergeability, title, body, and head SHA. `/files` is conditional, and Phase 7 parses fetched run JSON locally.
- **Hotspot:** release Phase 7 performs approximately **67 workflow-run list calls**, plus the pre-close lookup and close request, during a ten-minute no-run failure. Use exponential backoff capped at 30 seconds and a shorter registration deadline; expected reduction is 60–70%.
- **Probable per-item loop:** sweep run `35696444342` evaluated two PRs, checking active runs before one skip and one dispatch. **Inference:** active-run discovery is likely repeated per PR. Prefetch workflow runs once and group by head ref, reducing approximately N lookups to one paginated lookup per sweep.
- **Fallback duplication:** several review runs report GraphQL `closingIssuesReferences` empty and then resolve one issue via body text. Cache and reuse that fallback result across metadata, comments, and post-merge paths.
- **Correlation gap:** scheduled cleanup run `35696385215` preserved three active runs without PR linkage. Emit run ID, workflow, head SHA/ref, event, and correlation failure reason.
- No GitHub rate-limit event was observed. The visible rate-limit storm was OpenRouter/Mistral, not GitHub.

Recommended telemetry:

`GH_API_CALL endpoint_class=<...> method=<...> status=<...> attempts=<n> latency_ms=<n> response_bytes=<n> rate_remaining=<n> cache=<hit|miss>`

## Prompt Cache & Memory System

- Collector aggregate `cache_hit_rate` is null, but token totals yield a weighted rate of **77.42%**: 112,764,505 cache-read tokens versus 32,888,714 fresh prompt tokens.
- Failed editor runs still achieved 81.5–83.6% cache hit, demonstrating that caching softened—but did not prevent—46.0M units of wasted work.
- The stable smoke run was a cold start: 175,879 cache-write tokens and 0% hit rate.
- Fragmentation indicators include reviewer slots with zero/unknown cache reads and same-head prompt sizes changing from 464KB to 514KB.
- The single `CONTEXT_BUDGET_WARN` shows prompt growth is beginning to erode available context even with Semble.

**Changes:**
1. Compute weighted aggregate cache hit in `scripts/cost_audit.py`; the current aggregate loop at `scripts/cost_audit.py:840-862` does not produce it.
2. Log prompt-prefix hash and dynamic-suffix bytes per model call.
3. Keep stable system/rubric/memory blocks first; append timestamps, run IDs, diffs, and live API results last.
4. Cap memory retrieval below 95% of budget unless ranking confidence requires more records.

## Orchestrator Health

- Outcome health is strong: 48/49 poll runs succeeded; p50 301s and p95 570s.
- Run `35695640921` processed two tracking issues:
  - Issue `#4255` was stuck in `ai:done` for 235 minutes; no live review run existed, but checkout failed after fetch.
  - Issue `#4260` was stuck for 145 minutes but correctly skipped recovery because a fresh push was found.
- Sweep run `35696444342` dispatched PR `#4259` roughly six minutes after the poller failed to retrigger it, showing eventual recovery but unnecessary delay.
- The same run spent 107s writing memory start/end events.
- 83 workflow-failure-heal runs were skipped; likely expected filtering, but skip reasons were not available in empty archives.

**Smallest safe mitigation:** after confirming no live review run, fall back from failed empty-commit checkout to direct review workflow dispatch. Log fetch refspec, checkout RC, `show-ref` result, and fallback decision.

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Priority fix |
|---|---|---|---|
| Clarify/plan | Mostly expected skips | p50 1s; 155/165 clarify and 143/152 plan skipped | Add structured skip reasons; no speed work |
| Implement | Model execution/failure | Successful p50 858s; three failures at `Run Codex implementation` | Emit implementation failure class and last heartbeat |
| Review/autofix | Queueing plus multi-model compute | p50 1,005s; p95 4,504s; 14 cancellations | Same-head concurrency and provider circuit breaker |
| CI | Late deterministic failure | 27/35 failed; checks run near suite end | Move parity checks first |
| Orchestrate | Memory pushes and checkout | `35695640921`: 107s memory writes, 50s checkout | Batch writes and reuse checkout |
| Validate/release | Event polling and missing workflow correlation | `35672590166` ran 6,576s, then `no_run` | Split registration/execution timeout |
| Merge/conflict | Recovery checkout failure | Issue `#4255` could not checkout after fetch | Direct-dispatch fallback |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix consumed 49.1 of 79.2 total wall-hours; CI consumed 13.7 hours; pollers consumed 4.5 hours.
- **Top failure modes:** 23 inventory parity failures, four script-reference failures, three editor-auth failures, three implementation failures, and one release webhook-correlation failure.
- **Highest cost driver:** review/autofix generated 146.8M of 147.0M OpenRouter token units.
- **Top actions:**
  1. Fix `scripts/helper.sh` references and move both deterministic checks to the start of CI.
  2. Add editor isolation preflight before reviewers.
  3. Deduplicate same-head review runs and enable provider circuit-breaking.

## Metrics Appendix

### Run outcomes

| Scope | Runs | Success | Failure | Cancelled | Skipped | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Overall | 1,000 | 270 | 37 | 17 | 676 | 2s | 1,937s |
| CI | 35 | 8 | 27 | 0 | 0 | 1,033s | 2,525s |
| Review/autofix | 97 | 77 | 4 | 14 | 2 | 1,005s | 4,504s |
| Orchestrate poll | 49 | 48 | 1 | 0 | 0 | 301s | 570s |
| Implement | 152 | 8 | 3 | 3 | 138 | 1s | 789s |

Executed, non-skipped runs were 83.3% successful, 11.4% failed, and 5.2% cancelled.

### Token and cache metrics

| Metric | Value |
|---|---:|
| OpenRouter calls | 213 |
| Usage available / unavailable | 200 / 13 |
| Fresh prompt tokens | 32,888,714 |
| Completion tokens | 1,368,834 |
| Cache-read tokens | 112,764,505 |
| Cache-write tokens | 175,879 |
| Total token units | 147,016,176 |
| Collector `cache_hit_rate` | null |
| Derived weighted cache-hit rate | 77.42% |
| `wall_clock_p50_ms` | 10,500 |
| `wall_clock_p99_ms` | 6,522,180 |
| Wall-clock samples | 124 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 1 |

### MCP telemetry

| System | Queries | Logged bytes | Fallbacks | Notes |
|---|---:|---:|---:|---|
| Semble | 28 | 325,053 | 61 | 60 contract-test; one runtime budget fallback |
| Serena | 0 | 0 | 0 | Disabled; no tool calls or probes |
| Other MCP servers observed | 0 | — | — | None |

| MCP target | Probe OK | Probe failed | Probe skipped |
|---|---:|---:|---:|
| Serena — no target emitted | 0 | 0 | 0 |

### GH API summary

| Signal | Value |
|---|---:|
| Aggregate call count | Not collected |
| Rate-limit events observed | 0 |
| Phase 7 estimated calls in failed smoke | ~69 |
| Retry/latency/response-byte coverage | Not collected |

## Deep Audit — Workflows & Scripts (2026-09-22)

### Section 1: Bug & Correctness Sweep

#### BUG-001 — Required standalone validation can be silently skipped

- **File:** `.github/workflows/review_autofix.yml:1126-1250`
- **Severity:** High
- **Category:** `bug`
- **Description:** Post-merge validation dispatch uses raw `gh workflow run`. If both the configured and fallback workflows fail, the step only warns and exits successfully. Repository-wide references show no scheduled recovery consumer for the retained `ai:orchestrator-validate-required` label, so a transient dispatch failure can permanently omit required validation.
- **Recommended fix:** Stage and use `scripts/dispatch_and_watch_workflow_run.sh` in registration-only mode. Fail the job if neither workflow registers, and remove the label through `gh_retry` only after confirmed registration.

All workflow YAML and Python files parsed successfully, and every shell script passed `bash -n`. No direct secret-value logging or unsafe issue/PR-body interpolation was found. Previously reported stale-helper, CI-order, and editor-preflight incidents are not duplicated here.

### Section 2: GitHub API Call Redundancy Audit

#### API-001 — Same resources are fetched twice for adjacent fields

- **File:** `scripts/orchestrate_poll_process.sh:10230-10242`, `scripts/orchestrate_poll_process.sh:13731-13733`, `scripts/orchestrate_poll_process.sh:16413-16421`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** Three paths issue two consecutive GETs for one PR or issue, separately extracting state/merged status or title/body.
- **Current calls:** 2 per path; up to 6 across the three paths.
- **Proposed calls:** 1 per path; up to 3 total.
- **Recommended fix:** Fetch one JSON object and parse all required fields locally, following the existing `final_pr_json_snapshot` and `_candidate_details_json` cache patterns.

#### BATCH-001 — Standalone recovery lists issues once per phase label

- **File:** `scripts/orchestrate_poll_process.sh:15504-15510`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** The loop executes seven `gh issue list --limit 1000` calls every standalone-recovery sweep. Pagination can increase this to approximately 70 underlying requests.
- **Current calls:** 7 logical calls per sweep.
- **Proposed calls:** 1 initial aliased GraphQL call, with pagination only for overflowing aliases.
- **Recommended fix:** Add `_fetch_phase_issue_numbers_graphql(labels...)`, following `_fetch_candidate_issue_details_graphql`, and locally union the seven alias results.

#### BATCH-002 — Promote-cycle baseline lookup fetches comments per search result

- **File:** `scripts/promote_main_cycle.sh:237-265`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** `last_cycle_baseline_sha` searches for up to ten issues, then fetches each issue’s comments individually.
- **Current calls:** Up to 11: one search plus ten comment requests.
- **Proposed calls:** 2: one search plus one aliased GraphQL comments query.
- **Recommended fix:** Batch candidate issues with `comments(last:100)` aliases, extending the batching pattern used by `_fetch_candidate_issue_details_graphql`.

#### BATCH-003 — Body-fallback linked issues trigger per-issue label reads

- **File:** `.github/workflows/review_autofix.yml:1206-1247`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** When linked issues come from PR text rather than GraphQL, each issue causes a separate `gh issue view` label request.
- **Current calls:** N label reads for N fallback issues.
- **Proposed calls:** `ceil(N/25)`; normally one.
- **Recommended fix:** Move the generic `_fetch_issue_labels_batch_graphql(numbers_json)` pattern from `orchestrate_poll_process.sh:2972-3049` into `gh_helpers.sh` and reuse it here. Mutation calls remain per issue.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Release publication logic is duplicated byte-for-byte

- **File:** `.github/workflows/mark-stable.yml:678-892`, `.github/workflows/test-and-mark-stable.yml:5415-5629`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** The 6,306-character tag-publication block and 2,345-character release-creation block are exact duplicates in both release workflows. Security or recovery fixes can drift between release paths.
- **Recommended fix:** Create `scripts/release_publish_helpers.sh` containing:
  - `release_publish_tags <version> <source_branch> <tested_sha>`
  - `release_create_github_release <repository> <version> <notes_file> <source_branch>`
  
  Update both workflows to call these functions.

#### DUP-002 — Review runtime helpers have three exact duplicate families

- **File:** `scripts/review_apply_fixes.sh:164-202,909-918`, `scripts/review_rb_judge.sh:168-182,256-294`, `scripts/review_run_reviewers.sh:69-107,324-338,1760-1769`, `scripts/review_conflict_resolve.sh:255-269`, `scripts/review_conflict_prepare.sh:605-614`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Exact copies exist for context-budget warning emission, stall-state parsing, and bounded Semble-section rendering.
- **Recommended fix:** Add `scripts/review_runtime_helpers.sh` with:
  - `emit_context_budget_warn_for_prompt <phase> <prompt_path> <model>`
  - `read_codex_stall_guard_state <status_file>`
  - `append_semble_query_section <label> <path> [max_bytes]`
  
  Source it from the five callers above.

No workflow pair was found to be more than 70% structurally identical overall.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Implement support-staging block exceeds the medium-risk threshold

- **File:** `.github/workflows/implement.yml:979-1337`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** The interpolated `Stage workflow support files` run block is approximately **16,985 characters**, contains three `${{ }}` expressions, and has only **4,015 characters** of headroom before GitHub’s 21,000-character limit.
- **Recommended fix:** Extend `scripts/stage_workflow_support.sh` with an implement/in-tree ledger mode and replace the inline block with a short invocation. Pass repository variables through `env:` rather than interpolating them into `run:`.

No block exceeds 18,000 characters. No workflow exceeds 800 KB; the largest is `review_autofix.yml` at 505,283 bytes.

### Section 5: Cross-Cutting Concerns

#### CONSIST-001 — Review thread-reuse flag contradicts its documented contract

- **File:** `scripts/review_apply_fixes.sh:1598-1606`, `scripts/review_conflict_resolve.sh:1659-1671`
- **Severity:** Medium
- **Category:** `consistency`
- **Description:** `README.md:129` says `CODEX_THREAD_REUSE_ENABLED` enables reuse for `review_autofix`, but both OpenCode editor paths explicitly ignore it and use fresh full prompts. Their thread-reuse resolution functions are consequently unreachable.
- **Recommended fix:** Update the documented “Used By” contract to `implement, validate`, explicitly document that OpenCode review phases ignore the flag, and retain the existing function names as compatibility shims under §6.

#### DEAD-001 — Three poller functions have no callers

- **File:** `scripts/orchestrate_poll_process.sh:11348-11356`, `scripts/orchestrate_poll_process.sh:12958-12977`, `scripts/orchestrate_poll_process.sh:13087-13097`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `get_last_validation_run_conclusion`, `read_standalone_state_json`, and `stall_recovery_action_is_terminal` occur only at their definitions across the repository.
- **Recommended fix:** Remove them in an explicitly approved §6 cleanup after confirming no external sourcing contract; otherwise annotate them as compatibility-reserved and add a contract test establishing that status.

No `TODO`, `FIXME`, `HACK`, or standalone `XXX` markers were found. Reviewed ShellCheck warnings were intentional dynamic exports, glob matching, or documented compatibility no-ops; no separate high-confidence shellcheck finding remains.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 6 | API-001, BATCH-001, BATCH-002, DUP-001, EXPR-001, CONSIST-001 |
| Low | 3 | BATCH-003, DUP-002, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2–3 | Medium |
| API call optimization | 3–5 | Medium |
| Code modularization | 8–10 | Large |
| Expression size reduction | 2 | Medium |
| Medium/Low fixes | 3–5 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-09-22)

### Safety Tag Legend

`SAFE_TO_MERGE` is directly actionable; `NEEDS_VERIFICATION` requires the specified checks; `RISKY_SKIP` must not be automated because polling, pagination, orchestrator-race, or error-semantics risks apply.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — Batch plan-archival issue metadata lookups

- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:** `scripts/lint_plan_archival_completeness.py:85-107`, `scripts/lint_plan_archival_completeness.py:172-219`
- **Current call count:** N `gh issue view` calls for N referenced issues.
- **Proposed call count:** `ceil(N/25)` GraphQL calls.
- **Endpoint(s):** GraphQL `repository.issue(number)` fetching `body` and `labels`.
- **Evidence:**
  ```python
  cmd = [
      "gh", "issue", "view", str(issue_number),
      "--repo", repo,
      "--json", "labels,body",
  ]
  ```
  ```python
  for issue_num in referenced_issues:
      issue = issue_fetcher(repo, issue_num)
  ```
  `_extract_referenced_issues` already deduplicates issue numbers, but each distinct issue still incurs a separate lookup.
- **Proposed fix:** Add a default batched fetch path in `lint()` using aliased GraphQL groups of 25, following `_fetch_candidate_issue_details_graphql`. Request `body`, `labels(first:100)`, and label `pageInfo`; retain `_fetch_issue_via_gh` as the per-unresolved-issue fallback and injectable test interface.
- **Safety rationale:** The calls occur in one function without mutations, but batching changes per-item failure handling and label-pagination behavior, so strict error-semantic equivalence is not yet proven.
- **Downstream signal:** Verify partial GraphQL errors, issues with over 100 labels, inaccessible issues, and batch failure all produce the same `lookup_errors` and exit codes as the current per-item implementation.

#### MERGE-002 — Batch auto-close lint label lookups by repository

- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:** `scripts/lint_pr_body_auto_close.py:128-163`, `scripts/lint_pr_body_auto_close.py:233-267`
- **Current call count:** U calls for U unique `(repository, issue)` pairs.
- **Proposed call count:** `sum(ceil(U_repo/25))` GraphQL calls.
- **Endpoint(s):** GraphQL `repository.issue(number)` fetching labels.
- **Evidence:**
  ```python
  issue_label_cache: dict[tuple[str, int], list[str]] = {}
  ...
  labels = label_lookup(lookup_repo, issue)
  ```
  The cache removes repeated references to the same issue, but distinct issues are still fetched serially.
- **Proposed fix:** Group `candidate_matches` by repository, batch issue aliases in groups of 25, and populate `issue_label_cache` before evaluating matches. Follow `_fetch_candidate_issue_details_graphql`; retain `_fetch_issue_labels_gh` for unresolved aliases.
- **Safety rationale:** Cross-repository batching changes GraphQL authorization and partial-error handling even though there is no intervening mutation.
- **Downstream signal:** Test mixed same-repo/cross-repo references, inaccessible repositories, partial alias errors, over-100-label issues, and preservation of fail-open versus exit-2 behavior.

#### MERGE-003 — Extend BATCH-002’s trusted-comment batching to analysis dispatch

- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:** `scripts/apply_analysis_on_main.sh:167-188`, `scripts/apply_analysis_on_main.sh:196-218`
- **Current call count:** `1 + N`, up to 21 calls per document: one search plus up to 20 issue-comment reads.
- **Proposed call count:** 2 calls per document.
- **Endpoint(s):** REST `GET /search/issues`; currently REST `GET /repos/{repo}/issues/{number}/comments`, proposed aliased GraphQL `repository.issue(number).comments(first:100)`.
- **Evidence:**
  ```bash
  numbers="$(gh_retry gh api -X GET search/issues ... -f per_page=20 ...)"
  ...
  trusted_marker_comment_present "${issue_number}" "${marker_line}"
  ```
  `trusted_marker_comment_present` then reads each candidate’s first 100 comments separately.
- **Proposed fix:** Reuse the batching helper planned by Deep Audit `BATCH-002`, requesting comment body, author login, and `authorAssociation`; process aliases in original search order and retain per-issue REST fallback for unresolved aliases.
- **Safety rationale:** The calls share one function and have no mutation boundary, but switching comment transport must preserve trusted-author, ordering, and lookup-failure semantics.
- **Downstream signal:** Confirm `comments(first:100)` matches the current REST window and that any batch/alias failure returns the same `doc_dispatched_before` status `2` unless the legacy fallback succeeds.

#### MERGE-004 — Extend API-001 to the implementation-failed reissue path

- **Safety tag:** `RISKY_SKIP`
- **File:** `scripts/orchestrate_poll_process.sh:21397-21400`
- **Current call count:** 2 calls per affected reissue.
- **Proposed call count:** 1 successful-path call.
- **Endpoint(s):** REST `GET /repos/{repo}/issues/{issue_number}`.
- **Evidence:**
  ```bash
  IF_TITLE="$(gh_retry _safe_gh_jq ".../issues/${if_issue}" --jq '.title' || echo "")"
  IF_BODY="$(gh_retry _safe_gh_jq ".../issues/${if_issue}" --jq '.body' || echo "")"
  ```
- **Proposed fix:** Fetch one `IF_META_JSON` object and parse `.title` and `.body` locally; retain the two original field reads only as a failed-full-fetch fallback if partial-success behavior must remain unchanged.
- **Safety rationale:** This is inside `orchestrate_poll_process.sh` and an issue-processing loop, an explicit `RISKY_SKIP` trigger; one combined failure would also couple two currently independent fallbacks.
- **Downstream signal:** Do not auto-implement; manually validate reissue race behavior, partial API failures, cycle-local cache expectations, and unchanged recovery logs before consolidation.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — Persist the fresh failure-path PR payload for the next step

- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:** `.github/workflows/review_autofix.yml:7265-7283`, `.github/workflows/review_autofix.yml:7285-7334`
- **Current call count:** 2 calls when `PR_META_FILE` lacks usable title/body.
- **Proposed call count:** 1 on a successful first fetch; retain the second only after first-fetch failure.
- **Endpoint(s):** REST `GET /repos/{repo}/pulls/{pr_number}`.
- **Evidence:**
  ```bash
  pr_meta="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" ...)"
  ```
  The immediately following step can then re-fetch:
  ```bash
  PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" \
    --jq '.title + " " + (.body // "")' ...)"
  ```
- **Proposed fix:** After validating `pr_meta`, atomically refresh the existing `${PR_META_FILE}`. The following step already reads that file before invoking its API fallback, so no new cache contract is required.
- **Safety rationale:** The endpoint, authentication, and payload overlap exactly, but the reuse crosses workflow steps rather than remaining within one step.
- **Downstream signal:** Verify both steps share the same workspace and conditions, no mutation can occur between them, first-fetch failure preserves the existing fallback, and all failure-alert log keys remain unchanged.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: RISKY_SKIP — Correct overlap, but every cited call is inside `orchestrate_poll_process.sh`, requiring manual race and fallback review.
- BATCH-001: RISKY_SKIP — The path is orchestrator recovery code and performs pagination, both mandatory skip triggers.
- BATCH-002: NEEDS_VERIFICATION — Validate trusted-author fields, comment-window ordering, partial GraphQL errors, and REST fallback parity.
- BATCH-003: NEEDS_VERIFICATION — Confirm complete label retrieval, GraphQL authorization parity, and unchanged per-issue mutation behavior.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 6 | MERGE-001, MERGE-002, MERGE-003, REUSE-001, BATCH-002, BATCH-003 |
| RISKY_SKIP | 3 | MERGE-004, API-001, BATCH-001 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
