## Executive Summary

- **`review_autofix` is the dominant latency hotspot because it serially waits on sibling check-runs for 7–8 minutes per slow run before doing useful work.** In failed runs `25115530167` and `25127054791`, the resolver step logged 21–24 wait cycles at a 20s poll interval, adding roughly 420–480s of pure waiting inside runs that lasted 1,508s and 1,614s. **Estimated impact:** 5–8 min faster per affected review run. **Confidence:** high.
- **The pipeline is over-triggering and then skipping/canceling downstream workflows.** `review_autofix` had **93 cancelled runs out of 140**, while `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` each show very high “other/skipped” counts with p50 duration of 1s. This indicates dispatch fanout is happening before gate decisions are finalized. **Estimated impact:** major compute/API reduction, moderate end-to-end improvement from less queue contention. **Confidence:** high.
- **AI-generated failures are still escaping into full CI, where they fail late.** CI failures `25144143440` and `25144388441` each consumed about **10.5 minutes** before `ruff` failed on six `E101` indentation errors in `scripts/mcp_handshake_probe.py`. **Estimated impact:** save ~10 min on bad PRs by linting touched files before PR push or before opening review. **Confidence:** high.
- **Implement/orchestrator reliability is being hurt by duplicate runs and integration-state conflicts.** In the E2E smoke log for run `25126757724`, issue `#1783` reached **5 implement runs total / 4 active** at one point. Separately, sampled implement failure `25143249766` ended on integration fingerprint regressions after a full run. **Estimated impact:** lower rerun/failure rate and less queue churn. **Confidence:** medium-high.
- **AI memory is enabled but low-yield in the sampled window.** Across observed `AI_MEMORY_TELEMETRY`, retrieval hit rate was **41.2%** (7/17), with **10/17 retrieves returning 0 records** and average selected-context size only **11.5 estimated tokens**. The system is healthy operationally (`fail_open: true` not seen; `enabled: false` not seen), but effectiveness is weak. **Estimated impact:** modest token/quality improvement if tuned. **Confidence:** high.
- **Prompt-cache/token telemetry is materially under-instrumented.** `OPENROUTER_PROMPT_CACHE_DISABLED=false` is present, but the sampled logs do not emit usable cache-hit/miss or token totals. Repeated large issue/context blocks in implement/review logs suggest avoidable prompt duplication, but savings cannot be quantified precisely from this window. **Estimated impact:** medium cost reduction once instrumented and stabilized. **Confidence:** medium.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Shorten or bypass `review_autofix` check-run waiting on the critical path
- **Evidence:** Failed review runs `25115530167` and `25127054791` both logged repeated lines of `Waiting for 1 in-progress/queued check-run(s)...` every 20s. Run `25115530167` shows 21 such waits; `25127054791` shows 24. Slow success `25143964823` also spent 24 wait cycles before continuing.
- **Root cause:** `review_autofix` blocks on sibling check-runs for the same SHA before collecting PR failure context, even when that wait does not change the final resolution outcome.
- **Exact change:**  
  - Reduce `CHECK_RUNS_WAIT_TIMEOUT_SECS` from `1200` to `180–300` for review runs.  
  - Add an early-exit condition: if only one known long-running peer remains, snapshot current failures and continue.  
  - Keep fail-open behavior by annotating the snapshot as partial instead of failing the run.
- **Estimated time savings:** ~5–8 minutes per affected slow review run.
- **Implementation risk:** low-medium. Main risk is acting on incomplete CI context; mitigate by tagging the collected snapshot as partial.

### 2. Stop dispatching workflows that are likely to skip immediately
- **Evidence:** Workflow-family aggregates show large “other/skipped” volumes: `clarify` 166/180, `plan` 148/160, `implement` 137/161, `orchestrate_clarify_respond` 158/161. Recent runs around `03:46–04:22 UTC` show repeated 1s skipped runs across all four families.
- **Root cause:** orchestration appears to fan out multiple downstream workflows before final gating state is known, so many runs start only to self-skip.
- **Exact change:**  
  - Move gate evaluation into a single router/dispatcher step before workflow dispatch.  
  - Only dispatch `clarify`, `plan`, `implement`, or `orchestrate_clarify_respond` when the repo/issue state truly requires it.  
  - Preserve current behavior as fallback by keeping in-workflow guards for safety.
- **Estimated time savings:** indirect but meaningful; less queue pressure and less runner churn, especially during active orchestrator periods.
- **Implementation risk:** medium. Requires careful preservation of current routing semantics.

### 3. Preflight lint touched files before AI-generated changes reach full CI
- **Evidence:** CI failures `25144143440` and `25144388441` both ran ~614–631s before failing in `Python lint (ruff)`. The failure was six `E101` mixed tabs/spaces errors in `scripts/mcp_handshake_probe.py:125-130`.
- **Root cause:** style regressions are discovered only in the normal CI lane, not during implement/review generation.
- **Exact change:**  
  - In `implement` and `review_autofix`, run `ruff check` on changed Python files before commit/push.  
  - If that passes, allow the normal CI lane to proceed unchanged.
- **Estimated time savings:** ~10 minutes on every AI-generated PR that would otherwise fail only in CI.
- **Implementation risk:** low.

### 4. Reduce duplicate implement fanout during approval-to-implementation handoff
- **Evidence:** In E2E smoke run `25126757724`, the implementation watcher logged growth from 0 to 5 implement workflow runs, with 4 still active at one point for issue `#1783`.
- **Root cause:** approval-triggered and label/state-triggered workflows overlap before idempotency settles.
- **Exact change:**  
  - Strengthen idempotency before dispatch, not just inside the implement workflow.  
  - Cache and check “implement already active for issue N” at dispatch time.  
  - Collapse repeated approval-triggered starts into one canonical run.
- **Estimated time savings:** 1–5 minutes per noisy issue cycle, plus lower queue contention.
- **Implementation risk:** medium.

### 5. Make release-smoke failures fail faster
- **Evidence:** `test_and_mark_stable` failed in `25126757724` after **4,270s** and in `25115169454` after **3,677s**. One failure was in `Phase 4b: Verify editor removed bait line`; another failed after orchestrate decomposition produced only one child issue.
- **Root cause:** long serial smoke phases allow failure discovery very late in the run.
- **Exact change:**  
  - Split smoke phases into fail-fast subjobs.  
  - End the workflow as soon as a release-blocking phase fails instead of continuing unrelated supplemental steps.  
  - Keep supplemental diagnostics as `if: failure()` follow-ups.
- **Estimated time savings:** 10–20 minutes on failing stable-test runs.
- **Implementation risk:** low-medium.

**Critical-path wins:** items 1, 3, 5.  
**Local micro-optimizations:** items 2, 4.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

### 1. Lower default reasoning level for non-escalated implement/review paths
- **Evidence:** Sampled implement env for run `25143404687` used `MODEL_EDITOR: openai/gpt-5.3-codex` with `MODEL_REASONING_EFFORT: xhigh`. Recent review runs also show `REVIEWER_REASONING_EFFORT: xhigh`, `EDITOR_REASONING_EFFORT: xhigh`, and a six-model reviewer slate.
- **Root cause:** high reasoning is being used as the default, not only for hard conflict-resolution or integration-sync cases.
- **Exact change:**  
  - Default implement/review to `medium` or `high`.  
  - Escalate to `xhigh` only when merge conflict, fingerprint regression, or retry budget >0 is detected.
- **Estimated savings:** likely 20–40% token cost on the long-tail AI runs, plus lower latency.
- **Quality-risk notes:** medium. Keep `xhigh` on explicit escalation paths.

### 2. Eliminate repeated prompt/context expansion across retries
- **Evidence:** In implement failure `25143404687`, the same long issue context and remediation text is repeated many times through the run log. Review failure logs also repeat long AI memory telemetry explanations and workflow/docs content multiple times.
- **Root cause:** immutable context appears to be re-inserted on each diagnose/repair/retry pass rather than referenced once.
- **Exact change:**  
  - Build one immutable “static context” artifact per run.  
  - Pass only deltas, failure summaries, and changed-file lists into retry prompts.  
  - Deduplicate repeated issue-body and telemetry-doc blocks before model invocation.
- **Estimated savings:** likely 15–25% token reduction on multi-pass implement/review runs.
- **Quality-risk notes:** low if immutable context remains available by reference.

### 3. Reduce cancelled review runs before expensive AI work starts
- **Evidence:** `review_autofix` has **93 cancelled runs out of 140 total**. Recent cancelled runs include `25146793200` (92s), `25146341117` (520s), `25146291654` (437s), `25145539874` (334s), and older samples above 900s.
- **Root cause:** duplicated or superseded review runs are allowed to begin substantial work before being cancelled.
- **Exact change:**  
  - Move peer-detection/dedup before heavy setup and model startup.  
  - If a newer review run exists for the same PR/SHA, exit before reviewer/editor phases.
- **Estimated savings:** large compute and token savings; exact dollar amount unavailable from current telemetry.
- **Quality-risk notes:** low if the newest run remains authoritative.

### 4. Make reviewer fanout adaptive instead of fixed
- **Evidence:** Slow review runs routinely ended with `REVIEWERS_SUCCESSFUL: 6`, while some recent successful review runs completed in 17–22s because they skipped expensive review paths entirely.
- **Root cause:** the expensive multi-reviewer path appears to be binary rather than risk-based.
- **Exact change:**  
  - Use diff size, changed-file types, and CI-failure ambiguity to decide reviewer count.  
  - Example: 2 reviewers for small/localized changes, 4 for mixed code+workflow changes, 6 only for integration or workflow edits.
- **Estimated savings:** potentially high on review volume.
- **Quality-risk notes:** medium. Keep full fanout on workflow/integration changes.

### 5. Stop spending AI cycles on runs that will fail due to deterministic repository state
- **Evidence:** Implement failure `25143249766` ended on deterministic integration fingerprint regressions; CI failures `25144143440`/`25144388441` were deterministic style errors.
- **Root cause:** AI/editor stages are running before cheap deterministic validators have ruled out bad states.
- **Exact change:**  
  - Run integration-fingerprint preflight and touched-file lint preflight before launching expensive model steps where possible.
- **Estimated savings:** medium.
- **Quality-risk notes:** low.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Add pre-commit `ruff` and formatting guards inside AI workflows
- **Failure evidence:** CI runs `25144143440` and `25144388441` failed at `Python lint (ruff)` after ~10 minutes because `scripts/mcp_handshake_probe.py` contained six `E101` mixed-indentation errors.
- **Root cause category:** deterministic code-quality regression escaping generator/editor stages.
- **Exact fix:** run `ruff check` on changed Python files before commit in `implement` and `review_autofix`.
- **Expected reliability impact:** should eliminate this failure class from downstream CI for AI-authored changes.
- **Rollback/fail-open considerations:** safe to fail closed in AI workflows; downstream CI remains unchanged.

### 2. Detect duplicate implement starts before dispatch, not after workflow start
- **Failure evidence:** E2E smoke run `25126757724` observed up to 5 implement runs for the same issue lifecycle; implement family also shows many 1s skipped runs and a 4.97% failure rate.
- **Root cause category:** orchestration/idempotency race.
- **Exact fix:** atomically check “active implement exists for issue N” before dispatching implement, using one cycle-local state check.
- **Expected reliability impact:** fewer duplicate runs, fewer racey label transitions, fewer skip storms.
- **Rollback/fail-open considerations:** fail open by allowing the current behavior if the pre-dispatch check errors.

### 3. Fail fast on impossible integration-state merges
- **Failure evidence:** Implement run `25143249766` failed with integration fingerprint violations, including reverted `must_contain`/`must_not_contain` patterns in `.github/workflows/implement.yml` and `scripts/foo.sh`.
- **Root cause category:** integration conflict / orchestrator state conflict.
- **Exact fix:** run fingerprint verification as a preflight before commit-creation or expensive repair loops; escalate directly to orchestrator judge when regressions are already present.
- **Expected reliability impact:** lower implement failure rate and cleaner orchestrator handoff.
- **Rollback/fail-open considerations:** fail open only on parser/read errors, not on real fingerprint violations.

### 4. Tighten review conflict-resolution no-progress detection
- **Failure evidence:** Review failures `25115530167` and `25127054791` ended with `MERGE_CONFLICT=true` and `CONFLICT_RESOLVED=false` after long resolver runs.
- **Root cause category:** stuck merge/conflict resolution loop.
- **Exact fix:** stop after unchanged conflict fingerprints or unchanged violation count across attempts, and dispatch the orchestrator judge immediately.
- **Expected reliability impact:** fewer long failing review runs and faster recovery path.
- **Rollback/fail-open considerations:** low risk if escalation preserves current manual/orchestrator fallback.

### 5. Stabilize orchestrator decomposition assertions
- **Failure evidence:** `test_and_mark_stable` run `25115169454` failed because decomposition produced **1 child issue** instead of expected `>= 2`; a later run `25126757724` produced 2 child issues and passed the same assertion.
- **Root cause category:** nondeterministic orchestrator decomposition.
- **Exact fix:** assert on required outcome shape more defensively: minimum child count should depend on parsed project structure, and test fixtures should encode the expected decomposition cardinality explicitly.
- **Expected reliability impact:** lower false-negative release-smoke failures.
- **Rollback/fail-open considerations:** keep current strict assertion in a dedicated regression fixture if needed.

## AI Memory Health

Observed `AI_MEMORY_TELEMETRY` was present in the sampled logs.

- **Retrieve hit rate:** **41.2%** (7/17 retrieves selected at least one record).
- **Average `estimated_tokens`:** **11.5**.
- **`keyword_method` distribution:**  
  - `none`: **10**  
  - `plain`: **7**  
  - `llm`: **0 observed**
- **Zero-record retrieves:** **10/17**.
- **`fail_open: true` entries:** **0 observed**.
- **`enabled: false` entries:** **0 observed**.
- **High push retry counts:** **1 event** with `push_attempts > 1` (a `record-run-event` in review failure `25127054791` used 2 attempts).

Assessment:
- Operationally, memory is healthy: no silent fail-open entries and no disabled state in the sample.
- Effectiveness is weak: most retrievals returned nothing, and successful retrievals were tiny.
- Reviewer memory seems especially underpowered: sampled review failures `25115530167` and `25127054791` both showed `retrieve` with `records_selected: 0` and `keyword_method: none`.

Recommendation:
- Improve retrieval keywording for reviewer flows first. The current mix suggests the system often skips keyword extraction altogether.
- Track hit rate separately by workflow family (`implement` vs `review_autofix`) because the sampled `implement` retrieves were more successful than reviewer retrieves.

## GH API Call Audit

**Important caveat:** the sampled logs do not contain a clean executed-call counter, so the numbers below are based on log-observed command mentions and repeated polling behavior. They are good for hotspot identification, not exact billing.

### High-volume patterns

1. **`review_autofix` check-run polling**
   - **Evidence:** Runs `25115530167`, `25127054791`, and `25143964823` repeatedly poll commit check-runs every 20s for 7–8 minutes.
   - **Pattern:** repeated `GET /repos/{repo}/commits/{sha}/check-runs?per_page=100`.
   - **Issue:** high redundancy with low information gain once the remaining in-flight checks are known.
   - **Recommendation:** switch to capped exponential backoff or stop once only one long-running sibling remains.
   - **Estimated reduction:** ~10–20 API calls per slow review run.
   - **Rate-limit risk reduction:** medium.

2. **Per-run issue/PR metadata refetching in `implement`**
   - **Evidence:** Sampled implement failures `25143404687` and `25143249766` are the highest-volume API hotspots in the sample by log mention count.
   - **Pattern:** repeated issue metadata, label, and workflow-dispatch checks within one run.
   - **Issue:** metadata appears to be fetched more than once instead of reused cycle-locally.
   - **Recommendation:** fetch issue metadata and labels once into JSON, reuse across all implement substeps.
   - **Estimated reduction:** several API calls per implement run.
   - **Rate-limit risk reduction:** low-medium.

3. **Per-issue validation dispatch checks after merge**
   - **Evidence:** Recent review run `25147630820` calls GraphQL for linked issues, then still falls back to per-issue `gh issue view` and `gh issue edit`.
   - **Pattern:** per-item lookups in a loop.
   - **Issue:** partially redundant because labels are already available for some issue nodes.
   - **Recommendation:** ensure the initial GraphQL query always includes the needed label fields so the per-issue `gh issue view` fallback is rarely or never used.
   - **Estimated reduction:** 1–2 calls per linked issue.
   - **Rate-limit risk reduction:** low.

4. **E2E/test poll loops**
   - **Evidence:** `test_and_mark_stable` orchestrate-decompose and smoke phases poll every 10–20s for long windows.
   - **Pattern:** status polling loops on workflow runs and issue state.
   - **Issue:** acceptable for smoke tests, but still expensive and noisy.
   - **Recommendation:** after the first minute, back off to 30–60s intervals for long-lived steps.
   - **Estimated reduction:** moderate on long release-smoke runs.
   - **Rate-limit risk reduction:** medium.

### API hygiene conclusions

- **Batching:** some batching exists (`--paginate --slurp`, GraphQL issue-node fetches), but not consistently.
- **Cycle-local caches:** weak in implement/review orchestration paths.
- **Fail-open behavior:** generally good; many API-dependent steps warn and continue instead of hard-failing. Keep that pattern.

## MCP & Serena Efficiency

### What the sampled logs show

- In review failures `25115530167` and `25127054791`, **Context7 MCP** and **Git MCP** were explicitly skipped:
  - `Context7 MCP setup skipped (CONTEXT7_DISABLED=true).`
  - `Git MCP setup skipped (GIT_MCP_DISABLED=true).`
- In implement failure `25143404687`, Serena was configured as optional:
  - `Serena MCP config hardened: startup_timeout=60s, required=false (graceful fallback on failure).`
- Many runs emitted a `SERENA_REPORT_FILE` path, but the report artifact contents were not included in the sampled window.

### Efficiency assessment

- The system already has a **good fail-open posture** for Serena.
- The **main inefficiency is not Serena failure**; it is that Git MCP/Context7 were disabled in sampled review runs, forcing broader shell/GitHub API reads instead of targeted symbol/diff access.
- Because the actual Serena efficiency reports were not present, I cannot verify broad-read vs symbol-targeted usage rates directly.

### Recommendations

1. **Enable Git MCP for review/autofix on the paths where diff-heavy inspection happens**
   - Expected benefit: lower prompt size and faster context gathering.
   - Risk: low if kept optional with current fallback.

2. **Persist the Serena efficiency report as a surfaced artifact in workflow-log collection**
   - Expected benefit: lets future analyses measure repeated broad reads, symbol lookup hit rates, and redundant file-region access.
   - Risk: low.

3. **Do one symbol/diff discovery pass, then reuse it**
   - Current logs suggest context is rebuilt repeatedly.
   - Create one per-run symbol/diff summary and feed that into diagnose/repair/review steps.

4. **Parallelize independent metadata reads**
   - PR metadata, linked issues, and current check-run snapshot can be fetched concurrently before model invocation.
   - Safe because they are read-only and branch-local.

## Prompt Cache & Memory System

### Observed behavior

- `OPENROUTER_PROMPT_CACHE_DISABLED=false` is present in sampled implement/review environments.
- Some logs contain comments indicating a later step should become a “no-op cache read.”
- **But** the sampled window does **not** expose usable cache hit/miss counters, cache creation counts, or token totals.

### Assessment

- Prompt caching is probably enabled, but **not measurable** from current telemetry.
- Prompt structure appears unstable:
  - long issue bodies and remediation notes are repeated across retries,
  - review logs repeat lengthy memory/telemetry explanatory blocks.
- That kind of variance and duplication usually fragments prompt-cache reuse even when caching is enabled.

### Recommendations

1. **Emit explicit cache metrics per model call**
   - Log: cache create tokens, cache read tokens, cache hit/miss boolean, prompt/completion/total tokens.
   - Impact: unlocks real cost analysis and cache tuning.

2. **Stabilize prompt prefixes**
   - Put fixed instructions first.
   - Move dynamic noise (timestamps, retry counters, transient diagnostics) to the suffix or separate attachment files.
   - Impact: likely better cache reuse and lower latency.

3. **Deduplicate static context**
   - Store issue body, workflow policy text, and long remediation notes once per run.
   - Pass short references/deltas on retries.
   - Impact: lower token spend and better cache locality.

4. **Pair cache telemetry with memory retrieval outcomes**
   - Memory hit + stable prompt prefix is where compounding gains should show up.
   - Right now both are partially opaque.

## Orchestrator Health

### Observed health signals

- **High skip churn:** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` have extremely high skipped/other counts with p50 around 1s.
- **Wave progression is partly healthy but not fully stable:** `orchestrate_decompose_test` failed once (`25115169454`) with only 1 child issue, then succeeded in `25126757724` with 2 child issues.
- **Poller cadence is stable:** `orchestrate_poll` averages 43.8s with p95 55.2s.
- **Conflict-heal path is still expensive:** review failures end in unresolved conflicts after long runs.

### Recurring pain points

1. Over-dispatch followed by immediate skip.
2. Duplicate implementation starts from state-transition races.
3. Nondeterministic decomposition cardinality in orchestrator smoke.
4. Slow escalation from stuck integration/review states.

### Smallest safe mitigations

- Precompute downstream eligibility before dispatching child workflows.
- Add one canonical “active run exists” guard for implement/review orchestration.
- Escalate stuck conflict/fingerprint states immediately to the judge/poller instead of waiting on long generic loops.
- Keep all current fail-open fallbacks for API/MCP failures.

### Indicators to track next

- `% skipped downstream runs per orchestrated issue`
- `avg implement runs started per approved issue`
- `review_autofix check-run wait-loop count`
- `orchestrator child issues created per decomposition request`
- `time from conflict detection -> judge escalation`

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

### 1. Review/autofix wait-loop overhead
- **Stage:** review/autofix
- **Type:** retry/poll overhead
- **Evidence:** 21–24 check-run wait cycles in slow review runs.
- **Fix:** shorten or smarter-skip check-run waiting.

### 2. Workflow fanout that turns into immediate skips
- **Stage:** clarify → plan → implement → respond routing
- **Type:** queueing/control-plane overhead
- **Evidence:** very high skipped counts and 1s runs.
- **Fix:** dispatch only the needed next workflow.

### 3. Duplicate implement starts during handoff
- **Stage:** plan → implement
- **Type:** retry/race overhead
- **Evidence:** E2E smoke observed up to 5 implement runs for one issue.
- **Fix:** stronger pre-dispatch idempotency.

### 4. Late deterministic failures in CI
- **Stage:** validate/CI
- **Type:** compute waste
- **Evidence:** ~10-minute CI runs failing on simple lint errors.
- **Fix:** touched-file lint before PR push/commit.

### 5. Merge/conflict and integration-fingerprint regressions
- **Stage:** review/autofix and implement
- **Type:** merge/conflict overhead
- **Evidence:** `CONFLICT_RESOLVED=false` in review failures; fingerprint failures in implement.
- **Fix:** no-progress detection and preflight fingerprint validation.

### 6. Release-smoke serialism
- **Stage:** validate/orchestrate/release smoke
- **Type:** compute + polling overhead
- **Evidence:** `test_and_mark_stable` failures took 61–71 minutes.
- **Fix:** fail-fast subjobs and slower backoff polling for long-running smoke phases.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-tail runtime caused by check-run polling and conflict resolution.
- High skipped-run fanout across orchestrator-related workflows.
- CI failures discovered late for deterministic style issues.
- Release-smoke workflows fail late and serially.

**Top failure modes**
- Review resolver exits with unresolved merge conflict (`25115530167`, `25127054791`).
- Implement/integration failures on fingerprint regressions (`25143249766`).
- CI `ruff` failures on generated code (`25144143440`, `25144388441`).
- Orchestrator decomposition nondeterminism in release smoke (`25115169454`).
- Nightly validation self-test had 2 failing fixtures out of 3 in sampled run `25145624630`.

**Highest-cost drivers**
- Slow `review_autofix` successes/failures: 1,452s to 2,287s in sampled slow set.
- Repeated cancelled `review_autofix` runs.
- Full-CI reruns for simple lint failures.
- Serial stable-test failures at 3,677s and 4,270s.

**Top 3 prioritized actions**
1. **Reduce `review_autofix` check-run waiting** and exit earlier with partial snapshot semantics.
2. **Add pre-commit touched-file lint** in implement/review workflows.
3. **Prevent duplicate/skipped workflow fanout** by moving gating before dispatch.

## Metrics Appendix

### Repository summary

| Repo | Total runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg dur (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 277 | 16 | 97 | 610 | 1.6% | 151.9 | 1.0 | 635.0 |

### Key workflow-family metrics

| Workflow family | Total | Success | Failure | Cancelled | Other | Avg dur (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 87 | 84 | 3 | 0 | 0 | 603.0 | 608.0 | 640.4 |
| implement | 161 | 13 | 8 | 3 | 137 | 50.0 | 1.0 | 285.0 |
| review_autofix | 140 | 45 | 2 | 93 | 0 | 407.8 | 30.5 | 1865.7 |
| clarify | 180 | 14 | 0 | 0 | 166 | 10.5 | 1.0 | 77.3 |
| plan | 160 | 12 | 0 | 0 | 148 | 27.7 | 1.0 | 180.9 |
| orchestrate_clarify_respond | 161 | 3 | 0 | 0 | 158 | 1.6 | 1.0 | 7.0 |
| orchestrate | 4 | 4 | 0 | 0 | 0 | 482.0 | 450.5 | 757.7 |
| orchestrate_poll | 33 | 33 | 0 | 0 | 0 | 43.8 | 43.0 | 55.2 |
| test_and_mark_stable | 2 | 0 | 2 | 0 | 0 | 3973.5 | 3973.5 | 4240.4 |
| nightly_validation_selftest | 1 | 0 | 1 | 0 | 0 | 94.0 | 94.0 | 94.0 |

### Notable failed runs

| Run ID | Family | Duration (s) | Failure point |
|---|---|---:|---|
| 25126757724 | test_and_mark_stable | 4270 | e2e-smoke-test / Phase 4b: Verify editor removed bait line |
| 25115169454 | test_and_mark_stable | 3677 | orchestrate-decompose-test / Dispatch internal-orchestrate.yml with multi-issue project |
| 25127054791 | review_autofix | 1614 | review / codex-agent / Run Codex resolver, validate, stage, commit |
| 25115530167 | review_autofix | 1508 | review / codex-agent / Run Codex resolver, validate, stage, commit |
| 25143404687 | implement | 658 | implement / implement / Run Codex implementation |
| 25144143440 | ci | 631 | lint / Python lint (ruff) |
| 25144388441 | ci | 614 | lint / Python lint (ruff) |
| 25145624630 | nightly_validation_selftest | 94 | validation-selftest / Run validation self-test matrix |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total telemetry events observed | 70 |
| Retrieve events | 17 |
| Retrieve hit rate | 41.2% |
| Retrieve zero-record count | 10 |
| Avg estimated tokens per retrieve | 11.5 |
| `keyword_method=none` | 10 |
| `keyword_method=plain` | 7 |
| `keyword_method=llm` | 0 |
| `fail_open: true` seen | 0 |
| `enabled: false` seen | 0 |
| Events with `push_attempts > 1` | 1 |

### Sampled GH API hotspot summary  
*(approximate; derived from log-observed command mentions/poll loops, not exact executed-call counts)*

| Run ID | Family | Approx `gh api` mentions | Approx `gh workflow run` mentions | Check-run wait-loop lines |
|---|---|---:|---:|---:|
| 25143249766 | implement | 221 | 50 | 0 |
| 25143404687 | implement | 207 | 50 | 0 |
| 25115618107 | implement | 207 | 50 | 0 |
| 25127108576 | implement | 207 | 50 | 0 |
| 25115530167 | review_autofix | 166 | 36 | 21 |
| 25127054791 | review_autofix | 146 | 30 | 24 |
| 25143964823 | review_autofix | 15 | 0 | 24 |
| 25115192726 | workflow_log_analysis | 96 | 0 | 0 |

### Token/cache telemetry availability

| Metric | Availability in sampled logs |
|---|---|
| Prompt tokens | Not emitted in usable form |
| Completion tokens | Not emitted in usable form |
| Total tokens | Not emitted in usable form |
| Prompt cache create counts | Not emitted in usable form |
| Prompt cache read/hit counts | Not emitted in usable form |
| Cache miss counts | Not emitted in usable form |

If you want, I can turn this into a **prioritized implementation backlog** with owner, effort, and rollout order.
