## Executive Summary

_All findings below are for `shubhodeep1/coding-workflows`. Aggregate metrics come from the collector window; run-specific evidence comes from deep-dive logs, plus evidence-grade run-row summaries where no deep-dive folder was collected._

- `review_autofix` is the dominant latency and stability problem. It accounted for `130` runs with `80` successes, `9` failures, and `38` cancellations; average duration was `1109.192s`, family p95 was `4171.15s`, and cancellations were `29.2%` of the family (`38/130`). In the full 1,000-run window, `60/61` runs at or above `1000s` and all `42/42` runs at or above `1800s` were `review_autofix`. **Estimated impact:** ~20–45% reduction in long-tail end-to-end latency if the top `review_autofix` failure modes are fixed. **Confidence:** high.

- Two `review_autofix` failures hit the hard `60` minute cap in `Internal: AI Review & Autofix` → job `review / codex-agent` → step `Run Codex resolver, validate, stage, commit` (`run_id=25616314314`, total `6469s`; `run_id=25613903546`, total `6092s`). The resolver script has retry logic, but unlike the editor path it lacks a per-attempt watchdog around `codex exec`. **Estimated impact:** save ~30–50 minutes on pathological conflict runs and remove a major p95 tail driver. **Confidence:** high.

- Failed runs are burning expensive model work before failing in non-durable ways. In `review_autofix`, multiple failures reached `REVIEWERS_SUCCESSFUL: 6` and then died because the editor produced no usable output (`run_id=25613657806`, `25615068886`, `25617161072`, `25613659201`). In `implement`, `run_id=25614767039` retried five times with “Codex returned output but produced no file changes” and still logged `42,103` tokens on attempt 5 before failing. **Estimated impact:** ~60–80% cost reduction on these failure cases, plus 5–20 minutes saved per affected run. **Confidence:** high.

- Prompt/template validation is failing too late. `review_autofix` runs `25620282902` and `25619919130` failed in `Run reviewer models` on `Unresolved WORKFLOW_EDIT_RESTRICTION placeholder ... reviewer_prompt_body.txt`, meaning the workflow reached model execution before detecting a template/rendering bug. **Estimated impact:** convert several-minute failures into near-immediate preflight failures and reduce misleading reruns. **Confidence:** high.

- Orchestrator state persistence is still a correctness risk on large tracking issues. In recent `review_autofix` run `25621080933`, the embedded bug summary states that state snapshots above GitHub’s `65,536` byte comment limit became a silent no-op; on tracking issue `#2373`, that stale state re-advanced a wave and created six duplicate issues (`#2390`, `#2395`, `#2399`, `#2410`, `#2420`, `#2421`) from a ~`75 KB` wave-3 state. **Estimated impact:** eliminate duplicate issue churn and stale-wave loops on large orchestrator projects. **Confidence:** high.

- AI memory retrieval is active but underperforming on the review path, and prompt-cache telemetry is not actionable. Across deep-dive logs, `AI_MEMORY_TELEMETRY` showed `20` retrieves with only `2` hits (`10.0%`); all sampled reviewer retrieves were zero-hit, while the only hits came from `implement` runs `25615174460` and `25614767039`. Meanwhile sampled prompt-cache probe logs in `review_autofix` (`25620282902`, `25617161072`, `25616314314`) emitted `prompt_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, and `cache_read_input_tokens=na`. **Estimated impact:** moderate cost/context-quality gains once retrieval inputs and cache telemetry are fixed. **Confidence:** high on diagnosis, medium on upside.

## Speed Optimizations

### 1) Critical-path win: add a budgeted watchdog to the merge-conflict resolver

- **Evidence:** In `shubhodeep1/coding-workflows`, `review_autofix` runs `25616314314` (`6469s`) and `25613903546` (`6092s`) both failed in workflow `Internal: AI Review & Autofix`, job `review / codex-agent`, step `Run Codex resolver, validate, stage, commit`, with `The action 'Run Codex resolver, validate, stage, commit' has timed out after 60 minutes.` In code, `.github/workflows/review_autofix.yml:3694-3714` sets `timeout-minutes: 60`, while `scripts/review_conflict_resolve.sh:540-586` runs `codex exec` directly without the per-attempt wall-clock watchdog that already exists in `scripts/review_apply_fixes.sh:915-1065`.

- **Root cause:** The resolver’s retry loop is bounded by the job-level step timeout, not by an attempt-level watchdog. A single long `codex exec` can consume the full 60-minute budget before the script’s own retry/no-progress logic can finish.

- **Exact change:** Reuse the editor’s heartbeat + wall-clock wrapper from `scripts/review_apply_fixes.sh:915-1065` inside `scripts/review_conflict_resolve.sh:540-586`. Add:
  - per-attempt max wall time,
  - idle timeout with socket-activity extension,
  - remaining-budget check before each attempt,
  - explicit `resolver_timeout` / `resolver_idle_kill` reason codes,
  - immediate early escalation to the integration judge when the resolver times out or makes no progress.

- **Estimated time savings:** ~30–50 minutes on the pathological conflict runs already seen in `25616314314` and `25613903546`; this is the single biggest p95 reduction available.

- **Implementation risk:** low to medium. The watchdog pattern already exists and is battle-tested on the editor path; the main risk is overtightening the wall clock for legitimate large merges. Roll out behind a repo variable first.

---

### 2) Critical-path win: fail on unresolved prompt placeholders before reviewer or implement model calls

- **Evidence:** `review_autofix` run `25620282902` failed in `Internal: AI Review & Autofix` → `review / codex-agent` → `Run reviewer models` with `Unresolved WORKFLOW_EDIT_RESTRICTION placeholder in rendered output for ... reviewer_prompt_body.txt`. `review_autofix` run `25619919130` failed on the same step with the same error. The current reviewer path still reaches `scripts/review_run_reviewers.sh` before failing. In `implement`, the failing runs `25615174460` and `25614767039` also showed repeated unresolved-placeholder check lines in `Run Codex implementation`, but the visible failure signature there was repeated no-file-change retries.

- **Root cause:** Prompt/template rendering validation happens too late. Template defects are discovered only after the workflow has already paid setup cost and, on some paths, after it has reached model execution.

- **Exact change:** Add a shared `assert_no_unresolved_placeholders` helper and run it:
  - immediately after every `render_prompt.sh` invocation,
  - before `Run reviewer models`,
  - before `Run Codex implementation`,
  - before conflict-resolver prompt dispatch.
  
  Concretely, standardize all prompt rendering through the same rendering path used in `.github/workflows/implement.yml:1001` and then hard-fail on any `{{...}}` or named placeholder residue.

- **Estimated time savings:** ~3–11 minutes on affected failures:
  - `25620282902` failed after `277s`,
  - `25619919130` failed after `211s`,
  - implement failures `25615174460` and `25614767039` ran `645s` and `570s`.

- **Implementation risk:** low. This is a deterministic preflight check, not a behavioral change in the AI path.

---

### 3) Critical-path win: shorten `review_autofix` check-run polling before generating CI/lint context

- **Evidence:** In `review_autofix` run `25620282902`, step `Collect PR check-run failures (CI/lint autofix context)` waited from `04:59:54` to at least `05:01:36` for exactly one incomplete check-run, logging seven polls like `Waiting for 1 in-progress/queued check-run(s) ... (sleep 20s, deadline in 1199s)…`. The default config in `.github/workflows/review_autofix.yml:151-153` is `CHECK_RUNS_WAIT_TIMEOUT_SECS=1200` and `CHECK_RUNS_POLL_INTERVAL_SECS=20`.

- **Root cause:** The workflow is trying to collect a cleaner CI snapshot than is necessary for many review passes. The prompt format already supports incomplete CI context, but the step still waits aggressively before giving the reviewers/editor the snapshot.

- **Exact change:** In `.github/workflows/review_autofix.yml:1674-1731`:
  - lower the default wait timeout from `1200s` to `60–90s`,
  - stop waiting once only non-failing check-runs remain,
  - optionally use backoff (`10s -> 20s -> 40s`) instead of fixed `20s`,
  - continue to write `incomplete_count` into the context file so reviewers can see that the CI state is partial.

- **Estimated time savings:** ~1–2 minutes on affected runs; in the observed case, roughly a minute can be reclaimed even with a conservative cap, and much more on worst-case waits.

- **Implementation risk:** low. The context format already supports incomplete snapshots.

---

### 4) Critical-path win: reuse reviewer consensus on same-SHA editor-only retries

- **Evidence:** `review_autofix` runs `25613657806`, `25615068886`, `25617161072`, and `25613659201` all reached `REVIEWERS_SUCCESSFUL: 6` and then logged:
  - `Editor produced no summary and no committed changes — treating as retryable no-op.`
  - `Editor summary file is missing or empty — editor never produced output.`
  
  The workflow already caches the review issue ledger in `.github/workflows/review_autofix.yml:2821-2985`, but it does not currently appear to reuse reviewer consensus keyed to the same head SHA for editor-only reruns.

- **Root cause:** Full six-model reviewer fan-out is being paid even when the actual failure is downstream: empty editor output or no durable editor/commit state.

- **Exact change:** Persist `REVIEWER_CONSENSUS_FILE`, reviewer manifests, and a hash of the check-run context by `PR_NUMBER + HEAD_SHA`. On reruns where:
  - `HEAD_SHA` is unchanged,
  - check-run context hash is unchanged,
  - the prior failure class was `editor_empty_output` or `editor_changes_lost`,
  
  skip `Run reviewer models` and restore the prior reviewer consensus for an editor-only retry.

- **Estimated time savings:** **Inference:** ~8–20 minutes on affected reruns, because it avoids one full six-model reviewer pass when the code under review has not changed.

- **Implementation risk:** medium. The cache key must include enough invalidators to avoid stale reviewer guidance.

---

### 5) Micro-optimization: collapse dual status scans in `cancel_on_pr_close`

- **Evidence:** `.github/workflows/cancel_on_pr_close.yml:68-89` makes separate paginated list calls for `status=queued` and `status=in_progress`, then merges the responses. Recent runs `25621080878` and `25621089033` found no matching runs to cancel, so the common case still pays both list calls.

- **Root cause:** Statuses are fetched separately even though the branch and event filters already sharply bound the result set.

- **Exact change:** Make one branch+event-bounded `actions/runs` request and filter `queued` / `in_progress` client-side.

- **Estimated time savings:** ~1–3 seconds per invocation, plus one fewer API roundtrip in the common no-match case.

- **Implementation risk:** low.

## Cost Optimizations

### 1) Stop `implement` after repeated “output but no file changes” signatures instead of burning all five attempts

- **Evidence:** In `shubhodeep1/coding-workflows`, `implement` run `25614767039` failed in workflow `Internal: AI Implement` → job `implement / implement` → step `Run Codex implementation` after five warnings:
  - `Codex returned output but produced no file changes on attempt 1`
  - ...
  - `Codex returned output but produced no file changes on attempt 5`
  
  The same run logged `tokens used` followed by `42,103` on attempt 5. `implement` run `25615174460` showed the same five-attempt no-file-change pattern and failed after `645s`.

- **Root cause:** The retry policy assumes each attempt may become productive, even when the workflow has a repeated signature of “model answered, but produced no diff”.

- **Exact change:** In the implement loop, stop after two consecutive no-file-change attempts when these inputs are unchanged:
  - same issue/plan,
  - same targeted-file context,
  - same head state.
  
  After the second no-op, jump directly to the diagnose path and surface the failure as `implement_no_worktree_change`.

- **Estimated savings:** ~60–80% of model spend on these failed implement runs, plus ~5–7 minutes on each affected run.

- **Quality-risk notes:** low. This only cuts off repeated, already-unproductive attempts.

---

### 2) Avoid paying for a full six-reviewer panel again when only the editor failed on the same SHA

- **Evidence:** The review failures `25613657806`, `25615068886`, `25617161072`, and `25613659201` each had `REVIEWERS_SUCCESSFUL: 6` before the editor failed to produce a usable summary. The configured reviewer panel in `.github/workflows/review_autofix.yml:91-99` contains six models.

- **Root cause:** Cost is concentrated in the reviewer fan-out, but the rerun trigger is not distinguishing “review guidance good, editor failed” from “reviews need to be recomputed.”

- **Exact change:** Cache reviewer consensus and manifests by `HEAD_SHA` and CI-context hash, and reuse them for editor-only retries as described in Speed Optimization #4.

- **Estimated savings:** one full six-model review pass per editor-only retry on unchanged code; exact token totals are unavailable because provider usage totals were not emitted.

- **Quality-risk notes:** low to medium. Safe if invalidated on SHA or CI-context change.

---

### 3) Sample or disable the two-call prompt-cache probe until it emits real cache/token metrics

- **Evidence:** `scripts/review_run_reviewers.sh:113-160` runs `run_cache_probe()`, which makes **two** `codex exec` calls (`call=1`, `call=2`) before the actual reviewer panel. In sampled `review_autofix` runs `25620282902`, `25617161072`, and `25616314314`, both probe calls logged:
  - `prompt_tokens=na`
  - `completion_tokens=na`
  - `total_tokens=na`
  - `cache_creation_input_tokens=na`
  - `cache_read_input_tokens=na`

- **Root cause:** The system is spending model calls to measure prompt caching, but the normalized telemetry is not returning usable numbers.

- **Exact change:** Until the provider telemetry is reliable:
  - run the probe only on a sample rate (for example 1 in 10 runs),
  - or run it only in nightly/self-test flows,
  - or gate it behind a repo variable.

- **Estimated savings:** 2 model invocations per `review_autofix` run that reaches the reviewer step; exact token savings cannot be quantified from the current logs.

- **Quality-risk notes:** low if sampled, medium if fully disabled because you lose cache-regression visibility.

---

### 4) **Inference:** reintroduce real reasoning tiers instead of `xhigh` everywhere on small-diff or comment-only review paths

- **Evidence:** In `.github/workflows/review_autofix.yml:103-114`, both `REVIEWER_PASS2_REASONING_SMALL` and `REVIEWER_PASS2_REASONING_LARGE` default to `xhigh`, which makes the diff-size reasoning gate effectively a no-op. Recent run summary `25621063211` also recorded `REVIEWER_REASONING_EFFORT: xhigh` and `EDITOR_REASONING_EFFORT: xhigh` on a claude-branch comment-only path. Recent run summary `25620586046` shows a claude-branch review that took `1769s` while `editor/commit/judge/auto-merge` were skipped.

- **Root cause:** Expensive reasoning defaults are being applied even on paths that do not auto-apply code and may not need maximum deliberation.

- **Exact change:** Behind repo variables, lower reasoning effort for:
  - small-diff pass-2 reviews,
  - claude-branch comment-only review mode,
  - possibly the summarizer path if quality holds.
  
  Keep `xhigh` on large diffs, merge conflicts, and high-risk workflow edits.

- **Estimated savings:** **Inference:** moderate; exact token savings are not measurable in this window because usage totals were not emitted.

- **Quality-risk notes:** medium. Roll this out behind variables and compare review quality before making it default.

## Reliability Improvements

### 1) Add an attempt-level watchdog to the conflict resolver and escalate early instead of timing out at 60 minutes

- **Failure evidence:** `review_autofix` runs `25616314314` and `25613903546` both failed in `Run Codex resolver, validate, stage, commit` after the full 60-minute step timeout.

- **Root cause category:** hung/slow tool execution without attempt-level time budgeting.

- **Exact fix:** Port the watchdog pattern from `scripts/review_apply_fixes.sh:915-1065` into `scripts/review_conflict_resolve.sh:540-586`, and emit a distinct resolver failure reason so the orchestrator can escalate immediately.

- **Expected reliability impact:** should eliminate the exact timeout class already seen twice in the sampled window and reduce resolver rerun churn.

- **Rollback / fail-open:** gate with an env var such as `RESOLVER_WATCHDOG_ENABLED`; disabling it restores current behavior.

---

### 2) Make unresolved-placeholder detection a mandatory preflight for all rendered prompts

- **Failure evidence:** `review_autofix` runs `25620282902` and `25619919130` failed in `Run reviewer models` on unresolved `WORKFLOW_EDIT_RESTRICTION` placeholders.

- **Root cause category:** prompt assembly / template validation regression.

- **Exact fix:** Add a shared preflight helper that validates every rendered prompt file before model execution in:
  - reviewer paths,
  - implement paths,
  - conflict-resolver paths.

- **Expected reliability impact:** removes a fully deterministic failure class and prevents expensive runs from failing only after model setup.

- **Rollback / fail-open:** start with warning-only for one release if needed, but the long-term target should be hard-fail-before-model-call.

---

### 3) Split editor failures into explicit classes: `editor_empty_output` and `editor_changes_lost`, and recover by SHA instead of rerunning the whole workflow blind

- **Failure evidence:**  
  - Empty-output pattern in `review_autofix` runs `25613657806`, `25615068886`, `25617161072`, `25613659201`: `Editor summary file is missing or empty — editor never produced output.`  
  - Durable-state mismatch in `review_autofix` run `25616314314`: `DID_COMMIT: true`, `LEDGER_ONLY_COMMIT: true`, `EDITOR_CHANGES_LOST: true`, `MERGE_CONFLICT: true`, `CONFLICT_RESOLVED: false`.  
  - The same mismatch signature also appears in cleanup logs for `25620282902` and `25619919130`.

- **Root cause category:** LLM output persistence / worktree-to-summary mismatch.

- **Exact fix:** After editor execution:
  - snapshot `git status --porcelain`, diff stat, summary hash, stdout/stderr,
  - classify failure as `editor_empty_output` vs `editor_changes_lost`,
  - allow exactly one same-SHA editor-only recovery path,
  - if recovery fails, fail with the explicit class and upload the artifact bundle.

- **Expected reliability impact:** should materially reduce the five editor-step family failures seen in this window and make retries targeted instead of blind.

- **Rollback / fail-open:** keep current behavior behind a flag while validating the classifier on a subset of runs.

---

### 4) Add a two-strike no-progress guard to `implement`’s no-file-change loop

- **Failure evidence:** `implement` runs `25615174460` and `25614767039` both failed after five “output but produced no file changes” attempts.

- **Root cause category:** repeated non-durable implementation output.

- **Exact fix:** After two consecutive no-diff attempts on the same issue/plan/head state, stop retrying and jump to the diagnose/failure-comment branch.

- **Expected reliability impact:** removes noisy, low-signal retries and makes implement failures more deterministic and easier to debug.

- **Rollback / fail-open:** configurable attempt limit; setting the threshold back to 5 restores current behavior.

---

### 5) Ship chunked orchestrator state persistence and stop advancing waves when state could not be durably posted

- **Failure evidence:** In recent `review_autofix` run `25621080933`, the embedded bug summary states that state comments above `65,536` bytes were silently skipped, leading to stale orchestrator state and duplicate issue creation on tracking issue `#2373`.

- **Root cause category:** fail-open persistence on oversize state snapshots.

- **Exact fix:** Upgrade `scripts/orchestrate_poll_process.sh:1012-1053` from “oversize guard + skip” to a chunked V2 state-comment chain, and make wave advancement conditional on successful persistence. During rollout, keep the V1 reader for backward compatibility.

- **Expected reliability impact:** prevents stale-wave re-advancement, duplicate issue creation, and lost state on larger orchestrator projects.

- **Rollback / fail-open:** dual-write V1 + V2 temporarily; if V2 parsing regresses, the V1 reader remains available.

---

### 6) Repair the orchestrator CI/self-test contract before more workflow edits land

- **Failure evidence:**  
  - CI run `25619909717`: `160 passed, 1 failed`, specifically `test_verify_integration_fingerprints_substring_dedups_self_contradictory_pairs() missing 1 required positional argument: 'capsys'`.  
  - CI run `25620680564`: `89 passed, 68 failed`; one visible failure was `expected merged_issue_fingerprints field to be seeded by ensure_integration_conflict_state_fields on every poll tick`.  
  - Nightly self-test run `25618624980`: `fixtures=3 passed=1 failed=2`.

- **Root cause category:** test contract breakage / orchestrator state schema regression.

- **Exact fix:** Restore the `merged_issue_fingerprints` seeding contract, fix the missing `capsys` fixture, and publish the fixture-level contents of `artifacts/validation-selftest-summary.json` in the workflow logs so the nightly failure is directly actionable.

- **Expected reliability impact:** should return CI from its current `18.2%` failure rate (`2/11`) toward baseline and keep orchestrator regressions from shipping.

- **Rollback / fail-open:** none recommended; these should stay blocking once fixed.

## AI Memory Health

Deep-dive logs in `shubhodeep1/coding-workflows` did contain `AI_MEMORY_TELEMETRY:` lines.

| Metric | Value |
|---|---:|
| Total telemetry entries | 79 |
| `record-run-event` ops | 40 |
| `record-candidate` ops | 15 |
| `retrieve` ops | 20 |
| `processed-command-check` ops | 2 |
| `processed-command-claim` ops | 2 |
| Retrieve hits (`records_selected > 0`) | 2 / 20 |
| Retrieve hit rate | 10.0% |
| Zero-record retrieves | 18 / 20 |
| Average `estimated_tokens` | 2.8 |
| Min / max `estimated_tokens` | 0 / 28 |
| `keyword_method=none` | 18 |
| `keyword_method=plain` | 2 |
| `keyword_method=llm` | 0 |
| Retrieve entries with `enabled:false` | 0 |
| Retrieve entries with `fail_open:true` | 0 |
| Observed memory writes with `push_attempts > 1` | 0 |

Key readouts:

- **Reviewer memory retrieval is effectively not helping today.** In the sampled `review_autofix` runs, reviewer retrieves returned `records_selected: 0`, including `25616314314`, `25620282902`, `25617161072`, `25613903546`, `25613657806`, `25615068886`, `25619919130`, `25613659201`, and `25616324582`.

- **The only successful retrieves were in `implement`.** Runs `25615174460` and `25614767039` each logged `role=implementation`, `records_selected=1`, `estimated_tokens=28`, and `keyword_method=plain`.

- **Average retrieved context is tiny, but there is no explicit budget field in the telemetry.** The average `estimated_tokens` was only `2.8`, which suggests retrieval is cheap, but because no retrieve budget is emitted, I cannot compare “average retrieved tokens vs allowed budget” directly.

- **Health of the memory plumbing itself looks good.** No sampled retrieve logged `fail_open:true`, none logged `enabled:false`, and all observed write telemetry had `push_attempts: 1`.

- **Telemetry coverage is incomplete for some operations.** In the sampled deep-dive logs I did **not** observe `finalize-task`, `promote`, `compact`, or `processed-command-complete` telemetry lines. If those operations are expected in this time window, verify that their telemetry emission is still wired through `scripts/memory_helpers.sh:25-30`.

Recommended next steps:

1. Enrich reviewer retrieval inputs in `.github/workflows/review_autofix.yml:1264-1274` to mirror the richer `implement` call in `.github/workflows/implement.yml:873-885` by adding PR title/body, linked issue numbers/titles, and possibly a changed-files summary.
2. Emit a retrieve budget field and a zero-hit reason code so the next audit can distinguish “no relevant records exist” from “filters too weak/too strong”.
3. Track reviewer-retrieve hit rate separately from implementation-retrieve hit rate; the combined `10.0%` number currently hides a review-path problem.

## GH API Call Audit

Exact GitHub API call totals were **not** emitted in this window, so this audit focuses on redundant patterns and high-risk loops rather than absolute counts. I did **not** see any HTTP `429`, secondary rate-limit, or abuse-detection events in the sampled deep-dive logs or in evidence-grade recent run summaries.

### What is already good

- `issue_pr_status.yml:280-321` already follows a batched GraphQL classification pattern for linked issues; the workflow comment explicitly calls out the “single batched GraphQL call” approach.
- `cancel_on_pr_close.yml:62-89` already filters by `branch` and `event` at the API level, which keeps the response bounded.
- `review_autofix.yml:1386-1425` already caches linked issue numbers in `LINKED_ISSUES_JSON` so a later step can skip refetching them.

### 1) Highest redundancy: `review_autofix` check-run polling

- **Workflow/job/step:** `Internal: AI Review & Autofix` → `review / codex-agent` → `Collect PR check-run failures (CI/lint autofix context)`.
- **Evidence:** Run `25620282902` polled the same head SHA every `20s`, waiting at least seven times over ~`123s` for one incomplete check-run.
- **Redundancy pattern:** repeated `GET repos/{repo}/commits/{HEAD_SHA}/check-runs?per_page=100`.
- **Concrete change:** cap wait at `60–90s`, add backoff, and stop early once only non-failing incomplete checks remain.
- **Estimated reduction:** ~4–6 GETs in the observed case; up to ~55 GETs avoided versus the configured `1200s / 20s` worst-case loop.
- **Rate-limit risk reduction:** moderate to high, because this is the only clearly repeated GH API loop in the sampled logs.

### 2) High redundancy, low volume: `cancel_on_pr_close` does two list scans where one would do

- **Workflow/job/step:** `Internal: Cancel on PR Close` → `cancel / cancel-active-runs` → `Cancel queued/in-progress runs for closed PR branch`.
- **Evidence:** `.github/workflows/cancel_on_pr_close.yml:68-89` performs separate paginated GETs for `status=queued` and `status=in_progress`. Recent runs `25621080878` and `25621089033` both found no matching runs to cancel.
- **Redundancy pattern:** dual list queries on the same branch/event scope.
- **Concrete change:** issue one list request and filter statuses client-side.
- **Estimated reduction:** 50% fewer list calls in the common no-match case.
- **Rate-limit risk reduction:** low to moderate.

### 3) `issue_pr_status` already batches reads, but still does avoidable no-op writes

- **Workflow/job/step:** `Internal: Issue-PR Status Sync` → `sync-status / sync-issue-status` → `Update linked issue labels when PR closes`.
- **Evidence:** `.github/workflows/issue_pr_status.yml:280-321` batches classification reads, but `.github/workflows/issue_pr_status.yml:366-376` still unconditionally:
  - calls `set_issue_phase_label_resilient`,
  - then attempts `gh issue close`.
- **Redundancy pattern:** write calls even when the target label is already present or the issue is already closed.
- **Concrete change:** extend the batch query to fetch issue `state` and current phase label, then skip:
  - label-add when target label already exists,
  - close when state is already `closed`.
- **Estimated reduction:** up to 2 write calls per already-terminal linked issue.
- **Rate-limit risk reduction:** moderate on PRs linked to multiple issues.

### 4) **Inference:** make `review_autofix` metadata fetches mode-aware

- **Workflow/job/step:** `Internal: AI Review & Autofix` → `review / codex-agent` → `Collect PR metadata`.
- **Evidence:** `.github/workflows/review_autofix.yml:1369-1375` fetches PR payload, issue comments, reviews, and review comments every run. Recent run summaries `25620586046` and `25621063211` show claude-branch review paths where `editor/commit/judge/auto-merge` were skipped.
- **Redundancy pattern:** heavy review-context fetches even on comment-only or reduced-functionality paths.
- **Concrete change:** if the gate has already decided on claude-branch comment-only review mode, fetch only the minimal discussion history needed for reviewer comments, not the full payload set.
- **Estimated reduction:** **Inference:** 2–3 paginated calls per comment-only run.
- **Rate-limit risk reduction:** low to moderate.

## Prompt Cache & Memory System

### What is working

- Both major AI paths intentionally build a stable prompt prefix:
  - `review_autofix`: `.github/workflows/review_autofix.yml:1222-1262`
  - `implement`: `.github/workflows/implement.yml:864-871` and prompt assembly at `990-1012`
  
  That is the right architecture for provider-side prompt-prefix caching.

- Prompt caching is enabled by default in both workflows via `OPENROUTER_PROMPT_CACHE_DISABLED=false` unless overridden.

### What is not working

- **The cache probe is not producing usable observability.** In sampled `review_autofix` runs `25620282902`, `25617161072`, and `25616314314`, the probe logged `prompt_tokens=na`, `completion_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, and `cache_read_input_tokens=na`.

- **The probe itself adds cost and latency.** `scripts/review_run_reviewers.sh:113-160` makes two extra probe calls before the reviewer panel.

- **Reviewer memory retrieval is under-specified.** `review_autofix` retrieves memory with only `--role reviewer --pr-number` (`.github/workflows/review_autofix.yml:1264-1274`), while `implement` retrieves with issue number, title, and body (`.github/workflows/implement.yml:873-885`). The hit-rate difference is visible in telemetry: reviewer path effectively `0/18`, implementation path `2/2`.

### Likely fragmentation causes

- **Good news:** the prompt builders mostly keep dynamic content after the stable prefix, so the biggest cache-fragmentation mistake—putting per-run noise before the static prefix—does not appear to be the main issue.

- **More likely problems:**
  1. missing cache telemetry makes hit/miss diagnosis impossible,
  2. unresolved placeholders create prompt instability,
  3. reviewer memory retrieval is so sparse that it adds little high-value reusable context.

### Recommended improvements

1. **Fix telemetry first.** Log a stable prompt-prefix hash, a full prompt hash, and real provider token/cache fields. Without that, cache tuning is guesswork.
2. **Sample the probe instead of running it every time.** Until metrics stop returning `na`, the probe is overhead without learning.
3. **Enrich reviewer retrieval inputs.** Add PR title/body, linked issue context, and maybe a compact changed-files list; the implementation workflow is already showing the direction that works.
4. **Persist reviewer consensus by `HEAD_SHA`.** This is both a memory-system improvement and a rerun-cost reduction.
5. **Keep the static prefix invariant.** Preserve the current design where `pre_assembled_static.txt` remains at the top of the prompt and dynamic warnings/notes stay below it.

### Estimated impact

- **Tokens/cost:** moderate reduction from sampling the probe and reusing reviewer consensus.
- **Latency:** modest per-run improvement from removing two probe calls; larger improvement on editor-only reruns.
- **Reliability:** moderate improvement because better telemetry will make cache regressions and prompt-instability bugs visible instead of silent.

## Orchestrator Health

### Healthy areas

- **Clarification loops are not the current problem.** In `shubhodeep1/coding-workflows`, `clarify` had `200` runs with `193` skipped/other and p95 `8.05s`; `orchestrate_clarify_respond` had `193` runs, all skipped/other, with p95 `2.0s`. The clarify/respond part of the pipeline is cheap and not visibly stuck.

### Pain points

### 1) Wave progression can become stale when state comments exceed the GitHub comment-body limit

- **Evidence:** Recent run `25621080933` includes the embedded bug summary describing:
  - GitHub comment-body cap `65,536` bytes,
  - `HTTP 422` on oversize posts,
  - silent swallow via `gh_retry ... || true`,
  - stale state re-advancing wave 3,
  - duplicate issues `#2390`, `#2395`, `#2399`, `#2410`, `#2420`, `#2421`,
  - triggering state size around `75 KB`.

- **Operational effect:** deferred-creation issues can be recreated, waves can be re-advanced, and the poller can keep acting on stale snapshots.

- **Smallest safe mitigation:** keep the current size guard in `scripts/orchestrate_poll_process.sh:1017-1020`, but replace “skip post” with chunked state persistence plus a hard “do not advance wave if state persistence failed” gate.

### 2) Conflict-heal retries are present, but terminal escalation is too slow

- **Evidence:** Resolver timeout failures `25616314314` and `25613903546` show that the system may spend nearly an hour in resolver work before escalating.

- **Operational effect:** orchestrator-controlled merge conflict healing spends too much time in a single terminal state before handing off to the judge/escalation path.

- **Smallest safe mitigation:** the attempt-level resolver watchdog described earlier.

### 3) Orchestrator state contract is currently unstable in CI

- **Evidence:** CI run `25620680564` failed `68` tests, including `expected merged_issue_fingerprints field to be seeded by ensure_integration_conflict_state_fields on every poll tick`.

- **Operational effect:** the orchestrator is vulnerable to shipping state-shape regressions that can break poller logic or recovery behavior.

- **Smallest safe mitigation:** repair the state-field seeding contract first, then keep the targeted CI tests blocking.

### 4) Post-merge validation dispatch is producing ambiguous warnings

- **Evidence:** Recent `review_autofix` successes:
  - `25621080933`: `Issue #2373 does not have ai:orchestrator-validate-required; skipping.` then `No standalone validation workflow could be dispatched for merged PR #2429.`
  - `25621089043`: `Issue #2849 does not have ai:orchestrator-validate-required; skipping.` then `No standalone validation workflow could be dispatched for merged PR #2426.`

- **Operational effect:** this may be correct behavior for some issues, but today it is hard to tell “not required” from “required but failed to dispatch.”

- **Smallest safe mitigation:** emit a structured reason code such as:
  - `validate_dispatch=not_required`,
  - `validate_dispatch=missing_label`,
  - `validate_dispatch=dispatch_failed`.

### Observable indicators to track next

1. `state_snapshot_bytes` for every orchestrator state post.
2. Count of skipped or failed state-post attempts.
3. Duplicate child issues per tracking issue per day.
4. Count of resolver attempts ending in timeout or no-progress escalation.
5. Count of merged PRs with `No standalone validation workflow could be dispatched`.
6. CI failures involving `merged_issue_fingerprints` or integration conflict state fields.

## Pipeline Flow Bottlenecks

### 1) Intake and lightweight orchestration are mostly fine

- `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` are overwhelmingly skipped in `0–1s` when they are not needed.
- This means the pipeline’s end-to-end pain is **not** coming from trigger spam alone; it is concentrated in the heavier execution paths.

### 2) Queueing overhead exists, but it is a secondary bottleneck

- **Evidence:**  
  - `review_autofix` run summary `25620680547` was cancelled after `1398s` and explicitly noted `Job is waiting for a hosted runner to come online.`  
  - `review_autofix` run `25616314314` logged hosted-runner waiting at job start.  
  - Smaller workflows like `issue_pr_status` (`25621089026`) and `cancel_on_pr_close` (`25621089033`) also showed runner pickup delays, but those runs were only `10s` and `8s`.

- **Interpretation:** queueing waste exists, especially in `review_autofix`, but it is not the dominant source of the multi-thousand-second tail. Compute and retry overhead matter more.

### 3) Compute overhead is concentrated almost entirely in `review_autofix`

- **Evidence:**  
  - `review_autofix` family metrics: `130` total runs, average `1109.192s`, p95 `4171.15s`.  
  - Among executed `review_autofix` runs, success p50 was `1437.5s`, failure p50 was `2064s`.  
  - Across the whole window, `60/61` runs at or above `1000s` and all `42/42` runs at or above `1800s` were `review_autofix`.

- **Interpretation:** six external reviewer models plus editor work plus retry logic dominate wall time.

### 4) Retry overhead is the main avoidable waste inside the heavy stages

- **Review/autofix retry overhead:** editor no-summary runs after six successful reviewers (`25613657806`, `25615068886`, `25617161072`, `25613659201`), plus durable-state mismatch (`25616314314`).
- **Implement retry overhead:** five no-file-change attempts in `25615174460` and `25614767039`.
- **Check-run wait overhead:** at least ~`123s` in `25620282902` before review context was built.

### 5) Merge/conflict overhead is the sharpest long-tail spike

- **Evidence:** two runs hit the full 60-minute resolver timeout (`25616314314`, `25613903546`).

- **Interpretation:** merge conflict resolution is the highest-severity single-step tail.

### 6) Validation/orchestrate loops can amplify problems even when the core AI steps succeed

- **Evidence:** the state-comment overflow in `25621080933` caused duplicate issue creation and stale-wave progression; CI run `25620680564` shows state-contract regressions.

- **Interpretation:** the orchestrator layer is not the main latency sink, but when it is wrong, it multiplies downstream churn.

### Fix order by end-to-end impact

1. **Resolver watchdog + early escalation** — biggest direct p95 win.
2. **Template preflight validation** — turns slow failures into fast failures.
3. **Editor/implement no-progress guards** — biggest pure waste reduction.
4. **SHA-keyed reviewer reuse on editor-only retries** — cuts repeated heavy compute.
5. **Shorter check-run polling** — trims per-run wait without changing correctness.
6. **Chunked orchestrator state persistence + CI contract repair** — prevents amplified downstream churn.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**

- `review_autofix` is by far the dominant bottleneck: `130` runs, avg `1109.192s`, p95 `4171.15s`, `9` failures, `38` cancellations.
- `ci` is small in volume but unstable right now: `11` runs, `2` failures, failure rate `18.2%`.
- `orchestrate_poll` is not failing in this window, but it is exposed to stale-state bugs; family p95 is `602.5s`.

**Top failure modes**

1. `review_autofix` editor no-output / no-durable-state failures:
   - `25613657806`, `25615068886`, `25617161072`, `25613659201`, `25616314314`
2. `review_autofix` conflict resolver timeouts:
   - `25616314314`, `25613903546`
3. `review_autofix` unresolved prompt placeholder failures:
   - `25620282902`, `25619919130`
4. `implement` no-file-change loops:
   - `25615174460`, `25614767039`
5. Orchestrator-state CI regressions:
   - `25619909717`, `25620680564`

**Highest-cost drivers**

- Six-model `review_autofix` reviewer fan-out paid before downstream editor failures.
- `implement` spending multiple attempts on no-diff outputs; one failure reached `42,103` tokens on attempt 5.
- Two extra prompt-cache probe calls per `review_autofix` reviewer step with no usable token/cache metrics.
- Default `xhigh` reasoning on reviewer/editor paths, including some comment-only review modes.

**Top 3 prioritized actions**

1. **Add resolver watchdog + universal prompt preflight lint.** This addresses the highest-latency and easiest-to-detect failure classes first.
2. **Make retries SHA-aware and targeted.** Reuse reviewer consensus on editor-only failures and stop implement after repeated no-diff attempts.
3. **Ship chunked orchestrator state persistence and repair orchestrator CI contracts.** This stops duplicate issue churn and makes future orchestrator changes safer.

## Metrics Appendix

### Window summary

| Scope | Total runs | Success | Failure | Cancelled | Other / skipped | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 191 | 14 | 38 | 757 | 170.327 | 1.0 | 1402.35 |

### Workflow-family summary

| Workflow family | Total | Success | Failure | Cancelled | Other | Failure rate | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `review_autofix` | 130 | 80 | 9 | 38 | 3 | 6.9% | 1109.192 | 277.5 | 4171.15 |
| `ci` | 11 | 9 | 2 | 0 | 0 | 18.2% | 613.636 | 620.0 | 655.0 |
| `implement` | 193 | 8 | 2 | 0 | 183 | 1.0% | 18.275 | 1.0 | 9.0 |
| `plan` | 192 | 7 | 0 | 0 | 185 | 0.0% | 18.635 | 1.0 | 7.0 |
| `clarify` | 200 | 7 | 0 | 0 | 193 | 0.0% | 3.985 | 1.0 | 8.05 |
| `orchestrate_clarify_respond` | 193 | 0 | 0 | 0 | 193 | 0.0% | 1.181 | 1.0 | 2.0 |
| `orchestrate_poll` | 11 | 11 | 0 | 0 | 0 | 0.0% | 215.455 | 137.0 | 602.5 |
| `issue_pr_status` | 16 | 16 | 0 | 0 | 0 | 0.0% | 53.313 | 61.5 | 75.0 |
| `copilot_pull_request_reviewer` | 32 | 32 | 0 | 0 | 0 | 0.0% | 235.188 | 235.5 | 318.45 |

### Executed-run durations for key families

| Family | Executed runs | Success p50 (s) | Failure p50 (s) | Cancelled p50 (s) | Notes |
|---|---:|---:|---:|---:|---|
| `review_autofix` | 127 | 1437.5 | 2064.0 | 15.0 | Family p50 is diluted by quick cancels/skips; successful runs are much longer. |
| `implement` | 10 | 321.0 | 607.5 | N/A | Failures are materially slower than successes. |
| `ci` | 11 | 634.0 | 567.0 | N/A | Current failures are test-regression-driven, not timeout-driven. |
| `plan` | 7 | 369.0 | N/A | N/A | Planning itself is not the dominant latency driver. |
| `orchestrate_poll` | 11 | 137.0 | N/A | N/A | No failures in the sampled window. |

### Long-run concentration

| Duration threshold | Runs at/above threshold | `review_autofix` runs | `review_autofix` share |
|---|---:|---:|---:|
| `>= 600s` | 75 | 62 | 82.7% |
| `>= 1000s` | 61 | 60 | 98.4% |
| `>= 1800s` | 42 | 42 | 100.0% |
| `>= 3600s` | 8 | 8 | 100.0% |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total telemetry lines | 79 |
| `record-run-event` | 40 |
| `record-candidate` | 15 |
| `retrieve` | 20 |
| Retrieve hits | 2 |
| Retrieve hit rate | 10.0% |
| Retrieve zero-hit count | 18 |
| Avg `estimated_tokens` | 2.8 |
| `keyword_method=none` | 18 |
| `keyword_method=plain` | 2 |
| `keyword_method=llm` | 0 |
| Retrieve `enabled:false` count | 0 |
| Retrieve `fail_open:true` count | 0 |
| Observed memory writes with `push_attempts > 1` | 0 |

### Token and cache metrics

| Metric | Value | Evidence / gap |
|---|---|---|
| Window total prompt/completion tokens | Not reliably available | No trustworthy aggregate token totals were emitted in the collected logs. |
| Prompt-cache hit/miss totals | Not measurable | Sampled probe logs in `25620282902`, `25617161072`, `25616314314` reported `prompt_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`. |
| Extra cache-probe calls | 2 per `review_autofix` reviewer step | `scripts/review_run_reviewers.sh:150-154` |
| Concrete token example | `42,103` | `implement` run `25614767039`, attempt 5 |
| Average memory retrieve size | `2.8` estimated tokens | Derived from `AI_MEMORY_TELEMETRY` retrieve entries; this is not provider billing usage. |

### GH API hotspot summary

| Workflow / step | Pattern | Evidence | Estimated reduction |
|---|---|---|---|
| `review_autofix` / `Collect PR check-run failures` | Repeated polling of check-runs on same head SHA | Run `25620282902` waited seven times over ~123s for one incomplete check-run; config is `1200s` timeout / `20s` poll interval | ~4–6 GETs in observed case; much more on worst-case waits |
| `cancel_on_pr_close` / `Cancel queued/in-progress runs...` | Two separate list calls for `queued` and `in_progress` | `.github/workflows/cancel_on_pr_close.yml:68-89`; recent runs `25621080878`, `25621089033` found no matching runs | ~50% fewer list calls in common no-match case |
| `issue_pr_status` / `Update linked issue labels when PR closes` | No-op label/close writes after already-batched classification | `.github/workflows/issue_pr_status.yml:280-321` then `366-376` | Up to 2 write calls avoided per already-terminal linked issue |
| `review_autofix` / `Collect PR metadata` | Full metadata/comments/reviews fetches on all review runs | `.github/workflows/review_autofix.yml:1369-1375`; likely overkill on comment-only claude-branch review mode | **Inference:** 2–3 paginated calls avoided on comment-only runs |

If you want, I can turn this report into a prioritized implementation checklist mapped directly to the affected workflow files and scripts.
