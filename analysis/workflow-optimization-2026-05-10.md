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

## Deep Audit — Workflows & Scripts (2026-05-10)

### Section 1: Bug & Correctness Sweep

Audited `34` workflow files under `.github/workflows/` and `60` top-level scripts under `scripts/`. Findings below intentionally omit issues already covered in `analysis/workflow-optimization-2026-05-10.md`.

#### Finding BUG-001
- **ID** — `BUG-001`
- **File path** — `.github/workflows/implement.yml`
- **Line range** — `127-145`, `2862-2873`
- **Severity** — Medium
- **Category tag** — `bug`
- **Description** — The implement workflow still detects “existing PRs for this issue” with `gh pr list --search "issue:${ISSUE_NUMBER}"` both in the upfront safety check and in the `gh pr create` failure recovery path. In contrast, `plan.yml:430-442` documents that this same `gh pr list --search` strategy produced false positives and was replaced with issue-timeline cross-reference checks. As written, `implement.yml` can skip work or attach to an unrelated open PR when the issue number appears in PR text/search results rather than in an actual issue↔PR linkage.
- **Recommended fix** — Reuse the timeline-based linked-PR detection already used in `.github/workflows/plan.yml:430-442`, preferably by extracting a shared helper into `scripts/gh_helpers.sh` with a signature such as `find_open_linked_prs <owner> <repo> <issue_number>`. Update both the initial precheck and the post-`gh pr create` recovery path to call that helper instead of `gh pr list --search`.

#### Finding BUG-002
- **ID** — `BUG-002`
- **File path** — `.github/workflows/review_autofix.yml`
- **Line range** — `3858-3865`, `3979-3986`, `4713-4720`
- **Severity** — Medium
- **Category tag** — `bug`
- **Description** — When `LINKED_ISSUES_JSON` is empty, three late-stage `review_autofix` blocks fall back to a broad regex that accepts bare prose mentions such as `issues/123` and `issue #123`. That is inconsistent with `.github/workflows/issue_pr_status.yml:195-210`, which explicitly restricts fallback matching to closing keywords or repo-scoped URLs because bare mentions previously caused incorrect issue transitions, and with `.github/workflows/review_autofix.yml:627-640`, which already disables this fallback on the deterministic-skip path for the same reason. These three late-stage blocks can therefore apply `ai:ready-to-merge` or `ai:review-blocked` to unrelated issues mentioned incidentally in documentation or discussion text.
- **Recommended fix** — Replace the broad fallback with the stricter pattern from `.github/workflows/issue_pr_status.yml:195-210` so only explicit closing references are honored, or better, treat `LINKED_ISSUES_JSON` / GraphQL `closingIssuesReferences` as authoritative and skip linked-issue mutation entirely when only prose mentions are available.

### Section 2: GitHub API Call Redundancy Audit

Items already called out in the in-progress report — including `review_autofix` check-run polling, `cancel_on_pr_close` dual status scans, `issue_pr_status` no-op writes, and `review_autofix` metadata overfetch on comment-only paths — are not repeated here.

#### Finding API-001
- **ID** — `API-001`
- **File path** — `scripts/orchestrate_poll_process.sh`
- **Line range** — `4214-4363`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — `_load_actions_runs_cached()` is documented as doing “one conditional fetch,” and its callers rely on that assumption (`4388-4394` and nearby comments), but on every cache miss it actually performs three separate GitHub API calls: one for `status=in_progress` (`4289`), one for `status=queued` (`4340`), and one for `status=completed` (`4345`). **Current call count:** `3` per cache refresh. **Proposed call count after fix:** `1`, or `2` if a separate completed-window fetch must be retained for semantics parity [NEEDS VERIFICATION].
- **Recommended fix** — Collapse the status-specific fetches inside `_load_actions_runs_cached()` and filter statuses client-side from one cached payload. The closest existing pattern to extend is `autofix_retrigger_has_inflight_peer()` in `scripts/gh_helpers.sh:1171-1213`, which already does a single list call followed by local status/path filtering.

#### Finding API-002
- **ID** — `API-002`
- **File path** — `scripts/review_conflict_resolve.sh`
- **Line range** — `102-126`
- **Severity** — Low
- **Category tag** — `api-redundancy`
- **Description** — The fast-path dedupe before dispatching orchestrator polling checks for existing runs by calling `gh run list` once with `--status in_progress` and then again with `--status queued`. Both requests target the same workflow/repo scope and only answer the same boolean question: “is there already an active or queued poller?” **Current call count:** `2`. **Proposed call count after fix:** `1`.
- **Recommended fix** — Replace the two status-specific probes with a single `gh run list` invocation that returns `status` and `databaseId`, then filter `queued` / `in_progress` locally. Reuse the same “single list call + local status filter” pattern already present in `scripts/gh_helpers.sh:1171-1213`.

#### Finding BATCH-001
- **ID** — `BATCH-001`
- **File path** — `.github/workflows/review_autofix.yml`
- **Line range** — `1454-1485`
- **Severity** — Medium
- **Category tag** — `api-batching`
- **Description** — On the body-text linked-issue fallback path, `review_autofix` first computes fallback issue numbers once, then loops over them and issues `gh api repos/.../issues/${_fb_num}` for each issue individually, capped at `20`. **Current call count on the fallback path:** `1` initial GraphQL `closingIssuesReferences` query plus `N` REST issue fetches, where `1 <= N <= 20`. **Proposed call count after fix:** `2` total queries — keep the initial `closingIssuesReferences` query, then batch the fallback issue `number/title/body` fetch into one aliased GraphQL request.
- **Recommended fix** — Lift the alias-batching pattern from `_fetch_candidate_issue_details_graphql()` in `scripts/orchestrate_poll_process.sh:5823-5895` into a reusable helper or support script, with a contract like `fetch_issue_context_batch <numbers_json> -> [{number,title,body}]`. Update this workflow to use that batched helper instead of per-issue REST calls inside the loop.

### Section 3: Code Duplication & Modularization Opportunities

#### Finding DUP-001
- **ID** — `DUP-001`
- **File path** — `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/mark-stable.yml`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/review_autofix.yml`, `.github/workflows/test-and-mark-stable.yml`
- **Line range** — `26-53`; `309-336`, `458-485`; `76-107`; `1287-1325`; `4622-4648`
- **Severity** — Low
- **Category tag** — `duplication`
- **Description** — The same inline GitHub retry helper pattern (`_rl_wait` plus `_gh_retry` / `gh_retry`) is copied into at least six workflow steps. The blocks differ only in temp-file names, small logging strings, and attempt-loop cosmetics, but they all duplicate functionality already centralized in `scripts/gh_helpers.sh`.
- **Recommended fix** — Standardize on `scripts/gh_helpers.sh` as the owner for retry behavior. The shared signatures already exist (`gh_retry <cmd...>`, `gh_retry_to_file <outfile> <cmd...>`). If checkout order prevents early sourcing, add a small bootstrap step that stages `scripts/gh_helpers.sh` into a runtime location and source that file from the affected callers: `cancel_on_pr_close`, both `mark-stable` retry blocks, `orchestrate_poll`, `review_autofix`, and `test-and-mark-stable`.

#### Finding DUP-002
- **ID** — `DUP-002`
- **File path** — `scripts/label_helpers.sh`, `scripts/review_rb_judge.sh`, `.github/workflows/review_autofix.yml`
- **Line range** — `146-197`; `78-110`; `3819-3853`, `3954-3973`
- **Severity** — Low
- **Category tag** — `duplication`
- **Description** — Phase-label mutation logic exists in three separate implementations: canonical `set_issue_phase_label_resilient()` in `scripts/label_helpers.sh`, near-identical `_resilient_phase_swap()` in `scripts/review_rb_judge.sh`, and two simplified inline fallbacks in `review_autofix.yml`. The duplication has already drifted: the `review_autofix` fallbacks only POST-add the target label and do not remove previous phase labels, unlike the canonical GET+PUT+POST helper.
- **Recommended fix** — Keep `scripts/label_helpers.sh` authoritative with the signature `set_issue_phase_label_resilient <issue_number> <target_label> <repo>`. Stage a runtime copy of that helper for late workflow steps instead of redefining it inline, and update both `scripts/review_rb_judge.sh` and the two `review_autofix` late-stage label writers to call the shared helper.

### Section 4: Expression Size Limit Risk Assessment

Estimates below use the serialized `run:` block size for steps that contain at least one `${{ }}` interpolation. That is a practical screening proxy for this repo’s past expression-limit failures, but GitHub’s internal template expansion can differ slightly, so the flagged counts are conservative estimates.

#### Finding EXPR-001
- **ID** — `EXPR-001`
- **File path** — `.github/workflows/test-and-mark-stable.yml`
- **Line range** — `1204-1587`
- **Severity** — High
- **Category tag** — `expression-limit`
- **Description** — The interpolated `run:` block for **Phase 4: Wait for review & autofix to complete** is estimated at **19,899 characters**, leaving only about **1,101 characters** of headroom under GitHub’s `21,000`-character expression ceiling. This step already contains `${{ }}` interpolation and continues to accumulate inline polling, helper functions, and diagnostics, so it is one edit away from the same class of runner rejection this repository has already hit before. [NEEDS VERIFICATION]
- **Recommended fix** — Extract the whole wait-review implementation into an external script such as `scripts/test_and_mark_stable_wait_review.sh` and pass state through environment variables. If full extraction is deferred, split helper definitions and the polling loop into separate smaller steps so no single interpolated `run:` scalar carries the full block.

#### Finding EXPR-002
- **ID** — `EXPR-002`
- **File path** — `.github/workflows/test-and-mark-stable.yml`
- **Line range** — `1674-2078`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — The interpolated `run:` block for **Phase 4b: Verify editor restored canary (pytest + retry)** is estimated at **17,408 characters**, leaving about **3,592 characters** of headroom. That is already above the requested `15,000`-character warning threshold, and the step contains inline install, retry, polling, and classification logic that is likely to keep growing. [NEEDS VERIFICATION]
- **Recommended fix** — Move this step into an external script such as `scripts/test_and_mark_stable_verify_canary.sh`, or split it into three steps: dependency/bootstrap, canary fetch + pytest, and retry-dispatch polling.

#### Finding EXPR-003
- **ID** — `EXPR-003`
- **File path** — `.github/workflows/review_autofix.yml`
- **Line range** — `1285-1673`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — The interpolated `run:` block for **Collect PR metadata** is estimated at **17,408 characters**, leaving about **3,592 characters** of headroom. The block bundles retry helpers, PR payload fetches, GraphQL calls, linked-issue fallback logic, comment-context assembly, and diff capture into one interpolated step, so the remaining margin is already narrow for a high-churn workflow. [NEEDS VERIFICATION]
- **Recommended fix** — Extract PR context assembly into a support script such as `scripts/review_collect_pr_context.sh`, or split the workflow step into smaller units for PR payload/comments, linked-issue context, and diff capture.

Additional scan notes:
- No `if:` expression crossed the audit thresholds in this pass.
- No workflow exceeded the `800 KB` warning threshold. The largest files were `.github/workflows/review_autofix.yml` at `285,829` bytes and `.github/workflows/test-and-mark-stable.yml` at `279,323` bytes.

### Section 5: Cross-Cutting Concerns

#### Finding DEAD-001
- **ID** — `DEAD-001`
- **File path** — `scripts/review_issue_ledger.sh`
- **Line range** — `10-16`
- **Severity** — Low
- **Category tag** — `dead-code`
- **Description** — The shell-level `trim()` helper is defined but not called anywhere in shell code. Repo-wide search only found this definition plus separate `awk`-local `trim()` functions, so the shell function is currently dead code.
- **Recommended fix** — Remove the unused shell `trim()` helper. If a shell implementation is still desired, route one real callsite through it so the function remains exercised and justified.

#### Finding DEAD-002
- **ID** — `DEAD-002`
- **File path** — `scripts/orchestrate_poll_process.sh`
- **Line range** — `4764-4770`
- **Severity** — Low
- **Category tag** — `dead-code`
- **Description** — `read_standalone_state_json()` has no callsites in the repository. Its sibling `get_standalone_state_comment_id()` is still used later in the same file, but this wrapper is currently orphaned and would perform a full paginated comments fetch if it were accidentally revived.
- **Recommended fix** — Delete `read_standalone_state_json()`. If equivalent behavior is needed later, prefer passing already-fetched comment JSON into `_extract_standalone_state_json_from_comments()` rather than preserving a second fetch wrapper.

#### Finding SHELL-001
- **ID** — `SHELL-001`
- **File path** — `scripts/review_commit_changes.sh`, `scripts/review_conflict_resolve.sh`
- **Line range** — `448-455`; `1033-1034`
- **Severity** — Low
- **Category tag** — `shellcheck`
- **Description** — Both scripts set the authenticated `origin` URL with an unquoted shell argument: `git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}`. This is an avoidable shellcheck-class robustness problem because the URL is assembled from shell expansions and should not rely on word-splitting/globbing behavior.
- **Recommended fix** — Quote the full URL argument in both places, for example `git remote set-url origin "https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}"`, or build the command as an array and pass the URL as one array element.

Additional scan notes:
- No `TODO`, `FIXME`, or `HACK` markers were present under `.github/workflows/` or `scripts/` in this pass.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | EXPR-001 |
| Medium | 6 | BUG-001, BUG-002, API-001, BATCH-001, EXPR-002, EXPR-003 |
| Low | 6 | API-002, DUP-001, DUP-002, DEAD-001, DEAD-002, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 1 workflow (`.github/workflows/test-and-mark-stable.yml`) | Medium |
| API call optimization | 3 files (`scripts/orchestrate_poll_process.sh`, `scripts/review_conflict_resolve.sh`, `.github/workflows/review_autofix.yml`) | Medium |
| Code modularization | 7 files (`.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/mark-stable.yml`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/review_autofix.yml`, `.github/workflows/test-and-mark-stable.yml`, `scripts/label_helpers.sh`, `scripts/review_rb_judge.sh`) | Medium |
| Expression size reduction | 2 workflows (`.github/workflows/test-and-mark-stable.yml`, `.github/workflows/review_autofix.yml`) | Medium |
| Medium/Low fixes | 6 files (`.github/workflows/implement.yml`, `.github/workflows/review_autofix.yml`, `scripts/review_issue_ledger.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/review_commit_changes.sh`, `scripts/review_conflict_resolve.sh`) | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-05-10)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap was proven safe to collapse without changing filters, retries, failure behavior, cache contracts, or concurrency boundaries. `NEEDS_VERIFICATION` means the redundancy is plausible and concrete, but a human must confirm freshness/error-handling assumptions before implementation. `RISKY_SKIP` means the overlap sits in a race-defense, poller, retry, or similarly sensitive path where this pass should surface it for visibility but not auto-implement it.

### Consolidation Candidates (MERGE-###)

#### Finding MERGE-001
- **ID** — `MERGE-001`
- **Safety tag** — `RISKY_SKIP`
- **File path and line ranges** — `scripts/orchestrate_poll_process.sh:701-705`, `scripts/orchestrate_poll_process.sh:3406-3412`, `scripts/orchestrate_poll_process.sh:3462-3467`, `scripts/orchestrate_poll_process.sh:3516-3518`
- **Current call count** — `8` `GET /repos/{owner}/{repo}/pulls/{pull_number}` calls across the shown final-merge path clusters.
- **Proposed call count** — `3` calls by hydrating one full PR JSON object per decision cluster and extracting fields locally.
- **Endpoint(s)** — `GET /repos/{owner}/{repo}/pulls/{pull_number}`
- **Evidence** — The file already has a whole-object helper, but the final-merge path still re-reads the same PR field-by-field:
  ```bash
  _fetch_pr_json() {
    local pr_number="$1"
    gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${pr_number}" || echo '{}'
  }

  existing_pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  existing_pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"

  pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  pr_mergeable="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' || echo "")"
  pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
  ```
- **Proposed fix** — In the final-merge function inside `scripts/orchestrate_poll_process.sh`, replace each local `_safe_gh_jq ... --jq '.state'/.mergeable/.merged_at'` cluster with one `_fetch_pr_json "${final_pr}"` call plus local `jq` field extraction from that cached object.
- **Safety rationale** — `RISKY_SKIP` is mandatory because this code is inside `scripts/orchestrate_poll_process.sh` and specifically controls a final-merge/self-healing path, which the brief treats as race-sensitive and unsuitable for automatic consolidation.
- **Downstream signal** — Manual review only: if this is ever implemented, compare pre/post behavior on the final-merge stall/self-heal path and confirm no race-defense, retry, or log-ordering contract changes.

### Redundant Re-Fetch (REUSE-###)

#### Finding REUSE-001
- **ID** — `REUSE-001`
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:385-452`
- **Current call count** — `2`
- **Proposed call count** — `1`
- **Endpoint(s)** — `POST /repos/{owner}/{repo}/issues`, `GET /repos/{owner}/{repo}/issues/{issue_number}`
- **Evidence** — The step creates the issue, then immediately re-fetches the same issue only to read `html_url`:
  ```bash
  ISSUE_NUMBER=$(gh api "repos/${TEST_REPO}/issues" \
    -f title="${TITLE}" \
    -f body="${BODY}" \
    --jq '.number')

  ISSUE_URL=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '.html_url')
  ```
  The second call is overlapping data: the create response already represents the newly created issue resource, and the URL is also derivable from `${GITHUB_SERVER_URL}/${TEST_REPO}/issues/${ISSUE_NUMBER}`.
- **Proposed fix** — In the `Create E2E test issue` step, capture the create response once, parse both `.number` and `.html_url` from that single payload, and drop the follow-up `GET /issues/{ISSUE_NUMBER}`.
- **Safety rationale** — `NEEDS_VERIFICATION` is appropriate because removing the second call changes failure semantics from “POST must succeed and subsequent GET must succeed” to “POST response parse alone must succeed,” and static review cannot prove the follow-up GET is not acting as an intentional consistency check.
- **Downstream signal** — Verify that the release-gate does not intentionally rely on the post-create GET as a propagation/existence check; if not, collapse the step to a single create-response parse.

#### Finding REUSE-002
- **ID** — `REUSE-002`
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path and line ranges** — `.github/workflows/review_autofix.yml:1281-1383`, `scripts/review_rb_judge.sh:146-156`, `scripts/review_rb_judge.sh:205-229`, `scripts/review_rb_judge.sh:262-263`
- **Current call count** — `2` `GET /repos/{owner}/{repo}/pulls/{pull_number}` calls on the judge fallback path: one earlier in `Collect PR metadata`, one later in `review_rb_judge.sh` when linked-issue GraphQL returns empty.
- **Proposed call count** — `1`
- **Endpoint(s)** — `GET /repos/{owner}/{repo}/pulls/{pull_number}`
- **Evidence** — `review_autofix` already stages PR title/body into runtime files, but the judge fallback re-fetches the same PR title/body from the API:
  ```bash
  # .github/workflows/review_autofix.yml
  gh_retry "${PR_PAYLOAD_FILE}" api repos/${{ github.repository }}/pulls/"${PR_NUMBER}"
  jq '{
    title: (.title // ""),
    body: (.body // ""),
    baseRefName: (.base.ref // ""),
    headRefName: (.head.ref // ""),
    headRepoFullName: (.head.repo.full_name // "")
  }' "${PR_PAYLOAD_FILE}" > "${PR_META_FILE}"

  # scripts/review_rb_judge.sh
  if [ -z "${ISSUE_NUMBERS}" ]; then
    PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' || echo "")"
  fi
  ```
  The same script later already consumes `PR_META_FILE` / `PR_PAYLOAD_FILE` for prompt assembly:
  ```bash
  PRELOADED_PR_META="$(jq -c '{ ... }' "${PR_META_FILE}" 2>/dev/null || echo '{}')"
  ...
  echo "Title: $(jq -r '.title // ""' "${PR_META_FILE}")"
  echo "Body: $(jq -r '.body // ""' "${PR_PAYLOAD_FILE}")"
  ```
- **Proposed fix** — In `scripts/review_rb_judge.sh`, change the linked-issue fallback block to read `PR_DATA` from `PR_META_FILE` or `PR_PAYLOAD_FILE` first, and only fall back to `_safe_gh_jq "repos/.../pulls/${PR_NUMBER}"` if the staged files are missing or empty.
- **Safety rationale** — `NEEDS_VERIFICATION` is required because static review cannot fully prove that `review_rb_judge.sh` never runs without populated metadata files or that live PR title/body freshness is irrelevant by the time the judge executes.
- **Downstream signal** — Verify that all in-repo launch paths for `review_rb_judge.sh` populate `PR_META_FILE`/`PR_PAYLOAD_FILE`, and confirm no intentional “re-read live PR body” requirement before replacing the fallback GET.

#### Finding REUSE-003
- **ID** — `REUSE-003`
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path and line ranges** — `.github/workflows/issue_pr_status.yml:280-330`, `.github/workflows/issue_pr_status.yml:384-386`, `.github/workflows/issue_pr_status.yml:447-512`
- **Current call count** — On the successful batch-classification path, `1` batched GraphQL classification call plus `N` later `GET /issues/{issue_number}` calls in the merged-alert step.
- **Proposed call count** — `1` batched GraphQL classification call plus `0` later merged-alert GETs.
- **Endpoint(s)** — GraphQL `repository { issue(number) { number labels body } }`, `GET /repos/{owner}/{repo}/issues/{issue_number}`
- **Evidence** — The earlier close-handling step already fetches the exact fields the later alert step re-fetches:
  ```bash
  ORCH_ALIAS_FRAGMENT+=" i${ORCH_IDX}: issue(number: ${_orch_num}) { number labels(first: 50) { nodes { name } } body }"
  ORCH_RESP="$(gh_retry gh api graphql -f query="${ORCH_QUERY}" 2>/dev/null || echo '')"
  ...
  echo "LINKED_ISSUE_NUMBERS<<EOF" >> "$GITHUB_ENV"
  echo "${ISSUE_NUMBERS}" >> "$GITHUB_ENV"
  echo "EOF" >> "$GITHUB_ENV"
  ```
  But the merged-alert step loops over the linked issues and fetches each body again:
  ```bash
  while IFS= read -r issue_number; do
    [ -n "${issue_number}" ] || continue
    BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
    if printf '%s' "${BODY}" | grep -qF 'Managed by: AI Orchestrator'; then
      IS_ORCHESTRATED="true"
      break
    fi
  done <<< "${LINKED_ISSUE_NUMBERS}"
  ```
- **Proposed fix** — Extend `Update linked issue labels when PR closes` to export either `MANAGED_ISSUES` / `TRACKING_ISSUES` or a dedicated boolean such as `HAS_ORCHESTRATED_LINKED_ISSUE` into `GITHUB_ENV`, and make `Send PR merged Telegram alert` consume that exported result instead of re-fetching each issue body.
- **Safety rationale** — `NEEDS_VERIFICATION` is correct because the later step currently suppresses alerts using a body-marker check, while the earlier classifier is broader (labels or body), so static review cannot prove that reusing the earlier result preserves alert-suppression semantics.
- **Downstream signal** — Verify whether alert suppression is supposed to track body marker only, broader orchestrator classification, or both; then export that exact decision from the earlier step and remove the later per-issue GET loop.

#### Finding REUSE-004
- **ID** — `REUSE-004`
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path and line ranges** — `.github/workflows/orchestrate_clarify_respond.yml:58-80`, `.github/workflows/orchestrate_clarify_respond.yml:387-405`
- **Current call count** — `2` child-issue fetches.
- **Proposed call count** — `1`
- **Endpoint(s)** — `GET /repos/{owner}/{repo}/issues/{issue_number}`
- **Evidence** — The same issue payload is fetched in two steps of the same job:
  ```bash
  # Check orchestrator metadata
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ISSUE_BODY="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -r '.body // ""')"
  ISSUE_TITLE="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -r '.title // ""')"

  # Fetch issue and tracking context
  ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ISSUE_BODY="$(printf '%s' "${ISSUE_META}" | jq -r '.body // ""')"
  ISSUE_TITLE="$(printf '%s' "${ISSUE_META}" | jq -r '.title // ""')"
  ```
- **Proposed fix** — In `Check orchestrator metadata`, persist the fetched child-issue JSON or at least `ISSUE_BODY`, `ISSUE_TITLE`, and `TRACKING_NUM` into `GITHUB_ENV` or the runtime workspace; in `Fetch issue and tracking context`, reuse that staged payload if present and retain the later `gh_retry gh api` call only as a missing-cache fallback.
- **Safety rationale** — `NEEDS_VERIFICATION` is required because the first step uses plain `gh api` while the second uses `gh_retry`, so any consolidation must preserve the later step’s retry/error-handling semantics.
- **Downstream signal** — Verify there is no intentional freshness check between the two steps, then stage the first step’s payload and keep the second step’s API call only as a missing-cache fallback.

### Dead Calls (DEAD-API-###)
No findings.

### Cross-References to Deep Audit Section
- `API-001`: `RISKY_SKIP` — lives in `scripts/orchestrate_poll_process.sh` with a documented cycle-local cache contract and poller/race-sensitive semantics.
- `API-002`: `RISKY_SKIP` — the dual `gh run list` probes sit in conflict-resolution dispatch dedupe logic, which is explicitly guarding against duplicate active pollers.
- `BATCH-001`: `NEEDS_VERIFICATION` — batching the fallback linked-issue context fetch looks directionally correct, but the capped body-text fallback semantics and fail-open behavior still need manual comparison.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 4 | REUSE-001, REUSE-002, REUSE-003, REUSE-004 |
| RISKY_SKIP | 1 | MERGE-001 |

### Implement-Stage Handoff
No SAFE_TO_MERGE findings in this pass.
