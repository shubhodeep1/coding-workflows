## Executive Summary

- **`review_autofix` is the dominant end-to-end drag and cost sink.** Recent successful runs `25276251365` (1,388s), `25276795302` (1,794s), `25278175531` (1,898s), `25279043495` (1,940s), and `25280032638` (1,597s) were dominated by `review / codex-agent (claude-branch-review)` on tiny PRs (3–5 files, 12–17 additions in the sampled summaries). Tightening the small-diff/comment-only path should save **15–25 minutes per affected run**. **Confidence: high**

- **Cancelled review runs are wasting substantial tokens and runner time.** Workflow-family telemetry shows `review_autofix` had **82 runs, 47 cancelled, 34 success**. The deep-audit workflow log (`25265928747`) explicitly reports cancelled run `25265763764` consumed **1,781,558 total tokens** (`1,747,212` input / `34,346` output) before cancellation after **533s**. Reducing superseded or low-value review fan-out is the highest cost win. **Confidence: medium** (token figure is quoted from the deep-audit log, not directly from the original run log)

- **Two hard failures are straightforward to remove:** run `25273372573` failed because `git push -f origin stable` hit `error: src refspec stable matches more than one`, and run `25272034874` failed because workflow support assets were missing (`.codex-workflow-src`, `prompts/serena-efficiency-block.txt`, `prompts/mode-implement-diagnose.txt`). These are low-risk fixes with immediate reliability payoff. **Confidence: high**

- **`test_and_mark_stable` is over-polling the GitHub API and paying for it in delay and 429/rate-limit churn.** Deep-dive logs show `25265920645` made **41 `gh api` calls** in `e2e-smoke-test` with **40 429-like/rate-limit-related hits**, and `25273372573` made **65 `gh api` calls** with **23 429-like hits** in the same phase. Batching and reusing run/PR metadata should reduce both latency and flakiness. **Confidence: high**

- **The orchestrator is generating lots of skip-only traffic.** Family totals show `clarify` had **151 runs / 20 success / 131 other**, `plan` had **126 / 16 / 110**, `orchestrate_clarify_respond` had **126 / 4 / 122**, and sampled `orchestrate_poll` runs (`25280685897`, `25279852853`, `25278662855`, `25278263198`) all completed with **no work** while still doing full checkout. Earlier gating can reduce queue load and noise. **Confidence: high**

## Speed Optimizations

### 1. Shrink the `review_autofix` small-diff / comment-only critical path
**Evidence**
- `review_autofix` p95 is **1,892.8s** across the family.
- Recent long runs: `25276251365` (1,388s), `25276795302` (1,794s), `25278175531` (1,898s), `25279043495` (1,940s), `25280032638` (1,597s).
- In the sampled logs, these were all dominated by `review / codex-agent (claude-branch-review)`.
- Sampled gate summaries show very small diffs still triggering the full path: 3–5 files, 12–17 additions.

**Root cause**
- The pipeline is sending tiny PRs through a heavyweight multi-reviewer + summariser path even when the branch mode is comment-only and editor/commit/judge are already skipped.

**Exact change**
- For `claude_branch_review` + small diffs, add a reduced path:
  - Use **1 fast reviewer model** instead of the full panel.
  - Lower `CHECK_RUNS_WAIT_TIMEOUT_SECS` from `1200` to `300` for comment-only mode.
  - Bypass XPOLL summarisation when only one reviewer is used.
  - Preserve full behavior behind an override label like `force-review`.

**Estimated time savings**
- **900–1,500s per affected run**.

**Implementation risk**
- **Medium.** Keep a manual override and restrict the optimization to low-change comment-only runs.

**Critical-path status**
- **Critical-path win.**

---

### 2. Replace repeated E2E polling with cached state snapshots in `test_and_mark_stable`
**Evidence**
- `test_and_mark_stable` runs are all failing and very slow: `25254380200` (6,049s), `25265920645` (5,858s), `25271960656` (3,676s), `25273372573` (3,235s).
- `25265920645` `e2e-smoke-test`:
  - **41 `gh api` calls**
  - repeated polling of `actions/runs`, `pulls`, labels, and jobs
  - frequent rate-limit handling and 429-like signals
- `25271960656` failed at `Phase 3b: Wait for PR creation (implement phase)` after the plan completed but labels were wrong:
  - `::error::Plan workflow completed but issue lacks expected labels`

**Root cause**
- The test workflow discovers the same PR/run/job state over and over instead of carrying forward known IDs and snapshots.
- It also uses issue labels as phase truth, which is eventually consistent and brittle under delay.

**Exact change**
- Persist `ISSUE_NUMBER`, `PR_NUMBER`, workflow run IDs, and phase outputs once they are known.
- On each polling loop, fetch one JSON snapshot and derive status/head/labels from that file rather than firing multiple `gh api` calls.
- Prefer phase artifacts or explicit success outputs over label-only checks.
- Add a shorter smoke-specific timeout around bait verification/review completion.

**Estimated time savings**
- **1–3 minutes on passing runs**, **10–25 minutes on failing runs**.

**Implementation risk**
- **Low to medium.** Behavior stays the same; state handling gets less redundant.

**Critical-path status**
- **Critical-path win.**

---

### 3. Make `orchestrate_poll` shallow by default
**Evidence**
- Recent poll runs `25280685897` (51s), `25281367715` (39s), `25279852853` (43s) all used `actions/checkout@v5` with `fetch-depth: 0`.
- `25280685897` shows a full ref/tag fetch while the run later records `poll_completed` with no actionable work.

**Root cause**
- The poller is paying full-history checkout cost even on no-op cycles.

**Exact change**
- Change the main poll checkout to:
  - `fetch-depth: 1`
  - `fetch-tags: false`
- Deepen only in branches of logic that actually need history or tag traversal.

**Estimated time savings**
- **8–15s per poll cycle**.

**Implementation risk**
- **Low.** Add a conditional deepen where needed.

**Critical-path status**
- **Moderate critical-path win** because poll runs are frequent.

---

### 4. Collapse runner-separated jobs in `copilot_pull_request_reviewer`
**Evidence**
- `25279779661` (308s), `25278891205` (270s), and `25276755456` (282s) all show queue/wait on multiple jobs: `Prepare`, `Upload results`, `Cleanup artifacts`.
- Logged work inside those jobs is small; runner acquisition dominates.

**Root cause**
- Lightweight tasks are split across multiple jobs, paying runner-queue overhead multiple times.

**Exact change**
- Merge `Prepare`, artifact cleanup, and upload into one job where artifact dependencies permit.
- Use `if: always()` for end-of-job cleanup instead of a separate cleanup job.

**Estimated time savings**
- **60–180s per run**.

**Implementation risk**
- **Low.**

**Critical-path status**
- **Moderate critical-path win.**

---

### 5. Parallelize or split the 9–10 minute `ci` lint path
**Evidence**
- `ci` p50 is **605s**, p95 **651.2s**.
- Repeated successful runs: `25276251327` (602s), `25277992079` (600s), `25279043453` (620s), `25279779160` (616s), `25280032591` (548s).
- The `lint` step dominates nearly the entire run.

**Root cause**
- One serialized job is carrying unit tests, workflow checks, and prompt/contract checks.

**Exact change**
- Split `lint` into at least two jobs:
  - fast contract/workflow validation
  - heavier unit/integration tests
- If test isolation is safe, enable parallel test execution for Python tests.

**Estimated time savings**
- **120–240s per CI run**.

**Implementation risk**
- **Medium.** Some tests may share filesystem or order assumptions.

**Critical-path status**
- **Critical-path win.**

## Cost Optimizations

### 1. Stop paying reviewer-model costs on runs that are likely to be cancelled or superseded
**Evidence**
- `review_autofix`: **82 total**, **47 cancelled**, **34 success**.
- Deep-audit run `25265928747` reports cancelled run `25265763764` spent **1,781,558 total tokens** before cancellation.
- Many cancelled review runs still lasted hundreds of seconds: `25276129771` (406s), `25278890786` (449s), `25277992150` (538s), `25279779213` (743s).

**Root cause**
- Full review work starts before the system knows whether the run will survive synchronization/cancellation churn.

**Exact change**
- Add a pre-review “superseded-run” check right before reviewer fan-out.
- For `claude/**` review mode, require a short quiet window on the PR head SHA before launching expensive review work.
- Skip full multi-model review for tiny comment-only updates.

**Estimated savings**
- **Largest cost lever in the dataset**; likely **50%+ of wasted review tokens** on cancelled runs.

**Quality-risk notes**
- Low if full review remains available on final/stable head SHAs and via override label.

---

### 2. Reduce reviewer panel width and XPOLL context for tiny diffs
**Evidence**
- `REVIEWER_MODELS` is multi-model in current review workflow.
- `review_autofix.yml` sets:
  - `XPOLL_SUMMARISER_LINES_PER_REVIEWER: 160`
  - `XPOLL_SUMMARISER_MAX_INPUT_LINES: 3000`
- Long review runs are occurring on very small PRs.

**Root cause**
- Expensive panel/summarisation settings are not scaled to diff size.

**Exact change**
- Add diff-size tiers:
  - **tiny diff**: 1 reviewer, no XPOLL summariser
  - **small diff**: 2 reviewers, lower max lines
  - **normal diff**: current behavior
- Lower `XPOLL_SUMMARISER_MAX_INPUT_LINES` for comment-only path.

**Estimated savings**
- **30–60% token reduction** on small-diff review runs.

**Quality-risk notes**
- Moderate; use overrides for risky areas such as workflow files, prompts, or infra-adjacent changes.

---

### 3. Fix prompt-cache fragmentation before adding more cache probes
**Evidence**
- `OPENROUTER_PROMPT_CACHE_DISABLED: false` is present in implement/review/poll sampled runs.
- Review logs show cache probes:
  - `INFO: openrouter usage phase=review_autofix_cache_probe ... cache_enabled=true`
  - but `cache_creation_input_tokens=na` and `cache_read_input_tokens=na`
- No direct evidence of useful prompt-cache reads in the expensive reviewer/editor calls.

**Root cause**
- Cache is enabled, but prompts likely vary too much run-to-run due to dynamic run IDs, temp paths, SHA values, and repeated environment dumps.

**Exact change**
- Make prompt prefixes stable:
  - keep policy/instructions first
  - move run-specific metadata and temp paths to the end
  - canonicalize field ordering
  - avoid embedding volatile diagnostics in the shared prefix
- Emit cache creation/read counters on the real model calls, not just probes.

**Estimated savings**
- **10–30% token and latency reduction** on repetitive review flows once hits materialize.

**Quality-risk notes**
- Low; content is unchanged, only organized for cacheability.

---

### 4. Downgrade no-work orchestrator polling to a cheaper model path
**Evidence**
- Sampled no-work poll runs still expose `MODEL_EDITOR: openai/gpt-5.4`.
- Recent sampled poll runs with visible outcome all ended with `has_work=false`.

**Root cause**
- No-work cycles appear to carry full model configuration even though most cycles do not require substantive reasoning.

**Exact change**
- Use rule-based/no-LLM prefiltering for obvious no-work cycles.
- If a model is still needed, switch no-work summarisation/judging to a mini model.

**Estimated savings**
- Likely modest per run, but meaningful due to poll frequency.

**Quality-risk notes**
- **Low to medium** because this depends on exactly where model calls occur; instrument first.

---

### 5. Put a budget cap on `workflow_log_analysis` deep-audit breadth during clean windows
**Evidence**
- `workflow_log_analysis` runs are themselves very slow: `25254390226` (5,641s), `25265928747` (5,476s), `25271970949` (3,290s).
- API-heavy steps inside those runs show large call counts.

**Root cause**
- The analysis workflow is expensive even when it is not directly unblocking PR flow.

**Exact change**
- Keep current deep audits for failing/slow runs, but reduce success-run expansion when the window has few failures/regressions.
- Preserve the current `log_summary` widening strategy for breadth.

**Estimated savings**
- **20–40 minutes** on clean analysis runs.

**Quality-risk notes**
- Low if deep-dive thresholds remain unchanged for failures/outliers.

## Reliability Improvements

### 1. Fix ambiguous `stable` ref pushes in the release path
**Failure evidence**
- Run `25273372573` failed in `release / Tag version and update stable pointer`.
- Exact log:
  - `Updated tag 'stable'`
  - `error: src refspec stable matches more than one`
  - `error: failed to push some refs`

**Root cause category**
- Git ref ambiguity between branch and tag names.

**Exact fix**
- Push fully-qualified refs:
  - `refs/tags/stable:refs/tags/stable` if `stable` is intended to be a tag
  - or `refs/heads/stable:refs/heads/stable` if it is intended to be a branch
- Do the same for any moving major pointer tags.

**Expected reliability impact**
- Removes a deterministic hard failure in the release lane.

**Rollback / fail-open**
- Low-risk; qualification only clarifies intent.

---

### 2. Extend support-source validation to cover prompt assets used at runtime
**Failure evidence**
- Run `25272034874` failed in `implement / Run Codex implementation`.
- Logs show:
  - `Failed to checkout workflow support source from ${SCRIPT_REF} or main`
  - `Failed to stage required file prompts/serena-efficiency-block.txt`
  - `Failed to stage required file prompts/mode-implement-diagnose.txt`

**Root cause category**
- Incomplete packaging/reference validation for workflow support files.

**Exact fix**
- Extend `check_workflow_script_refs.py` (or equivalent CI validation) to include:
  - prompt assets
  - support-source manifests
  - all runtime-staged files, not only scripts and workflow refs
- Fail CI before merge if any staged dependency is missing.

**Expected reliability impact**
- Prevents runtime failures caused by uncommitted or misreferenced support assets.

**Rollback / fail-open**
- Keep fallback-to-main behavior where intended, but fail merge if both primary and fallback assets are missing.

---

### 3. Make phase success depend on explicit phase outputs, not just issue labels
**Failure evidence**
- Run `25271960656` failed at `Phase 3b: Wait for PR creation (implement phase)`.
- The log shows:
  - `Plan workflow completed but issue lacks expected labels (current: ${LABELS_RECHECK:-none})`

**Root cause category**
- Eventual-consistency / state-contract mismatch between workflow completion and label propagation.

**Exact fix**
- Have clarify/plan/implement write explicit phase outputs/artifacts that downstream phases consume.
- Use labels as advisory state, not the only success criterion.
- Add a short propagation retry before declaring failure.

**Expected reliability impact**
- Reduces false negatives in E2E and orchestrator transitions.

**Rollback / fail-open**
- Keep label checks as a warning path during rollout.

---

### 4. Stabilize prompt/fixture contract tests that are drifting independently
**Failure evidence**
- `25267881013` failed with:
  - `AssertionError: 'Test script contract (MANDATORY):' not found`
- Multiple failing `ci` runs also show:
  - `Integration fingerprint verification FAILED — resolver output regressed merged sub-issue intent`

**Root cause category**
- Contract drift between prompts, tests, and resolver-output expectations.

**Exact fix**
- Generate prompt-contract assertions from the canonical prompt source or add golden-file snapshots updated in the same PR.
- Separate “expected intentional prompt change” updates from unrelated functional PRs.

**Expected reliability impact**
- Should remove repeated red builds on prompt-only or wording changes.

**Rollback / fail-open**
- Keep strict tests, but reduce brittleness by testing semantic markers instead of exact prose blocks where possible.

---

### 5. Improve nightly self-test observability before treating it as a hard gate
**Failure evidence**
- `25268666198` failed with:
  - `validation-selftest: fixtures=3 passed=1 failed=2`

**Root cause category**
- Insufficient surfaced failure detail.

**Exact fix**
- Echo the failing fixture names and reasons into the main job summary.
- Keep uploading the artifact, but make the console output directly actionable.

**Expected reliability impact**
- Faster repair turnaround; lower mean time to diagnose.

**Rollback / fail-open**
- No behavior change needed to the underlying tests.

## AI Memory Health

- **Telemetry is present but unevenly distributed.** Deep-dive logs show telemetry in `implement`, `review_autofix`, `orchestrate_poll`, and `workflow_log_analysis`, but many sampled `ci` and lightweight workflows had no `AI_MEMORY_TELEMETRY` lines at all.

- **Observed operation mix from inspected deep-dive logs**
  - `record-run-event`: 25
  - `retrieve`: 7
  - `record-candidate`: 5
  - `summarize_unselected_runs`: 4
  - `processed-command-check`: 2
  - `processed-command-claim`: 2

- **Retrieve effectiveness is weak in reviewer flows.**
  - Observed `retrieve` count: **7**
  - Retrieves with `records_selected > 0`: **2**
  - **Hit rate: 28.6%**
  - Average `estimated_tokens`: **16**
  - `keyword_method` distribution:
    - `none`: **5**
    - `plain`: **2**
    - `llm`: **0**

- **Zero-hit retrieves are concentrated in `review_autofix`.**
  - Zero-record reviewer retrieves appeared in:
    - `25254574828`
    - `25267058904`
    - `25268065004`
    - `25280032638` (twice, including the fail-open retrieval step file)
  - That suggests reviewer memory lookup keys/roles are too weak or too narrowly matched.

- **Positive retrieves were seen in implementation-oriented flows.**
  - `25272034874` (`implement`): `records_selected: 2`, `estimated_tokens: 56`, `keyword_method: plain`
  - `25265928747` (`workflow_log_analysis` implementation-side retrieval): same pattern

- **No bad fail-open telemetry was observed in sampled JSON lines.**
  - No sampled `retrieve` entry had `fail_open: true`
  - No sampled entry had `enabled: false`
  - Observed `push_attempts` were consistently **1** in the sampled `record-run-event` and `record-candidate` entries

**Recommendation**
- Improve reviewer-memory retrieval keys first:
  - include PR type / workflow family / changed-file classes
  - promote successful `review_autofix` candidates with stronger reviewer-role tags
  - emit telemetry in `ci` and `test_and_mark_stable` too, so memory health can be measured outside implement/review

## GH API Call Audit

### Highest-volume patterns

1. **`test_and_mark_stable` E2E polling**
   - `25265920645` `step-005-e2e-smoke-test.log`: **41 `gh api` calls**, **40 429-like hits**
   - `25273372573` `step-011-e2e-smoke-test.log`: **65 `gh api` calls**, **23 429-like hits**
   - `25271960656` `step-005-e2e-smoke-test.log`: **20 `gh api` calls**
   - Common patterns:
     - repeated `actions/runs/{id}` polling
     - repeated `actions/runs/{id}/jobs`
     - repeated PR head/label/issue queries
     - `/rate_limit` checks inside wrappers

2. **`review_autofix` review agent**
   - `25254574828` `step-001-review_codex-agent.log`: **81 `gh api` calls**
   - `25267058904` `step-001-review_codex-agent_claude-branch-review.log`: **11 `gh api` calls**
   - Includes repeated PR metadata, linked-issue GraphQL, file pagination, and `/rate_limit` handling.

3. **`workflow_log_analysis` analysis jobs**
   - `25254390226` `step-001-api-redundancy.log`: **138 `gh api` calls**
   - `25265928747` `step-001-api-redundancy.log`: **80 `gh api` calls**
   - These are not user-facing critical path, but they create API pressure.

4. **`cancel_on_pr_close`**
   - `25281865088` checks `/rate_limit` before cancel/list calls even for a 6-second no-op case.

5. **`copilot_pull_request_reviewer`**
   - `Prepare` uses `github.rest.pulls.get` plus paginated `pulls.listFiles`
   - `Cleanup artifacts` separately calls the run-artifacts endpoint
   - Not huge individually, but duplicated across runner-separated jobs

### Redundancy findings

- **Per-item polling loops are the main problem.**
  - `test_and_mark_stable` repeatedly asks for the same run/PR state instead of caching a snapshot per loop.
- **Rate-limit probes are too eager.**
  - `/rate_limit` is being queried inside wrappers rather than only after a 403/429 response.
- **PR metadata is fetched multiple ways in the same workflow.**
  - REST pull lookup + file pagination + GraphQL linked-issue lookup + later issue/label lookups.

### Concrete batching/reuse changes

- Build and reuse `pr_meta.json` once per run with:
  - PR state
  - head/base refs and SHA
  - labels
  - additions/deletions
  - linked issues
- Build and reuse `run_meta.json` once per polling loop with:
  - run status/conclusion
  - in-progress job names
  - latest step names
- Replace preflight `/rate_limit` calls with:
  - only query reset headers after an actual rate-limit failure
- In E2E, stop rediscovering:
  - the created issue number
  - PR number
  - child workflow run IDs
  - tracking issue number

### Estimated impact

- `test_and_mark_stable`: **20–35 fewer API calls per run**
- `review_autofix`: **25–40 fewer API calls per run** on heavier paths
- `cancel_on_pr_close`: **25–50% fewer calls**
- Secondary effect: lower chance of 403/429/backoff-induced latency spikes

### Repo-specific API hygiene cross-check

The implementation logs themselves already encode the right rule:
- “Cycle-local caches are first-class”
- “Prefetch once into a shell/file cache”

The workflows are not consistently following that rule yet.

## MCP & Serena Efficiency

### What the logs show

- Serena setup is successful in the sampled implement/review runs:
  - `25272034874` shows Serena cache warm, startup validation, and config hardening succeeded.
- But actual tool usage is still churny in the long review path.
  - In `25254574828`, the log records **repeated `serena.activate_project` calls** (parsed count: 24; clearly more than once per task).
  - An internal deep-audit line in the same log family reports top Serena tools as:
    - `sh` (21)
    - `activate_project` (12)
    - `onboarding` (12)
    - `search_for_pattern` (8)
    - `find_symbol` (6)

### Efficiency issues

1. **Repeated project activation**
   - `activate_project` is being called many times in a single review run.

2. **Onboarding/tool churn**
   - The deep-audit output explicitly shows `onboarding` usage despite the intended pattern being “activate once, no onboarding”.

3. **Shell-heavy behavior remains common**
   - `sh` dominates the Serena tool list in the deep audit, which means the flow is still leaning on shell commands instead of targeted symbol/search tools.

4. **Git MCP opportunity is underused**
   - The logs include broad raw git context dumps (`git status`, `git diff --stat`, runtime context files), even though the workflow guidance prefers targeted Git MCP queries.

### Concrete recommendations

- Enforce a single `activate_project` call per Codex task/session.
- Hard-block onboarding calls in the review/implement prompt templates.
- Prefer:
  - `get_symbols_overview`
  - `find_symbol`
  - `find_referencing_symbols`
  - `search_for_pattern`
- In review flows, prefer Git MCP:
  - `git_status`
  - `git_diff`
  - `git_show`
  - `git_log`
  - `git_branch`
- Remove prompt examples that encourage raw `cat runtime_context/...` usage when structured MCP alternatives exist.

### Estimated impact

- **5–15% lower turnaround** in long Codex review/edit loops
- **Token savings** from fewer broad shell/file reads
- Lower risk of duplicate file-region reads and repeated setup chatter

## Prompt Cache & Memory System

### Prompt cache observations

- Prompt cache is generally **enabled** in sampled workflows:
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` is present in implement, review, and poll logs.
- However, the useful cache evidence is weak:
  - review cache-probe lines show `cache_enabled=true`
  - but `cache_creation_input_tokens=na` and `cache_read_input_tokens=na`
- That means the current logs do **not** prove that the expensive reviewer/editor calls are getting real prompt-cache hits.

### Likely cache-fragmentation causes

- Volatile prompt prefixes:
  - run IDs
  - temp file paths
  - dynamic SHA values
  - changing diagnostic blocks
  - repeated environment dumps
- Long review flows appear to restate a lot of stable policy mixed with run-specific noise.

### Memory system observations

- Retrieval works better for implementation than for review.
- Reviewer-side memory lookups are often `keyword_method: none` with zero selected records.

### Concrete improvements

1. **Stabilize prompt prefixes**
   - Keep invariant policy text first.
   - Move dynamic run metadata to the end or to external files.

2. **Emit real cache metrics on actual model calls**
   - Add `cache_creation_input_tokens` and `cache_read_input_tokens` for reviewer/editor requests, not just probes.

3. **Reduce prompt variance**
   - Canonicalize ordering of sections and metadata.
   - Avoid embedding runner diagnostics and support-source bootstrap noise into prompts.

4. **Align memory taxonomies**
   - Store and retrieve reviewer memories under the same role/category scheme used by `review_autofix`.

### Estimated impact

- **Latency:** 10–30% lower on repetitive review loops once cache hits stabilize
- **Tokens:** material reduction on repeated PR-review retries
- **Reliability:** fewer provider retries and lower chance of oversized-context regressions

## Orchestrator Health

### Observed health signals

- **High skip-only fan-out**
  - `clarify`: 151 total, 20 success, 131 other
  - `plan`: 126 total, 16 success, 110 other
  - `orchestrate_clarify_respond`: 126 total, 4 success, 122 other
  - `implement`: 126 total, 15 success, 105 other, 5 cancelled, 1 failed
- **Poll loop often finds no work**
  - sampled recent poll runs repeatedly end with `has_work=false`
- **Review path is cancellation-prone**
  - `review_autofix` cancelled **47** times out of **82**
- **Phase-contract brittleness exists**
  - E2E plan/label mismatch in `25271960656`
- **Support-source fallback noise exists**
  - recent status sync run `25281865084` warned that the support checkout ref was unavailable and fell back to `main`

### Smallest safe mitigations

- Add earlier parent-level gating so child reusable workflows are not dispatched unless the comment/event can actually match.
- Add a cheap “orchestrator-managed?” precheck before fanning out clarify/plan/respond/implement.
- Debounce review dispatch slightly on synchronize-heavy PRs so superseded runs do not start expensive work.
- Record and alert on:
  - skip-only run ratio
  - cancelled review ratio
  - `CHECK_RUNS_WAIT_TIMEOUT` occurrences
  - support-source fallback count
  - label-mismatch count
  - no-work poll ratio

### Trackable indicators

- `% skipped runs by family`
- `% cancelled review_autofix runs`
- `orchestrate_poll has_work=false rate`
- count of `CHECK_RUNS_WAIT_TIMEOUT` warnings
- count of `support checkout ref unavailable; using main`
- count of `records_selected=0` reviewer-memory retrieves

## Pipeline Flow Bottlenecks

### 1. Review/autofix loop
- **Dominant bottleneck**
- Compute-heavy and cancellation-prone
- Longest recurring user-facing path
- Main issues:
  - over-review of tiny diffs
  - wasted cancelled runs
  - repeated API/tool setup
  - weak reviewer memory hit rate

**Fix first:** small-diff branch, pre-fan-out supersede check, narrower reviewer panel

---

### 2. Test & mark stable E2E path
- **Second biggest bottleneck**
- Fails slowly and repeatedly
- Main issues:
  - repeated GH API polling
  - label-based state checks
  - review bait verification waiting too long
  - rate-limit sensitivity

**Fix second:** explicit phase outputs + batched polling + shorter smoke-specific timeouts

---

### 3. CI validation
- Stable but slow; some failures are contract drift rather than real runtime breakage
- Main issues:
  - one big serialized lint job
  - prompt/test drift
  - integration fingerprint regressions surfaced late in the run

**Fix third:** split fast contract checks from slower suites

---

### 4. Orchestrate poll / clarify / plan / respond fan-out
- High event noise, low value density
- Main issues:
  - many skip-only runs
  - no-work poll cycles still doing full checkout

**Fix fourth:** gate earlier and make no-work polls cheap

---

### 5. Copilot reviewer and analysis side workflows
- Mostly queue/runner overhead rather than compute
- Main issues:
  - multi-job runner waits
  - non-critical analysis workflows consuming large time and API budgets

**Fix fifth:** merge lightweight jobs and trim analysis breadth on clean windows

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-tail runtime and cancellation churn
- `test_and_mark_stable` slow failing E2E loops
- `ci` single-job 9–10 minute lint path
- `orchestrate_poll` full-history checkout on no-work cycles

**Top failure modes**
- Release ref ambiguity: run `25273372573`
- Missing workflow support assets: run `25272034874`
- Label/state mismatch in E2E PR creation path: run `25271960656`
- Prompt contract drift / integration fingerprint regressions in CI: runs `25266932433`, `25266996700`, `25267881013`, `25267991186`, `25272902365`

**Highest-cost drivers**
- Cancelled and long-running `review_autofix` executions
- Wide reviewer/summariser settings on tiny diffs
- Repeated GH API polling in E2E and review
- Slow `workflow_log_analysis` meta-runs

**Top 3 prioritized actions**
1. **Add a reduced `review_autofix` path for tiny comment-only diffs** and suppress expensive review work on superseded runs.
2. **Refactor `test_and_mark_stable` state polling** to reuse explicit IDs/artifacts instead of label-only checks and repeated `gh api` loops.
3. **Fix release/support-source reliability bugs** (`stable` ref push qualification + support-asset manifest validation in CI).

## Metrics Appendix

### Repository summary

| Repository | Total runs | Success | Failure | Cancelled | Other | Failure rate | p50 duration | p95 duration |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 810 | 273 | 11 | 52 | 474 | 1.36% | 1s | 636s |

### Key workflow-family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | Other | Avg duration | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 82 | 34 | 0 | 47 | 1 | 466.4s | 48s | 1,892.8s |
| ci | 63 | 58 | 5 | 0 | 0 | 597.7s | 605s | 651.2s |
| test_and_mark_stable | 4 | 0 | 4 | 0 | 0 | 4,704.5s | 4,767s | 6,020.4s |
| orchestrate_poll | 48 | 48 | 0 | 0 | 0 | 46.8s | 46s | 50.3s |
| workflow_log_analysis | 4 | 4 | 0 | 0 | 0 | 4,041.5s | 4,383s | 5,616.3s |
| copilot_pull_request_reviewer | 22 | 22 | 0 | 0 | 0 | 176.7s | 148s | 380.2s |
| clarify | 151 | 20 | 0 | 0 | 131 | 18.4s | 1s | 134s |
| plan | 126 | 16 | 0 | 0 | 110 | 12.8s | 1s | 141.5s |

### Sampled GH API hotspots from deep-dive logs

| Run ID | Workflow family | Step | `gh api` calls | GraphQL calls | Rate-limit-related lines | 429-like lines |
|---|---|---|---:|---:|---:|---:|
| 25254390226 | workflow_log_analysis | `step-001-api-redundancy.log` | 138 | 3 | 13 | 37 |
| 25254574828 | review_autofix | `step-001-review_codex-agent.log` | 81 | 2 | 37 | 70 |
| 25265928747 | workflow_log_analysis | `step-001-api-redundancy.log` | 80 | 4 | 4 | 31 |
| 25273372573 | test_and_mark_stable | `step-011-e2e-smoke-test.log` | 65 | 0 | 13 | 23 |
| 25265920645 | test_and_mark_stable | `step-005-e2e-smoke-test.log` | 41 | 0 | 11 | 40 |
| 25271960656 | test_and_mark_stable | `step-005-e2e-smoke-test.log` | 20 | 0 | 7 | 7 |
| 25281865088 | cancel_on_pr_close | cancel step | 4 | 0 | 1 | 0 |

### AI memory telemetry summary from inspected deep-dive logs

| Metric | Value | Notes |
|---|---:|---|
| Observed `retrieve` ops | 7 | sampled deep-dive logs only |
| Retrieve hit rate | 28.6% | 2 of 7 had `records_selected > 0` |
| Avg `estimated_tokens` | 16 | 56 on positive retrievals, 0 on reviewer misses |
| `keyword_method=none` | 5 | mostly reviewer flows |
| `keyword_method=plain` | 2 | implement / analysis implementation-side retrievals |
| `keyword_method=llm` | 0 | none observed |
| `fail_open:true` retrieves | 0 | none observed in sampled JSON telemetry |
| `enabled:false` entries | 0 | none observed |
| Typical `push_attempts` | 1 | in sampled record-run-event / record-candidate entries |

### Prompt/cache signals

| Signal | Value | Notes |
|---|---:|---|
| `OPENROUTER_PROMPT_CACHE_DISABLED` | `false` in sampled implement/review/poll logs | cache enabled in config |
| Real prompt cache read metrics on expensive calls | not observed | cache probe logs show `cache_*_input_tokens=na` |
| Dependency cache hits | observed | e.g. `setup-uv` cache hit in `25280032638`, `25279043495`, `25278175531` |
| Reviewer-memory retrieve zero-hit rate | 71.4% | 5 of 7 retrieves had `records_selected=0` |

### Token evidence explicitly surfaced in logs

| Source | Run ID | Evidence |
|---|---:|---|
| `workflow_log_analysis` deep-audit log | 25265928747 | quoted cancelled `review_autofix` run `25265763764` with **1,781,558 total tokens** (`1,747,212` input / `34,346` output) |
| `implement` telemetry | 25272034874 | `estimated_tokens_used: 56` on retrieved memory context |
| reviewer retrieve telemetry | 25254574828 / 25267058904 / 25268065004 / 25280032638 | reviewer memory retrieves estimated 0 tokens with 0 selected records |

If you want, I can turn this into a prioritized implementation checklist mapped to specific workflow files and likely edit locations.

## Deep Audit — Workflows & Scripts (2026-05-03)

### Section 1: Bug & Correctness Sweep

- **ID** — `BUG-001`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:2726-2775,2798-2833,2861-2898,2920-2957,3217-3259`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The repeated “dispatch & watch” blocks treat any `gh api` failure as empty output via `2>/dev/null || echo ""`, then continue polling until the outer deadline. That turns transport/auth/404/secondary-limit failures into misleading timeout symptoms such as “dispatch did not register” or “run timed out”, and it keeps burning API calls after the real failure is already known. The pattern is repeated for `workflow-log-analysis`, `validation-refresh`, `update_workflows`, `internal-memory-maintenance`, and `internal-validate`.  
  **Recommended fix** — Extract a shared watcher that returns explicit states (`registered`, `completed`, `api_error`) instead of empty-string fallbacks. Reuse the earlier `gh_api_safe` style already present in the same workflow (`test-and-mark-stable.yml:396-410`) or move the watcher into a shared script such as `scripts/watch_workflow_run.sh` so failures short-circuit immediately and consistently.

- **ID** — `SHELL-001`  
  **File path** — `scripts/review_commit_changes.sh:448-455; scripts/review_conflict_resolve.sh:852-853`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — Both scripts set the authenticated remote URL with unquoted expansions: `git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}`. ShellCheck flags this as `SC2086`. Even if current token/repo formats are usually safe, this is still a word-splitting/globbing footgun on a credential-bearing command line.  
  **Recommended fix** — Build the URL in a variable and quote the full argument, e.g. `remote_url="https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}"` then `git remote set-url origin "${remote_url}"`. Better yet, reuse the repository’s existing authenticated-CLI path and avoid embedding the token in argv at all.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`  
  **File path** — `.github/workflows/review_autofix.yml:1371-1406`  
  **Severity** — High  
  **Category tag** — `api-batching`  
  **Description** — The PR hydration step makes five logical API calls on the same execution path: 1x `pulls/{pr}`, 1x `issues/{pr}/comments`, 1x `pulls/{pr}/reviews`, 1x `pulls/{pr}/comments`, then 1x GraphQL `closingIssuesReferences`. The repo already has a GraphQL-first batching helper for most of this shape in `scripts/gh_helpers.sh:735-860` (`gh_pr_with_all_comments`), but `review_autofix` still does the legacy REST fan-out inline.  
  **Recommended fix** — Extend `scripts/gh_helpers.sh::gh_pr_with_all_comments()` (or add `gh_pr_with_full_context()`) so one GraphQL call returns PR meta, issue comments, review comments, review summaries, and linked issues. Then replace the inline hydration block in `review_autofix.yml` with that helper.  
  **Current call count** — 5 logical calls.  
  **Proposed call count after fix** — 1 logical call.  
  **Existing batching pattern to extend** — `scripts/gh_helpers.sh::gh_pr_with_all_comments`.

- **ID** — `API-002`  
  **File path** — `scripts/review_rb_judge.sh:146-208`  
  **Severity** — High  
  **Category tag** — `api-batching`  
  **Description** — `review_rb_judge.sh` first fetches linked issue numbers with GraphQL, then loops over each linked issue and calls `repos/{repo}/issues/{issue_number}` individually to get the body. That is a classic per-item API loop in a judge path. For a PR with `N` linked issues, this path costs `1 + N` issue-context calls before the script even finishes collecting prompt inputs.  
  **Recommended fix** — Batch linked issue number/title/body in the initial GraphQL query, matching the richer shape already used in `review_autofix.yml:1401-1423`, and stop the per-issue REST loop entirely. Keep `FIRST_ISSUE` selection local from the returned array.  
  **Current call count** — `1 + N` calls for linked-issue context (e.g. 11 calls for 10 linked issues).  
  **Proposed call count after fix** — 1 call.  
  **Existing batching pattern to extend** — the `closingIssuesReferences(first: 50) { nodes { number title body } }` pattern already used in `review_autofix.yml`, or a shared helper in `scripts/gh_helpers.sh`.

- **ID** — `API-003`  
  **File path** — `scripts/orchestrate_poll_process.sh:3412-3418,3432-3439,3471-3473,3522-3524`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The final-merge path repeatedly re-fetches the same PR payload field-by-field. In the common path it calls `repos/{repo}/pulls/{final_pr}` seven times just to read `state`, `merged_at`, and `mergeable`, even though the file already defines `_fetch_pr_json()` and `_jq_field()` at `scripts/orchestrate_poll_process.sh:691-720` specifically to collapse that pattern.  
  **Recommended fix** — Replace the repeated `_safe_gh_jq` field fetches with three snapshot reads at most: one pre-create/pre-check snapshot, one pre-mergeability snapshot, and one post-merge-attempt snapshot, all via `_fetch_pr_json` + `_jq_field`.  
  **Current call count** — 7 pull-lookups on the common path.  
  **Proposed call count after fix** — 3 pull-lookups.  
  **Existing batching pattern to extend** — `scripts/orchestrate_poll_process.sh::_fetch_pr_json` and `_jq_field`.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`  
  **File path** — `.github/workflows/issue_pr_status.yml:41-140; .github/workflows/validate.yml:185-280; .github/workflows/validation-improvements-intake.yml:63-129`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Three workflows carry near-identical support-bootstrap logic: `checkout_support_ref`, `support_primary_root` / `support_main_root` selection, and `fetch_from_ref_or_local` / `copy_from_ref_or_local`. The blocks have already drifted in naming and options, which raises the chance of future asset-resolution bugs landing in only one caller.  
  **Recommended fix** — Move this into a shared module, preferably `scripts/fetch_workflow_support.sh`, with a signature like `fetch_workflow_support --workflow <name> --script-ref <ref> --dest-root <dir> [--script <path>] [--prompt <path>] [--allow-main-fallback] [--require-remote <path>]`. Update `issue_pr_status.yml`, `validate.yml`, and `validation-improvements-intake.yml` to call it instead of maintaining separate bootstrap copies.

- **ID** — `DUP-002`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:2721-2957,3003-3065,3212-3259`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — `test-and-mark-stable.yml` repeats the same “snapshot previous run ID → dispatch workflow → poll for new run ID → poll status/conclusion until deadline” block for multiple workflows (`workflow-log-analysis`, `validation-refresh`, `update_workflows`, `internal-memory-maintenance`, `internal-orchestrate`, `internal-validate`). The structure is >70% identical and already has small behavior drifts (`success|skipped` handling, timeout lengths, poll intervals, log messages).  
  **Recommended fix** — Extract a shared watcher script, e.g. `scripts/watch_workflow_run.sh`, with a signature like `watch_workflow_run --repo <repo> --workflow <file> --timeout-secs <n> [--field key=value ...] [--accept-conclusion success,skipped]`. Update all repeated dispatch/watch callers in `test-and-mark-stable.yml` to use it so timeout/error handling and API backoff stay synchronized.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1118-1449`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Phase 4: Wait for review/autofix to finish` `run:` block contains one `${{ }}`-interpolated body estimated at **16,626 characters**. That is below the hard 21,000-character runner limit, but already above the 15,000-character medium-risk threshold, leaving only **4,374 characters of headroom**. This block keeps growing with new polling heuristics, live-log shortcuts, and diagnostics, so it is a realistic candidate to become the next expression-limit regression.  
  **Recommended fix** — Extract the entire wait loop to an external script such as `scripts/e2e_wait_review.sh` and pass the current inputs via environment variables. If that is too invasive, split live-log probing and activity detection into separate steps so no single `${{ }}`-compiled `run:` body keeps accumulating logic.

### Section 5: Cross-Cutting Concerns

- **ID** — `DEAD-001`  
  **File path** — `scripts/review_issue_ledger.sh:10-15,866-917`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — Two pieces of ledger scaffolding are currently write-only / unused: the shell-level `trim()` helper at lines 10-15 is not referenced by the shell flow, and the `CURRENT_FLOOR` associative array is declared and populated but never read before exit. Parsing work is handled by embedded `awk`-side `trim()` functions instead.  
  **Recommended fix** — Remove the unused shell `trim()` helper and delete `CURRENT_FLOOR` unless a follow-on feature is about to consume it. If floor persistence is intended, thread it into emitted ledger state so the variable is no longer dead.

- **ID** — `DEAD-002`  
  **File path** — `scripts/orchestrate_poll_process.sh:9754-9785,10003-10057`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `RB_FOLLOWUP_REFUSED` and `IF_BLOCKERS_SOURCE` are assigned but never read later in the script. That means the code pays state-management complexity without any behavioral effect, and the intended provenance/refusal signal is lost.  
  **Recommended fix** — Either remove these variables entirely or promote them into a real output/log/telemetry field that downstream logic consumes. If the intent was diagnostics, emit a structured log line next to the assignment.

- **ID** — `CONSIST-001`  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/mark-stable.yml:308-335,457-484; .github/workflows/orchestrate_poll.yml:63-97; scripts/gh_helpers.sh:122-171,391-650`  
  **Severity** — Medium  
  **Category tag** — `consistency`  
  **Description** — Multiple workflows still carry bespoke inline GitHub retry wrappers even though the repo’s canonical behavior now lives in `scripts/gh_helpers.sh` (`gh_retry`, breaker trip, admin alert throttle, JSON validation, curl parity). The inline versions have already drifted: they do not share the breaker/alert behavior, classify errors differently, and duplicate the `/rate_limit` reset parsing logic.  
  **Recommended fix** — Introduce a minimal bootstrap helper that can be sourced before the main support checkout, or move the common pre-checkout retry logic into a dedicated script/composite action. Then migrate `cancel_on_pr_close.yml`, `mark-stable.yml`, and `orchestrate_poll.yml` to that shared implementation so rate-limit handling is uniform.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | API-001, API-002 |
| Medium | 6 | BUG-001, API-003, DUP-001, DUP-002, EXPR-001, CONSIST-001 |
| Low | 3 | SHELL-001, DEAD-001, DEAD-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 4 | Large |
| Expression size reduction | 1 | Small |
| Medium/Low fixes | 5 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-03)

### Safety Tag Legend
`SAFE_TO_MERGE` means this pass found a mechanically actionable consolidation/elimination with no visible contract risk from static reading. `NEEDS_VERIFICATION` means the overlap looks real, but a human or follow-up analysis must confirm cache/input contracts or error-handling equivalence before changing it. `RISKY_SKIP` means the overlap is real but sits in a retry/pagination/race-defense path where this pass does **not** authorize auto-implementation.

### Consolidation Candidates (MERGE-###)
No findings.

### Redundant Re-Fetch (REUSE-###)

- **ID** — `REUSE-001`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/internal-review.yml:98-101`  
  **Current call count** — 2  
  **Proposed call count** — 1  
  **Endpoint(s)** — `GET /repos/{repo}/pulls?state=open&head={owner}:{branch}` and `GET /repos/{repo}`  
  **Evidence** — The step already makes one PR-discovery call, then makes a second repo-metadata call only to obtain the default branch, even though this job runs only on `push` and already has event context available.
  ```bash
  existing_pr="$(gh api \
    "repos/${REPOSITORY}/pulls?state=open&head=${REPOSITORY%/*}:${HEAD_REF}" \
    --jq '[.[] | .number] | first // empty' 2>/dev/null || echo "")"
  base_ref="$(gh api "repos/${REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo 'main')"
  ```
  **Proposed fix** — In `resolve-claude-branch-pr`, replace the repo-metadata API read with `${{ github.event.repository.default_branch }}` (falling back to `main` exactly as today). Keep the PR lookup call unchanged.  
  **Safety rationale** — This is a pure re-fetch elimination, but it depends on the `push` event payload always carrying `repository.default_branch`, so it does not meet this pass’s strict SAFE criteria without verification.  
  **Downstream signal** — Verify from a sampled `push` run of `internal-review.yml` that `github.event.repository.default_branch` is always populated for the `claude/**` path; then remove only the `gh api "repos/${REPOSITORY}"` call.

- **ID** — `REUSE-002`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:372-377`  
  **Current call count** — 2  
  **Proposed call count** — 1  
  **Endpoint(s)** — `POST /repos/{repo}/issues` and `GET /repos/{repo}/issues/{issue_number}`  
  **Evidence** — The workflow creates the issue, extracts only `.number`, then immediately re-fetches the same issue just to read `.html_url`.
  ```bash
  ISSUE_NUMBER=$(gh api "repos/${TEST_REPO}/issues" \
    -f title="${TITLE}" \
    -f body="${BODY}" \
    --jq '.number')

  ISSUE_URL=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '.html_url')
  ```
  **Proposed fix** — In the `Create test issue` step, capture the full create-response JSON once (for example into `ISSUE_CREATE_JSON` or a temp file), parse both `.number` and `.html_url` from that response, and drop the follow-up GET.  
  **Safety rationale** — The second call is a same-step re-fetch of data that should already be in the create response, but this pass did not independently verify the exact response-shape dependency under the current GH CLI path.  
  **Downstream signal** — Verify on one sample run that `gh api POST repos/.../issues` returns `html_url` in the response body under the current runner/CLI version; then collapse the pair into a single create-response parse.

- **ID** — `REUSE-003`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/orchestrate_clarify_respond.yml:61-76`, `.github/workflows/orchestrate_clarify_respond.yml:418-429`  
  **Current call count** — 4  
  **Proposed call count** — 2  
  **Endpoint(s)** — `GET /repos/{repo}/issues/{ISSUE_NUMBER}` and `GET /repos/{repo}/issues/{TRACKING_NUM}`  
  **Evidence** — The job fetches the child issue and tracking issue once in `Check orchestrator metadata`, then re-fetches both later in `Fetch issue and tracking context`.
  ```bash
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ...
  TRACKING_TITLE="$(gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.title // ""' 2>/dev/null || echo "")"
  ```
  ```bash
  ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ...
  TRACKING_BODY="$(gh_retry gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.body // ""')"
  ```
  **Proposed fix** — Extend `Check orchestrator metadata` to persist the full child-issue JSON and full tracking-issue JSON into temp files or exported env-backed files; have `Fetch issue and tracking context` consume those cached payloads first and fall back to the existing `gh_retry` fetches only on cache miss/parse failure.  
  **Safety rationale** — The endpoints are identical and there is no obvious in-job mutation between the two steps, but the reuse crosses step boundaries and changes which call provides retry semantics, so verification is required.  
  **Downstream signal** — Verify there is no intervening step that edits either issue between lines 61-76 and 418-429, and require a cache-miss fallback to the current `gh_retry` calls before removing the re-fetches.

- **ID** — `REUSE-004`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `scripts/review_rb_judge.sh:146-166`, `scripts/review_rb_judge.sh:193-214`  
  **Current call count** — 1 extra PR fetch on the “no linked issues from GraphQL” fallback path  
  **Proposed call count** — 0 extra PR fetches on that path when `PR_META_FILE` is present and parseable  
  **Endpoint(s)** — `GET /repos/{repo}/pulls/{pr_number}`  
  **Evidence** — When GraphQL returns no linked issues, the script re-fetches PR title/body from the API, even though later in the same script it already reads `PR_META_FILE` as its PR metadata source.
  ```bash
  if [ -z "${ISSUE_NUMBERS}" ]; then
    PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' || echo "")"
    ...
  fi
  ```
  ```bash
  PRELOADED_PR_META="$(jq -c '{
    title: (.title // ""),
    body: (.body // ""),
    head_ref: (.head_ref // .head.ref // .headRefName // ""),
    base_ref: (.base_ref // .base.ref // .baseRefName // ""),
    head_sha: (.head_sha // .head.sha // .headSha // "")
  }' "${PR_META_FILE}" 2>/dev/null || echo '{}')"
  ...
  if [ "${PR_META_JSON}" = "{}" ]; then
    PR_META_JSON="$(jq '.' "${PR_META_FILE}" 2>/dev/null || echo "{}")"
  fi
  ```
  **Proposed fix** — In `review_rb_judge.sh`, derive `PR_DATA` from `PR_META_FILE` first (`[.title // "", .body // ""] | join(" ")`), and keep the existing `_safe_gh_jq pulls/{PR_NUMBER}` call only as a cache-miss / parse-failure fallback.  
  **Safety rationale** — The data is already file-cached locally in the same script invocation, but this pass cannot prove from static reading alone that every judge entrypoint always provides a valid `PR_META_FILE`.  
  **Downstream signal** — Verify every `review_rb_judge.sh` caller populates `PR_META_FILE` before execution; then switch the no-linked-issues fallback to file-first, API-second behavior.

### Dead Calls (DEAD-API-###)

- **ID** — `DEAD-API-001`  
  **Safety tag** — `SAFE_TO_MERGE`  
  **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:1508-1511`  
  **Current call count** — 1  
  **Proposed call count** — 0  
  **Endpoint(s)** — `GET /repos/{repo}/commits?sha={branch}&per_page=20`  
  **Evidence** — `COMMITS_AFTER` is assigned from a GitHub API call and never read afterward; the actual gate uses only `PR_HEAD` vs `BAIT_SHA`.
  ```bash
  COMMITS_AFTER=$(gh api "repos/${TEST_REPO}/commits?sha=${BRANCH}&per_page=20" \
    --jq "[.[] | select(.sha != \"${BAIT_SHA}\") | .sha] | length" 2>/dev/null || echo "0")
  # The PR head SHA should differ from the bait SHA.
  PR_HEAD=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")
  if [ "${PR_HEAD}" = "${BAIT_SHA}" ]; then
  ```
  A repository-wide search in this audit found no later `COMMITS_AFTER` read in the file.  
  **Proposed fix** — Delete the `COMMITS_AFTER` assignment and leave the existing `PR_HEAD` check as the sole “editor pushed past bait” assertion.  
  **Safety rationale** — Static reading shows the fetched value is never consumed, the call is outside retry/auth/race-defense paths, and removing it does not alter any downstream branch condition or log key.  
  **Downstream signal** — Remove the unused `COMMITS_AFTER` API call at `.github/workflows/test-and-mark-stable.yml:1508-1509` and keep the `PR_HEAD`-based validation unchanged.

### Cross-References to Deep Audit Section
- `API-001`: `RISKY_SKIP` — strong batching candidate, but the current path includes multiple `--paginate` calls, so this pass does not authorize auto-merging page-sensitive hydration logic.
- `API-002`: `NEEDS_VERIFICATION` — batching linked issue title/body into the existing GraphQL lookup is directionally correct, but the current per-issue REST loop fail-opens item-by-item and that failure behavior must be preserved deliberately.
- `API-003`: `RISKY_SKIP` — it lives inside `scripts/orchestrate_poll_process.sh` on a race-defense merge path, which this pass must not auto-consolidate.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 1 | DEAD-API-001 |
| NEEDS_VERIFICATION | 4 | REUSE-001, REUSE-002, REUSE-003, REUSE-004 |
| RISKY_SKIP | 0 | — |

### Implement-Stage Handoff
- `DEAD-API-001`
