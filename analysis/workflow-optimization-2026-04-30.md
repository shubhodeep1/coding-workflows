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

## Deep Audit — Workflows & Scripts (2026-04-30)

### Section 1: Bug & Correctness Sweep

Thin `internal-*` wrappers were audited end-to-end. Most are pure `workflow_call` shims; the only bespoke wrapper logic that raised a correctness issue was `internal-review.yml`.

- **ID** — BUG-001  
  **File path** — `.github/workflows/internal-review.yml:91-134`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The `resolve-claude-branch-pr` step uses two raw `gh api` calls with `2>/dev/null || echo ""` / `|| echo 'main'` fallbacks. If GitHub returns a transient error or rate limit at lines 98-101, `existing_pr` becomes empty and `base_ref` falls back to `main`, so the job emits `proceed=true` and the downstream `review-claude-branch-push` job dispatches a no-PR review run even when an open PR already exists for the same `claude/**` branch. That creates duplicate reviewer/editor runs for the same head SHA.  
  **Recommended fix** — Source `scripts/gh_helpers.sh` in this step and replace both raw lookups with `gh_retry`/`_safe_gh_jq`. On lookup failure, fail open to `proceed=false` instead of dispatching, so the `pull_request` event remains the sole owner when PR-state resolution is uncertain.

- **ID** — BUG-002  
  **File path** — `scripts/tg_helpers.sh:167-205,241-276`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — `tg_store_msg_id()` and `tg_store_phase_msg_id()` implement a read-modify-write cycle on a marker comment: fetch recent comments, choose the first matching marker, append the new Telegram ID locally, then `PATCH` the whole comment body. Two concurrent alerts on the same issue/phase can read the same old body and the later patch overwrites the earlier one, dropping one message ID. The scan is also capped at the last 30 comments, so an older marker can be missed and a second tracking comment created.  
  **Recommended fix** — Make tracking append-only instead of in-place mutable: either create one tracking comment per message/phase, or store a dedicated marker comment ID and retry a refetch/merge/patch loop until the updated body contains both the old and new IDs. If keeping comment discovery, paginate until the marker is found instead of limiting to 30 comments.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — API-001  
  **File path** — `.github/workflows/clarify.yml:365-383`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — When semantic cache is enabled, `Fetch issue comments` performs two separate comment fetches on the same execution path: first `GET /issues/{n}/comments?per_page=50` into `ISSUE_COMMENTS_FILE`, then a second paginated `GET /issues/{n}/comments?per_page=100` to build `THREAD_HISTORY_FILE`. Current call count: **2** logical API fetches for the same comment set. Proposed call count: **1**.  
  **Recommended fix** — Fetch the full paginated comment set once, write it to a temp JSON file, derive the bounded 50-comment prompt slice and the full thread-history view locally with `jq`. Extend the existing JSON-file pattern in `scripts/gh_helpers.sh` (`gh_api_json_to_file`) so the workflow reuses one cached payload instead of re-hitting GitHub.

- **ID** — BATCH-001  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:59-100`  
  **Severity** — Low  
  **Category tag** — `api-batching`  
  **Description** — The workflow fetches queued runs and in-progress runs in two separate `GET /repos/{repo}/actions/runs` calls, then merges them in `combined_runs_json`. Current call count: **2** list-runs calls per close event. Proposed call count: **1**.  
  **Recommended fix** — Issue one branch/event-scoped `actions/runs` listing and filter `queued|in_progress` statuses client-side with `jq`. The existing single-list-plus-local-filter pattern in `scripts/gh_helpers.sh:1170-1215` (`autofix_retrigger_has_inflight_peer`) is the right helper to extend.

- **ID** — API-002  
  **File path** — `.github/workflows/issue_pr_status.yml:188-349,501-513`  
  **Severity** — Low  
  **Category tag** — `api-redundancy`  
  **Description** — The main status-sync step already classifies linked issues via one batched GraphQL query and, on failure, a per-issue REST fallback. Later, the `Send PR merged Telegram alert` step loops over `LINKED_ISSUE_NUMBERS` and re-fetches each issue body with `_safe_gh_jq "repos/.../issues/{n}"` just to decide whether any linked issue is orchestrator-managed. Current call count: **1 batched classification + N extra issue fetches**. Proposed call count: **1 batched classification + 0 extra issue fetches**.  
  **Recommended fix** — Export `IS_ORCHESTRATED` (or the managed/tracking issue sets) from the earlier classification step into `$GITHUB_ENV`/outputs and reuse it in the merged-alert step. If a reusable helper is preferred, mirror the batched detail-prefetch approach already used in `scripts/orchestrate_poll_process.sh`.

- **ID** — BATCH-002  
  **File path** — `.github/workflows/test-and-mark-stable.yml:2528-2758`  
  **Severity** — Low  
  **Category tag** — `api-batching`  
  **Description** — The `Dispatch & watch` blocks for `workflow-log-analysis`, `validation-refresh`, `update_workflows`, and `internal-memory-maintenance` each repeat the same API-heavy pattern: fetch latest prior run ID, poll workflow-runs until a new run appears, then poll `/actions/runs/{id}` until completion. Current call count per dispatched child workflow is **at least 3 API calls and grows with every 5s/15s poll cycle**; across the four blocks shown here it quickly becomes dozens of calls. This is the concrete code site behind the existing report’s “E2E/test poll loops” hotspot. [NEEDS VERIFICATION]  
  **Recommended fix** — Extract a shared `dispatch_and_watch_workflow` helper into `scripts/gh_helpers.sh` with backoff and cached run-ID detection, then reuse it for every smoke sub-dispatch. That keeps the behavior but removes four copied pollers and reduces steady-state list/status polling.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — DUP-001  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/mark-stable.yml:198-225,321-348; .github/workflows/orchestrate_poll.yml:63-97; .github/workflows/comprehensive-test-and-release.yml:72-98,315-341; .github/workflows/test-and-mark-stable.yml:294-341,612-640,915-934,1565-1576`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The repo carries multiple near-identical inline GitHub retry helpers (`_rl_wait`, `_gh_retry`, `gh_api_safe`) with slightly different retry counts, backoff ceilings, and stderr handling. This duplicates logic that `scripts/gh_helpers.sh` already centralizes and increases drift risk: a rate-limit fix applied to one helper does not automatically reach the others.  
  **Recommended fix** — Consolidate on `scripts/gh_helpers.sh` for workflow-side GH access. If a workflow needs a specialized helper, add it there with a stable signature such as `gh_api_safe_json <endpoint> [jq args...]` or `gh_retry_status_poll <cmd...>`, then source it from each workflow. Callers to update: the listed workflows plus any future smoke/release watchers.

- **ID** — DUP-002  
  **File path** — `.github/workflows/implement.yml:372-470; .github/workflows/review_autofix.yml:848-940; .github/workflows/validate.yml:185-282; .github/workflows/orchestrate_poll.yml:266-360`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Four workflows independently implement “stage workflow support files” logic: determine self-repo vs consumer-repo behavior, fall back from pinned ref to `main`, copy scripts/prompts/schemas, and write `scripts/.gitignore`. The implementations have already diverged in capability (`validate.yml` has `copy_from_ref_or_local`, `review_autofix.yml` has required/optional bootstrap lists, `implement.yml` tracks a fetched manifest, `orchestrate_poll.yml` has a smaller hardcoded set). This is a textbook drift surface.  
  **Recommended fix** — Move support staging into a shared script, e.g. `scripts/stage_workflow_support.sh --mode <implement|review|validate|poll> --script-ref "$SCRIPT_REF" --support-root "$RUNNER_TEMP/..."`, returning env exports/manifest paths on stdout. Update the four workflows to call that script and keep only mode-specific required/optional file lists in the YAML.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — EXPR-001  
  **File path** — `.github/workflows/validate.yml:188-481`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Fetch workflow support files` step contains `${{ }}` interpolations inside a large inline `run:` body whose estimated template body length is **16,530 chars**, leaving only **4,470 chars** of headroom before GitHub’s 21,000-char expression cap. The block already contains cloning, fallback, copy, and manifest logic plus long literal template lists, so routine maintenance can push it over the limit.  
  **Recommended fix** — Extract the support-fetch/stage logic to an external script under `scripts/` and keep the workflow step as a short wrapper that passes env vars. This matches the repo’s existing mitigation pattern for `review_autofix` and `implement`.

- **ID** — EXPR-002  
  **File path** — `.github/workflows/orchestrate_clarify_respond.yml:840-1123`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — `Parse and post answer` has an estimated expression-template body length of **15,141 chars**, leaving **5,859 chars** of headroom. The step mixes processed-command claim logic, loop-guard handling, Telegram, and memory completion payload construction in one block; it is already large enough to be fragile under additive edits.  
  **Recommended fix** — Extract the answer/loop-break posting flow to a dedicated script such as `scripts/orchestrate_post_clarify_answer.sh`, with the workflow only wiring env/inputs and handling outputs.

- **ID** — EXPR-003  
  **File path** — `.github/workflows/review_autofix.yml:1266-1588`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — `Collect PR check-run failures` has an estimated expression-template body length of **16,438 chars**, leaving **4,562 chars** of headroom. The block includes retry setup, wait-loop logic, JSON-shape normalization, and an inline Python writer. It is one of the remaining large inline blocks in the largest workflow file in the repo.  
  **Recommended fix** — Extract the collector into a dedicated script (for example `scripts/collect_pr_check_runs.sh`) and keep the YAML step to env setup + one script invocation. That is the same extraction strategy already used for other `review_autofix` over-limit blocks.

- **ID** — EXPR-004  
  **File path** — `.github/workflows/test-and-mark-stable.yml:902-1186`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — `Wait for review workflow on PR` has an estimated expression-template body length of **16,301 chars**, leaving **4,699 chars** of headroom. The block combines rate-limit helpers, run discovery, live log probing, reviewer success heuristics, and inactivity detection in a single templated `run:` body.  
  **Recommended fix** — Split the live-log shortcut logic into a support script or multiple smaller steps. Keeping polling and live-log parsing separate would materially reduce expression size and make failures easier to isolate.

No workflow file exceeds the 800 KB audit threshold. The largest audited workflow is `review_autofix.yml` at **264,067 bytes**, well below the 1 MB GitHub hard limit.

### Section 5: Cross-Cutting Concerns

- **ID** — CONSIST-001  
  **File path** — `scripts/tg_helpers.sh:175-205,246-276`  
  **Severity** — Medium  
  **Category tag** — `consistency`  
  **Description** — `tg_helpers.sh` sources `curl_gh_api` and uses it for comment-list reads, but its write paths (`POST`/`PATCH` tracking comments) use raw `curl` with no retry or rate-limit handling. That makes Telegram tracking less reliable than the rest of the repo’s GitHub interactions and can silently lose tracking updates under 403/429/5xx responses.  
  **Recommended fix** — Add a method-capable JSON helper to `scripts/gh_helpers.sh` (for example `curl_gh_api_json <method> <url> <json-body>`) and use it consistently for both reads and writes in `tg_helpers.sh`.

- **ID** — SHELL-001  
  **File path** — `scripts/review_commit_changes.sh:448-455; scripts/review_conflict_resolve.sh:852-853`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — Both scripts pass the authenticated remote URL to `git remote set-url origin` without quoting the full argument. ShellCheck flags this as SC2086. Today’s token format is usually safe, but this still relies on shell word-splitting not changing and duplicates the same risky pattern in two places.  
  **Recommended fix** — Quote the full URL argument in both scripts: `git remote set-url origin "https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}"`. If remote-auth setup is centralized later, move this into one helper.

No `TODO`, `FIXME`, `HACK`, or `XXX` markers were found in the audited `.github/workflows/*.yml`, `scripts/*.sh`, or `scripts/*.py` scope. No dead-code finding cleared the evidence threshold without speculative reachability assumptions.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 10 | BUG-001, BUG-002, API-001, DUP-001, DUP-002, EXPR-001, EXPR-002, EXPR-003, EXPR-004, CONSIST-001 |
| Low | 4 | BATCH-001, API-002, BATCH-002, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 4 | Medium |
| Code modularization | 6 | Large |
| Expression size reduction | 4 | Medium |
| Medium/Low fixes | 4 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-04-30)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is statically proven and can be consolidated/eliminated without changing control flow, retry semantics, or race boundaries. `NEEDS_VERIFICATION` means the overlap is real but at least one safety precondition is not fully provable from static reading alone. `RISKY_SKIP` means the redundancy is visible, but the call lives in a retry/poll/race-defense path (or another explicitly protected area), so it must not be auto-implemented without manual review.

### Consolidation Candidates (MERGE-###)

- **ID** — `MERGE-001`  
  **Safety tag** — `SAFE_TO_MERGE`  
  **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:269-273`; `.github/workflows/test-and-mark-stable.yml:275-275`  
  **Current call count** — 2  
  **Proposed call count** — 1  
  **Endpoint(s)** — `POST /repos/{owner}/{repo}/issues`; `GET /repos/{owner}/{repo}/issues/{issue_number}`  
  **Evidence** — the workflow creates an issue, then immediately re-fetches the created issue only to read `html_url`:
  ```bash
  ISSUE_NUMBER=$(gh api "repos/${TEST_REPO}/issues" \
    -f title="${TITLE}" \
    -f body="${BODY}" \
    --jq '.number')

  ISSUE_URL=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '.html_url')
  ```
  The second call is reading resource data from the issue that was just created in the same step.  
  **Proposed fix** — In the `Create smoke-test issue` step, capture the full `POST /issues` response once (temp JSON file or shell variable), then derive both `ISSUE_NUMBER` and `ISSUE_URL` locally with `jq`; remove the follow-up `GET /issues/${ISSUE_NUMBER}`.  
  **Safety rationale** — The GET only re-reads fields from the resource created by the immediately preceding POST in the same step, with no intervening mutation, no retry loop, and no change to auth scope.  
  **Downstream signal** — Capture the `POST /issues` response once and derive both `number` and `html_url` from it; delete the follow-up `GET /issues/${ISSUE_NUMBER}`.

- **ID** — `MERGE-002`  
  **Safety tag** — `RISKY_SKIP`  
  **File path and line ranges** — `scripts/review_conflict_resolve.sh:97-103`; `scripts/review_conflict_resolve.sh:108-114`  
  **Current call count** — 2  
  **Proposed call count** — 1  
  **Endpoint(s)** — Actions workflow-runs listing for `${_poll_workflow}` via `gh run list --workflow ...`  
  **Evidence** — the same dedupe guard asks twice whether `internal-orchestrate-poll.yml` already has an active run, once for `in_progress` and once for `queued`:
  ```bash
  _active_count="$(GH_TOKEN="${GH_PAT}" gh run list \
      --workflow="${_poll_workflow}" \
      --repo "${GITHUB_REPOSITORY}" \
      --status in_progress \
      --limit 1 \
      --json databaseId \
      --jq 'length' 2>/dev/null || echo 0)"
  ```
  ```bash
  _active_count="$(GH_TOKEN="${GH_PAT}" gh run list \
      --workflow="${_poll_workflow}" \
      --repo "${GITHUB_REPOSITORY}" \
      --status queued \
      --limit 1 \
      --json databaseId \
      --jq 'length' 2>/dev/null || echo 0)"
  ```
  The queried object set overlaps completely except for the status filter.  
  **Proposed fix** — If this is ever hand-edited, extend `_dispatch_integration_judge_now` to perform one workflow-run listing with enough headroom (`--limit` > 1, include `status` in `--json`) and filter `queued|in_progress` locally.  
  **Safety rationale** — This sits inside an integration-conflict recovery/dedupe path that explicitly defends against upstream races, so collapsing the two status probes can change queue-visibility timing and must not be auto-implemented.  
  **Downstream signal** — Do not auto-implement; manually validate on a live integration-conflict path that a single combined run-list query cannot miss queued/in-progress poller runs or allow duplicate dispatches.

### Redundant Re-Fetch (REUSE-###)

- **ID** — `REUSE-001`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/implement.yml:53-65`; `.github/workflows/implement.yml:511-543`  
  **Current call count** — 2 on the non-skipped implement path  
  **Proposed call count** — 1  
  **Endpoint(s)** — `GET /repos/{owner}/{repo}/issues/{issue_number}`  
  **Evidence** — the job first fetches the issue only for labels during precheck, then later fetches the full issue JSON again:
  ```bash
  ISSUE_LABELS_JSON="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '[.labels[].name]')"
  ```
  ```bash
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"

  ISSUE_BODY="$(jq -r '.body // ""' "${ISSUE_META_FILE}")"
  ISSUE_TITLE="$(jq -r '.title // ""' "${ISSUE_META_FILE}")"
  ```
  The later step already needs the full issue object that subsumes the earlier label-only read.  
  **Proposed fix** — Make `Precheck approval phase label` the authoritative issue fetch for the job: either move `Create runtime workspace` earlier so precheck can write `ISSUE_META_FILE`, or write a temp JSON file in precheck and teach `Fetch issue metadata` to no-op when that file already exists and parses cleanly.  
  **Safety rationale** — The overlap is real and there is no intervening issue mutation, but the early call is non-retrying and skip-gating while the later call is `gh_retry`-backed and initializes cached files consumed by later steps.  
  **Downstream signal** — Verify two cases before merging: (1) a happy-path implement run still populates `ISSUE_BODY/ISSUE_TITLE/ISSUE_URL` identically from the cached file, and (2) a transient early-fetch failure preserves current skip/retry behavior instead of widening or shrinking failure exposure.

- **ID** — `REUSE-002`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/orchestrate_clarify_respond.yml:55-81`; `.github/workflows/orchestrate_clarify_respond.yml:403-430`  
  **Current call count** — 4 on the orchestrator-managed path  
  **Proposed call count** — 2  
  **Endpoint(s)** — `GET /repos/{owner}/{repo}/issues/{issue_number}` for the child issue; `GET /repos/{owner}/{repo}/issues/{tracking_number}` for the tracking issue  
  **Evidence** — the workflow fetches the same child issue twice and the same tracking issue twice, just to read different fields:
  ```bash
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ISSUE_BODY="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -r '.body // ""')"
  ISSUE_TITLE="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -r '.title // ""')"
  ...
  TRACKING_TITLE="$(gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.title // ""' 2>/dev/null || echo "")"
  ```
  ```bash
  ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ISSUE_BODY="$(printf '%s' "${ISSUE_META}" | jq -r '.body // ""')"
  ISSUE_TITLE="$(printf '%s' "${ISSUE_META}" | jq -r '.title // ""')"
  ...
  TRACKING_BODY="$(gh_retry gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.body // ""')"
  ```
  **Proposed fix** — Extend `Check orchestrator metadata` to persist the fetched child-issue payload and optional tracking-issue payload into `${RUNNER_TEMP}` or exported env/file paths, then have `Fetch issue and tracking context` consume those cached payloads first and only fall back to `gh_retry` on cache miss/parse failure.  
  **Safety rationale** — The calls hit the same resources in the same job with no visible intervening mutation, but the first step is the early gate and runs before support helpers are sourced, so reuse must preserve its current fail-open/skip behavior.  
  **Downstream signal** — Verify three cases before merging: (1) non-orchestrator issues still exit at the early gate, (2) smoke-fixture alert suppression still works when tracking metadata is present, and (3) a failure in the early plain `gh api` path still degrades to the later `gh_retry` path instead of hard-failing the job.

### Dead Calls (DEAD-API-###)

- **ID** — `DEAD-API-001`  
  **Safety tag** — `SAFE_TO_MERGE`  
  **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:1245-1246`  
  **Current call count** — 1  
  **Proposed call count** — 0  
  **Endpoint(s)** — `GET /repos/{owner}/{repo}/commits?sha={branch}&per_page=20`  
  **Evidence** — `COMMITS_AFTER` is assigned from the API call, but never read afterward in the step or elsewhere:
  ```bash
  COMMITS_AFTER=$(gh api "repos/${TEST_REPO}/commits?sha=${BRANCH}&per_page=20" \
    --jq "[.[] | select(.sha != \"${BAIT_SHA}\") | .sha] | length" 2>/dev/null || echo "0")
  # The PR head SHA should differ from the bait SHA.
  PR_HEAD=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")
  ```
  The actual pass/fail gate uses only `PR_HEAD` vs `BAIT_SHA`.  
  **Proposed fix** — Delete the `COMMITS_AFTER` call from `Phase 4b: Verify editor removed bait line`; if commit-count evidence is desired later, add an explicit assertion that consumes it in a separate change.  
  **Safety rationale** — This is a true dead read: the value is never consumed, its failure is already masked with `|| echo "0"`, and removing it does not alter any downstream branch or log key.  
  **Downstream signal** — Delete the `COMMITS_AFTER` call in Phase 4b and keep the existing `PR_HEAD != BAIT_SHA` check as the editor-push proof.

- **ID** — `DEAD-API-002`  
  **Safety tag** — `RISKY_SKIP`  
  **File path and line ranges** — `scripts/orchestrate_poll_process.sh:11390-11400`  
  **Current call count** — 1 per standalone conflict-sweep cycle  
  **Proposed call count** — 0  
  **Endpoint(s)** — `GET /repos/{owner}/{repo}`  
  **Evidence** — the sweep fetches `DEFAULT_BRANCH`, but the variable is not referenced anywhere in the remainder of the block:
  ```bash
  STANDALONE_PRS="$(gh_retry gh pr list \
    --repo "${GITHUB_REPOSITORY}" \
    --state open \
    --json number,headRefName,baseRefName \
    --limit 100 2>/dev/null || echo "[]")"

  STANDALONE_COUNT="$(echo "${STANDALONE_PRS}" | jq 'length')"
  echo "Found ${STANDALONE_COUNT} open PR(s) to scan."

  CONFLICT_SWEEP_FIXED=0
  DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
  ```
  The subsequent loop uses `S_BASE`, `S_HEAD`, `S_PR_JSON`, and `S_MERGEABLE_STATE`, but not `DEFAULT_BRANCH`.  
  **Proposed fix** — Manual cleanup only: remove the unused `DEFAULT_BRANCH` assignment from the standalone conflict sweep, unless a reviewer confirms it was intended for an imminent branch filter that never landed.  
  **Safety rationale** — Although the call appears dead, it lives inside `scripts/orchestrate_poll_process.sh`, which is explicitly a race-defensive poller path and therefore outside SAFE_TO_MERGE auto-implementation scope.  
  **Downstream signal** — Do not auto-remove; manually trace one standalone conflict-sweep cycle and confirm no hidden diagnostic, grep contract, or planned branch-filter dependency relies on this assignment before deleting it.

### Cross-References to Deep Audit Section

- API-001: `NEEDS_VERIFICATION` — same comment resource is fetched twice, but replacing the split 50-comment/full-history reads with one paginated fetch changes pagination and truncation semantics that should be validated.
- BATCH-001: `NEEDS_VERIFICATION` — one branch/event-scoped run listing should subsume queued+in-progress queries, but removing server-side `status` filtering changes response shape and page composition.
- API-002: `NEEDS_VERIFICATION` — the later orchestrator-alert body fetch should reuse earlier classification data, but the cross-step export and fail-open behavior must match the current per-issue fallback.
- BATCH-002: `RISKY_SKIP` — the repeated dispatch/watch pollers are real duplication, but they live inside long-running smoke-test polling loops where timing and retry behavior are observable and not safe for auto-implementation.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 2 | MERGE-001, DEAD-API-001 |
| NEEDS_VERIFICATION | 2 | REUSE-001, REUSE-002 |
| RISKY_SKIP | 2 | MERGE-002, DEAD-API-002 |

### Implement-Stage Handoff

- MERGE-001
- DEAD-API-001
