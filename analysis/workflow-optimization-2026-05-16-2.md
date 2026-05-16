## Executive Summary

- **Fix the two deterministic regressions first.** All 17 observed failures were first-attempt runs (`run_attempt=1`, `retries=0`). Ten failures came from the same ShellCheck parse error at `scripts/review_apply_fixes.sh` line 854 (9 CI runs plus stable-release run 25956662404), and the other 7 CI failures came from the missing `implement.yml` resolved-ref log contract. **Estimated impact:** remove 17/17 observed failures in this window. **Confidence:** high.
- **CI is failing late and wasting most of its wall time.** In `.github/workflows/ci.yml`, `Orchestrate poll process unit tests` is earlier than both `Validation self-test unit tests` and `ShellCheck static analysis`. Run 25957285639 spent 626.8s in `tests/test_orchestrate_poll_process.py` before a 29.4s ShellCheck failure; run 25957836297 spent 525.4s before a 2.7s validation assertion. **Estimated impact:** ~9,300s (~2.6 runner-hours) of CI waste avoided in this window if those cheap checks fail first (inference). **Confidence:** high.
- **`review_autofix` has a low-risk early-exit win on closed PRs.** Run 25958451918 set `PR_CLOSED=true` at 09:29:59, then still spent 120.7s polling check-runs, 103.7s freeing disk, 11.2s installing Semble, and ~35s on memory I/O before the reviewer step finally logged that PR #2645 was closed and skipped. **Estimated impact:** ~4 minutes saved per closed-PR review run. **Confidence:** high.
- **The active-path hotspot is reviewer fan-out plus editor.** Slow review run 25957565280 spent 1805.8s in `Run reviewer models` and 680.9s in `Apply fixes with editor model`—83.8% of the 2969s run. Six deep-dive review runs also showed `x-ai/grok-4.1-fast` failing after 3 attempts, and pass-2 summariser prompts averaged 46,147 bytes with a 136,918-byte outlier in run 25957565280. **Estimated impact:** biggest remaining latency and model-cost lever, though exact dollars are unobservable because runtime token usage was not emitted. **Confidence:** medium-high.
- **Semble looks useful and cheap; memory retrieval does not; Serena is simply off.** Deduped runtime telemetry showed 11 `SEMBLE_QUERY` lines totaling 132,458 bytes at 438.5 ms average latency, with no live review-path fallbacks. The 35 `SEMBLE_FALLBACK` lines were all CI `target=overflow` missing-binary fixtures. AI memory retrieval hit 0/8 times, and runtime Serena telemetry was 0/0/0 with `SERENA_ENABLED=false` in sampled review runs. **Estimated impact:** keep Semble, fix memory retrieval, and do not prioritize Serena rollout yet. **Confidence:** high.

## Speed Optimizations

1. **[Critical-path] Move CI fail-fast checks ahead of the long poll-process test.**
   - **Evidence:** CI family is 16 failures out of 28 runs (57.1%). In run 25957285639 (`CI` → `lint`), `tests/test_orchestrate_poll_process.py` ran from 08:26:42 to 08:37:09 (626.8s), then `ShellCheck static analysis` started at 08:38:24 and failed 29.4s later on `scripts/review_apply_fixes.sh` line 854. In run 25957836297, `Validation self-test unit tests` hit `AssertionError: implement.yml missing resolved-ref log output` 2.7s after it started, but only after ~525.4s of earlier work.
   - **Root cause:** `.github/workflows/ci.yml` currently orders `Orchestrate poll process unit tests` before `Validation self-test unit tests` and far before `ShellCheck static analysis`.
   - **Exact change:** introduce a `preflight` job or reorder the existing `lint` job so `Validation self-test unit tests`, `bash -n`, and `ShellCheck static analysis` run before `tests/test_orchestrate_poll_process.py`.
   - **Estimated time savings:** ~525-627s per failing CI run; ~9,300s across the 16 CI failures in this window (inference from runs 25957285639 and 25957836297).
   - **Implementation risk:** low.

2. **[Critical-path] Exit `review_autofix` immediately after the PR-closed check.**
   - **Evidence:** In run 25958451918 (`Internal: AI Review & Autofix`), step `Check PR state (defense-in-depth)` logged at 09:29:59 that PR #2645 was closed and set `PR_CLOSED=true`. The workflow still ran `Install semble` (11.2s), `Record review run start in memory` (15.4s), `Retrieve reviewer memory context` (4.9s), `Collect PR check-run failures` (120.7s), `Free disk space` (103.7s), and `Record review run completion in memory` (14.7s). The reviewer step only skipped at 09:33:04—184.7s after the closed-PR signal.
   - **Root cause:** `.github/workflows/review_autofix.yml` gates `Run reviewer models` on `env.PR_CLOSED != 'true'`, but the expensive setup steps occur earlier.
   - **Exact change:** move the PR-state check before Semble install / memory retrieval / check-run polling / free-disk cleanup, or apply the same `env.PR_CLOSED != 'true'` guard to those steps.
   - **Estimated time savings:** ~256-271s per closed-PR run, depending on whether completion-memory logging is also skipped.
   - **Implementation risk:** low.

3. **[Critical-path] Shorten or conditionalize the check-run polling loop.**
   - **Evidence:** `review_autofix` step `Collect PR check-run failures (CI/lint autofix context)` polls `repos/.../commits/${HEAD_SHA}/check-runs?per_page=100`. Three deep-dive review runs hit the full 120s wait cap: 25954929371 (1 in-flight check-run), 25956422495 (1), and 25958451918 (2). In 25958451918 the step emitted four ~30s wait messages before timing out.
   - **Root cause:** repeated polling against an unchanged head SHA, even when only queued/in-progress checks remain and the workflow is already fail-open.
   - **Exact change:** snapshot once, then do at most one shortened recheck (15-30s) only if in-flight count is falling; skip the wait entirely when `PR_CLOSED=true`; continue to fail open with the latest snapshot.
   - **Estimated time savings:** up to 120s on affected review runs.
   - **Implementation risk:** low.

4. **[Critical-path on failure] Gate stable-release long-tail jobs on fast validation.**
   - **Evidence:** Stable-release run 25956662404 failed after 3742s in `validate-scripts` → `ShellCheck static analysis`, but the failing validation job itself took only 44.2s. In the same run, `workflow-log-analysis-test` consumed 3702.8s, `Dispatch watch workflow-log-analysis` 3654.0s, and `e2e-smoke-test` 3173.5s.
   - **Root cause:** expensive long-tail jobs start without waiting for a fast script-validation gate.
   - **Exact change:** split out a lightweight `validate-fast` job (YAML lint, `bash -n`, ShellCheck) and add it to `needs` for `e2e-smoke-test` and `workflow-log-analysis-test`.
   - **Estimated time savings:** ~3,650s (~61 minutes) on syntax-failure release runs.
   - **Implementation risk:** low-medium.

5. **[Critical-path, medium risk] Reduce reviewer/editor critical-path depth.**
   - **Evidence:** Run 25957565280 spent 1805.8s in `Run reviewer models`, 680.9s in `Apply fixes with editor model`, and 89.9s in `Run Codex resolver validate stage commit`; reviewer + editor alone were 2486.7s of a 2969s run.
   - **Root cause:** six reviewers, two-pass review, `xhigh` reasoning defaults, and an unstable reviewer provider.
   - **Exact change:** demote `x-ai/grok-4.1-fast`, run pass 2 only when pass-1 findings disagree or diff size crosses threshold, and lower pass-2/editor reasoning on small/no-conflict diffs behind repo variables.
   - **Estimated time savings:** multi-minute reduction on active review runs; exact per-run savings unobservable in this window.
   - **Implementation risk:** medium.

6. **[Micro-optimization] Make free-disk cleanup conditional.**
   - **Evidence:** `Free disk space` took 103.7s in run 25958451918 and 213.6s in run 25957565280, saving 26 GiB in both cases.
   - **Root cause:** the cleanup runs even on closed/no-op paths where the extra 26 GiB is not needed.
   - **Exact change:** run it only after the PR-open gate and only on model-heavy paths, or only when initial free space is below a threshold.
   - **Estimated time savings:** ~1.7-3.6 minutes on closed/no-op review runs.
   - **Implementation risk:** medium-low.

## Cost Optimizations

Exact per-model dollar savings are **not directly observable** in this window because sampled runtime logs did not emit `prompt_tokens`, `completion_tokens`, `total_tokens`, or prompt-cache read/write counters. Rankings below use prompt sizes, retry behavior, and avoidable runner work.

1. **Demote or single-attempt `x-ai/grok-4.1-fast`.**
   - **Evidence:** Six deep-dive `review_autofix` runs—25951655677, 25951672388, 25954929371, 25955886363, 25956422495, 25957565280—showed `Reviewer x-ai/grok-4.1-fast failed after 3 attempts.` Run 25957565280 showed the failure in both review passes.
   - **Root cause:** one unstable reviewer in a six-model, two-pass fan-out burns retries without adding output.
   - **Exact change:** remove `x-ai/grok-4.1-fast` from the default reviewer set, or cap it to one attempt / one pass while leaving the other five reviewers unchanged.
   - **Estimated savings:** repeated failed reviewer invocations on at least 6 deep-dive runs; exact token savings unavailable.
   - **Quality-risk notes:** low-medium. Diversity drops slightly, but five other reviewers remain.

2. **Trim pass-2 summariser context before touching cache-placement logic.**
   - **Evidence:** Across 7 deduped reviewer runs with summariser telemetry, pass-1 prompt size averaged 25,920 bytes (median 24,978) while pass-2 averaged 46,147 bytes (median 30,218). Run 25957565280 hit 136,918 bytes for the pass-2 summariser prompt.
   - **Root cause:** repeated reviewer output and dynamic tail expansion are larger cost drivers than the static prompt prefix.
   - **Exact change:** dedupe repeated reviewer findings, strip boilerplate/status lines, and hard-cap pass-2 input near ~2× the current median (~60,436 bytes) after severity ranking.
   - **Estimated savings:** ~76,482 bytes removed from the 25957565280 pass-2 prompt alone (~56% of that prompt; inference).
   - **Quality-risk notes:** low-medium if dedupe preserves unique findings and severity order.

3. **Lower reasoning on small/no-conflict review paths.**
   - **Evidence:** Sampled review runs logged `REVIEWER_REASONING_EFFORT=xhigh`, `REVIEWER_PASS2_REASONING_SMALL=xhigh`, `REVIEWER_PASS2_REASONING_LARGE=xhigh`, and `EDITOR_REASONING_EFFORT=xhigh`. The workflow already computes diff-size gate signals such as `AUTOFIX_GATE_DET_SKIP_EVAL pr=2645 files=1 additions=2 ...`.
   - **Root cause:** the review stack uses the same expensive reasoning profile even when the diff is small.
   - **Exact change:** lower `REVIEWER_PASS2_REASONING_SMALL` first, then step down editor reasoning on small/no-conflict diffs behind variables; keep pass 1 unchanged until telemetry confirms headroom.
   - **Estimated savings:** likely recurring on small PRs, but exact token savings are not measurable here.
   - **Quality-risk notes:** medium. Roll out behind repo variables and compare review quality on a few PRs first.

4. **Keep Semble enabled; it looks cheaper than the prompt bloat it likely avoids.**
   - **Evidence:** Deduped runtime Semble telemetry showed 11 `SEMBLE_QUERY` lines totaling 132,458 bytes with 438.5 ms average latency. Targets were `reviewer-context` (7), `overflow` (3), and `conflict-resolver-context` (1). By comparison, a single pass-2 summariser prompt reached 136,918 bytes in run 25957565280.
   - **Root cause:** Semble is acting like targeted retrieval; the bigger cost issue is downstream prompt growth.
   - **Exact change:** keep Semble on the reviewer/editor/conflict-resolver path; spend effort on context dedupe, not on disabling Semble.
   - **Estimated savings:** inference only, but Semble is likely reducing prompt expansion rather than adding noisy low-value bytes.
   - **Quality-risk notes:** low. No live review-path Semble fallbacks were observed.

5. **Do not prioritize Serena rollout for cost reasons yet.**
   - **Evidence:** Deduped runtime counts were `SERENA_QUERY=0`, `SERENA_FALLBACK=0`, `SERENA_PROBE=0`. Sampled review runs logged `SERENA_ENABLED=false` / `SERENA_AVAILABLE=false`.
   - **Root cause:** Serena is not active in the runtime path, so it is neither replacing downstream tool/model work nor adding low-value response bytes.
   - **Exact change:** keep Serena disabled until there is a targeted benchmark showing it can replace existing work more cheaply.
   - **Estimated savings:** avoids rollout/debug effort rather than immediate runtime spend.
   - **Quality-risk notes:** low.

6. **Eliminate avoidable rerun-equivalent waste before deeper token tuning.**
   - **Evidence:** All 17 failures had `retries=0`; cost is coming from repeated new runs after deterministic regressions, not from automatic retries. The biggest runner waste is fail-late CI, closed-PR review work, and late stable-release validation.
   - **Root cause:** deterministic regressions plus late surfacing.
   - **Exact change:** implement the speed items above first.
   - **Estimated savings:** likely larger than any near-term cache tweak in this window.
   - **Quality-risk notes:** low.

7. **Add real token/cache telemetry before further cache work.**
   - **Evidence:** `OPENROUTER_PROMPT_CACHE_DISABLED=false` was present, but no sampled runtime log emitted `INFO: openrouter usage`, `prompt_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens`.
   - **Root cause:** observability gap.
   - **Exact change:** make the `Log token usage` step emit those fields for reviewer, summariser, and editor calls; optionally surface existing cache-probe output from `scripts/review_run_reviewers.sh`.
   - **Estimated savings:** none immediately, but it is the prerequisite for exact cost accounting.
   - **Quality-risk notes:** none.

Secondary cost note: `workflow_log_analysis` run 25956673303 logged `tokens_used=140475` for `summarize_unselected_runs` (targeted 100 runs, summarized 83, skipped 17 empty logs). That is real spend, but it is a single observed run and a lower priority than the repeated `review_autofix` and CI waste above.

## Reliability Improvements

This window looks **logic-regression dominated, not flaky**: all 17 failures were first attempts with 0 retries.

1. **Fix the `scripts/review_apply_fixes.sh` parse regression.**
   - **Failure evidence:** CI runs 25938975842, 25938980466, 25954929349, 25956662565, 25956810947, 25956811575, 25956858465, 25957051001, and 25957285639 failed in `lint` → `ShellCheck static analysis`; stable-release run 25956662404 failed in `validate-scripts` → `ShellCheck static analysis`. The logs point to `In scripts/review_apply_fixes.sh line 854:` with `SC1073` / `SC1072`.
   - **Root cause category:** deterministic shell syntax regression.
   - **Exact fix:** repair the malformed redirection/syntax at line 854, then add a dedicated single-file `bash -n scripts/review_apply_fixes.sh && shellcheck scripts/review_apply_fixes.sh` preflight.
   - **Expected reliability impact:** removes 10 of 17 observed failures (58.8%).
   - **Rollback / fail-open:** none; this is static validation.

2. **Restore the `implement.yml` resolved-ref log contract.**
   - **Failure evidence:** CI runs 25955702244, 25956422446, 25957322686, 25957523274, 25957601330, 25957637550, and 25957836297 failed in `lint` → `Validation self-test unit tests`; run 25957836297 logged `AssertionError: implement.yml missing resolved-ref log output`.
   - **Root cause category:** workflow/test contract drift.
   - **Exact fix:** add the missing `echo "Resolved ref: ${{ steps.refctx.outputs.ref || github.event.repository.default_branch }}"` line to `.github/workflows/implement.yml`, or update the test only if the log format was intentionally changed and replaced by an equally stable sentinel.
   - **Expected reliability impact:** removes the other 7 observed failures (41.2%).
   - **Rollback / fail-open:** low risk; this is a logging-contract fix.

3. **Add a fast validation gate in front of CI and stable-release long tails.**
   - **Failure evidence:** the two regressions above surfaced only after 525-627s of CI work, and stable-release run 25956662404 burned 3742s before surfacing a 44.2s validation failure.
   - **Root cause category:** failure surfacing too late in the workflow graph.
   - **Exact fix:** split out `validate-fast` (ShellCheck, `bash -n`, key workflow contract tests) and make long-tail jobs depend on it.
   - **Expected reliability impact:** cuts rerun pressure by turning deterministic regressions into immediate failures.
   - **Rollback / fail-open:** if the gate proves too strict, scope it to syntax + contract tests only.

4. **Treat current Semble fallbacks as healthy fail-open tests, not as a broken rollout.**
   - **Failure evidence:** 35 deduped `SEMBLE_FALLBACK` lines appeared across 7 CI runs, all `workflow_family=ci`, all `target=overflow`, and all with missing-binary paths ending in `/missing_semble`. No live `review_autofix` run showed a Semble fallback.
   - **Root cause category:** intentional fail-open contract-test behavior.
   - **Exact fix:** keep the fail-open behavior, but label fixture-driven fallbacks explicitly (for example by tagging `missing_semble` as a test-fixture reason) so they do not look like production outages.
   - **Expected reliability impact:** improves incident signal quality without changing runtime behavior.
   - **Rollback / fail-open:** none; preserve current fail-open contract.

5. **Do not treat Serena as a reliability incident in this window.**
   - **Failure evidence:** runtime `SERENA_QUERY/FALLBACK/PROBE` counts were all zero, and sampled review runs logged `SERENA_ENABLED=false`.
   - **Root cause category:** disabled feature, not failed rollout.
   - **Exact fix:** no rollout change needed; optionally emit one explicit disabled-state metric so operators can distinguish “off” from “broken.”
   - **Expected reliability impact:** prevents wasted debugging effort.
   - **Rollback / fail-open:** keep it disabled until benchmarked.

## AI Memory Health

- **Telemetry is present, but only meaningfully in `review_autofix`.** Deduped `AI_MEMORY_TELEMETRY` produced 26 events: 16 `record-run-event`, 8 `retrieve`, 1 `record-candidate`, and 1 `summarize_unselected_runs`.
- **Retrieval effectiveness is currently poor.** Runtime retrieve hit rate was **0/8 = 0%**. Every retrieve had `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`, and `enabled=true`. Example: run 25958451918 step `Retrieve reviewer memory context` took 4.9s and returned 0 records; run 25957565280 took 5.1s and also returned 0.
- **The issue is not fail-open churn.** No retrieve logged `fail_open=true`, and none logged `enabled=false`. This looks like “retrieval has nothing useful to retrieve,” not “retrieval is breaking and getting bypassed.”
- **Write-side health is mostly okay.** Runtime write operations showed `push_attempts` distribution of 1=>15, 2=>1, 3=>1. That is not alarming yet, but it is worth watching as a contention signal.
- **Coverage gap:** no sampled deep-dive `AI_MEMORY_TELEMETRY:` lines appeared in `plan`, `implement`, `orchestrate`, `orchestrate_poll`, or `copilot_pull_request_reviewer`.
- **Unobserved ops:** no sampled `finalize-task`, `promote`, `compact`, `processed-command-claim`, or `processed-command-complete` events appeared.
- **One extra analysis-side event exists:** `workflow_log_analysis` run 25956673303 emitted `summarize_unselected_runs` with model `openai/gpt-5.4-mini`, `targeted=100`, `summarized=83`, `skipped_empty_logs=17`, and `tokens_used=140475`.

**Smallest safe fix:** if keyword generation would be `none`, skip the retrieve RPC entirely and log `skipped_no_keywords`; then improve keyword generation from PR title, linked issue text, touched files, and CI failure step names. That should both save ~5s on empty retrieves and improve hit rate.

## GH API Call Audit

- **Main hotspot: `review_autofix` check-run polling.**
  - **Evidence:** step `Collect PR check-run failures (CI/lint autofix context)` repeatedly calls `gh api --paginate --slurp "repos/shubhodeep1/coding-workflows/commits/${HEAD_SHA}/check-runs?per_page=100"`.
  - **Run-specific evidence:** runs 25954929371, 25956422495, and 25958451918 all hit `CHECK_RUNS_WAIT_TIMEOUT` after 120s with 1, 1, and 2 in-flight check-runs respectively.
  - **Redundancy:** the same head SHA is re-polled in a loop even when the result is just “still queued/in progress.”
  - **Concrete change:** cache the first snapshot by `HEAD_SHA`, do one shortened recheck only when count is falling, and reuse the last snapshot when only queued runs remain.
  - **Estimated call reduction:** roughly 60-80% on timeout cases (inference from the observed 4 wait cycles plus final snapshot in run 25958451918).
  - **Rate-limit benefit:** lower risk, though no 429s or secondary limits were observed in this window.

- **`cancel_on_pr_close` is already using good API hygiene.**
  - **Evidence:** run 25958544882 used two API-level filtered `actions/runs` list calls (`status=queued` and `status=in_progress`, both constrained by `event=pull_request` and `branch=${PR_HEAD_REF}`), then optional `POST /actions/runs/{id}/cancel`, with a `/rate_limit` helper only inside the retry path.
  - **Assessment:** this is already the right repo-local pattern: filter at the API layer first, then do local defensive filtering.
  - **Concrete change:** none required now.

- **Other observed API usage is low volume.**
  - **Evidence:** `copilot_pull_request_reviewer` run 25958452431 listed artifacts once via `/actions/runs/{run_id}/artifacts`.
  - **Assessment:** not a hotspot.

**Bottom line:** GH API volume is not a repo-wide problem; one `review_autofix` polling step is.

## Prompt Cache & Memory System

- **Prompt-cache intent is sound, but observability is missing.**
  - `OPENROUTER_PROMPT_CACHE_DISABLED=false` was present in sampled review runs.
  - `Pre-assemble static context (cacheable across runs)` exists in `.github/workflows/review_autofix.yml:1383` and `.github/workflows/plan.yml:794`.
  - But sampled runtime logs emitted **no** `INFO: openrouter usage`, `prompt_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens`.

- **Do not “optimize away” the intentional non-cacheable tail.**
  - `scripts/review_apply_fixes.sh:395-432` explicitly documents why the tail-positioned discipline blocks stay after the cache breakpoint, with a documented non-cached overhead of about **420 tokens/run**.
  - That is cheap compared with the observed pass-2 prompt bloat; the outlier is run 25957565280’s **136,918-byte** pass-2 summariser prompt, not the documented 420-token tail.

- **The real fragmentation/cost issue is dynamic-tail variance.**
  - The pass-2 summariser prompt size spread (median 30,218 bytes, outlier 136,918) is the clearest evidence.
  - Likely dynamic-noise sources are reviewer duplication, check-run context, linked issue context, and unstable ordering of appended context blocks (**inference**).

- **Memory retrieval is currently not contributing to prompt quality.**
  - Retrieve hit rate is 0%.
  - Because all retrieves used `keyword_method=none`, the system is paying latency without improving prompt relevance.

- **Semble is the one targeted-context mechanism that looks net-positive.**
  - 11 live queries, 132,458 bytes total, 438.5 ms average latency, no live review fallbacks.
  - That is modest compared with reviewer/editor prompt sizes.

**Recommended changes, in order:**
1. Emit actual token/cache metrics for reviewer, summariser, and editor calls.
2. Dedupe and severity-rank reviewer findings before pass-2 summarisation.
3. Canonicalize dynamic input ordering (linked issues, changed files, check-run summaries) to reduce avoidable prompt variance.
4. Skip memory retrieval when no keywords can be formed, and improve keyword generation.
5. Keep Semble enabled.

## Orchestrator Health

- **Healthy signals**
  - Skip logic looks healthy, not broken:
    - `clarify`: 172/185 skipped (93.0%), p50 1.0s
    - `plan`: 156/168 skipped (92.9%), p50 1.0s
    - `implement`: 154/168 skipped (91.7%), p50 1.0s
    - `orchestrate_clarify_respond`: 167/168 skipped (99.4%), p50 1.0s
  - That argues **against** a broad clarification-loop or wave-progression problem in this window.

- **Operational pain points**
  - `review_autofix` is the real orchestrator hot spot: 121 runs, 99 success, 18 cancelled, 4 other; p50 326s, p95 2427s.
  - The concurrency block queues PR-backed runs (`cancel-in-progress: false` for normal PR paths). Given 18 cancelled `review_autofix` runs, some churn is likely queued-run replacement rather than hard workflow failure (**inference**).
  - Three deep-dive review runs hit 120s check-run wait caps.
  - Six deep-dive review runs showed the same reviewer-model instability (`x-ai/grok-4.1-fast` failed after 3 attempts).
  - Conflict-heal overhead exists but is secondary: run 25957565280 used one `SEMBLE_QUERY target=conflict-resolver-context` and spent 89.9s in `Run Codex resolver validate stage commit`.

- **Smallest safe mitigations**
  - Add an early `PR_CLOSED` / superseded-run exit before expensive setup.
  - Track cancellation reason explicitly (`closed`, `superseded`, `manual`, `other`) so cancellation churn is diagnosable.
  - Count `CHECK_RUNS_WAIT_TIMEOUT` and reviewer-model failures by model name in the workflow summary.
  - Verify memory telemetry emission in plan/implement/orchestrate families.

- **Indicators to track**
  | Indicator | Current observation | Why it matters |
  |---|---:|---|
  | `review_autofix` cancel rate | 18/121 = 14.9% | Distinguishes healthy supersession from workflow churn |
  | `CHECK_RUNS_WAIT_TIMEOUT` count | 3 deep-dive runs | Captures idle GH API waiting |
  | `x-ai/grok-4.1-fast` fail-after-3 count | 6 deep-dive runs | Captures unstable reviewer provider cost/risk |
  | AI memory retrieve hit rate | 0/8 | Captures memory usefulness |
  | Fast-skip family rates | 91.7%-99.4% skipped | Confirms gating is generally healthy |

## Pipeline Flow Bottlenecks

| Stage | Evidence | Dominant bottleneck type | Recommendation |
|---|---|---|---|
| Clarify → respond gate | `clarify` p50 1.0s, 93.0% skipped; `orchestrate_clarify_respond` 99.4% skipped | Not a current bottleneck | Leave as-is; just keep skip counters visible |
| Plan (active path) | Recent active plan runs in the sample window reached 554-766s (for example 25951224645, 25951227632, 25954591397, 25955325756) while family p95 is diluted by skipped runs | Compute, but with step-level observability gap | Add per-step/token telemetry before tuning plan internals |
| Implement (active path) | Recent active implement runs reached 544-1377s (for example 25954786564, 25955565450, 25926649315) | Compute, but with step-level observability gap | Add per-step/token telemetry before tuning implement internals |
| Review / autofix | `review_autofix` p95 2427s; run 25957565280 spent 1805.8s reviewer + 680.9s editor; run 25958451918 wasted 120.7s check-run wait + 103.7s free-disk after PR close | Compute first, then retry/poll overhead; some queue churn | Early closed-PR exit, shorten check-run wait, demote unstable reviewer, trim pass-2 prompts |
| CI / validate | `ci` p50 728.5s, p95 780.8s, 57.1% failure rate; all 16 failures are in two late checks | Compute + fail-late ordering | Move ShellCheck / validation self-test ahead of long poll-process test |
| Stable-release validation | Run 25956662404 failed after 3742s on a 44.2s validation issue while long-tail jobs still ran | Workflow graph dependency / fail-late | Add `validate-fast` prerequisite before expensive release/audit jobs |
| Orchestrate poll / merge-conflict overhead | `orchestrate_poll` p50 114.0s, p95 221.8s; one deep-dive review run spent 89.9s in conflict resolver validate | Polling / merge-conflict overhead, but secondary | Track conflict-resolver invocation rate; not a top optimization target yet |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` active path: p50 326s, p95 2427s; run 25957565280 spent 2486.7s in reviewer+editor alone.
  - `ci`: 28 runs, 16 failures (57.1%), p50 728.5s; failures surface late.
  - Stable-release failure path: run 25956662404 took 3742s to report a 44.2s script-validation failure.

- **Top failure modes**
  - Shell syntax/ShellCheck regression in `scripts/review_apply_fixes.sh` line 854: 10 failures (9 CI + 1 stable-release).
  - `implement.yml` resolved-ref logging contract drift: 7 CI failures.

- **Highest-cost drivers**
  - Six-model, two-pass review fan-out with one unstable reviewer provider.
  - Oversized pass-2 summariser prompts.
  - Missing runtime token/cache telemetry, which blocks exact cost accounting.
  - Secondary: `workflow_log_analysis` summarization spent 140,475 tokens in one observed run.

- **Top 3 prioritized actions**
  1. Fix the two deterministic regressions and move those checks to the front of CI / release validation.
  2. Add an immediate closed-PR exit in `review_autofix`, then shorten the 120s check-run wait loop.
  3. Demote `x-ai/grok-4.1-fast`, cap/dedupe pass-2 summariser context, and emit real token/cache metrics.

## Metrics Appendix

### Run outcomes and latency

| Scope/family | Runs | Success | Failure | Cancelled | Other | Fail % | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| overall | 945 | 254 | 17 | 20 | 654 | 1.8% | 1.0 | 866.0 |
| cancel_on_pr_close | 16 | 16 | 0 | 0 | 0 | 0.0% | 7.0 | 9.2 |
| ci | 28 | 12 | 16 | 0 | 0 | 57.1% | 728.5 | 780.8 |
| clarify | 185 | 13 | 0 | 0 | 172 | 0.0% | 1.0 | 77.6 |
| copilot_pull_request_reviewer | 26 | 26 | 0 | 0 | 0 | 0.0% | 193.5 | 281.2 |
| forward_merge_stable_to_main | 2 | 2 | 0 | 0 | 0 | 0.0% | 20.5 | 20.9 |
| implement | 168 | 12 | 0 | 2 | 154 | 0.0% | 1.0 | 303.3 |
| issue_pr_status | 16 | 16 | 0 | 0 | 0 | 0.0% | 60.5 | 66.0 |
| memory_maintenance | 1 | 1 | 0 | 0 | 0 | 0.0% | 38.0 | 38.0 |
| nightly_validation_selftest | 1 | 1 | 0 | 0 | 0 | 0.0% | 120.0 | 120.0 |
| orchestrate | 3 | 3 | 0 | 0 | 0 | 0.0% | 493.0 | 653.2 |
| orchestrate_clarify_respond | 168 | 1 | 0 | 0 | 167 | 0.0% | 1.0 | 2.0 |
| orchestrate_poll | 33 | 33 | 0 | 0 | 0 | 0.0% | 114.0 | 221.8 |
| plan | 168 | 12 | 0 | 0 | 156 | 0.0% | 1.0 | 179.6 |
| promote_main_to_stable | 2 | 2 | 0 | 0 | 0 | 0.0% | 23.0 | 25.7 |
| review_autofix | 121 | 99 | 0 | 18 | 4 | 0.0% | 326.0 | 2427.0 |
| test_and_mark_stable | 1 | 0 | 1 | 0 | 0 | 100.0% | 3742.0 | 3742.0 |
| update_workflows | 1 | 0 | 0 | 0 | 1 | 0.0% | 1.0 | 1.0 |
| validate | 2 | 2 | 0 | 0 | 0 | 0.0% | 175.5 | 219.1 |
| validation_refresh | 2 | 2 | 0 | 0 | 0 | 0.0% | 209.0 | 213.5 |
| workflow_log_analysis | 1 | 1 | 0 | 0 | 0 | 0.0% | 3634.0 | 3634.0 |

*`Other` is the collector’s non-success/non-failure/non-cancelled bucket; in this dataset it is dominated by skipped runs.*

### Failure clusters

| Failure point | Count | Representative runs |
|---|---:|---|
| `ci / lint / ShellCheck static analysis` | 9 | 25954929349, 25956662565, 25956810947, 25956811575, 25956858465, 25957051001, 25957285639 |
| `test_and_mark_stable / validate-scripts / ShellCheck static analysis` | 1 | 25956662404 |
| `ci / lint / Validation self-test unit tests` | 7 | 25955702244, 25956422446, 25957322686, 25957523274, 25957601330, 25957637550, 25957836297 |

### Observed token and cache metrics

| Prompt/cache metric | Observed value | Notes |
|---|---|---|
| `OPENROUTER_PROMPT_CACHE_DISABLED` | `false` in sampled review runs | Prompt cache is intended to be on. |
| Runtime `INFO: openrouter usage` lines | 0 | Reviewer/editor runtime usage was not emitted. |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | not emitted | Blocks exact dollar analysis for `review_autofix`. |
| `cache_creation_input_tokens` / `cache_read_input_tokens` | not emitted | Cache hit/miss rate is unobservable in this window. |
| Static prefix assembly | present | `.github/workflows/review_autofix.yml:1383`, `.github/workflows/plan.yml:794`, `scripts/build_static_context.sh`. |
| Intentional non-cacheable tail | ~420 tokens/run | Documented in `scripts/review_apply_fixes.sh:395-432`; should not be hoisted above first dynamic embed. |
| Reviewer summariser prompt bytes | pass1 avg 25,920 / median 24,978; pass2 avg 46,147 / median 30,218 | Outlier: run 25957565280 pass2 prompt = 136,918 bytes. |
| Explicit token total observed | 140,475 | `workflow_log_analysis` run 25956673303 `summarize_unselected_runs`. |

### Semble / Serena / MCP telemetry

| Server | Queries | Fallbacks | Probes | Query bytes | Avg query ms | Response bytes observed | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| Semble | 11 | 35 | 0 | 132,458 | 438.5 | n/a | Queries only in `review_autofix`; fallbacks only in CI contract-test paths. |
| Serena | 0 | 0 | 0 | 0 | n/a | 0 | No runtime telemetry; sampled review logs showed `SERENA_ENABLED=false`. |
| Other MCP servers observed | 0 | 0 | 0 | 0 | n/a | 0 | None in sampled runtime logs. |

| Semble target | Query count | Query bytes | Avg ms | Fallback count | Evidence |
|---|---:|---:|---:|---:|---|
| reviewer-context | 7 | 99,873 | 454.6 | 0 | Review runs 25927682586, 25951655677, 25951672388, 25954929371, 25955886363, 25956422495, 25957565280 |
| overflow | 3 | 21,439 | 421.7 | 35 | Queries in 25956422495 and 25957565280; all 35 fallbacks were CI `target=overflow` missing-binary fixtures |
| conflict-resolver-context | 1 | 11,146 | 377.0 | 0 | Run 25957565280 |

| MCP server | Target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---:|---:|---:|---|
| Serena | all | 0 | 0 | 0 | No runtime `SERENA_PROBE` lines emitted; sampled review logs showed `SERENA_ENABLED=false`. |
| Other MCP servers | all | 0 | 0 | 0 | No other `*_PROBE` telemetry observed. |

### AI memory metrics

| AI memory metric | Value | Notes |
|---|---:|---|
| Deduped telemetry events | 26 | 16 `record-run-event`, 8 `retrieve`, 1 `record-candidate`, 1 `summarize_unselected_runs` |
| Runtime retrieve count | 8 | All from `review_autofix` |
| Retrieve hit rate | 0% | 0/8 had `records_selected > 0` |
| Avg estimated_tokens on retrieve | 0 | No retrieve budget field emitted |
| `keyword_method` distribution | `none=8` | No `llm` or `plain` keyword extraction observed |
| `enabled=false` retrieves | 0 | All retrieves had `enabled=true` |
| `fail_open=true` retrieves | 0 | No explicit fail-open activations logged |
| Push attempts on write ops | `1=>15, 2=>1, 3=>1` | Occasional retry contention, but all observed writes succeeded |
| Non-review memory coverage | 0 sampled telemetry lines | No `AI_MEMORY_TELEMETRY:` seen in plan, implement, orchestrate, orchestrate_poll, or Copilot deep dives |
| `summarize_unselected_runs` tokens | 140,475 | Run 25956673303; targeted 100, summarized 83, skipped_empty_logs 17 |

### GH API summary

| Workflow / job / step | Observed API pattern | Sample evidence | Redundancy / risk | Safe optimization |
|---|---|---|---|---|
| `review_autofix / review-codex-agent / Collect PR check-run failures` | Repeated `gh api --paginate --slurp repos/.../commits/${HEAD_SHA}/check-runs?per_page=100` polling loop | Runs 25954929371, 25956422495, 25958451918 hit 120s timeout with 1, 1, and 2 in-flight check-runs | Highest call volume in sampled logs; repeated unchanged head-SHA lookups; no 429s observed | Poll once, then one shortened recheck; reuse first snapshot when only queued runs remain |
| `cancel_on_pr_close / cancel-active-runs / Cancel queued/in-progress runs for closed PR branch` | Two filtered `actions/runs` GETs plus optional POST cancel | Run 25958544882 found no matches; branch+event filtering already applied; retry helper consults `/rate_limit` | Low redundancy; bounded response size | Keep as-is; this is good repo-local API hygiene |
| `copilot_pull_request_reviewer / Cleanup artifacts` | One `/actions/runs/{run_id}/artifacts` list call | Run 25958452431 log summary | Not a hotspot | No change needed |

## Deep Audit — Workflows & Scripts (2026-05-16)

### Section 1: Bug & Correctness Sweep

- **BUG-001**
  - **File path:** `.github/workflows/review_autofix.yml:4451-4471,4572-4592,5326-5344`
  - **Severity:** High
  - **Category tag:** `bug`
  - **Description:** When `LINKED_ISSUES_JSON` is empty, three late-stage `review_autofix` paths fall back to a broad regex that matches bare `issues/123` and `issue #123` references, then immediately mutates every matched issue by setting `ai:ready-to-merge` or `ai:review-blocked`. That is broader than the hardened fallback already shipped in `.github/workflows/issue_pr_status.yml:195-210`, whose comments explicitly document that bare prose references caused false positives on orchestrator-tracking issues. As written, a PR body that merely mentions an issue can still cause unrelated issue-label mutations during ready-to-merge and review-blocked flows.
  - **Recommended fix:** Reuse the `issue_pr_status.yml` closing-keyword-only fallback contract here, or centralize linked-issue extraction in one helper and make `review_autofix`/`review_rb_judge.sh` call that shared implementation.

- **CONSIST-001**
  - **File path:** `.github/workflows/review_autofix.yml:4410-4445,4547-4566,5311-5318`
  - **Severity:** Medium
  - **Category tag:** `consistency`
  - **Description:** The late-stage fallback `set_issue_phase_label_resilient()` definitions only do `POST /labels` for the target label. The canonical implementation in `scripts/label_helpers.sh:160-196` first reads current labels and `PUT`s a phase-cleaned label set, which is required by `.github/ai/label_contract.v1.json:149-171`'s single `issue_phase` group. These fallback blocks are explicitly intended to run after helper artifacts may have been deleted during commit cleanup, so this is not dead code: if the fallback fires, an issue can retain contradictory phase labels like `ai:done` plus `ai:ready-to-merge` or `ai:review-blocked`.
  - **Recommended fix:** Cache a durable copy of `scripts/label_helpers.sh` in `${RUNNER_TEMP}`/runtime state before cleanup and source that in late steps, or inline the full GET/PUT phase-replacement logic from `scripts/label_helpers.sh` in both `review_autofix.yml` and `.github/workflows/issue_pr_status.yml:241-248`.

### Section 2: GitHub API Call Redundancy Audit

- **BATCH-001**
  - **File path:** `.github/workflows/review_autofix.yml:514-540`
  - **Severity:** Medium
  - **Category tag:** `api-batching`
  - **Description:** In the standalone-validate dispatch step, the fallback path already does one GraphQL `closingIssuesReferences` lookup and one REST PR fetch, then loops over every extracted issue number with `gh issue view ... --json labels` to detect `ai:orchestrator-validate-required`. **Current call count:** `N + 2` logical API calls on the fallback path for `N` extracted issues. **Proposed call count after fix:** `3` logical calls (`closingIssuesReferences`, PR metadata, one batched label lookup). **Existing batching pattern to extend:** `scripts/orchestrate_poll_process.sh:1516-1582` (`_fetch_issue_labels_batch_graphql`).
  - **Recommended fix:** After building `issue_numbers`, issue one aliased GraphQL labels query and drive the loop from that JSON instead of per-issue `gh issue view`.

- **BATCH-002**
  - **File path:** `scripts/orchestrate_poll_process.sh:10707-10733`
  - **Severity:** Medium
  - **Category tag:** `api-batching`
  - **Description:** After `REVIEW_BLOCKED_STATE_CHANGED=true`, the poller rebuilds `LABELS_JSON` by making one `/issues/{n}/labels` call for every current-wave issue and every reissued issue. The same file already batch-fetches labels earlier with `_fetch_issue_labels_batch_graphql`. **Current call count:** `W + R` logical label fetches for `W` current-wave issues plus `R` reissued issues. **Proposed call count after fix:** `ceil((W + R) / 25)` logical GraphQL calls, with REST only as the existing per-key fallback. **Existing batching pattern to extend:** `scripts/orchestrate_poll_process.sh:1516-1582` (`_fetch_issue_labels_batch_graphql`).
  - **Recommended fix:** Rebuild a single JSON array of issue numbers after review-blocked handling and call `_fetch_issue_labels_batch_graphql` once, then patch only missing keys with the existing REST fallback logic.

- **BATCH-003**
  - **File path:** `scripts/orchestrate_poll_process.sh:6872-6885`
  - **Severity:** Medium
  - **Category tag:** `api-batching`
  - **Description:** `run_standalone_stall_recovery()` iterates over every tracking issue and fetches `/issues/{tracking}/comments` individually just to recover the latest orchestrator state. **Current call count:** `T` logical paginated comment fetches per sweep for `T` tracking issues. **Proposed call count after fix:** `ceil(T / 25)` logical GraphQL calls. **Existing batching pattern to extend:** `scripts/orchestrate_poll_process.sh:6393-6475` (`_fetch_candidate_issue_details_graphql`), which already returns recent comments keyed by issue number.
  - **Recommended fix:** Collect tracking issue numbers into one JSON array, batch-fetch their recent comments with the existing GraphQL alias pattern, and feed `extract_latest_valid_orchestrator_state` from the returned `comments` field.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001**
  - **File path:** `.github/workflows/review_autofix.yml:521-524,4451-4458,4572-4579,5326-5333`
  - **Severity:** Low
  - **Category tag:** `duplication`
  - **Description:** The PR-body/title issue-number extraction regex is duplicated four times inside `review_autofix`, and another copy exists in `scripts/review_rb_judge.sh:241-245`. `issue_pr_status.yml:195-210` already has a safer, different fallback, so the duplicated copies have drifted and are now the direct cause of BUG-001.
  - **Recommended fix:** Move this into a shared helper module, preferably `scripts/gh_helpers.sh` or a new `scripts/issue_link_helpers.sh`.
    - **Shared module:** `scripts/issue_link_helpers.sh`
    - **Function signature:** `extract_linked_issue_numbers_from_pr_text <repo_slug> <mode>`
    - **Callers to update:** `.github/workflows/review_autofix.yml`, `scripts/review_rb_judge.sh`, `.github/workflows/issue_pr_status.yml`

- **DUP-002**
  - **File path:** `.github/workflows/test-and-mark-stable.yml:468-482,1233-1255,1728-1750,4655-4668`
  - **Severity:** Low
  - **Category tag:** `duplication`
  - **Description:** The release smoke workflow contains four local GH retry/backoff wrappers, and `.github/workflows/mark-stable.yml:340-352,489-501` repeats two more. They all solve the same rate-limit/retry problem with slightly different names and logging, which makes later policy changes easy to miss.
  - **Recommended fix:** Extract one shared release-facing GH API helper and source it after checkout.
    - **Shared module:** `scripts/gh_helpers.sh` (or `scripts/release_gh_helpers.sh`)
    - **Function signature:** `gh_api_retry <max_attempts> <gh-args...>` and `gh_api_safe_json <gh-args...>`
    - **Callers to update:** `.github/workflows/mark-stable.yml`, `.github/workflows/test-and-mark-stable.yml`

### Section 4: Expression Size Limit Risk Assessment

No new expression-limit findings beyond the historical incidents already captured in the report. Scanning every current `${{ ... }}` block found no expression over the 15,000-character medium-risk threshold; the largest current block is 234 characters, leaving 20,766 characters of headroom under GitHub's 21,000-character limit. The largest workflow file is `.github/workflows/review_autofix.yml` at 332,294 characters, leaving 716,282 characters below the 1 MiB file cap; no workflow exceeds the 800 KB alert threshold.

### Section 5: Cross-Cutting Concerns

- **DEAD-001**
  - **File path:** `scripts/orchestrate_poll_process.sh:5219-5238`
  - **Severity:** Low
  - **Category tag:** `dead-code`
  - **Description:** `get_standalone_state_comment_id()` and `read_standalone_state_json()` are defined but have no callers anywhere in the repository. Both still contain their own paginated issue-comment fetch logic, so they add unused API-touching code and maintenance surface.
  - **Recommended fix:** Delete these helpers, or wire existing standalone-state paths to them and add a direct caller test so the code becomes live again.

- **DEAD-002**
  - **File path:** `scripts/review_issue_ledger.sh:67-94,866-918`
  - **Severity:** Low
  - **Category tag:** `dead-code`
  - **Description:** `read_anchor_context()` parses `line_end` but never uses it, and the ledger merge loop stores `CURRENT_FLOOR["${issue_id}"]` without any later read. Both are confirmed by ShellCheck as unused, so they currently obscure the real inputs to anchor selection and issue identity without changing behavior.
  - **Recommended fix:** Remove the unused variables, or explicitly consume them in later ledger merge/output logic if range-end and floor metadata are supposed to affect decisions.

- **SHELL-001**
  - **File path:** `scripts/validate_process.sh:229-236`
  - **Severity:** Low
  - **Category tag:** `shellcheck`
  - **Description:** `tg_notify()` uses `local msg="$1$(_tg_link_suffix)"`. In bash, `local` returns success even if the command substitution fails, which is why ShellCheck reports SC2155 here. Under `set -euo pipefail`, that masks `_tg_link_suffix()` failures instead of propagating them.
  - **Recommended fix:** Split declaration and assignment (`local msg; msg="$1$(_tg_link_suffix)"`) so suffix-generation failures keep their non-zero status.

No `TODO`, `FIXME`, or `HACK` markers were present in `.github/workflows/*.yml` or `scripts/*`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 4 | CONSIST-001, BATCH-001, BATCH-002, BATCH-003 |
| Low | 5 | DUP-001, DUP-002, DEAD-001, DEAD-002, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 5 | Medium |
| Expression size reduction | 0 | Small |
| Medium/Low fixes | 6 | Medium |
