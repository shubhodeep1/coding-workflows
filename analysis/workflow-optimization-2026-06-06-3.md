## Executive Summary

- `review_autofix` is the dominant speed and cost hotspot in `shubhodeep1/coding-workflows`: 69 runs, p50 `514s`, p95 `3809.6s`, `461,928` Codex tokens across `228` calls. Outlier run `27052802146` waited `1085.7s` for a runner before `review / codex-agent`, then spent `4262.6s` in the main review step. **Estimated impact:** save `2–20+` minutes on affected runs by cutting queue pressure and no-PR overhead. **Confidence:** high.

- CI has a confirmed repeated regression: runs `27055264741`, `27055527464`, `27055922824`, `27056146674`, `27057075000`, and `27057087357` all failed `tests/test_codex_thread_reuse_core.py` on `assert "codex_thread_reuse.sh; do" in text`. Those six reruns alone consumed `842s` (~`14m`) of runner time. **Estimated impact:** immediate reduction in CI failures and reruns. **Confidence:** high.

- CI also wastes large amounts of time before known failures surface. In run `27054604674` (`CI`), `tests/test_orchestrate_poll_process.py` started `363.9s` into the run, the first failure appeared at `776.5s`, and the job still ran to `1607.5s`. **Estimated impact:** save `2–14m` per failing CI run by moving brittle contract tests earlier. **Confidence:** high.

- Issue/comment routing is overly broad: `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` generated `726` runs, `685` of which were skipped (`94.4%`). That is likely contributing to hosted-runner contention seen in long review waits. **Estimated impact:** lower queue time and cleaner operator signal by collapsing these into one dispatcher workflow. **Confidence:** high.

- Prompt/cache observability is missing where it matters: repo summary shows `cache_hit_rate=null`, `or_calls=0`, and all `or_*` cache/token counters at `0` across `114` telemetry-covered runs, despite `235` Codex calls. **Estimated impact:** medium future savings, because prompt-cache tuning is blocked until telemetry is fixed. **Confidence:** high.

- AI memory is not pulling its weight in sampled deep dives: `10/10` `retrieve` operations selected `0` records, average `estimated_tokens=0`, and `9` memory write operations failed open, concentrated in review runs `27054609696` and `27057066136` with repeated `working tree ... already exists` errors. **Estimated impact:** medium reliability and latency improvement after workspace-collision fixes. **Confidence:** high.

- `validate` is a real downstream problem, but the current window is partially under-instrumented: `9/11` validate runs failed, and `7` recent failures (for example `27057087118`, `27057065875`, `27056146456`) have missing log archives and `0s` durations. **Estimated impact:** high debuggability gain once log retention is fixed; failure root cause remains bounded by missing evidence. **Confidence:** high.

## Speed Optimizations

### Critical-path wins

1. **Collapse skipped issue-comment wrappers into one dispatcher**
   - **Evidence:** `clarify` skipped `179/189` runs, `plan` skipped `167/184`, `implement` skipped `164/176`, and `orchestrate_clarify_respond` skipped `175/177`. Representative skipped runs: `27055561766` (`clarify`), `27055561768` (`plan`), `27055561781` (`implement`), `27055561769` (`orchestrate_clarify_respond`). The internal wrappers `.github/workflows/internal-clarify.yml`, `internal-orchestrate-clarify-respond.yml`, `internal-plan.yml`, and `internal-implement.yml` all trigger broadly on `issue_comment` and/or `issues` events.
   - **Root cause:** routing decisions happen after run creation, so GitHub still allocates and schedules mostly-useless runs.
   - **Exact change:** replace the four separate issue/comment wrapper workflows with one dispatcher workflow that evaluates the comment/issue state once and calls only the matching reusable workflow.
   - **Estimated time savings:** indirect but large; likely the best lever for reducing runner queue time. On outlier run `27052802146`, the queue wait before review pickup was `1085.7s`; this recommendation targets that kind of saturation.
   - **Implementation risk:** medium.

2. **Add a true no-PR fast path in `review_autofix`**
   - **Evidence:** run `27057066136` (`Internal: AI Review & Autofix`, no-PR claude branch review) spent:
     - `88.9s` in `Free disk space`
     - `31.3s` in `Collect PR metadata`
     - `62.3s` in `Collect PR check-run failures (CI/lint autofix context)`
     - total avoidable pre-review overhead: `182.5s` of a `506s` run (`36%`).
     The same run logged `could not determine current branch: failed to run git: not on any branch`, `gh pr diff failed`, and `PR diff snapshot ... bytes: 0`, i.e. PR-specific work ran with no PR.
   - **Root cause:** no-PR review mode still executes PR-only metadata and check-run collection.
   - **Exact change:** when `PR_NUMBER=''`, skip `Collect PR metadata` and `Collect PR check-run failures`; write deterministic empty/sentinel artifacts instead. Add a cheap disk-space threshold guard before `Free disk space`, especially for no-PR light mode.
   - **Estimated time savings:** `90–180s` per no-PR run.
   - **Implementation risk:** low-medium.

3. **Fail fast in CI by moving brittle contract tests to the front**
   - **Evidence:** the thread-reuse contract failed six times in `131–150s` runs (`27055264741`, `27055527464`, `27055922824`, `27056146674`, `27057075000`, `27057087357`). In longer CI failure `27054604674`, the first visible failure in `test_implementation_failed_reissue_preserves_dependency_gates_and_pending_defs` landed at `776.5s`, but the run continued to `1607.5s`.
   - **Root cause:** deterministic contract failures are discovered late in large aggregated test jobs.
   - **Exact change:** run `tests/test_codex_thread_reuse_core.py` and the most failure-prone orchestrator unit tests as a small first-stage CI preflight job before the broader lint/test bundle.
   - **Estimated time savings:** `2–14m` per failing CI run; immediate savings already visible from the six repeated thread-reuse failures.
   - **Implementation risk:** low.

### Micro-optimizations

4. **Trim fixed overhead in `orchestrate_poll`**
   - **Evidence:** run `27057201185` (`Internal: AI Orchestrate Poller`) took `142s`, of which:
     - `12.9s` = `Install Semble`
     - `18.4s` = `Record poll run start`
     - `43.5s` = `Process each tracking issue`
     - `17.2s` = `Record poll run end`
     Combined fixed/setup/bookkeeping time was about `92s` (`~65%`) of the run.
   - **Root cause:** short poll cycles pay setup and memory-write overhead every run.
   - **Exact change:** cache/reuse Semble install between runs, and reduce synchronous memory-helper overhead where safe.
   - **Estimated time savings:** `20–35s` per poll run.
   - **Implementation risk:** medium.

## Cost Optimizations

1. **Shrink oversized review prompts before model fan-out**
   - **Evidence:** failed review run `27054609696` emitted:
     - `CONTEXT_BUDGET_WARN: phase=review prompt_tokens=248188 model_context_window=262144 ratio=0.9468`
     - `... model_context_window=256000 ratio=0.9695`
     - `... model_context_window=200000 ratio=1.2409`
     That run still consumed `145,872` Codex tokens across `72` calls. Repo-wide, `review_autofix` accounts for `461,928` Codex tokens and `22` context-budget warnings. Outlier success `27051284971` also cost `145,872` tokens / `72` calls; `27052802146` cost `121,560` / `60`.
   - **Root cause:** reviewer/editor prompts still carry too much static and overflow context even when retrieval is available.
   - **Exact change:** hard-cap overflow/context bytes per target, dedupe static rubric/policy blocks across reviewer passes, and automatically drop non-critical adjunct context once `CONTEXT_BUDGET_WARN` fires.
   - **Estimated savings:** `20–30%` on heavy `review_autofix` runs, or about `24k–44k` tokens on the `121k–145k` outliers; roughly `92k–138k` tokens across the current `review_autofix` sample if repeated.
   - **Quality-risk notes:** medium; keep the current full path for workflow edits, merge-conflict cases, and high-disagreement reviews.

2. **Extend the existing lightweight reviewer profile to low-risk PR-backed runs**
   - **Evidence:** no-PR review run `27057066136` explicitly set `REVIEWER_REASONING_EFFORT=low`, `ENABLE_REVIEWER_TWO_PASS=false`, and `reviewer_count=3`; its `Run reviewer models` step finished in `250.4s`. Heavy PR-backed review runs still hit `60–72` calls and `121k–145k` tokens.
   - **Root cause:** the low-cost reviewer profile exists, but is currently reserved for a narrow no-PR path.
   - **Exact change:** reuse that light profile for conservative cases: small diffs, no workflow edits, no merge conflict, no failing checks. Escalate back to the current default profile only when risk signals appear.
   - **Estimated savings:** `15–30%` token reduction on eligible review runs.
   - **Quality-risk notes:** medium; this is an inference from workflow defaults and observed no-PR behavior, so gate it conservatively.

3. **Stop building PR-only context when there is no PR**
   - **Evidence:** in `27057066136`, `Collect PR metadata` produced a `0`-byte diff snapshot, and `Collect PR check-run failures` still wrote `17174` bytes of context after waiting `20s` then `40s`.
   - **Root cause:** empty PR state still triggers API work and prompt-context generation.
   - **Exact change:** short-circuit no-PR mode with empty deterministic files for diff/check-run context.
   - **Estimated savings:** several thousand prompt bytes plus about `1` minute of runner time per no-PR run.
   - **Quality-risk notes:** low.

4. **Treat Semble as useful but insufficiently budgeted**
   - **Evidence:** Semble is doing targeted retrieval:
     - `27054609696`: `3` queries / `30819` bytes (`reviewer-context`, `overflow`, `conflict-resolver-context`)
     - `27057066136`: reviewer-context query `10179` bytes
     But in `27054609696`, prompts still hit `248,188` tokens, so Semble did not prevent near-window or over-window prompts.
   - **Root cause:** Semble reduces raw prompt expansion somewhat, but downstream prompt assembly is still too large.
   - **Exact change:** keep Semble, but enforce byte budgets per target and prefer tighter summaries over raw overflow payloads once prompt pressure is detected.
   - **Estimated savings:** medium, especially on conflict/overflow-heavy review runs.
   - **Quality-risk notes:** low-medium; targeted retrieval is still valuable.

5. **Fix prompt-cache telemetry before attempting cache optimization**
   - **Evidence:** repo summary: `cache_hit_rate=null`, `or_calls=0`, `or_total_tokens=0`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0` across `114` runs with log telemetry, despite `235` Codex calls.
   - **Root cause:** likely telemetry-emission gap rather than a meaningful “0 cache usage” state (inference; the collector/parser supports these fields).
   - **Exact change:** restore prompt-cache emission first; only then tune prompt prefix stability.
   - **Estimated savings:** unknown today; this is a prerequisite, not an immediate savings lever.
   - **Quality-risk notes:** none.

6. **Do not overreact to current Serena metrics**
   - **Evidence:** aggregate summary shows `3` Serena queries, `1` fallback, and `1` probe skipped, but no direct live Serena query/fallback/probe lines were retained in sampled production deep dives. `serena_query_response_bytes` and `serena_query_tool_calls` were both `0` in the aggregate.
   - **Root cause:** rollout is too sparsely observed to judge cost efficiency.
   - **Exact change:** keep Serena fail-open, add clearer live target/tool emission, and verify a few production review runs before expanding usage.
   - **Estimated savings:** not yet measurable.
   - **Quality-risk notes:** none; current evidence is too thin.

## Reliability Improvements

1. **Fix the repeated thread-reuse contract regression first**
   - **Failure evidence:** CI runs `27055264741`, `27055527464`, `27055922824`, `27056146674`, `27057075000`, and `27057087357` all failed the same assertion in `tests/test_codex_thread_reuse_core.py`: `assert "codex_thread_reuse.sh; do" in text`.
   - **Root cause category:** code/config regression in workflow generation or wiring.
   - **Exact fix:** repair the generated workflow/template so the expected thread-reuse wiring is present, and keep a targeted contract test pinned to that exact string.
   - **Expected reliability impact:** likely removes the majority of current CI failures immediately (`6` of `9` CI failures in this sample).
   - **Rollback / fail-open:** none needed; this is a correctness fix.

2. **Repair `validate` log retention before guessing at validate root causes**
   - **Failure evidence:** `validate` family had `11` runs and `9` failures; `7` recent failures (`27057087118`, `27057065875`, `27056146456`, `27055922619`, `27055527312`, `27055263802`, `27054603897`) returned missing log archives (`404`) and showed `0s` duration / no failure point.
   - **Root cause category:** observability / collector gap.
   - **Exact fix:** ensure `.github/workflows/validate.yml` always leaves retrievable logs or an `always()` failure artifact/summary, and have the collector retry or record failure-stage metadata when archive download fails.
   - **Expected reliability impact:** faster triage, fewer blind reruns, safer validate gating.
   - **Rollback / fail-open:** logging-only; validate behavior can remain unchanged if artifact upload fails.

3. **Harden `review_autofix` against prompt-size pressure and memory-workspace collisions**
   - **Failure evidence:** failed run `27054609696` had `6` context warnings, `MERGE_CONFLICT=true`, `CONFLICT_RESOLVED=false`, an `AI_MEMORY_TELEMETRY` retrieve with `enabled=false` and `warning="git_error"`, plus repeated `fatal: working tree ... already exists` memory-helper failures. Successful run `27057066136` showed the same working-tree collision pattern, but failed open.
   - **Root cause category:** prompt-size risk + workspace-state collision.
   - **Exact fix:** give AI-memory helper clones unique temp/worktree paths (or guaranteed cleanup), and switch to a reduced-context profile automatically when `CONTEXT_BUDGET_WARN` is emitted.
   - **Expected reliability impact:** fewer degraded review runs, fewer conflict-resolution stalls, better memory-write success.
   - **Rollback / fail-open:** keep memory fail-open semantics even after the fix.

4. **Reclassify Semble fallback counts as “upper bounds,” not production outage proof**
   - **Failure evidence:** aggregate summary reports `176` Semble fallbacks, but slow run `27051284971` contains `108` parsed `SEMBLE_FALLBACK` hits from quoted example text, and CI run `27054604674` carried `5` fallback hits from test-fixture output.
   - **Root cause category:** telemetry parsing / measurement error.
   - **Exact fix:** exclude quoted/fixture lines when parsing `_FALLBACK` signals.
   - **Expected reliability impact:** avoids masking real rollout health with inflated noise.
   - **Rollback / fail-open:** parser-only change.

5. **Keep Serena fail-open until live probe/query evidence is visible**
   - **Failure evidence:** summary shows `1` Serena fallback and `1` probe skipped, but no retained direct production lines identify the target or confirm repeated live failures.
   - **Root cause category:** rollout observability gap.
   - **Exact fix:** add target/tool fields to live Serena emission and sample a few production runs before broadening usage.
   - **Expected reliability impact:** better rollout safety with minimal behavior change.
   - **Rollback / fail-open:** current fail-open behavior is already the right fallback.

**Pressure signals:** repo-wide `BREAK_GLASS=0`, so there is no evidence of policy/rubric override pressure. `CONTEXT_BUDGET_WARN=43` (including `22` in `review_autofix`) points much more strongly to prompt-size risk.

## AI Memory Health

Across the retained deep-dive logs for `shubhodeep1/coding-workflows`, I found `44` `AI_MEMORY_TELEMETRY` lines:

- `25` `record-run-event`
- `10` `retrieve`
- `7` `record-candidate`
- `2` `summarize_unselected_runs`

No `promote`, `compact`, or `processed-command-*` telemetry appeared in the retained deep-dive sample. Outside the step logs, `issue_pr_status` run `27055556554` also reported a successful `finalize-task` event in its `log_summary`.

**Retrieve health**
- **Hit rate:** `0/10` (`0%`) had `records_selected > 0`.
- **Average `estimated_tokens`:** `0`.
- **`keyword_method` distribution:** `9` `llm`, `0` `plain`, `0` `none`; `1` retrieve had no method because retrieval was disabled.
- **Disabled retrieve:** run `27054609696` (`review_autofix`) logged `{"enabled": false, "warning": "git_error", "records_selected": 0}`.
- **Zero-hit examples:** `review_autofix` runs `27051284971`, `27052802146`, `27052515940`, `27054604783`, `27054968839`, plus sampled `workflow_log_analysis` deep dives such as `27050810707`.

**Write-side health**
- `9` fail-open memory writes were observed across `32` write operations (`25` `record-run-event` + `7` `record-candidate`).
- All observed fail-open writes were concentrated in `review_autofix`, especially runs `27054609696` and `27057066136`.
- The repeated failure signature was `fatal: working tree '...already exists'`.

**Good paths**
- `orchestrate_poll` run `27057201185` successfully recorded both `poll_started` and `poll_completed`, each with `push_attempts=1`.
- `issue_pr_status` run `27055556554` successfully finalized lineage state.

**Retry health**
- Only one retained event needed more than one push attempt: `review_autofix` run `27054604783` logged `push_attempts=2` for `phase_started`.

**Assessment**
- Retrieval effectiveness is currently poor.
- The biggest actionable problem is not retrieval quality first; it is workspace stability. Until the `working tree already exists` collisions are fixed, memory writes will keep degrading to fail-open and retrieval confidence will stay low.

## GH API Call Audit

| Workflow / step | Evidence | Observed pattern | Recommended change | Expected reduction |
|---|---|---|---|---|
| `review_autofix` / `Collect PR metadata` | Run `27057066136`, step time `31.3s`; logs show `not on any branch`, `gh pr diff failed`, diff bytes `0` | PR metadata collection still runs in no-PR mode | Skip the step entirely when `PR_NUMBER=''`; emit empty deterministic metadata files | Removes a whole API-heavy step on no-PR runs |
| `review_autofix` / `Collect PR check-run failures (CI/lint autofix context)` | Run `27057066136`, step time `62.3s`; waited `20s` then `40s`; workflow code uses `gh api --paginate --slurp` in a loop | Polling loop repeatedly re-fetches check-runs even in low-value/no-PR mode | Skip in no-PR mode; otherwise prefer one immediate snapshot unless the reviewer truly needs “all checks settled” | ~`60s` and several API calls per no-PR run |
| `issue_pr_status` / `Update linked issue labels when PR closes` | Run `27057087328` step includes `gh api graphql`, PR REST GET, label POST, second GraphQL query, per-issue REST fallback | Mixed GraphQL + REST with per-issue fallbacks | Keep the richer GraphQL payload and only fall back to per-issue REST for missing nodes; reuse PR title/body once | Fewer per-issue calls on PR-close events with multiple linked issues |
| `cancel_on_pr_close` / `Cancel queued/in-progress runs for closed PR branch` | Workflow performs two `actions/runs` list calls (`queued`, `in_progress`) plus one cancel POST per run; family is only `12` runs, p50 `6.5s` | Correct but intentionally double-queries statuses | Low priority; keep as-is unless branch churn grows materially | Low impact today |

**Audit summary**
- The repo already uses rate-limit-aware `gh_retry` wrappers in the hot paths; keep that convention.
- No retained production deep dive showed a live GitHub `429` / secondary-rate-limit event. The main issue is redundancy and unnecessary polling, not obvious rate-limit breakage.
- The highest-value API fix is the no-PR short-circuit in `review_autofix`.

## Prompt Cache & Memory System

**Prompt cache**
- Current repo-wide prompt-cache telemetry is effectively unusable:
  - `cache_hit_rate = null`
  - `or_calls = 0`
  - `or_total_tokens = 0`
  - `or_cache_read_tokens = 0`
  - `or_cache_write_tokens = 0`
- This is an instrumentation gap, not evidence that caching is healthy or irrelevant.

**Prompt-growth pressure**
- Repo-wide `CONTEXT_BUDGET_WARN = 43`; `review_autofix` alone produced `22`.
- `BREAK_GLASS = 0`, so the system is not showing rubric/policy override pressure; it is showing prompt-size pressure.

**Likely cache-fragmentation causes (inference)**
- Dynamic PR metadata, linked-issue context, check-run context, and fallback diffs are assembled early and vary run-to-run.
- In `review_autofix`, the no-PR path still builds PR-only artifacts, which adds prompt variance without value.
- Large overflow/context payloads reduce the practical value of any prefix-based caching.

**Concrete improvements**
1. Restore prompt-cache emission first.
2. Freeze a stable prompt prefix: instructions/rubric first, dynamic metadata last.
3. Canonicalize ordering of context blocks and omit empty PR/check-run blocks entirely.
4. Once `CONTEXT_BUDGET_WARN` fires, stop appending low-value overflow/context.
5. After workspace-collision fixes, add a simple plain-keyword fallback when memory retrieval returns zero records repeatedly.

**Estimated impact**
- **Tokens:** medium, but not quantifiable until cache telemetry is restored.
- **Latency:** medium, because smaller/more-stable prompts reduce per-call cost and retry risk.
- **Reliability:** high, because prompt-size pressure is already producing hard warnings in production review runs.

## Orchestrator Health

- **Routing correctness looks better than routing efficiency.** The clarify/plan/implement/respond families are mostly skipped, not repeatedly retried. That suggests the conditions are logically correct, but they are applied too late.
- **Poller and lineage sync are operationally healthy.**
  - `orchestrate_poll`: `11/11` success.
  - `issue_pr_status`: `12/12` success; run `27055556554` successfully emitted `finalize-task`.
- **The main orchestrator pain is review/autofix, not clarify loops.**
  - `review_autofix`: `69` runs, `49` success, `1` failure, `15` cancelled, `4` skipped.
  - The biggest problems were queue wait, prompt expansion, and workspace collisions.
- **Conflict-heal remains expensive.** Failed run `27054609696` ended with `MERGE_CONFLICT=true` and `CONFLICT_RESOLVED=false`; that is a real flow-health problem, but secondary to context size and workspace stability.
- **Smallest safe operational metrics to track going forward**
  1. review queue wait before `codex-agent`
  2. skipped-wrapper run ratio
  3. `CONTEXT_BUDGET_WARN` count by workflow family
  4. AI memory `fail_open` count by workflow family
  5. validate missing-log count
  6. check-run poll wait seconds in `review_autofix`

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Recommended fix |
|---|---|---|---|
| Intake (`clarify` → `plan` → `implement` → `respond`) | Run-churn / queueing | `685/726` runs skipped across the four issue-comment families | Replace four wrappers with one dispatcher |
| CI | Compute wasted before deterministic failure | `27054604674` ran `1607.5s`; first visible failure at `776.5s` | Preflight brittle contract tests first |
| Review / autofix | Queueing + heavy model compute | `27052802146` waited `1085.7s` for a runner and then spent `4262.6s` in `review / codex-agent`; family p95 `3809.6s` | Reduce queue pressure, shrink prompts, keep fast path for no-PR review |
| Review context collection | Poll / retry overhead | `27057066136` spent `62.3s` polling check-runs with `20s` and `40s` sleeps | Snapshot once or skip in no-PR mode |
| Merge / conflict resolution | Conflict-heal overhead | `27054609696` hit merge conflict plus conflict-resolver Semble context | Shrink conflict-context inputs and fix memory workspace collisions |
| Validate | Downstream opaque blocker | `9/11` validate failures, `7` missing archives | Fix validate log retention before changing gating behavior |
| Orchestrate poll | Fixed setup overhead | `27057201185` spent ~`92s` of `142s` in setup/bookkeeping | Reuse Semble/install state and trim synchronous bookkeeping |

Ordered by end-to-end impact:  
1. Collapse skipped wrappers  
2. Preflight CI contract tests  
3. Add no-PR review fast path  
4. Shrink heavy review prompts  
5. Repair validate observability  
6. Trim orchestrate-poll fixed overhead

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` is the main latency/cost sink: `69` runs, p50 `514s`, p95 `3809.6s`, `461,928` Codex tokens / `228` calls.
- CI is the main deterministic failure sink: `43` runs, `9` failures, p50 `1474s`.
- Issue-comment wrappers are noisy: `685` skipped runs across `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`.
- `validate` is failing often but with poor evidence retention: `9/11` failures.

**Top failure modes**
- Repeated thread-reuse contract regression in CI.
- Late CI unit-test failure in `test_orchestrate_poll_process.py`.
- `review_autofix` context pressure + unresolved merge conflict (`27054609696`).
- Opaque validate failures because archives are missing.

**Highest-cost drivers**
- Heavy `review_autofix` runs:
  - `27051284971`: `145,872` tokens / `72` calls
  - `27054609696`: `145,872` / `72`
  - `27052802146`: `121,560` / `60`
- Repo-wide Semble volume is moderate (`51` queries / `479,628` bytes), but fallback counts are noisy and should not drive rollout decisions yet.
- Prompt-cache telemetry is unavailable, so cache-value cannot be measured.

**Top 3 prioritized actions**
1. Fix the thread-reuse regression and move brittle contract tests to the first CI lane.
2. Consolidate the skipped issue-comment wrappers into one dispatcher workflow.
3. Add a real no-PR fast path in `review_autofix`, then cap review-context growth when `CONTEXT_BUDGET_WARN` fires.

## Metrics Appendix

**Notes**
- Repo-wide summary metrics below use the richer aggregate supplied with the task.
- Workflow-family rows are from direct run-row aggregation in `workflow_log_report.json`, so family telemetry subtotals may not sum exactly to repo totals.
- `SEMBLE_FALLBACK` counts should be treated as **upper bounds** until quoted/fixture lines are excluded from parsing.

### Repo summary

| Repo | Runs | Success | Failure | Cancelled | Other/neutral | Failure rate | p50 s | p95 s | Telemetry-covered runs | Codex tokens | Codex calls | `cache_hit_rate` | OR calls / total tokens | OR cache read / write | `BREAK_GLASS` | `CONTEXT_BUDGET_WARN` | Semble queries / bytes / fallbacks* | Serena queries / fallbacks | Serena probes ok / failed / skipped | Wall p50 / p99 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|---:|---:|---|---|---|---|
| `shubhodeep1/coding-workflows` | 1000 | 271 | 19 | 19 | 691 | 1.9% | 1.0 | 1332.0 | 114 | 474,204 | 235 | null | `0 / 0` | `0 / 0` | 0 | 43 | `51 / 479,628 / 176*` | `3 / 1` | `0 / 0 / 1` | `1,000 / 5,309,700` |

### Key workflow-family metrics

| Family | Runs | Success | Failure | Cancelled | Skipped | p50 s | p95 s | Codex tokens | Codex calls | Context warns | Semble queries / bytes / fallbacks | Serena queries / fallbacks / probes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| `review_autofix` | 69 | 49 | 1 | 15 | 4 | 514.0 | 3809.6 | 461,928 | 228 | 22 | `13 / 135,634 / 90*` | `0 / 0 / 0/0/0` |
| `ci` | 43 | 34 | 9 | 0 | 0 | 1474.0 | 1572.7 | 0 | 0 | 0 | `0 / 0 / 5*` | `0 / 0 / 0/0/0` |
| `validate` | 11 | 2 | 9 | 0 | 0 | 0.0 | 139.0 | 0 | 0 | 0 | `0 / 0 / 0` | `0 / 0 / 0/0/0` |
| `clarify` | 189 | 10 | 0 | 0 | 179 | 1.0 | 53.4 | 0 | 0 | 0 | `0 / 0 / 0` | `0 / 0 / 0/0/0` |
| `plan` | 184 | 17 | 0 | 0 | 167 | 1.0 | 22.8 | 0 | 0 | 0 | `0 / 0 / 0` | `0 / 0 / 0/0/0` |
| `implement` | 176 | 8 | 0 | 4 | 164 | 1.0 | 14.0 | 0 | 0 | 0 | `0 / 0 / 0` | `0 / 0 / 0/0/0` |
| `orchestrate_clarify_respond` | 177 | 2 | 0 | 0 | 175 | 1.0 | 2.0 | 0 | 0 | 0 | `0 / 0 / 0` | `0 / 0 / 0/0/0` |
| `orchestrate_poll` | 11 | 11 | 0 | 0 | 0 | 170.0 | 816.0 | 0 | 0 | 0 | `0 / 0 / 0` | `0 / 0 / 0/0/0` |
| `issue_pr_status` | 12 | 12 | 0 | 0 | 0 | 12.5 | 67.0 | 0 | 0 | 0 | `0 / 0 / 0` | `0 / 0 / 0/0/0` |
| `cancel_on_pr_close` | 12 | 12 | 0 | 0 | 0 | 6.5 | 8.5 | 0 | 0 | 0 | `0 / 0 / 0` | `0 / 0 / 0/0/0` |

### Prompt-cache and prompt-pressure metrics

| Metric | Value |
|---|---:|
| `runs_with_log_telemetry` | 114 |
| `cache_hit_rate` | null |
| `or_calls` | 0 |
| `or_total_tokens` | 0 |
| `or_cache_read_tokens` | 0 |
| `or_cache_write_tokens` | 0 |
| `BREAK_GLASS` | 0 |
| `CONTEXT_BUDGET_WARN` | 43 |
| `review_autofix` context warns | 22 |

### AI memory telemetry (retained deep-dive logs only)

| Scope | Telemetry lines | `retrieve` ops | Retrieve hit rate | Avg `estimated_tokens` | `keyword_method` seen | `enabled=false` retrieves | `record-run-event` fail-open | `record-candidate` fail-open | Max `push_attempts` |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| Deep-dive logs | 44 | 10 | 0% | 0.0 | `llm=9`, `plain=0`, `none=0`, `missing/disabled=1` | 1 | 6 | 3 | 2 |

### GH API hotspot summary

| Workflow / step | Run evidence | Runtime signal | Main redundancy | Suggested reduction |
|---|---|---:|---|---|
| `review_autofix` / `Collect PR metadata` | `27057066136` | 31.3s | PR metadata fetched even when no PR exists | Skip on `PR_NUMBER=''` |
| `review_autofix` / `Collect PR check-run failures` | `27057066136` | 62.3s | Poll loop with repeated check-run fetches | One snapshot or skip in no-PR mode |
| `issue_pr_status` / `Update linked issue labels when PR closes` | `27057087328` | dominant step in a 12s run | GraphQL + REST + per-issue fallback mix | Reuse richer GraphQL payload |
| `cancel_on_pr_close` / `Cancel queued/in-progress runs` | `27057087287` | low | Two list calls by status before cancel POSTs | Low priority; keep unless churn grows |

### MCP telemetry summary

| Server | Queries | Query bytes | Response bytes | Fallbacks | Probe ok | Probe failed | Probe skipped | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `Semble` | 51 | 479,628 | n/a | 176* | n/a | n/a | n/a | Query volume is real; fallback count is inflated by quoted/fixture lines |
| `Serena` | 3 | n/a | 0 | 1 | 0 | 0 | 1 | Live retained evidence is too sparse to judge efficiency |
| Other MCP servers observed | 0 | — | — | — | — | — | — | None in retained direct evidence |

### MCP availability by target

| Server | Target | Probe ok | Probe failed | Probe skipped | Notes |
|---|---|---:|---:|---:|---|
| `Serena` | target not recoverable from retained direct evidence | 0 | 0 | 1 | Aggregate-only signal |

