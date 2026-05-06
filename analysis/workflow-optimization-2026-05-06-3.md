## Executive Summary

- **Fix the review waiter’s cancelled-run handoff first.** Failed `test_and_mark_stable` run **25428461223** spent ~**19m 38s** repeatedly printing `Review run was cancelled — checking for newer run...` before timing out in `e2e-smoke-test / Phase 4: Wait for review & autofix to complete`; the step finally failed with `Review phase stalled — no activity for 30 minutes`. The same failure mode is explicitly described in recent `review_autofix` gate run **25431162324** as the observed issue on `run-25428461223`. **Estimated impact:** recover **20–30 min** on each affected E2E run and cut a major false-failure class. **Confidence:** high.

- **The alt-model smoke path is not reliably using the alt model, and that cascades into long E2E failures.** Failed implement runs **25417030055** and **25417040196** were created from alt-model smoke issues whose body said the run should use `anthropic/claude-sonnet-4-6`, but the job environment still showed `MODEL_EDITOR: openai/gpt-5.3-codex`; both runs died with `Codex bailed: 2 consecutive attempts with no actionable output`. **Estimated impact:** remove a failure chain that consumed **4,579s** in run **25416934394** and **3,427s** in run **25428461223**. **Confidence:** high.

- **`orchestrate_poll` is failing from runner starvation, not business logic.** Failed poller runs **25381014761**, **25383797907**, and **25424218738** all show only system logs repeating `Waiting for a runner to pick up this job...` until the job ended at **903s**. **Estimated impact:** eliminate an **8.6% poller failure rate** and reduce stale orchestration gaps. **Confidence:** high.

- **Prompt cache is enabled but still not measurable, while reviewer memory retrieval is mostly wasted.** Sampled `review_autofix` cache probes in runs **25394267845** and **25413999630** reported `cache_enabled=true` but all token/cache counters as `na`; across deep-dive logs, AI memory `retrieve` succeeded only **6/17 times (35.3%)**, with **11/17 zero-record retrieves**, mostly reviewer-side. **Estimated impact:** likely **10–20% token reduction** once cache counters and stable prefixes are made auditable, plus small latency savings from skipping zero-yield retrievals. **Confidence:** medium.

- **GH API volume is dominated by polling loops and artifact cleanup, not rate-limit recovery.** Deep-dive logs contain about **1,430 `gh api` lines**; the heaviest sampled run was failed E2E **25428461223** with **292** such lines. Repeated polling every ~10–12s appears in `test_and_mark_stable`, while `copilot_pull_request_reviewer` repeatedly lists and deletes artifacts via per-run API calls. No sampled deep-dive logs showed actual 429s. **Estimated impact:** **40–70% fewer GH API calls** in the no-progress wait paths. **Confidence:** high.

- **CI is stable overall but expensive in wall time, and recent failures are prompt-contract drift rather than infrastructure.** `ci` has **70 runs**, **7.1% failure rate**, and **p50 612.5s / p95 653s**. Failures in **25425264723** and **25425830472** came from targeted contract tests (`test_validate_process_cross_cycle_escalation.py`, `test_plan_clarify_blocked_output.py`), while successful CI runs like **25430699602** still spent ~**10 min** in `lint`. **Estimated impact:** **2–4 min** wall-time reduction per CI run from sharding/path-filtering, plus fewer red PRs from prompt/test drift controls. **Confidence:** medium.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Repair cancelled-review successor handoff in `test_and_mark_stable`
- **Evidence:** Failed run **25428461223**, step `e2e-smoke-test / Phase 4: Wait for review & autofix to complete`, logged repeated checks against cancelled review run **25428854885** from `10:36:35Z` to `10:44:00Z`, then failed with `Review phase stalled — no activity for 30 minutes`. Recent gate run **25431162324** includes the exact mitigation note: track mutable `PIN_SHA`, advance it when the pinned run is cancelled and the PR head moves, and reset inactivity.
- **Root cause:** Waiter logic stayed pinned to a cancelled bait run instead of latching onto the successor review run after the PR head advanced.
- **Exact change:** Implement the already-described `PIN_SHA` advancement and timer reset in the review wait loop; distinguish “successor never appeared after repin” from “no run ever appeared.”
- **Estimated time savings:** **20–30 min** on each affected E2E run; also reduces false failures in `test_and_mark_stable` (**p50 3,628.5s**, **p95 4,466.7s**).
- **Implementation risk:** **Low.**
- **Critical-path or micro:** **Critical-path win.**

### 2. Fix alt-model editor override propagation before dispatching smoke implement runs
- **Evidence:** Failed implement runs **25417030055** and **25417040196** came from alt-model smoke issues whose body specified `anthropic/claude-sonnet-4-6`, but both jobs showed `MODEL_EDITOR: openai/gpt-5.3-codex`. The parent E2E run **25416934394** then remained in `ai:implementing` until `06:00:12Z`, when `e2e-alt-model-test` failed with `Alt-model run timed out before reaching review stage`.
- **Root cause:** The intended alt-model override is not consistently reaching the actual implement job environment.
- **Exact change:** Pass the alt-model editor override through the dispatch/output chain as a first-class input and assert it in `implement` setup before Codex starts; fail immediately if requested override and resolved `MODEL_EDITOR` differ.
- **Estimated time savings:** avoids **30–75 min** wasted per broken alt-model smoke sequence, including failed downstream waits.
- **Implementation risk:** **Low-medium** because it touches workflow plumbing, but it is backward-compatible.
- **Critical-path or micro:** **Critical-path win.**

### 3. Reduce `orchestrate_poll` checkout cost by avoiding full heads+tags fetch on every poll
- **Evidence:** Recent successful poller run **25430938203** spent most of its **57s** in `poll / Checkout repository`; the log shows `git fetch --prune ... +refs/heads/*:refs/remotes/origin/* +refs/tags/*:refs/tags/*` and a very large branch/tag enumeration. Another sampled poller run **25428900340** similarly had checkout dominate a **62s** run.
- **Root cause:** Poller uses a full branch+tag mirror fetch even though the main work shown in the same run was just `gh issue list` returning `Found 0 active tracking issue(s)`.
- **Exact change:** For no-work detection, skip repository checkout entirely until at least one tracking issue exists; if checkout is required, fetch only the default branch and required support refs, not all issue branches and tags.
- **Estimated time savings:** **8–15s per poll run**; across **35 poller runs** in the sample, that is meaningful recurring savings.
- **Implementation risk:** **Low-medium**; verify downstream steps do not depend on complete tag/branch enumeration in the no-work path.
- **Critical-path or micro:** **High-value recurring optimization.**

### 4. Prevent queue-only poller failures with workflow-level concurrency
- **Evidence:** Failed `orchestrate_poll` runs **25381014761**, **25383797907**, and **25424218738** never reached execution logs; all three spent the entire **903s** repeating runner wait messages.
- **Root cause:** Poll jobs can stack while previous ones are still queued/running, turning runner scarcity into hard failures.
- **Exact change:** Add a single concurrency group for `orchestrate_poll` with cancellation or skip semantics for stale queued runs, so only the newest poll proceeds.
- **Estimated time savings:** saves up to **15 min** per failed poller run and reduces noise for downstream workflows.
- **Implementation risk:** **Low.**
- **Critical-path or micro:** **Critical-path reliability/speed hybrid.**

### 5. Shorten CI wall time by splitting prompt/workflow contract tests from the main `lint` path
- **Evidence:** Successful CI runs **25430699602**, **25429281875**, **25428753714**, and **25428714474** all took about **597–619s** with `lint` dominating. Recent failures were isolated to narrow tests: **25425264723** failed in `test_validate_process_cross_cycle_escalation.py`, **25425830472** failed in `test_plan_clarify_blocked_output.py`.
- **Root cause:** A broad sequential `lint` job carries both stable library tests and prompt/workflow contract tests, so small prompt changes still pay the full 10-minute runtime.
- **Exact change:** Split the contract/prompt tests into a separate shard or path-triggered job; keep core library/unit coverage in the main lane.
- **Estimated time savings:** **2–4 min** off median CI wall time.
- **Implementation risk:** **Medium** because job structure changes can affect required checks.
- **Critical-path or micro:** **Critical-path for developer feedback.**

### 6. Stop running expensive implement setup when the issue is already closed or duplicate work is in flight
- **Evidence:** Successful implement runs **25428657325** and **25428636688** spent **246s** and **179s** respectively only to conclude `Issue #2158 is not in ai:awaiting-approval phase` or `Issue #2160 is closed. Skipping implementation steps.` Failed implement excerpt **25417040196** already contains a state gate comment explaining this cost.
- **Root cause:** Some implement runs still start and spend non-trivial setup time before discovering they should no-op.
- **Exact change:** Move the issue-state / label preflight to the earliest possible reusable-workflow gate, before checkout and tool bootstrap.
- **Estimated time savings:** **2–4 min** for each stale implement invocation.
- **Implementation risk:** **Low.**
- **Critical-path or micro:** **Medium win.**

## Cost Optimizations

Ranked by expected token/dollar savings.

### 1. Eliminate wasted review/implement reruns caused by the cancelled-review handoff bug
- **Evidence:** `test_and_mark_stable` failures **25428461223** and **25416934394** consumed **3,427s** and **4,579s**. In **25428461223**, the E2E waiter spent nearly 20 minutes polling a cancelled review run after the head SHA advanced.
- **Root cause:** Rerun/repoll logic spends runner time and GH API calls on a known-dead execution path.
- **Exact change:** Repin to successor review runs once, then fail fast with a specific handoff error if no successor appears.
- **Estimated savings:** **20–30 runner minutes** and associated API churn per occurrence; likely the single biggest avoidable cost in the current sample.
- **Quality-risk notes:** Very low; it narrows a false-negative state machine.

### 2. Fix the alt-model override so the system does not pay for duplicate broken implement attempts
- **Evidence:** Alt-model implement issues requested `anthropic/claude-sonnet-4-6`, but failed implement runs **25417030055** and **25417040196** still used `openai/gpt-5.3-codex` and failed with the same announce-without-edit pattern.
- **Root cause:** Costly “alt-model” tests are effectively replaying the default failing path.
- **Exact change:** Validate the resolved `MODEL_EDITOR` against the requested override before launching Codex; abort immediately if mismatched.
- **Estimated savings:** avoids duplicate failed implement runs plus downstream E2E wait waste; substantial but not precisely tokenized.
- **Quality-risk notes:** Low; this improves test validity.

### 3. Make prompt-cache value measurable and preserve a stable cacheable prefix
- **Evidence:** Sampled `review_autofix_cache_probe` lines in slow runs **25394267845** and **25413999630** show `cache_enabled=true` but `prompt_tokens=na`, `completion_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`. Workflow-log-analysis run **25428493736** explicitly called out likely **10–20%** savings if cache counters become observable and prefixes stabilize.
- **Root cause:** Cache is enabled, but the team cannot verify hit/write behavior or optimize prompt structure with evidence.
- **Exact change:** Emit numeric cache read/create counters in every LLM step summary; keep static instructions strictly before dynamic per-PR/per-issue material in `review_autofix`, `implement`, `workflow_log_analysis`, and `test_and_mark_stable`.
- **Estimated savings:** likely **10–20%** token reduction in LLM-heavy workflows once measurable.
- **Quality-risk notes:** Low; this is observability plus prompt assembly hygiene.

### 4. Skip reviewer memory retrieval for roles with a sustained zero-hit rate
- **Evidence:** Across deep-dive logs, AI memory `retrieve` ran **17** times with only **6 hits (35.3%)**; reviewer retrievals in **25394267845**, **25413999630**, and **25430920264** all returned `records_selected: 0`, `estimated_tokens: 0`, `keyword_method: none`.
- **Root cause:** Reviewer role retrieval is being attempted even when the corpus or selector path yields nothing.
- **Exact change:** Add a cheap guard: if reviewer memory has produced 0 records for N consecutive runs on the same repo/workflow, skip retrieval until a new reviewer candidate is promoted.
- **Estimated savings:** small token savings, small latency savings, cleaner prompts.
- **Quality-risk notes:** Low if applied only to the reviewer role; keep implementation-role retrieval enabled because its sampled hit rate was better.

### 5. Reduce multi-model review spend on paths already classified as low-risk
- **Evidence:** Recent `review_autofix` runs such as **25431162324**, **25429282023**, **25428764774**, and **25428714438** show six reviewer models plus `ENABLE_REVIEWER_TWO_PASS: true`. Run **25428714438** also showed deterministic docs-only skip logic already exists.
- **Root cause:** The pipeline appears capable of preclassifying low-risk diffs, but the expensive reviewer panel still exists for several comment-only or specialized paths.
- **Exact change:** Extend current deterministic-skip logic so docs-only/small-diff/comment-only Claude-branch review paths can use a reduced panel or single summariser pass where no autofix/judge/merge is possible.
- **Estimated savings:** moderate, but not precisely quantifiable from current telemetry.
- **Quality-risk notes:** **Medium**; keep full panel on code-changing or merge-blocking paths.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Repair cancelled-review successor detection in `test_and_mark_stable`
- **Failure evidence:** Run **25428461223** failed in `e2e-smoke-test / Phase 4: Wait for review & autofix to complete` after a long loop over cancelled review run **25428854885**.
- **Root cause category:** Orchestrator state-machine bug.
- **Exact fix:** Advance the pinned SHA after bait-run cancellation and reset inactivity, as described in run **25431162324**.
- **Expected reliability impact:** removes a proven false-failure class from a workflow family currently at **50% failure rate (2 failures / 4 runs)**.
- **Rollback/fail-open:** Safe rollback; if reverted, fall back to current timeout behavior.

### 2. Enforce model-override integrity for alt-model tests
- **Failure evidence:** Alt-model implement failures **25417030055** and **25417040196** used `MODEL_EDITOR: openai/gpt-5.3-codex` despite alt-model issue instructions.
- **Root cause category:** Workflow input propagation / configuration drift.
- **Exact fix:** Pass override as an explicit reusable-workflow input and assert it before execution.
- **Expected reliability impact:** should remove a major source of false alt-model failures and improve test validity.
- **Rollback/fail-open:** If assertion fails, fail fast before spending compute.

### 3. Route single-file overwrite smoke tasks away from the known `gpt-5.3-codex` announce-without-edit bug
- **Failure evidence:** Implement failures **25417030055** and **25417040196** ended with `Codex bailed: 2 consecutive attempts with no actionable output`; the logs themselves call out a known `gpt-5.3-codex` “announce-without-emit” bug and show warnings like `Codex announced an edit/apply_patch ... but produced no file changes`.
- **Root cause category:** Model/tooling failure mode.
- **Exact fix:** For exact single-file overwrite smoke tasks, force a safer editor profile or fallback shell-write path instead of sending them through the fragile generic flow.
- **Expected reliability impact:** high on smoke/canary tasks; moderate globally unless expanded carefully.
- **Rollback/fail-open:** Keep the generic path as fallback behind a flag.

### 4. Add concurrency protection to `orchestrate_poll`
- **Failure evidence:** Poller failures **25381014761**, **25383797907**, **25424218738** were queue-only failures with no business-step logs.
- **Root cause category:** Scheduling / workflow overlap.
- **Exact fix:** Use a single concurrency group so stale queued polls are cancelled or skipped.
- **Expected reliability impact:** should materially reduce the current **8.6% failure rate** in `orchestrate_poll`.
- **Rollback/fail-open:** Very safe; worst case is fewer overlapping poll cycles.

### 5. Fix missing GitHub token injection in `copilot_pull_request_reviewer`
- **Failure evidence:** Failed run **25389586417**, job `Prepare`, ended with `Error: Input required and not supplied: github-token` from `actions/github-script@v8`.
- **Root cause category:** Authentication wiring / hosted-runner env setup.
- **Exact fix:** Always pass `github-token` explicitly to `actions/github-script@v8` instead of depending on ambient `GH_TOKEN`/`GITHUB_TOKEN`.
- **Expected reliability impact:** should remove a hard failure class from `copilot_pull_request_reviewer`.
- **Rollback/fail-open:** Safe; explicit input is the standard path.

### 6. Reduce prompt/test drift by centralizing prompt contract strings
- **Failure evidence:** CI run **25425264723** failed two escalation prompt assertions; CI run **25425830472** failed `assert "emit exactly \`BLOCKED: <short reason>\`" in plan_prompt`.
- **Root cause category:** Prompt contract drift.
- **Exact fix:** Generate or import the tested contract strings from one source rather than duplicating them across prompt text and test expectations.
- **Expected reliability impact:** reduces prompt-only CI reds and unnecessary reruns.
- **Rollback/fail-open:** Medium; if generation feels heavy, start with shared constants.

## AI Memory Health

- **Telemetry coverage:** Present in deep-dive logs for `implement`, `review_autofix`, `memory_maintenance`, and `workflow_log_analysis`. No evidence of memory telemetry in many recent non-AI-heavy workflows, which is acceptable; there was at least one non-`op` ledger-style memory event in recent `orchestrate_poll` run **25430938203** (`poll_completed`).
- **Retrieve hit rate:** **35.3%** (**6/17** retrieves had `records_selected > 0`).
- **Average retrieved token estimate:** **19.8 tokens** overall; sampled implementation hits were consistently **56 tokens**, while reviewer misses were **0**.
- **Keyword method distribution:** `plain` **6** (**35.3%**), `none` **11** (**64.7%**), `llm` **0**.
- **Zero-record retrieves:** **11/17**; all sampled reviewer retrieves were zero-hit:
  - `review_autofix` **25394267845**: `records_selected: 0`, `keyword_method: none`
  - `review_autofix` **25413999630**: same
  - `review_autofix` **25430920264**: same
- **Positive retrieves:** sampled implementation retrieves in **25417030055** and **25417040196** returned **2 records** and **56 estimated tokens**.
- **Fail-open / disabled:** no sampled `retrieve` entries showed `fail_open: true` or `enabled: false`.
- **Push retry signals:** **3** telemetry events had `push_attempts > 1`, including `record-run-event` in implement flows such as **25417030055**.
- **Compaction health:** Recent memory maintenance run **25430899031** compacted **2,914 archived candidates** for month `2026-04`, with `did_push: true` and `push_attempts: 1`.

**Recommendation**
1. Keep implementation-role retrieval enabled.
2. Add an adaptive skip for reviewer retrieval when repeated zero-hit streaks are detected.
3. Emit a small summary line per run with retrieve hits/misses by role so teams can track whether memory is paying off.

## GH API Call Audit

### 1. `test_and_mark_stable` wait loops are the biggest avoidable API hotspot
- **Evidence:** Deep-dive logs contain about **1,430 `gh api` lines**; the heaviest sampled run was failed `test_and_mark_stable` **25428461223** with **292** such lines. Its wait step polled review state every ~**10–12s** for many minutes.
- **Pattern:** repeated lookups of workflow runs, jobs, labels, and current step state inside tight loops.
- **Redundancy:** jobs/status APIs appear to be queried even while the run is unchanged and known cancelled.
- **Concrete change:** cache the last seen `(run_id, status, conclusion, head_sha)` tuple and only query job-step detail when the tuple changes; increase poll interval progressively after N unchanged polls.
- **Estimated reduction:** **50–70% fewer API calls** on long waits; lower rate-limit risk and lower runner time.

### 2. `copilot_pull_request_reviewer` cleanup uses per-artifact list+delete loops
- **Evidence:** Recent run **25430923229** called:
  - `gh api /repos/.../actions/runs/25430923229/artifacts`
  - then per-artifact deletes via `/repos/.../actions/artifacts/$artifact_id`
- **Pattern:** list once, delete one-by-one.
- **Redundancy:** every run pays cleanup overhead even when artifacts are tiny and short-lived.
- **Concrete change:** skip cleanup when no artifacts were uploaded, or consolidate retention strategy so ephemeral artifacts are not created and immediately deleted in the same pipeline branch.
- **Estimated reduction:** small-to-moderate API savings; noticeable on this workflow family where artifact handling is a recurrent hotspot.
- **Repository hygiene note:** this repo already uses the GH CLI consistently; the main gap is avoiding unnecessary cleanup work, not batching availability.

### 3. `cancel_on_pr_close` does defensive rate-limit checks even when no 429s occur
- **Evidence:** Recent cancel runs **25431162291** and **25428764755** show `_rl_wait()` using `gh api -i /rate_limit`; no sampled logs showed 429 or secondary-rate-limit events.
- **Pattern:** proactive rate-limit probe before cancellation attempts.
- **Redundancy:** extra control-plane call on a path that usually cancels zero runs.
- **Concrete change:** only query `/rate_limit` after a retryable API failure, not before normal cancel operations.
- **Estimated reduction:** **1 API call per run**; small individually, worthwhile on frequent short workflows.

### 4. `orchestrate_poll` API usage is reasonable, but checkout cost dominates more than API cost
- **Evidence:** Recent poll run **25430938203** used `gh issue list` to find active tracking issues and inlined retry logic around `gh api -i /rate_limit`, but its dominant runtime was repository checkout, not API.
- **Pattern:** one issue listing step plus defensive retry scaffolding.
- **Concrete change:** preserve current API pattern; prioritize checkout reduction first.
- **Estimated reduction:** low API benefit, high runtime benefit from repo-fetch changes instead.

### 5. GraphQL usage is present but not an obvious problem
- **Evidence:** aggregated deep-dive scan found **62** GraphQL mentions; recent `review_autofix` run **25431162324** successfully used `gh api graphql` in `Dispatch standalone validate for orchestrator short-circuit issues`.
- **Pattern:** targeted GraphQL queries for dispatch/gate work.
- **Assessment:** no immediate action unless rate-limit evidence emerges.

## Prompt Cache & Memory System

### Current state
- **Cache enabled:** yes; many sampled workflows show `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
- **Cache observability:** poor. Sampled `review_autofix_cache_probe` lines in **25394267845** and **25413999630** report `cache_enabled=true` but all relevant counters as `na`.
- **Stable-prefix awareness already exists:** slow `review_autofix` logs explicitly mention keeping dynamic content separate so the provider can cache a stable prefix.
- **Memory usefulness differs by role:** implementation retrieval has some value; reviewer retrieval mostly does not.

### Cache-fragmentation / measurement issues
- Dynamic PR-specific material still appears to be mixed into long prompt assembly paths, and the team cannot prove whether reads vs creations are happening because provider counters are absent.
- Repeated environment/model/config lines across review steps suggest the static prefix is replicated in multiple places, but the logs do not reveal whether those segments are truly identical from the provider’s perspective.

### Recommendations
1. **Emit numeric cache counters everywhere**
   - Add `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens` to every LLM-heavy step summary.
   - **Impact:** unlocks evidence-based tuning; likely medium token savings.
   - **Reliability impact:** helps catch cache regressions after prompt edits.

2. **Freeze the cacheable prefix**
   - Keep stable system instructions, tool rules, and reviewer policy in a single preassembled prefix file; append issue/PR diff material strictly afterward.
   - **Impact:** likely lower prompt-token spend and lower latency on repeated runs.
   - **Risk:** low.

3. **Suppress zero-yield reviewer memory retrieval**
   - Given the current reviewer zero-hit pattern, skip retrieval when there is no reviewer memory corpus signal.
   - **Impact:** small token and latency savings, cleaner prompts.
   - **Risk:** low if limited to reviewer role.

4. **Track memory value by role**
   - Add run summaries like `memory_retrieve role=reviewer hits=0 misses=1`.
   - **Impact:** lets the team decide when to disable or re-enable retrieval paths.
   - **Risk:** negligible.

## Orchestrator Health

### Observed health signals
- `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` are mostly gated/skipped fast paths with **p50 ~1s**, so they are not the dominant bottleneck.
- `review_autofix` is operationally noisy:
  - **95 runs**
  - **60 cancelled**
  - **p50 57s**
  - **p95 1,677.4s**
- `orchestrate_poll` is fragile to runner queue conditions:
  - **35 runs**
  - **3 failures**
  - **p95 903s**
- Recent `review_autofix` gate logs already contain embedded operational notes for known failure modes, which is good evidence of active self-healing design.

### Recurring pain points
1. **Cancellation churn in `review_autofix`**
   - Multiple long cancelled runs (for example **25428753937**, **25428854885**, **25430920264**) indicate work is starting, then being invalidated by branch/head movement.
2. **Queue starvation in poller**
   - Poller failures are infrastructure-adjacent but can be mitigated with workflow concurrency.
3. **Long codex-agent spans**
   - Example: slow `review_autofix` **25413999630** ran **2,160s**; sampled recent run **25429282023** shows `review / codex-agent` taking roughly **9 minutes**.
4. **Prompt drift causing CI failures**
   - The orchestration layer depends on prompt contract wording, and small wording drifts are turning into red CI runs.

### Smallest safe mitigations
- Add concurrency protection to `orchestrate_poll`.
- Ship the `PIN_SHA` review waiter fix.
- Fail fast when requested model override does not equal resolved model.
- Centralize prompt contract text used by both workflow and tests.

### Observable indicators to track
- `review_autofix` cancelled-run ratio
- count of “review run was cancelled — checking for newer run...” loops
- count of queue-only `orchestrate_poll` failures
- reviewer memory zero-hit streak length
- cache probes with numeric counters vs `na`
- prompt-contract CI failure count

## Pipeline Flow Bottlenecks

### 1. Clarify → Plan
- **Current state:** mostly not the bottleneck; many runs are skipped in **1–2s** because command guards evaluate false.
- **Issue:** prompt drift still leaks into CI via plan/clarify contract tests.
- **Fix:** centralize contract strings; keep fast gating.

### 2. Plan → Implement
- **Bottleneck type:** wasted compute on stale or duplicate implement triggers.
- **Evidence:** successful but skipped/closed-state implement runs **25428657325** (**246s**) and **25428636688** (**179s**); failed smoke implement runs **25417030055/196** due model/tool failure.
- **Fix:** move issue-state gating earlier and validate model override before execution.

### 3. Implement → Review/Autofix
- **Bottleneck type:** dominant end-to-end bottleneck.
- **Evidence:** long `review_autofix` durations (family **p95 1,677.4s**, slow runs up to **2,160s**) plus cancellation churn.
- **Fix:** successor-run repinning, reduced reviewer scope on comment-only/low-risk branches, and stronger self-trigger skip usage.

### 4. Review/Autofix → Validate
- **Bottleneck type:** stalled waits and cancellation loops.
- **Evidence:** failed `test_and_mark_stable` run **25428461223**.
- **Fix:** status-change-aware polling and explicit successor detection.

### 5. Validate / Orchestrate loops
- **Bottleneck type:** queueing and polling overhead.
- **Evidence:** `orchestrate_poll` queue-only failures and expensive poll checkout; `workflow_log_analysis` runs are themselves very long (**2,700–3,287s** in sampled slow runs).
- **Fix:** poll concurrency, lighter no-work poller path, and tighter deep-dive selection if workflow-log-analysis runtime becomes a concern.

### Ordered by end-to-end impact
1. Fix review cancellation handoff
2. Fix alt-model override propagation
3. Add poller concurrency and lighter checkout
4. Split/path-filter CI contract tests
5. Improve cache observability and reviewer memory gating

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` is the biggest single end-to-end latency problem: **p50 3,628.5s**, **p95 4,466.7s**, **50% failure rate**.
- `review_autofix` is the main long-tail workflow: **95 runs**, **60 cancelled**, **p95 1,677.4s**.
- `ci` is consistently expensive: **p50 612.5s**, **p95 653s**.
- `orchestrate_poll` wastes time on both queue starvation and heavy checkout.

**Top failure modes**
- Cancelled review bait run not handing off to successor (`25428461223`)
- Alt-model override mismatch leading to default-model failure (`25417030055`, `25417040196`)
- Poller queue-only failures (`25381014761`, `25383797907`, `25424218738`)
- Prompt/test drift in CI (`25425264723`, `25425830472`)
- Missing token wiring in copilot review (`25389586417`)

**Highest-cost drivers**
- Long E2E waits in `test_and_mark_stable`
- Long/cancelled `review_autofix` executions
- Repeated full-checkout poller runs
- 10-minute CI `lint` runs on narrow prompt changes

**Top 3 prioritized actions**
1. **Ship the review waiter `PIN_SHA` handoff fix immediately.**
2. **Fix alt-model override propagation and assert resolved model before implement starts.**
3. **Add `orchestrate_poll` concurrency + no-work checkout skip.**

## Metrics Appendix

### Overall run metrics

| Scope | Total runs | Success | Failure | Cancelled | Other/skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 295 | 14 | 67 | 624 | 1.4% | 132.5 | 1.0 | 637.0 |

### Key workflow-family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | p50 (s) | p95 (s) | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| `test_and_mark_stable` | 4 | 2 | 2 | 0 | 3628.5 | 4466.7 | Highest end-to-end latency; 50% failure rate |
| `review_autofix` | 95 | 32 | 0 | 60 | 57.0 | 1677.4 | Heavy cancellation churn |
| `ci` | 70 | 65 | 5 | 0 | 612.5 | 653.0 | Consistently ~10 min |
| `orchestrate_poll` | 35 | 32 | 3 | 0 | 56.0 | 903.0 | Failures are queue-only |
| `workflow_log_analysis` | 4 | 4 | 0 | 0 | 2870.5 | 3250.1 | Expensive but successful |
| `implement` | 165 | 18 | 2 | 7 | 1.0 | 191.4 | Many are skipped/other states |

### Deep-dive coverage

| Deep-dive source | Run folders |
|---|---:|
| `errors/` | 14 |
| `slow/` | 14 |
| `recent/` | 11 |
| **Unique deep-dive runs** | **39** |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total `AI_MEMORY_TELEMETRY` retrieve ops | 17 |
| Retrieve hit rate | 35.3% (6/17) |
| Zero-record retrieves | 11 |
| Avg `estimated_tokens` | 19.8 |
| Max `estimated_tokens` | 56 |
| `keyword_method=plain` | 6 |
| `keyword_method=none` | 11 |
| `keyword_method=llm` | 0 |
| Sampled `fail_open: true` retrieves | 0 |
| Sampled `enabled: false` retrieves | 0 |
| Events with `push_attempts > 1` | 3 |

### Prompt cache metrics

| Metric | Value | Evidence |
|---|---:|---|
| Cache enabled seen? | Yes | `OPENROUTER_PROMPT_CACHE_DISABLED: false` in `review_autofix`, `implement`, `orchestrate_poll` |
| Numeric prompt/cache counters emitted? | No (sampled probes all `na`) | Slow runs `25394267845`, `25413999630` |
| Sampled `review_autofix_cache_probe` lines with numeric values | 0 | all sampled `prompt_tokens/cache_*` fields were `na` |
| Cacheable-prefix design intent present? | Yes | `review_autofix` logs explicitly mention stable prefix separation |

### GH API summary

| Metric | Value |
|---|---:|
| Approx. `gh api` lines in deep-dive logs | 1430 |
| Artifact API mentions | 157 |
| GraphQL mentions | 62 |
| Cancel API mentions | 5 |
| Highest sampled API-heavy run | `test_and_mark_stable` **25428461223** with **292** `gh api` lines |

### Example heavy runs

| Run ID | Family | Duration (s) | Dominant issue |
|---|---|---:|---|
| 25416934394 | `test_and_mark_stable` | 4579 | Alt-model implement path stalled in `ai:implementing` |
| 25428461223 | `test_and_mark_stable` | 3427 | Review waiter pinned to cancelled run |
| 25413999630 | `review_autofix` | 2160 | Long codex-agent review path |
| 25394267845 | `review_autofix` | 2027 | Long codex-agent review path |
| 25430699602 | `ci` | 605 | `lint` dominated |
| 25430938203 | `orchestrate_poll` | 57 | Checkout dominated despite zero active tracking issues |

### Token totals

| Metric | Value | Caveat |
|---|---:|---|
| Direct per-run token totals in sampled deep-dive workflow logs | Largely unavailable | Most target workflow logs did not emit `tokens_used`/provider totals |
| Collector-derived total tokens in workflow-log-analysis run **25428493736** | **918,175** | Derived inside the analysis workflow’s own telemetry window, not emitted by each target workflow directly |


## Deep Audit — Workflows & Scripts (2026-05-06)

### Section 1: Bug & Correctness Sweep

#### BUG-001
- **File path** — `.github/workflows/internal-review.yml:91-118`
- **Severity** — Medium
- **Category tag** — `bug`
- **Description** — The PR-resolution gate fails open on GitHub API errors. `existing_pr` is populated with a raw `gh api` call that falls back to `""` on error, and `base_ref` falls back to `'main'` the same way. If either lookup transiently fails, the step still emits `proceed=true`, which can dispatch the no-PR `review_autofix.yml` path even when an open PR already exists, and can also report the wrong base branch.
- **Recommended fix** — Source `scripts/gh_helpers.sh` here and use `gh_retry`/`_safe_gh_jq` for both lookups. If the PR lookup fails after retries, fail closed (`proceed=false`) or hard-fail the step instead of assuming “no PR”. Reuse the repo’s existing retry pattern rather than raw `gh api`.

#### SHELL-001
- **File path** — `scripts/review_commit_changes.sh:448-455; scripts/review_conflict_resolve.sh:993-994`
- **Severity** — Low
- **Category tag** — `shellcheck`
- **Description** — Both scripts set the authenticated remote URL with unquoted expansions: `https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}`. That is an avoidable SC2086 class issue: the token/repo portion is subject to shell word-splitting and glob expansion. The repo already uses the safer quoted form in multiple workflows, e.g. `.github/workflows/implement.yml:334` and `.github/workflows/review_autofix.yml:815`.
- **Recommended fix** — Quote the full URL argument exactly as the workflows already do: `git remote set-url origin "https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}"`. Apply the same normalization to every remaining unquoted `git remote set-url origin` site in shell scripts.

### Section 2: GitHub API Call Redundancy Audit

#### API-001
- **File path** — `.github/workflows/review_autofix.yml:1357-1363; scripts/gh_helpers.sh:761-900`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — The PR-context assembly step performs four separate logical GitHub calls on the happy path: one for PR payload, one for issue comments, one for reviews, and one for review comments. Immediately after that, it reshapes the fetched data into the same consolidated structure that `gh_pr_with_all_comments()` already exposes in `scripts/gh_helpers.sh`. **Current call count:** 4 logical PR-context calls (plus the separate linked-issues GraphQL call). **Proposed call count:** 1 logical PR-context call by using `gh_pr_with_all_comments`, then keep the existing linked-issues query. This is duplicate fetch logic rather than a missing capability.
- **Recommended fix** — Replace the four-call block with a single helper invocation and write its `meta/comments/review_comments` output into the existing files (`PR_META_FILE`, `PR_ISSUE_COMMENTS_FILE`, `PR_REVIEW_COMMENTS_FILE`). If the workflow still needs review-state objects that the helper does not expose, extend `gh_pr_with_all_comments()` in `scripts/gh_helpers.sh` instead of maintaining a second bespoke hydrator.

#### BATCH-001
- **File path** — `scripts/review_rb_judge.sh:146-170`
- **Severity** — High
- **Category tag** — `api-batching`
- **Description** — The judge first fetches linked issue numbers in one GraphQL call, then immediately loops over those numbers and performs one REST `gh api repos/.../issues/<n>` call per issue body. That is an N+1 pattern in a review-blocked path the repo explicitly treats as API-sensitive. **Current call count:** 1 GraphQL call + N REST issue calls. **Proposed call count:** 1 GraphQL call total by fetching `number` and `body` together for all linked issues. This is the strongest batching candidate in the scoped shell scripts.
- **Recommended fix** — Extend the initial GraphQL query so each linked issue node includes `body`, or batch bodies with aliases using the same pattern already used in `.github/workflows/issue_pr_status.yml:295-317`. If a reusable helper is preferred, extend `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh` to support this shape and consume that helper here.

#### API-002
- **File path** — `.github/workflows/issue_pr_status.yml:295-347,503-512`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — The workflow already batches orchestrator/tracking classification for all linked issues with one GraphQL request in the main status-sync step, but the later “Send PR merged Telegram alert” step re-fetches each linked issue body one-by-one just to decide whether to suppress the standalone merged alert. **Current call count:** 1 batched GraphQL classification call in the main step + N per-issue body lookups in the alert step. **Proposed call count:** keep the existing 1 batched call and reduce the alert step to 0 extra classification calls by reusing cached results.
- **Recommended fix** — Persist the tracked/managed classification from the main step into `$GITHUB_ENV` or a small JSON file under `${RUNNER_TEMP}`, then let the merged-alert step read that cache instead of re-querying issue bodies. The existing `ORCH_ALIAS_FRAGMENT` batch in this file is the pattern to extend; no new API shape is needed.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/orchestrate_poll.yml:67-101; .github/workflows/mark-stable.yml:309-335,457-484; .github/workflows/review_autofix.yml:1275-1313`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — The same inline rate-limit wrapper pattern (`_rl_wait` + `_gh_retry`/`gh_retry`) is duplicated across multiple workflows with only trivial naming differences. The copies are already drifting: some print stderr, some do not; some special-case 429/secondary-rate-limit text, some do not; some wrap `gh`, others `gh api`. This increases maintenance cost whenever retry policy changes.
- **Recommended fix** — Move the bootstrap retry logic into a shared module, e.g. `scripts/bootstrap_gh_retry.sh`, with a narrow contract such as `_gh_retry <command...>` and `_rl_wait`. Update the pre-support-fetch callers above to source that module. Post-checkout callers should continue standardizing on `scripts/gh_helpers.sh`.

#### DUP-002
- **File path** — `scripts/label_helpers.sh:102-196; .github/workflows/issue_pr_status.yml:239-249; .github/workflows/review_autofix.yml:3733-3768,3857-3889`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — The repo already has canonical label helpers (`ensure_label_exists` and `set_issue_phase_label_resilient`) in `scripts/label_helpers.sh`, but multiple workflows re-declare fallback versions inline. Those copies hardcode colors/descriptions and POST-only fallback behavior separately, which creates drift risk the next time label semantics or resilient phase swapping change.
- **Recommended fix** — Keep `scripts/label_helpers.sh` as the single owner with the existing signatures `ensure_label_exists <label_name> <repo>` and `set_issue_phase_label_resilient <issue_number> <target_label> <repo>`. Update the listed callers to fetch/source `label_helpers.sh` once and remove the inline function bodies, or introduce one tiny “ensure support helpers present” bootstrap step before these late-stage label mutations.

#### DUP-003
- **File path** — `.github/workflows/issue_pr_status.yml:41-171,466-499,555-590`
- **Severity** — Low
- **Category tag** — `duplication`
- **Description** — `issue_pr_status.yml` repeats the same support-repo clone / fallback-to-main / copy-into-scripts scaffolding three times in one workflow: once for memory helpers, once for merged-alert Telegram helpers, and once for Telegram cleanup. The code is structurally the same but maintained in separate blocks.
- **Recommended fix** — Extract the support-fetch logic into a shared script such as `scripts/fetch_support_assets.sh` with a contract like `fetch_from_ref_or_local <repo_path> <target_path> [allow_main_fallback=true]`. Then call that script from all three steps and reuse the already-fetched helper files instead of recloning the support repo.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001
- **File path** — `.github/workflows/test-and-mark-stable.yml:1187-1558`
- **Severity** — High
- **Category tag** — `expression-limit`
- **Description** — The `Phase 4: Wait for review & autofix to complete` `run:` block contains `${{ }}` interpolations and is already about **19,117 characters**, leaving only **1,883 characters** of headroom before GitHub’s 21,000-character expression hard limit. This block is already packed with inline helper functions, jq filters, and large comments; a modest future edit can push it over the limit and make the whole workflow unloadable.
- **Recommended fix** — Extract the entire wait loop into a dedicated script under `scripts/` (preferred), e.g. `scripts/test_and_mark_stable_wait_review.sh`, and pass only the minimal env needed from YAML. The repo already uses this pattern successfully in `scripts/implement_diagnose_post_codex_failure.sh`, which was extracted specifically to avoid the same limit.

#### EXPR-002
- **File path** — `.github/workflows/test-and-mark-stable.yml:1644-2049`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — The `Phase 4b: Verify bait removed after review` `run:` block also contains `${{ }}` interpolations and is about **17,408 characters**, leaving **3,592 characters** of headroom. It is below the hard threshold but already large enough that adding another retry path or helper function would meaningfully increase expression-length risk.
- **Recommended fix** — Split the inline helper functions (`gh_api_with_retry`, `fetch_canary_to_tmp`, `fetch_pr_head_sha`, `run_pytest`, `classify_pytest_failure`) into an external script and keep the YAML step as a thin wrapper. Reusing the same extraction style as `scripts/implement_diagnose_post_codex_failure.sh` would keep future edits away from the expression ceiling.

**Workflow file size check:** No workflow exceeds the 800 KB early-warning threshold. The largest scoped workflow files are `.github/workflows/review_autofix.yml` (**279,270 chars**) and `.github/workflows/test-and-mark-stable.yml` (**258,259 chars**), both well below GitHub’s 1 MB file limit.

### Section 5: Cross-Cutting Concerns

No `TODO`, `FIXME`, or `HACK` markers were found in the scoped workflows/scripts.

#### CONSIST-001
- **File path** — `.github/workflows/issue_pr_status.yml:264-316,503-512`
- **Severity** — Medium
- **Category tag** — `consistency`
- **Description** — The workflow uses two different definitions of “orchestrator-managed issue”. In the main label-sync step, managed issues are recognized when they have the `ai:orchestrator-managed` label **or** the `Managed by: AI Orchestrator` body marker. In the later merged-alert suppression step, the code checks only the body marker. A label-only managed issue can therefore be classified as orchestrator-managed for closing/labeling but still receive an incorrect standalone “PR merged” Telegram alert.
- **Recommended fix** — Reuse the earlier classification result instead of recomputing a weaker variant. The cleanest fix is to export `MANAGED_ISSUES`/`TRACKING_ISSUES` from the main step and consume them in the alert step; alternatively, move the classification into one shared helper and use that helper in both places.

#### DEAD-001
- **File path** — `scripts/validate_changed_files_syntax.sh:70-73`
- **Severity** — Low
- **Category tag** — `dead-code`
- **Description** — The redaction `case` arm contains overlapping patterns: `*.env*` on line 71 already matches `.envrc` and `.env*`, so the later `*,*.envrc|*,.env*` alternatives on line 73 are unreachable. ShellCheck reports this as SC2221/SC2222. The current behavior still works, but the dead branch obscures the real secret-redaction rule set.
- **Recommended fix** — Remove the shadowed alternatives or rewrite the case arm into a single non-overlapping pattern set. That keeps the redaction policy readable and prevents future edits from being made against a branch that can never match.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BATCH-001, EXPR-001 |
| Medium | 7 | BUG-001, API-001, API-002, DUP-001, DUP-002, EXPR-002, CONSIST-001 |
| Low | 3 | SHELL-001, DUP-003, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Small |
| API call optimization | 3 | Medium |
| Code modularization | 5 | Medium |
| Expression size reduction | 2 | Medium |
| Medium/Low fixes | 4 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-06)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is proven equivalent enough to consolidate without changing filters, pagination, retry/error behavior, cache contracts, or concurrency semantics. `NEEDS_VERIFICATION` means the overlap looks real, but static reading alone cannot prove that freshness, failure handling, or downstream assumptions stay unchanged. `RISKY_SKIP` means the overlap is visible but sits in a retry/race-sensitive/polling/recovery path where auto-consolidation would be unsafe without manual design review.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — `RISKY_SKIP`
- **File path and line ranges** — `scripts/orchestrate_poll_process.sh:3411-3412`, `scripts/orchestrate_poll_process.sh:3466-3468`, `scripts/orchestrate_poll_process.sh:3517-3519`
- **Current call count** — 8 `GET /repos/{repo}/pulls/{final_pr}` calls in one `finalize_integration_merge_if_needed()` invocation when the full path executes
- **Proposed call count** — 3 calls total, one PR snapshot per observation point
- **Endpoint(s)** — `GET /repos/{owner}/{repo}/pulls/{pull_number}`
- **Evidence** — The function fetches the same PR resource repeatedly just to extract different fields (`state`, `mergeable`, `merged_at`) from the same observation point.
  ```bash
  existing_pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  existing_pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
  ```
  ```bash
  pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  pr_mergeable="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' || echo "")"
  pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
  ```
  ```bash
  pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  pr_mergeable="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' || echo "")"
  pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
  ```
- **Proposed fix** — In `finalize_integration_merge_if_needed()`, add a tiny local helper that fetches one PR JSON object per observation point and parses `.state`, `.mergeable`, and `.merged_at` from that one payload; do **not** reuse a snapshot across the `gh pr merge` attempt boundary.
- **Safety rationale** — This sits inside `scripts/orchestrate_poll_process.sh` on the final-merge/self-healing path, which the repo treats as race-sensitive orchestration logic, so it is explicitly not safe for auto-merge treatment.
- **Downstream signal** — Do not auto-implement; a human must review merge-race behavior, retry semantics, and existing `[final-merge]` log expectations before replacing the per-field probes with per-observation PR snapshots.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — `NEEDS_VERIFICATION`
- **File path and line ranges** — `.github/workflows/orchestrate_clarify_respond.yml:65-80`, `.github/workflows/orchestrate_clarify_respond.yml:404-416`
- **Current call count** — 4 issue REST calls for the common orchestrator-managed path with a tracking issue:
  - child issue once in `Check orchestrator metadata`
  - tracking issue once for title in `Check orchestrator metadata`
  - child issue again in `Fetch issue and tracking context`
  - tracking issue again for body in `Fetch issue and tracking context`
- **Proposed call count** — 2 calls total: one child issue snapshot and one tracking issue snapshot, both reused later
- **Endpoint(s)** — `GET /repos/{owner}/{repo}/issues/{issue_number}`
- **Evidence** — The same child issue and the same tracking issue are fetched twice in one workflow job.
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
- **Proposed fix** — Extend `Check orchestrator metadata` to persist the child-issue JSON and a single tracking-issue JSON (title + body) to `${RUNNER_TEMP}` or step outputs, then update `Fetch issue and tracking context` to reuse those snapshots with the current fresh-fetch path kept only as a missing/invalid-cache fallback.
- **Safety rationale** — The overlap is real, but the calls are in different steps with substantial intervening work, so static review cannot prove that mid-job edits to the issue or tracking body are intentionally ignorable.
- **Downstream signal** — Verify whether clarify-respond is required to observe issue/tracking body edits made after the initial metadata step; if not, persist and reuse the first-step snapshots, then run one orchestrator-managed clarify-respond smoke case that exercises the tracking-title alert-silence path.

#### REUSE-002 — `NEEDS_VERIFICATION`
- **File path and line ranges** — `.github/workflows/implement.yml:74-84`, `.github/workflows/implement.yml:542-548`, `.github/workflows/implement.yml:639-644`
- **Current call count** — 2 issue REST calls on the normal non-skip path:
  - early precheck fetch of issue state+labels
  - later full issue metadata fetch
  - the label-validation step already reuses the later file on the happy path
- **Proposed call count** — 1 issue REST call total, with the early fetch expanded and reused downstream
- **Endpoint(s)** — `GET /repos/{owner}/{repo}/issues/{issue_number}`
- **Evidence** — The workflow fetches the same issue once for early gating, then again for prompt/context assembly.
  ```bash
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '{state: (.state // "open"), labels: [.labels[].name]}')"
  ```
  ```bash
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"
  ISSUE_BODY="$(jq -r '.body // ""' "${ISSUE_META_FILE}")"
  ISSUE_TITLE="$(jq -r '.title // ""' "${ISSUE_META_FILE}")"
  ISSUE_NUMBER_JSON="$(jq -r '.number' "${ISSUE_META_FILE}")"
  ISSUE_URL_JSON="$(jq -r '.html_url' "${ISSUE_META_FILE}")"
  ```
  The workflow already contains a local reuse pattern for the later snapshot:
  ```bash
  if [ -s "${ISSUE_META_FILE:-}" ]; then
    ISSUE_LABELS_JSON="$(jq -c '[.labels[].name]' "${ISSUE_META_FILE}" 2>/dev/null || true)"
  fi
  if [ -z "${ISSUE_LABELS_JSON}" ]; then
    ISSUE_LABELS_JSON="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '[.labels[].name]')"
  fi
  ```
- **Proposed fix** — Expand `Precheck approval phase label` to fetch and persist the full issue payload (for example into `${RUNNER_TEMP}` before the runtime workspace exists), then have `Fetch issue metadata` consume that cached payload and keep the current direct fetch only as a cache-miss/parsing-failure fallback.
- **Safety rationale** — The duplicate is clear, but the early precheck exists specifically to fail fast before expensive setup, so a reviewer must confirm that downstream implement context does not need a fresher issue body/title than the precheck snapshot.
- **Downstream signal** — Verify whether implement must reflect issue edits made after the precheck step; if not, persist the precheck payload and compare downstream env/files against current behavior on both a normal implement run and a skip-fast run.

### Dead Calls (DEAD-API-###)
No findings.

### Cross-References to Deep Audit Section
- API-001: NEEDS_VERIFICATION — The consolidation target is real, but helper parity must be checked because the current workflow also materializes `PR_REVIEWS_FILE` and relies on existing retry/pagination behavior.
- BATCH-001: NEEDS_VERIFICATION — The N+1 is valid, but review-blocked judge behavior should be checked for GraphQL body-size/error-handling parity before replacing the per-issue REST fetches.
- API-002: NEEDS_VERIFICATION — Reusing the earlier orchestrator classification cache is the right direction, but a reviewer should confirm the later alert step is supposed to consume the earlier snapshot rather than a freshly re-read body.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | REUSE-001, REUSE-002 |
| RISKY_SKIP | 1 | MERGE-001 |

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.
