## Executive Summary

- **Fix the `test_and_mark_stable` smoke-test / auto-merge race first.** All 5 sampled `Test & Mark Stable Release` runs failed (`run_ids: 25300046587, 25305535590, 25308071039, 25310399716, 25324103531`), and the deep-dive log for `25324103531` shows the PR was already closed before bait injection, so Phase 4b could never pass. **Estimated impact:** recover ~40–55 minutes per failed release attempt and move release success from 0/5 toward normal. **Confidence:** high.

- **`review_autofix` is the biggest AI-cost and latency hotspot.** Comment-only / claude-branch-review runs still consumed ~1,665s in `review / codex-agent` (`25327045933`, `25328431960`) while reporting `Reviewers_successful=6` and skipping editor/commit/judge paths. **Estimated impact:** 15–25 minutes faster and ~50–70% reviewer-token savings on low-risk review paths if the reviewer panel is trimmed and bootstrap duplication is removed. **Confidence:** medium-high.

- **CI is consistently dominated by a ~10 minute `lint` phase, even when review logic classified the PR as docs-only.** Example: docs-only review run `25324380473` skipped deterministically, but companion CI run `25324380459` still took 606s. **Estimated impact:** ~9–10 minutes saved on docs-only / non-code PRs with path-aware CI gating. **Confidence:** high.

- **The poller is doing real work when there is no work.** Recent `orchestrate_poll` runs repeatedly finished with `has_work=false` yet still performed repository checkout; run `25347221704` took 51s, and recent deep-dive evidence shows `fetch-depth: 0` plus full branch/tag fetch in no-work polls. **Estimated impact:** 8–12s saved per poll cycle and lower runner occupancy. **Confidence:** high.

- **AI memory is helping implement flows but not review flows.** Deep-audit telemetry reports `retrieve` hit rate `11/16 = 68.8%`, but every sampled `review_autofix` retrieve returned `records_selected: 0` with `keyword_method:"none"`; implement retrieves were lightweight and useful (`records_selected:1`, `estimated_tokens:28`). **Estimated impact:** small latency win and moderate quality/reliability win if reviewer retrieval is fixed or skipped when nonproductive. **Confidence:** high.

- **Prompt cache is enabled but effectively un-auditable.** `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears in implement/review paths, but cache probe lines in failed `review_autofix` runs (`25300219172`, `25324565713`) emit `prompt_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`. **Estimated impact:** no immediate runtime gain, but unlocking accurate cache tuning is prerequisite to safe model/cost optimization. **Confidence:** high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Block auto-merge until the smoke gate finishes in `test_and_mark_stable`
**Critical-path win**

- **Evidence**
  - Workflow family `test_and_mark_stable`: `total_runs=5`, `success_count=0`, `failure_count=5`, `avg_duration_seconds=2865.2`, `p50=2921`, `p95=3283.2`.
  - All 5 failures ended at `e2e-smoke-test / Phase 4b: Verify editor removed bait line`.
  - In deep-dive run `25324103531`, the smoke log states: `PR #... is already ... closed before bait could be injected`, and explicitly attributes it to `review_autofix.yml` auto-merging before the smoke/e2e labels took effect.

- **Root cause**
  - Release smoke validation races against deterministic skip / auto-merge behavior in `review_autofix`.

- **Exact change**
  - When the smoke gate labels or force-review labels are present, suppress deterministic auto-merge until smoke verification clears.
  - Alternatively, inject the bait before any merge-eligible review path starts, then allow merge only after the bait-removal check passes.

- **Estimated time savings**
  - Saves the full failed run: **~2,424–3,303s per failed release attempt**.

- **Implementation risk**
  - **Low-medium.** Behavior is narrow and label-gated; easy to fail closed by only delaying merge on smoke-tagged PRs.

---

### 2. Skip poller checkout/fetch when `has_work=false`
**Critical-path win for orchestrator cadence and runner occupancy**

- **Evidence**
  - `orchestrate_poll` family: `avg_duration_seconds=48.87`, `p50=46.5`, `p95=69.7`, `30/30` success.
  - Recent run `25347221704`: `Found 0 active tracking issue(s)` / `No active orchestrator projects`, yet the job still performed:
    - `actions/checkout@v5`
    - `fetch-depth: 0`
    - `git fetch ... +refs/heads/* ... +refs/tags/*`
  - Similar no-work completions appear across recent poll runs (`25342258010`, `25344912795`, `25346106032`), each ~41–51s.

- **Root cause**
  - The workflow determines “no work” early, but later checkout/setup steps are not fully gated on that result.

- **Exact change**
  - Make repository checkout, support checkout, and downstream setup conditional on `has_tracking == true` or `has_work == true`.
  - If checkout is still required, change the main repo checkout from `fetch-depth: 0` to `fetch-depth: 1` and keep `fetch-tags: false`.

- **Estimated time savings**
  - **~8–12s per poll run** in the no-work case; cumulative savings become material because polling is frequent.

- **Implementation risk**
  - **Low.** No behavioral change for active-work polls.

---

### 3. Remove repeated support-source/bootstrap work inside long `review_autofix` runs
**Critical-path win**

- **Evidence**
  - `review_autofix` family: `avg_duration_seconds=443.6`, `p95=1682.6`.
  - Runs `25327045933` and `25328431960` both took ~1,686s / ~1,682s, dominated by `review / codex-agent (claude-branch-review)`.
  - The API-redundancy deep audit (`workflow_log_analysis` run `25324145530`) explicitly calls out that long review runs show **multiple `actions/checkout@v5` sequences** and repeated support-source checkout/bootstrap before main work.

- **Root cause**
  - Support repo hydration and bootstrap checks are repeated inside the same review path.

- **Exact change**
  - Perform support checkout/bootstrap once per job and reuse the staged support directory.
  - Reuse already-fetched PR diff/comment artifacts across reviewer/editor phases instead of rehydrating them.

- **Estimated time savings**
  - **~60–180s per long `review_autofix` run** conservatively; potentially more on claude-branch-review paths.

- **Implementation risk**
  - **Medium.** Requires careful preservation of existing support-file contracts.

---

### 4. Add path-aware CI gating so docs-only PRs do not pay the 10-minute lint cost
**Critical-path win for low-risk PRs**

- **Evidence**
  - `ci` family: `avg_duration_seconds=611.8`, `p50=611`, `p95=647.8`.
  - Across many runs, `lint` dominates runtime: `25327045947` (~603s), `25328431755` (~593s), `25324380459` (~10m).
  - On PR `2093`, `review_autofix` run `25324380473` classified the change as `skip=true reason=docs_only`, but the same PR’s CI run `25324380459` still took 606s.

- **Root cause**
  - CI does not exploit already-available diff classification for docs-only / non-code changes.

- **Exact change**
  - Add path filters so docs-only / metadata-only PRs run a reduced CI subset:
    - workflow/script reference validation
    - yaml/static checks
    - skip full coverage-heavy test/lint path when Python/workflow logic is untouched

- **Estimated time savings**
  - **~570–610s per docs-only or non-code PR**.

- **Implementation risk**
  - **Low-medium.** Safe if limited to clearly non-runtime paths.

---

### 5. Stop running full free-disk cleanup on review paths that do not need it
**Micro-optimization with good aggregate payoff**

- **Evidence**
  - Failed `review_autofix` runs `25324565713` and `25300219172` both spend early time in `jlumbroso/free-disk-space@v1.3.1`, including large `apt-get remove` sweeps.
  - These same runs later failed for missing support files, not disk exhaustion.

- **Root cause**
  - Expensive disk cleanup runs unconditionally before verifying the path really needs it.

- **Exact change**
  - Gate free-disk-space to large-diff/editor paths or when available disk is below a threshold.
  - Skip it for comment-only/claude-branch-review jobs.

- **Estimated time savings**
  - **~30–90s per review run**, depending on runner state.

- **Implementation risk**
  - **Low.** Fail-open by re-enabling only on low-disk detection.

## Cost Optimizations

Ranked by expected token/dollar savings.

### 1. Shrink the reviewer panel on comment-only / claude-branch-review paths
- **Evidence**
  - `review_autofix` runs `25327045933` and `25328431960` each spent ~1,665s in `review / codex-agent`.
  - Both logs show `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... running reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped.`
  - `Reviewers_successful=6` was emitted.

- **Root cause**
  - Six external reviewer models are being used even when the workflow will not edit code or merge.

- **Exact change**
  - For comment-only / claude-branch-review runs, reduce to 2–3 reviewers or use a smaller fallback panel.
  - Keep the full six-model panel only for merge-blocking or autofix-capable paths.

- **Estimated savings**
  - **~50–70% reviewer-token savings** on those runs, plus major wall-clock reduction.

- **Quality-risk notes**
  - **Medium.** Keep full panel available behind a risk trigger:
    - large diff
    - workflow file touched
    - merge-conflict path
    - prior reviewer disagreement

---

### 2. Cut off deterministic implement retry loops earlier
- **Evidence**
  - Failed `implement` run `25294005792` shows:
    - `request_user_input is not supported in exec mode`
    - `Codex produced no actionable output ... attempts in a row`
    - terminal diagnostic after repeated attempts
  - The deep audit explicitly notes repeated implement failures wasting time and tokens on stale/mismatched context.

- **Root cause**
  - The loop retries even after high-signal failure modes that are unlikely to recover inside the same run.

- **Exact change**
  - For:
    - `request_user_input` in exec mode
    - repeated empty/no-actionable-output
    - repeated identical failure fingerprint
  - stop after 1–2 attempts and route directly back to clarify or failure diagnostics.

- **Estimated savings**
  - **Tens of thousands of tokens** and **~150–300s per failed implement run**.

- **Quality-risk notes**
  - **Low.** This removes retries only where the workflow already proves the run is nonrecoverable.

---

### 3. Stop paying full CI cost on docs-only PRs
- **Evidence**
  - Same evidence as Speed item #4: docs-only PR `2093` still incurred a 606s CI run (`25324380459`).

- **Root cause**
  - No cost-aware path classification in CI.

- **Exact change**
  - Reuse docs-only classification from review gating, or re-compute with path filters at CI entry.

- **Estimated savings**
  - Mostly compute cost rather than model cost, but high-frequency and safe.

- **Quality-risk notes**
  - **Low-medium.** Restrict to clearly non-executable file classes.

---

### 4. Make `workflow_log_analysis` summarization budget adaptive
- **Evidence**
  - In sampled `workflow_log_analysis` runs:
    - `25300062692`: `tokens_used=170,953`
    - `25310429821`: `tokens_used=221,799`
    - `25324145530`: `tokens_used=189,772`
  - These runs summarized `76–98` unselected runs each.

- **Root cause**
  - The summarizer targets up to 100 unselected runs regardless of whether recent deep dives already cover the important failures/slows.

- **Exact change**
  - Reduce the summarization target dynamically when:
    - one repo dominates
    - failures are already deeply sampled
    - the window is low-variance
  - Preserve the current deep-dive folders as primary evidence.

- **Estimated savings**
  - **~15–25% analysis-token reduction** on quieter windows.

- **Quality-risk notes**
  - **Low.** Keep minimum coverage floor for failures and newly slow families.

---

### 5. Defer model-selection tuning until prompt-cache telemetry is complete
- **Evidence**
  - In failed `review_autofix` runs `25300219172` and `25324565713`, cache-probe lines show `cache_enabled=true` but all token/cache counters are `na`.
  - `OPENROUTER_PROMPT_CACHE_DISABLED=false` is present, so caching is intended.

- **Root cause**
  - Missing counters make it impossible to determine whether expensive prompts are already being cached effectively.

- **Exact change**
  - Do **not** broadly downgrade models yet.
  - First emit real `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens`.

- **Estimated savings**
  - Not directly quantifiable today; this is a prerequisite to safe cost tuning.

- **Quality-risk notes**
  - **Low.** This avoids premature quality regressions from blind model downgrades.

## Reliability Improvements

Ranked by expected failure-rate / rerun-rate reduction.

### 1. Fix the smoke-gate / auto-merge race in release validation
- **Failure evidence**
  - `test_and_mark_stable` failed 5/5 times.
  - All failed at `e2e-smoke-test / Phase 4b: Verify editor removed bait line`.
  - Deep-dive run `25324103531` logs that the PR was already closed before bait injection.

- **Root cause category**
  - Workflow ordering / race condition.

- **Exact fix**
  - Make deterministic skip/merge logic aware of the smoke-gate labels or explicit smoke mode.
  - Refuse merge while smoke validation is outstanding.

- **Expected reliability impact**
  - **Highest-impact fix in the dataset.** Likely restores release pipeline from 0% success in sampled window.

- **Rollback / fail-open**
  - Fail closed on smoke-tagged PRs only; normal merge behavior unchanged elsewhere.

---

### 2. Add preflight validation that support scripts/prompts exist before entering `review / codex-agent`
- **Failure evidence**
  - Failed `review_autofix` runs `25324565713` and `25300219172` both show:
    - `Required bootstrap script ... is missing`
    - `Missing required support file prompts/mode-judge-review-blocked.txt`
  - Both failed at `Run Codex resolver, validate, stage, commit`.

- **Root cause category**
  - Packaging/version skew between workflow references and support files.

- **Exact fix**
  - Validate `REQUIRED_BOOTSTRAP_SCRIPTS` and required prompt files in a very early gate job.
  - Reuse the same “all workflow script references resolve” validation concept already present in CI.

- **Expected reliability impact**
  - Should materially reduce the current `review_autofix` failure rate (`2/78 = 2.56%`) and prevent late expensive failures.

- **Rollback / fail-open**
  - If preflight fails, exit early with a specific annotation and skip the costly reviewer/editor path.

---

### 3. Promote implement failure fingerprints to first-class terminal states
- **Failure evidence**
  - Failed implement run `25294005792` hit:
    - `request_user_input ... not supported in exec mode`
    - repeated no-actionable-output
    - stuck-in-exploration bailout
  - The deep audit also cites multiple implement failures with stale or mismatched context.

- **Root cause category**
  - Retry policy / orchestrator-state handling.

- **Exact fix**
  - Record and act on terminal fingerprints:
    - ambiguity → route to clarify
    - repeated empty output → stop and fail
    - unchanged failure fingerprint → no more retries
  - Surface the terminal reason in the issue label/comment so orchestrator doesn’t immediately recycle it.

- **Expected reliability impact**
  - Lower rerun loops and fewer repeated implement failures on the same issue.

- **Rollback / fail-open**
  - Keep current behavior as fallback if fingerprinting cannot classify the error.

---

### 4. Surface failing fixture names directly in nightly self-test logs
- **Failure evidence**
  - `nightly_validation_selftest` run `25299383150` failed with `fixtures=3 passed=1 failed=2`.
  - The deep-dive logs preserve summary counts but not the failing fixture names in the visible excerpt.

- **Root cause category**
  - Observability gap.

- **Exact fix**
  - Print the failed fixture names/stages directly before exiting non-zero.
  - Keep artifact upload, but don’t force incident responders to fetch artifacts for first diagnosis.

- **Expected reliability impact**
  - Faster diagnosis, lower mean time to repair.

- **Rollback / fail-open**
  - None needed; additive logging only.

## AI Memory Health

- **Telemetry present:** yes.
- **Sample basis:** deep-dive logs summarized by `workflow_log_analysis` run `25324145530`, plus direct telemetry in:
  - `review_autofix` failures `25300219172`, `25324565713`
  - `implement` failure `25294005792`
  - `orchestrate_poll` recent runs
  - `memory_maintenance` recent run `25326850014`

### Retrieve effectiveness
- **Retrieve hit rate:** **68.8%** (`11/16` retrieves had `records_selected > 0`).
- **Average `estimated_tokens` on retrieve:** **19.2**; **max 28**.
- **`keyword_method` distribution:** `plain=11`, `none=5`, `llm=0`.
- **Budget comparison:** **not assessable from current telemetry**; retrieve logs expose `estimated_tokens` but no retrieval budget field.

### Healthy patterns
- **Implement retrieval is productive and cheap.**
  - `implement` run `25294005792` logged `records_selected:1`, `keyword_method:"plain"`, `estimated_tokens:28`.
- **Poll ledger writes look healthy.**
  - Recent `orchestrate_poll` runs show `poll_completed` with `push_attempts:1`, `has_work:"false"`.

### Problems
- **Reviewer retrieval is ineffective.**
  - `review_autofix` failures `25300219172` and `25324565713` both logged `retrieve` with:
    - `enabled:true`
    - `records_selected:0`
    - `estimated_tokens:0`
    - `keyword_method:"none"`
- **0-record retrieves should be treated as a signal.**
  - Current review path still pays retrieval/setup overhead without receiving usable context.
- **High push retry counts exist, but are rare.**
  - Deep audit flagged **2** entries with `push_attempts:2`, including implement run `25293966619` and `workflow_log_analysis` run `25300062692`.
- **No sampled `fail_open:true` retrieves were observed.**
- **No sampled `enabled:false` retrieves were observed.**

### Recommendations
1. **Disable reviewer retrieval when keyword extraction returns `none`**, or fall back to a simpler `plain` method tied to linked issue/PR metadata.
2. **Emit retrieval budget fields** so “estimated_tokens vs budget” can be monitored directly.
3. **Alert on repeated `records_selected:0` for a role** over N runs; review is the obvious first candidate.
4. **Track `push_attempts > 1` as an SLO metric** for memory durability.

## GH API Call Audit

### Observed high-volume patterns
From the deep API audit in `workflow_log_analysis` run `25324145530`:

| API pattern | Count | Files | Note |
|---|---:|---:|---|
| `gh workflow run` | 138 | 11 | heavy in release/post-merge flows |
| `gh api graphql` | 44 | 7 | linked issue / PR graph lookups |
| `gh pr diff` | 33 | 3 | review diff fallback / hot path |
| `gh api /repos` | 19 | 4 | artifact + repo metadata lookups |
| `github.rest.pulls.get` | 3 | 2 | Copilot review / post-merge |
| `github.paginate pulls.listFiles` | 1 | 1 | Copilot review prepare |

### Findings and recommendations

#### 1. `cancel_on_pr_close` uses redundant run-list calls and per-run cancel POSTs
- **Evidence**
  - Recent run `25347708400`:
    - one `_gh_retry gh api ... /actions/runs` for queued
    - one for in-progress
    - then looped `_gh_retry gh api -X POST ... /cancel` per `run_id`
  - `_rl_wait()` calls `/rate_limit`.

- **Issue**
  - Redundant GETs; meta-call to `/rate_limit` inflates call count on slow paths.

- **Exact change**
  - Fetch runs once, filter both queued and in-progress client-side.
  - Only call `/rate_limit` after an actual 403/429, not preemptively inside helper logic.

- **Estimated reduction**
  - **1–3 calls per cancel run**, plus lower amplification under retries.

---

#### 2. `issue_pr_status` has a good batch path, but fallback can degenerate to per-issue REST
- **Evidence**
  - Run `25347708374` uses GraphQL for issue discovery.
  - The script contains:
    - batched GraphQL lookup
    - fallback per-issue REST `gh api "repos/.../issues/${_orch_num}"`

- **Issue**
  - When batch detection fails, the workflow can revert to N REST calls.

- **Exact change**
  - Harden the GraphQL batch query and schema handling so fallback is rare.
  - Reuse PR title/body from event payload whenever present to avoid extra `pulls/{PR}` fetches.

- **Estimated reduction**
  - **5–20 calls** on linked-issue-heavy PRs.

---

#### 3. Post-merge validate dispatch refetches PR/issue linkage data that is partially available already
- **Evidence**
  - Recent run `25347708390`:
    - `gh api graphql` for `closingIssuesReferences`
    - fallback `gh api repos/.../pulls/...`
    - warning `No standalone validation workflow could be dispatched`

- **Issue**
  - Multiple lookups are used just to determine target issues and dispatch validation.

- **Exact change**
  - Cache linked-issue resolution from the gate phase or merged PR event context.
  - Dispatch once with the final issue list rather than rediscovering it late.

- **Estimated reduction**
  - **2–6 calls per merged PR** and fewer false “no standalone validation” warnings.

---

#### 4. Copilot review still performs per-run artifact cleanup lookups
- **Evidence**
  - Deep audit cites repeated `gh api /repos/.../actions/runs/.../artifacts` in Copilot review runs.
  - Recent/sampled runs also show `github.rest.pulls.get` plus `github.paginate pulls.listFiles`.

- **Issue**
  - Cleanup and file enumeration are re-fetched each run.

- **Exact change**
  - Reuse artifact IDs and changed-file lists from prior job outputs where possible.
  - Skip artifact enumeration when no cleanup artifact was produced.

- **Estimated reduction**
  - **1–2 calls per Copilot review run**.

---

#### 5. E2E smoke test is polling and re-querying aggressively
- **Evidence**
  - `25324103531` e2e-smoke log repeatedly polls issue comments, labels, review-run state, and review jobs.
  - The deep audit identifies smoke/release polling as an API hotspot.

- **Issue**
  - The loop keeps querying even after it has enough evidence to know the path failed.

- **Exact change**
  - Fail immediately on:
    - PR already closed
    - review run has failed steps
    - missing expected labels after workflow completion
  - Back off polling once a run enters a terminal state.

- **Estimated reduction**
  - **~10–20 calls per release/smoke attempt**.

### Repository-specific API hygiene note
This repository already contains several thoughtful API-hygiene patterns:
- bounded retries
- GraphQL batching attempts
- rate-limit backoff helper

The main opportunity is **eliminating redundant fetches**, not emergency rate-limit mitigation. No `HTTP 429` or secondary-rate-limit event was visible in sampled deep dives.

## Prompt Cache & Memory System

### Prompt cache status
- **Configured on:** yes. `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears in implement/review flows.
- **Observable hit/miss quality:** **no**.
- **Concrete evidence**
  - `review_autofix` failures `25300219172` and `25324565713` both emitted cache probes like:
    - `cache_enabled=true`
    - `prompt_tokens=na`
    - `completion_tokens=na`
    - `total_tokens=na`
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`

### Assessment
- Prompt caching may be active, but current telemetry is too incomplete to tell:
  - whether cache entries are being created
  - whether reads are happening
  - whether cache prefixes are stable
  - whether any token savings are being realized

### Likely fragmentation causes
Evidence is incomplete, so this is bounded:
- `review_autofix` appears to rebuild support state, diffs, and runtime context repeatedly inside the same run.
- The review path carries many run-specific temp paths and per-run context artifacts.
- If any of that unstable material is incorporated early in the prompt prefix, cache reuse will fragment.

### Recommendations
1. **Emit real cache counters per model call.**
   - Required fields: prompt, completion, total, cache-create input, cache-read input, cache hit boolean.
2. **Add a stable prompt-prefix hash metric.**
   - This will show whether repeated review runs are actually sharing prefixes.
3. **Move volatile run-specific noise later in prompt assembly** where possible.
4. **Fix reviewer memory retrieval first.**
   - A zero-record memory path feeding a large reviewer prompt is all downside.

### Estimated impact
- **Tokens:** cannot quantify safely today due missing counters.
- **Latency:** likely moderate once repeated prefixes are stabilized.
- **Reliability:** improved observability alone is high value because it prevents blind cache tuning.

## Orchestrator Health

### What looks healthy
- `orchestrate_poll` success rate is currently **100%** in sampled aggregate (`30/30`).
- Poll ledger entries are consistent:
  - `poll_completed`
  - `status:"ok"`
  - `push_attempts:1`
  - `has_work:"false"` in recent no-work runs

### Pain points
#### 1. Poll cycles consume runners even when idle
- **Evidence**
  - Recent runs `25336849484`, `25338465666`, `25342258010`, `25347221704` all finished successfully with no work, but still spent ~45–51s and included runner wait + checkout/setup.

- **Smallest safe mitigation**
  - Gate checkout/setup on `has_work`.
  - Track `% poll cycles with has_work=false` and average duration of no-work polls.

#### 2. Implement/review flows still bounce through expensive loops before reaching terminal states
- **Evidence**
  - `implement` failure `25294005792` only terminated after repeated no-action attempts.
  - `review_autofix` failures `25300219172` / `25324565713` died late after setup, bootstrap, reviewers, and conflict-handling state.

- **Smallest safe mitigation**
  - Promote terminal fingerprints earlier.
  - Abort sooner on missing support files, ambiguity, or merge-conflict states that are already known to be unrecoverable in-run.

#### 3. Conflict resolution remains a weak point in review
- **Evidence**
  - Failed review runs end with `MERGE_CONFLICT: true` and `CONFLICT_RESOLVED: false`.
  - Both long failed reviews also show missing support assets around review-blocked/conflict infrastructure.

- **Smallest safe mitigation**
  - Separate “review produced findings” from “autofix/merge-conflict resolution failed” in status reporting.
  - If conflict-resolver prompt/support is absent, fail into comment-only mode instead of hard failing the whole job.

### Indicators to track
- `% orchestrate_poll runs with has_work=false`
- average no-work poll duration
- `% implement runs terminated by terminal fingerprint`
- `% review_autofix runs failing before editor vs during conflict resolution`
- memory `retrieve` 0-hit rate by role
- `push_attempts > 1` rate in memory telemetry

## Pipeline Flow Bottlenecks

### 1. Clarify → Plan → Implement
- **Queueing overhead**
  - Many runs are skipped quickly, but some “no-op/closed issue” runs still wait for runners. Example: `implement` run `25324345509` spent most of its 247s waiting, then skipped because the issue was not in the right phase.
- **Retry overhead**
  - Implement failures can still spend multiple attempts before conceding ambiguity or no-action output.
- **Recommendation**
  - Strengthen prechecks before acquiring expensive context and before entering Codex loops.

### 2. Implement → Review/Autofix
- **Dominant compute overhead**
  - `review_autofix` is the biggest compute + AI cost center.
  - Comment-only review paths still run six reviewers and repeated bootstrap/checkouts.
- **Merge/conflict overhead**
  - Failed runs show merge conflict unresolved after significant setup spend.
- **Recommendation**
  - Split comment-only review from autofix-capable review more aggressively.

### 3. Review/Autofix → Validate
- **Flow fragility**
  - Missing support files in review cause late failures after expensive work.
- **Recommendation**
  - Add hard preflight for support assets before reviewer/editor phases.

### 4. Release / Stable-marking loop
- **Largest end-to-end bottleneck**
  - `test_and_mark_stable` is entirely blocked by the smoke/merge race, causing full-run failures after ~40–55 minutes.
- **Recommendation**
  - Prioritize this before any micro-optimization elsewhere.

### 5. Poll / Orchestrate background loop
- **Runner occupancy bottleneck**
  - Frequent no-work polls still consume ~45–51s and full checkout steps.
- **Recommendation**
  - Make idle polls almost free.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` failures at Phase 4b after ~2,424–3,303s.
- `review_autofix` long-tail p95 of **1682.6s** with heavy reviewer/bootstrap overhead.
- `ci` consistently around **572–642s**, dominated by `lint`.
- `orchestrate_poll` spends ~45–51s even when no work exists.

**Top failure modes**
- Release smoke gate loses race with deterministic auto-merge.
- `review_autofix` fails on missing bootstrap/support assets.
- `implement` can get stuck in empty-output / ambiguity loops.
- Nightly validation self-test had `2/3` fixtures fail in the sampled run.

**Highest-cost drivers**
- Six-reviewer comment-only review paths.
- Analysis summarization (`workflow_log_analysis`) using ~171k–222k tokens per sampled run.
- Repeated CI on low-risk/docs-only changes.
- Idle poll cycles doing full checkout/fetch.

**Top 3 prioritized actions**
1. **Fix release smoke/auto-merge ordering** so `test_and_mark_stable` can succeed.
2. **Split cheap review paths from expensive autofix paths** and reduce reviewer fan-out on comment-only runs.
3. **Add path-aware CI gating and no-work poll short-circuiting** to reduce constant background spend.

## Metrics Appendix

### Repository summary

| Repository | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 294 | 9 | 43 | 654 | 0.9% | 119.9 | 1.0 | 632.0 |

### Key workflow families

| Workflow family | Runs | Success | Failure | Cancelled | Other | Failure rate | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `test_and_mark_stable` | 5 | 0 | 5 | 0 | 0 | 100.0% | 2865.2 | 2921.0 | 3283.2 |
| `review_autofix` | 78 | 37 | 2 | 38 | 1 | 2.56% | 443.6 | 49.0 | 1682.6 |
| `ci` | 65 | 65 | 0 | 0 | 0 | 0.0% | 611.8 | 611.0 | 647.8 |
| `orchestrate_poll` | 30 | 30 | 0 | 0 | 0 | 0.0% | 48.9 | 46.5 | 69.7 |
| `implement` | 174 | 20 | 1 | 5 | 148 | 0.57% | 23.4 | 1.0 | 177.4 |
| `plan` | 173 | 20 | 0 | 0 | 153 | 0.0% | 10.1 | 1.0 | 110.8 |
| `clarify` | 204 | 25 | 0 | 0 | 179 | 0.0% | 12.8 | 1.0 | 100.9 |
| `workflow_log_analysis` | 5 | 5 | 0 | 0 | 0 | 0.0% | 2489.4 | 2556.0 | 2904.0 |
| `nightly_validation_selftest` | 1 | 0 | 1 | 0 | 0 | 100.0% | 95.0 | 95.0 | 95.0 |

### Notable run-level timings

| Run ID | Workflow | Conclusion | Duration (s) | Dominant issue |
|---|---|---|---:|---|
| `25324103531` | `test_and_mark_stable` | failure | 3303 | smoke gate failed after PR auto-closed |
| `25310399716` | `test_and_mark_stable` | failure | 3204 | same failure mode |
| `25328431960` | `review_autofix` | success | 1682 | six-reviewer comment-only path |
| `25327045933` | `review_autofix` | success | 1686 | six-reviewer comment-only path |
| `25324565713` | `review_autofix` | failure | 925 | missing support assets / unresolved merge path |
| `25327045947` | `ci` | success | 611 | `lint` dominated runtime |
| `25347221704` | `orchestrate_poll` | success | 51 | no-work poll still checked out repo |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Retrieve count | 16 |
| Retrieve hit rate | 68.8% |
| Avg retrieve `estimated_tokens` | 19.2 |
| Max retrieve `estimated_tokens` | 28 |
| `keyword_method=plain` | 11 |
| `keyword_method=none` | 5 |
| `keyword_method=llm` | 0 |
| Retrieves with `fail_open:true` | 0 observed |
| Retrieves with `enabled:false` | 0 observed |
| Telemetry entries with `push_attempts > 1` | 2 observed |

### Workflow-log-analysis token usage observed

| Run ID | Operation | Model | Targeted runs | Summarized | Tokens used |
|---|---|---|---:|---:|---:|
| `25300062692` | `summarize_unselected_runs` | `openai/gpt-5.4-mini` | 100 | 76 | 170,953 |
| `25310429821` | `summarize_unselected_runs` | `openai/gpt-5.4-mini` | 100 | 98 | 221,799 |
| `25324145530` | `summarize_unselected_runs` | `openai/gpt-5.4-mini` | 100 | 95 | 189,772 |

### Prompt cache observability

| Workflow / Run | Cache enabled | Prompt tokens | Total tokens | Cache create tokens | Cache read tokens | Assessment |
|---|---|---|---|---|---|---|
| `review_autofix / 25300219172` | true | `na` | `na` | `na` | `na` | unusable for tuning |
| `review_autofix / 25324565713` | true | `na` | `na` | `na` | `na` | unusable for tuning |

### GH API summary from deep audit

| Pattern | Count | Files |
|---|---:|---:|
| `gh workflow run` | 138 | 11 |
| `gh api graphql` | 44 | 7 |
| `gh pr diff` | 33 | 3 |
| `gh api /repos` | 19 | 4 |
| `github.rest.pulls.get` | 3 | 2 |
| `github.paginate pulls.listFiles` | 1 | 1 |

If you want, I can turn this into a **prioritized implementation checklist** with owner, effort, and validation criteria.

## Deep Audit — Workflows & Scripts (2026-05-04)

### Section 1: Bug & Correctness Sweep

- **ID** — `BUG-001`  
  **File path** — `.github/workflows/review_autofix.yml:201-216`  
  **Severity** — High  
  **Category tag** — `bug`  
  **Description** — The gate step makes its only PR-state fetch with raw `gh api` at line 207, then immediately converts any empty result into `SHOULD_RUN=false` / `SKIP_REASON=pr_state_unknown` at lines 213-216. Because this fetch is not wrapped in `gh_retry` or `_safe_gh_jq`, a transient GitHub API failure, network hiccup, or rate-limit event can skip the entire review/autofix run for an otherwise-open PR. This is a correctness problem, not just observability noise, because the workflow exits the review path instead of retrying or failing loudly.  
  **Recommended fix** — Source `scripts/gh_helpers.sh` before the gate fetch and replace the raw call with one retry-aware fetch that returns all needed fields (`state`, `merged`, `head.ref`, labels, additions, deletions) in one response. If the fetch still fails after retries, fail the job explicitly or set a retryable sentinel rather than silently classifying the PR as `pr_state_unknown`.

- **ID** — `SEC-001`  
  **File path** — `scripts/run_validation_repo_checks.sh:14-23`  
  **Severity** — Medium  
  **Category tag** — `security`  
  **Description** — The script copies positional arguments directly into `CHECK_COMMANDS` and executes each entry via `/bin/sh -c "${check_cmd}"`. Any caller-supplied metacharacters (`;`, `&&`, `$()`, backticks, redirects) are therefore re-interpreted by the shell rather than treated as literal arguments. In the current repo the default commands are static, but the override path is generic and unvalidated, so this becomes a command-injection surface if any consumer-generated repo-check command is not fully trusted. [NEEDS VERIFICATION]  
  **Recommended fix** — Prefer an argv-based interface instead of shell strings: accept one command per invocation with explicit arguments, or accept only script paths plus fixed flags. If shell syntax must remain supported for backward compatibility, document the trust boundary and reject dangerous metacharacters before invoking `/bin/sh -c`.

### Section 2: GitHub API Call Redundancy Audit

> This section intentionally omits the already-documented `cancel_on_pr_close` and `issue_pr_status` hotspots from the in-progress report to avoid repeating the same call patterns.

- **ID** — `API-001`  
  **File path** — `scripts/orchestrate_poll_process.sh:3407-3468,3517-3519`  
  **Severity** — High  
  **Category tag** — `api-redundancy`  
  **Description** — `finalize_integration_merge_if_needed()` reads the same PR repeatedly from `repos/{repo}/pulls/{final_pr}`: two calls at lines 3411-3412 (`state`, `merged_at`), three more at lines 3466-3468 (`state`, `mergeable`, `merged_at`), and three more again at lines 3517-3519 after a failed merge attempt. That is **5 read calls before/around a normal finalize pass, and 8 if the merge attempt fails once**. Besides waste, those separate reads create a TOCTOU window where `state`, `mergeable`, and `merged_at` can come from different snapshots of the PR.  
  **Recommended fix** — Fetch the PR JSON once into a cycle-local cache/file, derive `state`, `mergeable`, and `merged_at` locally, and refresh it only once after an attempted merge. That reduces the path to **1 read call normally, 2 after a failed merge attempt**. Reuse the same cycle-local caching pattern this script already uses for `STALL_MANAGED_LINKED_PR_CACHE` and `_candidate_details_json`.

- **ID** — `BATCH-001`  
  **File path** — `.github/workflows/review_autofix.yml:478-530`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — `post-merge-validate-dispatch` starts with one GraphQL call for `closingIssuesReferences`, but when it falls back to PR body/title parsing it emits `issue_nodes_json` entries with `labels: null` and then does `gh issue view ... --json labels` inside the per-issue loop at lines 500-510. That turns the fallback path into **1 initial read + N per-issue REST reads** for N linked issues.  
  **Recommended fix** — After the regex fallback, batch-fetch labels for the discovered issue numbers in one GraphQL request and reuse that result in the loop, reducing the read path to **2 calls total regardless of N**. The cleanest existing pattern to extend is `_fetch_issue_labels_batch_graphql()` in `scripts/orchestrate_poll_process.sh`.

- **ID** — `API-002`  
  **File path** — `.github/workflows/review_autofix.yml:1381-1391,3750-3760,3871-3880,4605-4614`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The early `Collect PR metadata` step already caches linked issues into `LINKED_ISSUES_JSON` (lines 1381-1391), but three later steps independently rebuild fallback issue resolution by re-reading PR title/body and, if needed, calling `GET /repos/{repo}/pulls/{PR}` again. The repeated fallback blocks appear in `Mark linked issues ready to merge`, `Mark linked issues review-blocked`, and `Telegram success`. On a cache-miss path this is **up to 3 extra PR reads in one review run** for the same data.  
  **Recommended fix** — Persist one canonical fallback result during `Collect PR metadata`: either `LINKED_ISSUE_NUMBERS` directly or a precomputed fallback PR-text artifact. That reduces the tail path to **0 additional PR reads** instead of **up to 3**. Extend the existing early-cache pattern (`PR_META_FILE`, `LINKED_ISSUES_JSON`) rather than re-running the regex block in each tail step.

- **ID** — `BATCH-002`  
  **File path** — `scripts/review_rb_judge.sh:146-170`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The judge resolves linked issue numbers once with GraphQL, then loops over every linked issue and fetches each issue body via `_safe_gh_jq`. Only `FIRST_ISSUE_BODY` is retained, so when multiple issues are linked the script still performs **N issue-body reads while using only the first one**. Current path: **1 GraphQL call + N issue reads**.  
  **Recommended fix** — Either add `title`/`body` to the existing `closingIssuesReferences` GraphQL query, or break after fetching the first linked issue body. That brings the path down to **1 call total** (GraphQL with body) or **2 calls worst-case** (current GraphQL + one REST read). Reuse the `closingIssuesReferences { nodes { number title body } }` shape already implemented in `.github/workflows/review_autofix.yml:1366-1371`.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`  
  **File path** — `.github/workflows/review_autofix.yml:1254-1292; .github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/test-and-mark-stable.yml:396-429`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The repo has multiple inline `gh_retry` implementations with slightly different backoff, `/rate_limit` probing, stderr handling, and success/failure semantics, despite already shipping `scripts/gh_helpers.sh`. This is visible in `review_autofix`, `cancel_on_pr_close`, `test-and-mark-stable`, `mark-stable`, and several scripts with local fallback shims. The duplication makes API behavior drift-prone: one caller retries on generic failures, another waits on `/rate_limit`, another just defines `gh_retry() { "$@"; }`.  
  **Recommended fix** — Consolidate on `scripts/gh_helpers.sh` as the single owner. Keep the function surface as `gh_retry <command...>`, `gh_retry_to_file <outfile> <command...>`, and `gh_api_json_to_file <outfile> <endpoint...>`. Update callers in `review_autofix.yml`, `cancel_on_pr_close.yml`, `test-and-mark-stable.yml`, `implement.yml`, and `plan.yml` to source the helper instead of embedding local copies.

- **ID** — `DUP-002`  
  **File path** — `.github/workflows/review_autofix.yml:3752-3760,3873-3880,4607-4614; scripts/review_rb_judge.sh:153-156; .github/workflows/issue_pr_status.yml:195-210`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The repo repeats near-identical “resolve linked issues from PR body/title” regex logic across multiple workflows and scripts. The same repository-escaped grep pattern appears in three different review tail steps, the review-blocked judge, and `issue_pr_status`, with small semantic differences. That is brittle: any future fix to false positives/false negatives has to be applied in several places.  
  **Recommended fix** — Move this into a shared helper, e.g. `scripts/linked_issue_helpers.sh` with a function like `resolve_linked_issue_numbers <repo> <pr_number> <pr_meta_file>`. The helper should use GraphQL `closingIssuesReferences` first, then one regex fallback, and return a stable newline-delimited or JSON array result. Update callers in `review_autofix.yml`, `review_rb_judge.sh`, and `issue_pr_status.yml`.

- **ID** — `DUP-003`  
  **File path** — `.github/workflows/review_autofix.yml:3713-3747,3836-3868,4583-4599; .github/workflows/issue_pr_status.yml:239-249`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — `ensure_label_exists()` and `set_issue_phase_label_resilient()` are redefined inline in several late-stage workflow steps even though `scripts/label_helpers.sh` already provides the shared implementation. The repeated fallback bodies are similar but not identical, which increases the chance of label-handling drift between “ready-to-merge”, “review-blocked”, and PR-close paths.  
  **Recommended fix** — Make `scripts/label_helpers.sh` the sole owner of `ensure_label_exists <label> <repo>` and `set_issue_phase_label_resilient <issue_number> <target_label> <repo>`. Ensure the helper is always staged before late tail steps, or add one tiny bootstrap copy helper that restores `label_helpers.sh` if a prior cleanup step removed it.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`  
  **File path** — `.github/workflows/review_autofix.yml:1251-1573`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Collect PR metadata` `run:` block is an estimated **16,437 characters** in its current static body and contains multiple `${{ }}` interpolations, leaving only about **4,563 characters** of headroom against GitHub’s 21,000-character expression ceiling. This block already mixes retry helpers, PR metadata collection, linked-issue caching, comment aggregation, and diff capture, so normal feature growth can push it over the limit.  
  **Recommended fix** — Extract this step to `scripts/review_collect_pr_metadata.sh` and keep the workflow-side `run:` body to a thin wrapper that exports env and invokes the script.

- **ID** — `EXPR-002`  
  **File path** — `.github/workflows/validate.yml:183-476`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The support-bootstrap `run:` block in `validate.yml` is an estimated **16,485 characters**, leaving roughly **4,515 characters** of headroom. It contains many `${{ }}` references while embedding large fallback/copy logic inline, so adding even a few more supported files or rollout branches can recreate the same expression-limit failures already seen elsewhere in this repo.  
  **Recommended fix** — Move the support-fetch/bootstrap logic into a dedicated script such as `scripts/validate_fetch_support_files.sh`, or split the block into smaller steps (`resolve refs`, `copy scripts`, `copy prompts`, `copy schemas`) so each interpolated body stays well below the threshold.

- **ID** — `EXPR-003`  
  **File path** — `.github/workflows/orchestrate_clarify_respond.yml:813-1096`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The auto-answer / loop-guard `run:` block is an estimated **15,140 characters**, leaving about **5,860 characters** of headroom. It already combines memory claim logic, loop detection, Telegram escalation, label mutation, and comment posting in one interpolated shell body. This is below the hard limit today, but only by ~28%, and the block is still growing.  
  **Recommended fix** — Extract the loop-guard and answer-posting path into a script such as `scripts/orchestrate_clarify_answer.sh`, or split the step into separate `claim`, `guard`, `escalate`, and `post-answer` steps.

No workflow currently exceeds the 800 KB warning threshold for total file size; the largest workflow in this repo is `.github/workflows/review_autofix.yml` at 278,071 bytes.

### Section 5: Cross-Cutting Concerns

- **ID** — `DEAD-001`  
  **File path** — `scripts/orchestrate_poll_process.sh:4765-4772`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `read_standalone_state_json()` is defined but has no call sites in the repository. A repo-wide search only finds the definition, while writers route through `write_standalone_state_json()` and readers use other cached comment paths. That makes the function dead surface area in the hottest script in the repo.  
  **Recommended fix** — Remove the function if it is obsolete, or replace one of the existing ad hoc standalone-state readers with it so the helper becomes the canonical read path.

- **ID** — `CONSIST-001`  
  **File path** — `.github/workflows/forward-merge-stable-to-main.yml:320-346,374-395; .github/workflows/orchestrate_poll.yml:496-530`  
  **Severity** — Low  
  **Category tag** — `consistency`  
  **Description** — Three failure/alert steps use `set -uo pipefail` instead of the repo-standard `set -euo pipefail`. That means failed `source`, `curl`, `jq`, or helper calls in these steps do not trip `errexit` and can silently continue with partial state, even though surrounding steps in the same workflows use strict mode.  
  **Recommended fix** — Normalize these blocks to `set -euo pipefail`. If any command must fail open, keep strict mode and annotate that command with `|| true` plus a comment explaining why.

No literal `TODO`, `FIXME`, or `HACK` markers were found in the audited workflow/script files; the broad grep hits were false positives from `mktemp ...XXXXXX` patterns rather than debt annotations.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | `BUG-001`, `API-001` |
| Medium | 9 | `SEC-001`, `BATCH-001`, `API-002`, `BATCH-002`, `DUP-001`, `DUP-002`, `EXPR-001`, `EXPR-002`, `EXPR-003` |
| Low | 3 | `DUP-003`, `DEAD-001`, `CONSIST-001` |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 4 | Medium |
| Expression size reduction | 3 workflows (+ extracted scripts) | Large |
| Medium/Low fixes | 4 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-04)

### Safety Tag Legend
`SAFE_TO_MERGE` means the consolidation/elimination appears statically safe: same snapshot or already-available data, same step/path, and no visible retry/concurrency/error-semantic change. `NEEDS_VERIFICATION` means the overlap is real but at least one safety precondition cannot be proven from static reading alone. `RISKY_SKIP` means the waste is visible, but the call lives on a retry/race/recovery-sensitive path where this pass is not authorizing automatic removal.

### Consolidation Candidates (MERGE-###)

- **ID** — `MERGE-001`  
  **Safety tag** — `SAFE_TO_MERGE`  
  **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:372-377`  
  **Current call count** — 2  
  **Proposed call count** — 1  
  **Endpoint(s)** — `POST /repos/{owner}/{repo}/issues`; `GET /repos/{owner}/{repo}/issues/{issue_number}`  
  **Evidence** — The step creates the issue, then immediately re-fetches the same issue only to get `html_url`; the POST response already contains that field.
  ```bash
  # Create the issue and extract number in one call
  ISSUE_NUMBER=$(gh api "repos/${TEST_REPO}/issues" \
    -f title="${TITLE}" \
    -f body="${BODY}" \
    --jq '.number')

  ISSUE_URL=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '.html_url')
  ```
  **Proposed fix** — In the same step, change the create call to emit both `.number` and `.html_url` (for example as TSV or JSON), parse both into `ISSUE_NUMBER` and `ISSUE_URL`, and drop the follow-up GET.  
  **Safety rationale** — The two calls are back-to-back in the same step with no intervening mutation, and the create-issue response is a strict superset of the fields consumed by the second call.  
  **Downstream signal** — Replace the create step with one `gh api repos/.../issues` call that captures both `number` and `html_url`, then remove the immediate `GET /issues/{ISSUE_NUMBER}`.

### Redundant Re-Fetch (REUSE-###)

- **ID** — `REUSE-001`  
  **Safety tag** — `SAFE_TO_MERGE`  
  **File path and line ranges** — `.github/workflows/internal-review.yml:98-101`  
  **Current call count** — 2  
  **Proposed call count** — 1  
  **Endpoint(s)** — `GET /repos/{owner}/{repo}/pulls?state=open&head=...`; `GET /repos/{owner}/{repo}`  
  **Evidence** — The `resolve-claude-branch-pr` step uses one API call to find an open PR, then a second API call only to recover the repo default branch, even though this job runs only on `push` and the event payload already carries repository metadata.
  ```bash
  existing_pr="$(gh api \
    "repos/${REPOSITORY}/pulls?state=open&head=${REPOSITORY%/*}:${HEAD_REF}" \
    --jq '[.[] | .number] | first // empty' 2>/dev/null || echo "")"
  base_ref="$(gh api "repos/${REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo 'main')"
  ```
  **Proposed fix** — Keep the PR-existence lookup, but replace the repo metadata GET with `${{ github.event.repository.default_branch }}` (with the existing `main` fallback if desired) wired into `base_ref`.  
  **Safety rationale** — This step executes only on `push`, so the default branch value is already present in the immutable event payload and re-fetching it via GitHub API adds no freshness benefit.  
  **Downstream signal** — Remove the `gh api "repos/${REPOSITORY}"` call and source `base_ref` from `github.event.repository.default_branch` with `main` as the same fallback.

- **ID** — `REUSE-002`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/issue_pr_status.yml:295-330` and `.github/workflows/issue_pr_status.yml:503-512`  
  **Current call count** — classification path: `1` GraphQL call plus fallback `N` per-issue REST calls; alert path adds another up-to-`N` per-issue REST calls  
  **Proposed call count** — classification path unchanged; alert path reduced to `0` additional per-issue REST calls  
  **Endpoint(s)** — GraphQL `repository { issue(number) { number labels body } }`; `GET /repos/{owner}/{repo}/issues/{issue_number}`  
  **Evidence** — The sync step already batches labels and body to decide which linked issues are tracking/managed, then a later alert step re-fetches issue bodies again only to answer the same “Managed by: AI Orchestrator” question.
  ```bash
  ORCH_QUERY="query { repository(owner: \"${REPOSITORY%/*}\", name: \"${REPOSITORY#*/}\") {${ORCH_ALIAS_FRAGMENT} } }"
  ORCH_RESP="$(gh_retry gh api graphql -f query="${ORCH_QUERY}" 2>/dev/null || echo '')"
  ...
  _orch_meta="$(gh_retry gh api "repos/${REPOSITORY}/issues/${_orch_num}" --jq '{labels:[.labels[].name], body:(.body // "")}' 2>/dev/null || echo '')"
  ```
  ```bash
  IS_ORCHESTRATED="false"
  if [ -n "${LINKED_ISSUE_NUMBERS:-}" ]; then
    while IFS= read -r issue_number; do
      [ -n "${issue_number}" ] || continue
      BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
      if printf '%s' "${BODY}" | grep -qF 'Managed by: AI Orchestrator'; then
        IS_ORCHESTRATED="true"
        break
      fi
    done <<< "${LINKED_ISSUE_NUMBERS}"
  fi
  ```
  **Proposed fix** — In the classification step, persist either `MANAGED_ISSUES`, `TRACKING_ISSUES`, or a boolean like `HAS_MANAGED_LINKED_ISSUE` to `GITHUB_ENV`/step outputs; update the merged-alert step to read that cached result instead of re-fetching issue bodies.  
  **Safety rationale** — The overlap is clear, but static review cannot fully prove that the later alert step's behavior remains identical across GraphQL-batch failure, branch-derived issue augmentation, and post-label-mutation paths.  
  **Downstream signal** — Before merging this change, run fixtures for: (1) standalone issue PR, (2) orchestrator-managed child issue PR, (3) mixed child+tracking references, and (4) forced GraphQL-batch fallback; confirm alert suppression and label/close behavior stay unchanged while the late per-issue body GETs disappear.

### Dead Calls (DEAD-API-###)

- **ID** — `DEAD-API-001`  
  **Safety tag** — `SAFE_TO_MERGE`  
  **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:1508-1511`  
  **Current call count** — 1  
  **Proposed call count** — 0  
  **Endpoint(s)** — `GET /repos/{owner}/{repo}/commits?sha={branch}&per_page=20`  
  **Evidence** — `COMMITS_AFTER` is assigned from the commits-list API but never consumed; the actual pass/fail check uses only `PR_HEAD`.
  ```bash
  # Confirm the editor pushed at least one commit on top of the bait.
  COMMITS_AFTER=$(gh api "repos/${TEST_REPO}/commits?sha=${BRANCH}&per_page=20" \
    --jq "[.[] | select(.sha != \"${BAIT_SHA}\") | .sha] | length" 2>/dev/null || echo "0")
  # The PR head SHA should differ from the bait SHA.
  PR_HEAD=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")
  if [ "${PR_HEAD}" = "${BAIT_SHA}" ]; then
  ```
  **Proposed fix** — Delete the `COMMITS_AFTER` fetch and keep the existing `PR_HEAD != BAIT_SHA` assertion as the sole editor-pushed-commit guard.  
  **Safety rationale** — The fetched result is dead and the observable decision path already relies entirely on the subsequent PR-head check.  
  **Downstream signal** — Remove the unused `COMMITS_AFTER=$(gh api ... /commits ...)` assignment and leave the `PR_HEAD` comparison unchanged.

- **ID** — `DEAD-API-002`  
  **Safety tag** — `RISKY_SKIP`  
  **File path and line ranges** — `scripts/orchestrate_poll_process.sh:11382-11465` (dead fetch at `11392`)  
  **Current call count** — 1  
  **Proposed call count** — 0  
  **Endpoint(s)** — `GET /repos/{owner}/{repo}`  
  **Evidence** — The standalone conflict sweep fetches `DEFAULT_BRANCH`, but that variable is not referenced anywhere in the sweep body that follows.
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

  for (( sidx=0; sidx<STANDALONE_COUNT; sidx++ )); do
    ...
  done
  ```
  **Proposed fix** — If manual review confirms there is no hidden consumer, remove the `DEFAULT_BRANCH` fetch from this sweep.  
  **Safety rationale** — Even though the call appears dead, it sits inside `orchestrate_poll_process.sh`, which is explicitly a race-/recovery-sensitive path that this pass must not auto-edit.  
  **Downstream signal** — Do not auto-implement; manual review must prove `${DEFAULT_BRANCH}` has no downstream read in this sweep and that removing the fetch does not change any operator-visible logs or future shell-global behavior.

### Cross-References to Deep Audit Section

- `API-001`: `RISKY_SKIP` — Agreed; redundancy is real, but this is in `scripts/orchestrate_poll_process.sh` final-merge recovery, so any cache/refetch rewrite must be manually validated against race-handling and post-merge refresh semantics.
- `BATCH-001`: `NEEDS_VERIFICATION` — Agreed; batch-label fetch is the right direction, and the implement stage should also check whether the fallback can reuse already-supplied PR title/body before re-fetching `/pulls/{PR}`.
- `API-002`: `NEEDS_VERIFICATION` — Agreed; the late-stage refetches are redundant, but the implement stage must verify `PR_META_FILE`/`LINKED_ISSUES_JSON` propagation across all skipped and failure tails before removing the extra PR reads.
- `BATCH-002`: `NEEDS_VERIFICATION` — Agreed; only `FIRST_ISSUE_BODY` is consumed, but a human should confirm judge prompt quality is unchanged if non-first linked-issue body fetches are eliminated.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 3 | `MERGE-001`, `REUSE-001`, `DEAD-API-001` |
| NEEDS_VERIFICATION | 1 | `REUSE-002` |
| RISKY_SKIP | 1 | `DEAD-API-002` |

### Implement-Stage Handoff

- `REUSE-001`
- `MERGE-001`
- `DEAD-API-001`
