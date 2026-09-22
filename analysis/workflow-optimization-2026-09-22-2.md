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
