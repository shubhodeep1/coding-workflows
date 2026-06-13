## Executive Summary
- `review_autofix` is the main latency/reliability hotspot: family p50 is `9s`, but p95 is `2459.9s`; both observed failures were here. Run `27452733541` failed after `3400s` in `review / codex-agent -> Apply fixes with editor model` even though `REVIEWERS_SUCCESSFUL: 6`, while a similar no-commit path succeeded in run `27395063010` after `3189s`. **Estimated impact:** save ~53-57 minutes per affected run and remove up to 1 of the 2 observed `review_autofix` failures. **Confidence:** high.
- One `implement` run (`27452324372`) drove nearly all model spend: `1,326,191 / 1,346,453` repo tokens (`98.5%`), `13` Codex calls, `1085s`, with `MODEL_REASONING_EFFORT=xhigh`, `MODEL_DIAGNOSE_REASONING_EFFORT=xhigh`, `MODEL_REPAIR_REASONING_EFFORT=xhigh`, and `CODEX_THREAD_REUSE_ENABLED=false`. **Estimated impact:** ~20-40% token reduction on similar heavy implement runs (roughly `265k-530k` tokens/run) plus several minutes of latency. **Confidence:** medium.
- `orchestrate_poll` is healthy but adds recurring idle latency: `25/25` success, p50 `149s`, p95 `200s`; log summaries for runs `27453844723`, `27455695268`, `27452454913`, `27447006829`, and `27445265453` show `poll` dominating runtime and repeated hosted-runner wait. **Estimated impact:** cut `1-3` minutes per wave transition. **Confidence:** medium.
- Skip/no-op control-plane overhead is real but low risk to fix: `123` skipped runs consumed `382s` total; `plan` skipped `30/31`, `implement` `30/31`, `clarify` `32/33`, and `orchestrate_clarify_respond` `31/31`. `review_autofix` sweeps also had `6` zero-candidate runs costing `57s`. **Estimated impact:** save ~6-7 minutes per sampled window and reduce runner contention. **Confidence:** high.
- Cost tuning is partially blind: repo aggregate shows `or_calls=34` but `or_total_tokens=0`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, and `cache_hit_rate=null`; run `27455334066` also spent `1106s` in AI review work with `codex_calls=0`. **Estimated impact:** indirect but high-value for future tuning; current model/cache decisions are under-instrumented. **Confidence:** high.

## Speed Optimizations
1. **Normalize editor-empty/noop handling in `review_autofix`** *(critical path)*  
   - **Evidence:** run `27452733541` (`Internal: AI Review & Autofix`) failed after `3400s` at `review / codex-agent -> Apply fixes with editor model` with `REVIEWERS_SUCCESSFUL: 6`, `AUTOFIX_EDITOR_EMPTY_NOOP: true`, `EDITOR_NOOP_SUSPICIOUS: true`, `EDITOR_NOOP_REFUSAL: false`, empty `EDITOR_COMMIT_PRODUCED`, and `Editor summary file is missing or empty`. Run `27395063010` succeeded after `3189s` with `REVIEWERS_SUCCESSFUL: 6`, `EDITOR_COMMIT_PRODUCED: false`, and `EDITOR_NOOP_SUSPICIOUS: false`.  
   - **Root cause:** inconsistent disposition of “reviewers succeeded, editor produced no validated patch/comment” cases.  
   - **Exact change:** add a structured `noop_verified` outcome in `.github/workflows/review_autofix.yml`/editor post-processing: if reviewer findings are resolved or no actionable fix remains, end successfully and post the disposition; only hard-fail when unresolved required fixes remain.  
   - **Estimated time savings:** ~`3189-3400s` per incident, plus avoided reruns/manual restarts.  
   - **Implementation risk:** medium-low; keep the current fail-closed path when validation or reviewer findings still require a fix.

2. **Short-circuit no-PR / no-diff Claude-branch reviews** *(critical path)*  
   - **Evidence:** run `27455334066` succeeded after `1106s` on the `review-claude-branch-push` path even though the gate logged `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW_NO_PR pr=none`, diff generation logged `[NO_PR_DIFF_AVAILABLE]`, `PR diff snapshot (post gh pr diff) bytes: 0`, and the job already downgraded to `REVIEWER_MODELS: minimax/minimax-m2.5`.  
   - **Root cause:** the no-PR fallback path still runs a reviewer panel even when there is effectively no diff payload.  
   - **Exact change:** when `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW_NO_PR` is true and the synthesized diff is empty/`[NO_PR_DIFF_AVAILABLE]`, skip the full reviewer panel and use a minimal branch-health/comment-only path.  
   - **Estimated time savings:** up to ~`1106s` per no-PR/no-diff run.  
   - **Implementation risk:** medium; preserve a manual override and a lightweight health check so true branch-state issues are still surfaced.

3. **Stage down implement reasoning and enable thread reuse on repair loops** *(critical path)*  
   - **Evidence:** run `27452324372` took `1085s`, used `1,326,191` tokens across `13` Codex calls, and ran `MODEL_EDITOR=openai/gpt-5.4`, `MODEL_DIAGNOSE=openai/gpt-5.4`, `MODEL_REASONING_EFFORT=xhigh`, `MODEL_DIAGNOSE_REASONING_EFFORT=xhigh`, `MODEL_REPAIR_REASONING_EFFORT=xhigh`, with `CODEX_THREAD_REUSE_ENABLED=false` and `MAX_POST_CODEX_REPAIR_ATTEMPTS=3`.  
   - **Root cause:** every phase starts at maximum reasoning effort, and each call rebuilds context from scratch.  
   - **Exact change:** use `high` (or lower) reasoning for the first implement/diagnose pass, reuse the same thread for repair attempts, and escalate to `xhigh` only after failed validation or the second repair attempt.  
   - **Estimated time savings:** **inference:** ~`180-420s` on similar heavy implement runs.  
   - **Implementation risk:** medium; protect with escalation-on-failure and keep `xhigh` for the hardest recoveries.

4. **Reduce `orchestrate_poll` idle tax and runner waits** *(critical path)*  
   - **Evidence:** `orchestrate_poll` had `25` successes, p50 `149s`, p95 `200s`. Runs `27453844723` (`145s`), `27455695268` (`184s`), `27452454913` (`169s`), `27447006829` (`150s`), and `27445265453` (`141s`) all reported `poll` dominating runtime and/or hosted-runner wait.  
   - **Root cause:** state-change detection depends on periodic poll workflows instead of immediate event-triggered checks.  
   - **Exact change:** keep the scheduled poller as a safety net, but also trigger the existing poll workflow on already-available state-change events (merge/close, relevant label transitions, wave issue completion) and widen the passive schedule after consecutive no-op cycles.  
   - **Estimated time savings:** ~`60-180s` per wave transition.  
   - **Implementation risk:** medium; avoid trigger storms by deduping on the tracking issue or integration branch SHA.

5. **Stop dispatching obviously skipped child workflows** *(micro-optimization)*  
   - **Evidence:** skipped-run totals: `plan 30/31` (`89s` total skipped), `implement 30/31` (`71s`), `clarify 32/33` (`121s`), `orchestrate_clarify_respond 31/31` (`101s`). Recent examples: `27454032152` (`plan.if=false`), `27454032157` (`implement` skipped), `27455744931` (`respond` false).  
   - **Root cause:** gating is evaluated after the child workflow already starts.  
   - **Exact change:** move the same `if` predicates up into the parent dispatcher/event source so false branches never launch the child workflow.  
   - **Estimated time savings:** ~`382s` per sampled window, plus less runner contention.  
   - **Implementation risk:** low if the exact same predicates are reused.

## Cost Optimizations
1. **Make `implement` cheaper before tuning anything else**  
   - **Evidence:** run `27452324372` alone used `1,326,191` tokens (`98.5%` of repo total), `13` Codex calls, `1085s`, with `xhigh` reasoning on editor, diagnose, and repair, plus `CODEX_THREAD_REUSE_ENABLED=false`.  
   - **Root cause:** maximum reasoning on every pass and repeated context rebuilds.  
   - **Exact change:** lower first-pass reasoning, reuse the thread for repair loops, and only escalate when validation actually fails.  
   - **Estimated savings:** **inference:** ~`20-40%` on similar runs (`265k-530k` tokens/run).  
   - **Quality-risk notes:** medium; keep escalation-on-failure so hard tasks still get `xhigh`.

2. **Fix prompt-cache observability and stable-prefix reuse**  
   - **Evidence:** repo aggregate shows `or_calls=34` but `or_total_tokens=0`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, and `cache_hit_rate=null`; yet deep-dive runs `27395063010`, `27452144288`, and `27452324372` all logged `OPENROUTER_PROMPT_CACHE_DISABLED: false`.  
   - **Root cause:** cache/provider metrics are not being emitted or parsed correctly, and expensive paths also disable reuse (`WORKSPACE_REUSE_ENABLED=false` in review; `CODEX_THREAD_REUSE_ENABLED=false` in implement).  
   - **Exact change:** emit/cache provider metrics once per model call, dedupe mirrored step logs, and keep stable instruction/reference blocks ahead of high-variance payloads so reuse can work.  
   - **Estimated savings:** not safely quantifiable with current telemetry; likely moderate on multi-call review/implement loops.  
   - **Quality-risk notes:** low for telemetry fixes; medium for reuse changes if stale state is not guarded.

3. **Keep Semble reviewer-context, trim low-value overflow**  
   - **Evidence:** aggregate Semble usage was only `5` calls / `68,223` bytes / `0` runtime fallbacks. Deep-dive review runs surfaced `reviewer-context` payloads of `14,760-15,669` bytes (`27395063010`, `27452733541`, `27455334066`), which are much smaller than the raw PR diff snapshot in run `27395063010` (`94,069` bytes). The same run also pulled `SEMBLE_QUERY target=overflow file=.github/workflows/test-and-mark-stable.yml bytes=6456`.  
   - **Root cause:** reviewer-context queries look targeted, but overflow sometimes pulls unrelated workflow text.  
   - **Exact change:** keep `reviewer-context` prefetch; only allow `overflow` for touched files or explicitly workflow-centric reviews.  
   - **Estimated savings:** small per review run (~`6.5KB` plus downstream prompt expansion), but low-risk noise reduction.  
   - **Quality-risk notes:** low if overflow remains available when the diff actually touches workflows.

4. **Stop paying control-plane cost for zero-candidate sweeps**  
   - **Evidence:** `6` zero-candidate `review_autofix` sweep runs consumed `57s` total; `2` active-run-skip sweeps consumed `15s`. Run `27455581057` paginated all open PRs before dispatching `1` of `2` candidates; runs `27450804862`, `27452496003`, and `27447846984` ended with `candidates=0`.  
   - **Root cause:** sweeps enumerate PRs even when nothing actionable changed.  
   - **Exact change:** trigger the sweep only on relevant AI branch/PR changes, or stop after the first actionable candidate is resolved.  
   - **Estimated savings:** low-to-moderate runner/API cost; token savings minimal.  
   - **Quality-risk notes:** low.

5. **Treat some current telemetry counts as directional, not exact**  
   - **Evidence:** run `27455334066` reports `semble_query_calls=2` and `semble_query_bytes=31338`, but the deep-dive logs expose one unique real `SEMBLE_QUERY target=reviewer-context ... bytes=15669` event duplicated into both the umbrella and split-step logs. The parser currently uses `gh run view --log` and line-scans the concatenated output.  
   - **Root cause:** **inference:** mirrored umbrella/split-step log content can be double-counted; quoted telemetry strings in embedded docs also create false-positive risk.  
   - **Exact change:** dedupe by `run_id + timestamp + event payload`, or ignore umbrella logs when split-step logs exist.  
   - **Estimated savings:** measurement accuracy, not direct runtime savings.  
   - **Quality-risk notes:** none; this improves trust in later cost tuning.

**Model/agent-specific note:** Serena is not a cost lever in this window: repo aggregate has `0` `serena_query_calls`, `0` response bytes, `0` tool calls, `0` fallbacks, and `0` probes; review logs on `27400354851`, `27395063010`, and `27452733541` explicitly show `SERENA_ENABLED: false` or “No Serena log files were found.”

## Reliability Improvements
1. **Unify the editor-empty/noop outcome in `review_autofix`**  
   - **Failure evidence:** run `27452733541` failed after `3400s` with `REVIEWERS_SUCCESSFUL: 6`, `AUTOFIX_EDITOR_EMPTY_NOOP: true`, empty `EDITOR_COMMIT_PRODUCED`, and `Editor summary file is missing or empty`.  
   - **Root cause category:** post-editor disposition/validation mismatch.  
   - **Exact fix:** add a single decision point that classifies “no editor output” as either `noop_verified` or `hard_failure`, based on reviewer findings + remaining required-fix state.  
   - **Expected reliability impact:** could remove `1` of the `2` observed `review_autofix` failures in this window; family failure rate would drop from `7.1%` (`2/28`) toward `3.6%` if this exact mode is eliminated.  
   - **Rollback/fail-open:** keep the current hard fail when unresolved mandatory fixes remain.

2. **Make support-source checkout fail open to `main` before failing hard**  
   - **Failure evidence:** run `27400354851` failed after `150s` at `review / codex-agent -> Checkout PR head branch` with `Failed to checkout workflow support source from ${SCRIPT_REF} or main` and `stage_workflow_support.sh not found...`. The same run later showed `install_semble: Semble not found; attempting install...`, then `SEMBLE_AVAILABLE: true` and `SEMBLE_INDEX_AVAILABLE: true`, so the tool bootstrap itself recovered.  
   - **Root cause category:** workflow-support staging/bootstrap robustness.  
   - **Exact fix:** preflight `stage_workflow_support.sh` earlier; if `${SCRIPT_REF}` is missing, immediately fall back to the `main` support snapshot and continue.  
   - **Expected reliability impact:** removes the second observed failure mode in the sample window.  
   - **Rollback/fail-open:** only hard-fail if both `${SCRIPT_REF}` and `main` snapshots are missing required bootstrap files.

3. **Stop emitting `##[error]` on intentional success paths**  
   - **Failure evidence:** run `27455743262` (`Integration PR readiness check`) succeeded in `9s` but still logged `##[error][integration-pr-readiness] 4/4 sub-issues...` and `##[error]To merge anyway... apply the 'ai:override-incomplete-merge' label`.  
   - **Root cause category:** signal hygiene / operator UX.  
   - **Exact fix:** downgrade non-failing advisory output to `warning`/`notice`, or split it into a failing mode vs informational mode that matches the job conclusion.  
   - **Expected reliability impact:** lowers false-alarm noise and reduces human misreads/re-runs; does not materially change machine failure rate.  
   - **Rollback/fail-open:** keep hard failure only if branch protection is meant to block merge at this stage.

4. **Keep Semble contract-test fallbacks out of runtime incident views**  
   - **Failure evidence:** all `10` repo fallbacks were `semble_contract_test_fallbacks`, not runtime fallbacks. Deep-dive CI runs `27395062930` and `27400479718` logged `SEMBLE_FALLBACK target=overflow ... context=contract-test` against intentionally missing binaries (`.../missing_semble`) for `src/big.py` and `src/small.py`; both CI runs still concluded `success`.  
   - **Root cause category:** test-noise vs runtime-noise separation.  
   - **Exact fix:** continue counting them, but suppress alerting/escalation for `context=contract-test`; keep runtime fallbacks on the primary dashboard.  
   - **Expected reliability impact:** less false-positive rollout concern without changing production behavior.  
   - **Rollback/fail-open:** none needed; runtime fallback handling is already healthy (`semble_runtime_fallbacks=0`).

**Boundary conditions:** repo aggregate shows `break_glass_count=0` and `context_budget_warn_count=0`. A cancelled `review_autofix` run (`27455325400`) logged only the config value `CONTEXT_BUDGET_WARN_RATIO: 0.7`; no actual `CONTEXT_BUDGET_WARN:` event was emitted. Serena had `0` fallbacks and `0` probe failures, so its absence here looks like a disabled feature, not a broken rollout.

## AI Memory Health
- Deep-dive logs contained **6 unique `AI_MEMORY_TELEMETRY retrieve` operations** across `review_autofix` (`27452733541`, `27400354851`, `27395063010`), `implement` (`27452324372`), `plan` (`27452144288`), and `orchestrate` (`27451863493`).
- **Retrieve hit rate:** `1/6 = 16.7%`. The only hit was `implement` run `27452324372` with `records_selected=1`, `estimated_tokens=28`, `keyword_method=plain`, `role=implementation`.
- **Zero-hit retrieves:** `5/6` returned `records_selected=0` — all three reviewer retrieves and both planning retrieves.
- **Average `estimated_tokens`:** `4.7`. **Budget comparison gap:** no retrieval budget field was emitted in the telemetry, so budget utilization cannot be measured from this window.
- **Keyword-method distribution:** `llm=5`, `plain=1`, `none=0`.
- **Fail-open / disabled flags:** none observed. No retrieve had `fail_open: true` or `enabled: false`.
- **Push retry behavior:** mostly healthy; one deep-dive event in run `27455334066` recorded `push_attempts=2` on `phase_completed`, but not on a retrieve.
- **Assessment:** memory emission exists and is functioning, but it is helping `implement` far more than `review_autofix` or planning/orchestration. The smallest safe improvement is to record/retrieve more compact review/planning outcomes keyed to issue/PR lineage so reviewer/planner lookups stop returning empty sets.

## GH API Call Audit
The repo’s own `unattended_system_instructions.md` says: **“Check first, add second,” “Prefer batched GraphQL over per-item REST,” and “Cycle-local caches are first-class.”** The main hotspots in this window are places where the workflows still do broader or repeated lookups than necessary.

1. **`review_autofix` sweep enumerates all open PRs**
   - **Evidence:** run `27455581057` log summary says `gh api --paginate -X GET "repos/${REPOSITORY}/pulls"` fetched all open PRs before `AUTOFIX_SWEEP_DISPATCH pr=#3305`; other sweep summaries show `6` zero-candidate runs and `2` active-run-skip runs in the sample window.  
   - **Pattern:** broad open-PR scan on short control-plane runs.  
   - **Concrete change:** narrow the candidate query to AI/autofix head refs or labels already used by the workflow, and stop after the first dispatch target is found if only one dispatch is allowed.  
   - **Estimated call reduction:** about **1 paginated PR-list call per sweep run**, plus fewer downstream active-run lookups.  
   - **Rate-limit risk reduction:** low-to-moderate.

2. **`review_gate` contains two paginated `/pulls/{PR}/files` fetch sites**
   - **Evidence:** runs `27395063010` and `27455743237` printed two separate `gh api --paginate "repos/${REPOSITORY}/pulls/${PR_NUMBER}/files"` branches inside the `review_gate` step.  
   - **Pattern:** duplicate file-list fetch for doc-only and materiality checks.  
   - **Concrete change:** fetch `pr_files_json` once, cache it in-step, and route both decisions through that single payload; keep one explicit retry/fail-open wrapper.  
   - **Estimated call reduction:** roughly **1 paginated `/files` call per review run**.  
   - **Rate-limit risk reduction:** medium on large multi-page PRs.

3. **Check-run collection polls in a loop**
   - **Evidence:** run `27395063010` logged `CHECK_RUNS_WAIT_TIMEOUT_SECS: 300`, `CHECK_RUNS_POLL_INTERVAL_SECS: 20`, and the step script includes `gh api .../commits/${HEAD_SHA}/check-runs?per_page=100`.  
   - **Pattern:** repeated snapshotting of the same head SHA until the wait loop ends.  
   - **Concrete change:** only poll while check-runs are actually changing or still `queued/in_progress`; after unchanged snapshots, back off harder or reuse the last snapshot and fail open.  
   - **Estimated call reduction:** **inference:** ~`5-10` snapshot calls on worst long reviews.  
   - **Rate-limit risk reduction:** medium.

4. **`implement` re-reads issue metadata/comments multiple times**
   - **Evidence:** run `27452324372` logged multiple issue fetch sites (`gh api "repos/.../issues/${ISSUE_NUMBER}"` early, another `issue_meta_json` fetch later, then `gh api ... > "${ISSUE_META_FILE}"`), plus paginated comments fetch and later `gh issue edit`.  
   - **Pattern:** repeated same-scope REST reads inside one heavy run.  
   - **Concrete change:** materialize issue JSON/comments once near the top of the run and pass file paths through diagnose/repair/post steps.  
   - **Estimated call reduction:** about **2-4 API calls per heavy implement run**.  
   - **Rate-limit risk reduction:** low.

5. **`plan` mutates issue/comment state more than once for auto-answer flows**
   - **Evidence:** run `27452144288` logged repeated `gh issue edit`, comment delete, and comment update/post operations around `/answer [auto-answered-by-orchestrator]` and `/approved [auto-approved-by-plan]`.  
   - **Pattern:** separate delete/edit/post operations for one logical transition.  
   - **Concrete change:** collapse auto-answer progress reporting into an idempotent upsert/update path.  
   - **Estimated call reduction:** **1-3 calls per plan run**.  
   - **Rate-limit risk reduction:** low.

**Observed rate-limit status:** no `429` or secondary-rate-limit evidence surfaced in the sampled runs.

## Prompt Cache & Memory System
- **Prompt cache is under-observed, not demonstrably under-performing.** Repo aggregate has `cache_hit_rate=null`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, and `or_total_tokens=0` even though `or_calls=34`. That means the current window cannot prove cache hit/miss behavior.
- **Infra caches are healthy.** `setup-uv` cache hits appeared in `review_autofix` run `27395063010` and several `orchestrate_poll` summaries (`27455695268`, `27453844723`, `27452454913`, `27448152548`, `27447006829`); `codex-v0.114.0-v2` hit in `plan` run `27452144288`.
- **Reuse is disabled on the expensive paths.** `WORKSPACE_REUSE_ENABLED=false` is present throughout long `review_autofix` runs (`27395063010`, `27452733541`, `27455334066`), and `CODEX_THREAD_REUSE_ENABLED=false` is present in the token-heavy `implement` run `27452324372`.
- **Memory is not yet shrinking most prompts.** Deep-dive memory retrieve hit rate is only `16.7%`, so review/planning paths are not consistently getting pre-compressed context back from AI memory.
- **No prompt growth alarms actually fired.** Repo aggregate has `context_budget_warn_count=0`; no `CONTEXT_BUDGET_WARN:` or `BREAK_GLASS:` lines were observed.
- **Likely cache-fragmentation causes** *(inference)*: high-variance inputs such as PR diffs (`94,069` bytes in `27395063010`), issue comments, and check-run snapshots are present on the longest paths, while reuse is disabled. If those volatile inputs are injected too early, prefix stability will be poor even when the provider cache is enabled.
- **Recommended concrete improvements:**
  1. **Fix cache metric emission first.** Without non-null `cache_hit_rate` and non-zero cache token counters, cache optimization cannot be measured.
  2. **Stabilize prompt prefixes.** Keep static policy/instructions/reference blocks first; append volatile diff/comment/check-run payloads last.
  3. **Enable guarded reuse.** Turn on thread/workspace reuse only when branch cleanliness and workspace checks pass.
  4. **Trim Semble overflow noise.** Keep `reviewer-context`; gate `overflow` by touched-file overlap.
- **Estimated impact:** once measurable, these changes should yield **moderate** token and latency savings on review/implement loops; reliability should also improve by reducing long context rebuild paths. **Confidence:** medium because current prompt-cache telemetry is missing.

## Orchestrator Health
- **Wave decomposition looks healthy.** Run `27451863493` (`Internal: AI Orchestrate`, `659s`) cleanly decomposed the project into four issues, dispatched **Wave 1** with `phase-b-prompt-doc-inventory`, and explicitly deferred `phase-c-plan-severity-commit-discipline`, `phase-d-memory-injection-guard`, and `phase-e-workflow-install-profiles` until dependencies are met.
- **Bootstrap behavior is healthy fail-open, not a rollout issue.** The same run started with `SEMBLE_AVAILABLE: false` / `SEMBLE_INDEX_AVAILABLE: false`, then logged `build_semble_wrapper: Semble wrapper ready...`, then `SEMBLE_AVAILABLE: true` / `SEMBLE_INDEX_AVAILABLE: true`. No runtime Semble fallback was emitted.
- **Poller reliability is strong; poller latency is the problem.** `orchestrate_poll` had `25/25` successes and no failure cluster, but the family adds `149-200s` between decisions.
- **Human-latency reduction is working, but it is aggressive.** In `plan` run `27452144288`, `AUTO_IMPLEMENT_ON_CLEAR_PLAN: true` led to `/answer [auto-answered-by-orchestrator]` and `/approved [auto-approved-by-plan]`. That speeds flow, but it also means some planning runs proceed without fresh human input.
- **No stuck conflict-heal patterns were observed.** No break-glass events, no context-budget warnings, no Serena probe failures, and no repeated merge-conflict repair loops surfaced in the sampled deep dives.
- **Operational indicators worth tracking:**  
  - `review_autofix` editor-empty/noop count  
  - `orchestrate_poll` duration and runner-wait share  
  - auto-answered / auto-approved plan ratio  
  - skipped child-workflow count  
  - AI memory retrieve hit rate for reviewer/planning roles

## Pipeline Flow Bottlenecks
| Stage | Bottleneck | Evidence | Bottleneck type | Highest-value fix |
|---|---|---|---|---|
| Clarify | Many runs start only to skip | `clarify` had `32/33` skipped; skipped clarify runs consumed `121s` total | Dispatch overhead | Hoist gating before child workflow dispatch |
| Plan | Mostly skipped, one long success with comment churn | `30/31` skipped; success run `27452144288` took `449s` and auto-posted `/answer` + `/approved` | Control-plane + comment/API churn | Collapse comment mutations; skip earlier |
| Implement | Dominant AI compute and token sink | run `27452324372`: `1085s`, `1,326,191` tokens, `13` Codex calls | Compute / model cost | Stage reasoning down, reuse thread |
| Review / Autofix | Longest tail and only observed failures | family p95 `2459.9s`; runs `27395063010` `3189s`, `27452733541` `3400s`, `27455334066` `1106s` | Compute + disposition bug | Fix noop outcome; short-circuit no-PR/no-diff path |
| Validate / Orchestrate | Poll latency between waves | `orchestrate` run `27451863493` `659s`; `orchestrate_poll` p50 `149s`, p95 `200s` | Queueing / poll idle time | Event-trigger the existing poller and widen passive cadence |
| Merge governance | Correct but noisy | `integration_pr_readiness` run `27455743262` succeeded in `9s` but logged `##[error]` for 4 unfinished sub-issues | Merge-block signal hygiene | Align log severity with actual conclusion |

**Important:** retries are **not** the dominant bottleneck here. The main outliers were almost all `attempt=1`, `retries=0`; the wasted time is inside single long runs, poll cycles, and control-plane dispatch overhead.

## Per-Repo Breakdown
### shubhodeep1/coding-workflows
- **Top bottlenecks**
  - `review_autofix` long tail: p95 `2459.9s`; outliers `27395063010` (`3189s`), `27452733541` (`3400s`), `27455334066` (`1106s`)
  - `implement` compute spike: `27452324372` (`1085s`, `1,326,191` tokens, `13` Codex calls)
  - `orchestrate_poll` idle latency: p50 `149s`, p95 `200s`, repeated hosted-runner wait
- **Top failure modes**
  - Editor produced no validated output after successful reviewers (`27452733541`)
  - Workflow support-source checkout/staging failure (`27400354851`)
  - Success-with-error-text in readiness gating (`27455743262`)
- **Highest-cost drivers**
  - `implement` run `27452324372` using `xhigh` reasoning everywhere with no thread reuse
  - Missing OpenRouter token/cache telemetry despite `or_calls=34`
  - Minor Semble overflow noise; Serena unused in this window
- **Top 3 prioritized actions**
  1. Add structured `noop_verified` handling to `review_autofix`
  2. Stage `implement` reasoning and enable guarded thread reuse
  3. Trigger `orchestrate_poll` on state changes and hoist skip gates before child-workflow dispatch

## Metrics Appendix
**Metric sources:** aggregate/family metrics below come from `analysis/analysis_context.json`; run-level evidence comes from deep-dive folders under `/home/runner/work/_temp/workflow-log-output/{errors,slow,recent}` plus `workflow_log_report.json` log summaries.

### Repo overview
| Repo | Runs | Success | Failure | Cancelled | Skipped/Other | Success rate | Failure rate | p50 dur (s) | p95 dur (s) | Codex tokens | Codex calls | OR calls | cache_hit_rate | wall p50 / p99 (ms) | break_glass | context_warn |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 197 | 71 | 2 | 1 | 123 | 36.0% | 1.0% | 7.0 | 437.0 | 1,346,453 | 27 | 34 | null | 6,000 / 3,027,300 | 0 | 0 |

### Key workflow families
| Family | Runs | Success | Failure | Cancelled | Skipped/Other | p50 dur (s) | p95 dur (s) | Codex tokens | Codex calls | Semble q / bytes | Semble fallbacks | wall p99 (ms) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 28 | 25 | 2 | 1 | 0 | 9.0 | 2459.9 | 16,208 | 8 | 5 / 68,223 | 0 | 3,372,570 |
| implement | 31 | 1 | 0 | 0 | 30 | 1.0 | 9.5 | 1,326,191 | 13 | 0 / 0 | 0 | 902,250 |
| plan | 31 | 1 | 0 | 0 | 30 | 1.0 | 10.5 | 2,027 | 3 | 0 / 0 | 0 | 370,160 |
| clarify | 33 | 1 | 0 | 0 | 32 | 1.0 | 11.0 | 0 | 0 | 0 / 0 | 0 | 79,800 |
| orchestrate | 1 | 1 | 0 | 0 | 0 | 659.0 | 659.0 | 2,027 | 3 | 0 / 0 | 0 | 659,000 |
| orchestrate_poll | 25 | 25 | 0 | 0 | 0 | 149.0 | 200.0 | 0 | 0 | 0 / 0 | 0 | 204,700 |
| ci | 2 | 2 | 0 | 0 | 0 | 1669.5 | 1714.0 | 0 | 0 | 0 / 0 | 10 | 1,718,010 |
| integration_pr_readiness | 2 | 2 | 0 | 0 | 0 | 9.0 | 9.0 | 0 | 0 | 0 / 0 | 0 | 9,000 |

### Notable outlier runs
| Run ID | Family | Conclusion | Duration (s) | Key signal |
|---|---|---|---:|---|
| 27452733541 | review_autofix | failure | 3400 | editor-empty/noop failure after `REVIEWERS_SUCCESSFUL: 6` |
| 27395063010 | review_autofix | success | 3189 | same family long tail; no-commit success path plus Semble overflow query |
| 27455334066 | review_autofix | success | 1106 | no PR, no diff, still ran reviewer panel |
| 27452324372 | implement | success | 1085 | `1,326,191` tokens, `13` Codex calls, `xhigh` everywhere |
| 27451863493 | orchestrate | success | 659 | wave decomposition + deferred downstream issues |
| 27455695268 | orchestrate_poll | success | 184 | poll/runner-wait dominated |

### AI memory retrieve summary
| Run ID | Family | Role | records_selected | estimated_tokens | keyword_method |
|---|---|---|---:|---:|---|
| 27452733541 | review_autofix | reviewer | 0 | 0 | llm |
| 27400354851 | review_autofix | reviewer | 0 | 0 | llm |
| 27395063010 | review_autofix | reviewer | 0 | 0 | llm |
| 27451863493 | orchestrate | planning | 0 | 0 | llm |
| 27452144288 | plan | planning | 0 | 0 | llm |
| 27452324372 | implement | implementation | 1 | 28 | plain |

**Summary:** hit rate `16.7%` (`1/6`), avg `estimated_tokens=4.7`, `fail_open:true=0`, `enabled:false=0`.

### Semble / Serena telemetry
| Server | Queries | Logged bytes | Avg bytes/query | Fallbacks | Contract-test fallbacks | Runtime fallbacks | Response bytes | Tool calls | Probes ok / failed / skipped |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Semble | 5 | 68,223 | 13,645 | 10 | 10 | 0 | n/a | n/a | n/a |
| Serena | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 / 0 / 0 |

**Deep-dive Semble target breakdown**
| Target | Surfaced query count | Surfaced bytes | Runs | Notes |
|---|---:|---:|---|---|
| reviewer-context | 3 | 46,098 | 27395063010, 27452733541, 27455334066 | looks compact/useful |
| overflow | 1 | 6,456 | 27395063010 | pulled `.github/workflows/test-and-mark-stable.yml`; likely noisy |
| overflow fallback | 10 | 0 | 27395062930, 27400479718 | all `context=contract-test`, files `src/big.py` (8) and `src/small.py` (2) |

**Gap note:** aggregate Semble counts are higher than surfaced deep-dive query lines because one query occurred outside the deep-dive sample, and run `27455334066` also shows a likely mirrored-log double-count artifact.

### Skipped/no-op control-plane runs
| Family | Skipped runs | Total skipped duration (s) | Avg skipped duration (s) |
|---|---:|---:|---:|
| plan | 30 | 89 | 3.0 |
| implement | 30 | 71 | 2.4 |
| clarify | 32 | 121 | 3.8 |
| orchestrate_clarify_respond | 31 | 101 | 3.3 |
| **Total** | **123** | **382** | **3.1** |

### GH API hotspot summary
| Workflow / step | Evidence | Pattern | Estimated avoidable calls |
|---|---|---|---|
| `review_autofix` sweep (`27455581057`) | log summary says `gh api --paginate -X GET "repos/${REPOSITORY}/pulls"` fetched all open PRs | broad PR enumeration on short sweep runs | ~1 paginated PR-list call per sweep |
| `review_gate` (`27395063010`, `27455743237`) | two separate `/pulls/${PR_NUMBER}/files` fetch branches printed in the step | duplicate paginated file-list lookup | ~1 paginated `/files` call per review run |
| check-run collection (`27395063010`) | `CHECK_RUNS_WAIT_TIMEOUT_SECS=300`, `CHECK_RUNS_POLL_INTERVAL_SECS=20`, `/check-runs?per_page=100` collection | repeated polling on same head SHA | **inference:** ~5-10 snapshot calls on worst long runs |
| `implement` (`27452324372`) | repeated issue metadata fetches + paginated comments + label/edit flow | same-scope REST reads reissued in one run | ~2-4 calls per heavy implement run |
| `plan` (`27452144288`) | repeated issue edit/delete/update around auto-answer flow | comment mutation churn | ~1-3 calls per plan run |

### MCP availability
| Server | Target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---:|---:|---:|---|
| Serena | all | 0 | 0 | 0 | `SERENA_ENABLED: false` in sampled review runs; no query/fallback/probe activity |
| Semble | n/a | n/a | n/a | n/a | no `SEMBLE_PROBE` telemetry schema observed in this window |
| Other MCP servers observed | none | 0 | 0 | 0 | no `<NAME>_QUERY/FALLBACK/PROBE` lines beyond Semble |
