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

