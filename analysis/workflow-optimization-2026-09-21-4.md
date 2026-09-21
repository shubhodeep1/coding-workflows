## Executive Summary

- **Review/autofix has the largest avoidable loss.** PR #4174 failed four times at `Commit changes`—runs `35579063461`, `35586406493`, `35594596407`, `35604714444`—after the commit guard reported missing linked-issue metadata. Cost: **45.17M reported token units and 4.36 workflow-hours**. Fixing metadata preservation/reconstruction should eliminate this cluster. **Impact: very high; confidence: high.**
- **Review queueing creates hour-scale latency.** Runs `35585215419` and `35623781454` waited **4,730s and 8,154s** before their main jobs started; the latter then spent only 118s active before returning `pr_closed`. Review/autofix also cancelled 22/107 runs. **Impact: 79–136 minutes saved per redundant queued run; confidence: high.**
- **CI is structurally serial.** The single `lint` job has p50 **2,449.5s** and p95 **2,594.1s**. Run `35609244845` executed 109 orchestrator tests before one failure. Splitting independent suites should cut critical-path latency by roughly **15–25 minutes**. **Confidence: high.**
- **Implementation repeats deterministic failures after model spend.** Runs `35614385686` and `35628923735` both spent 12 Codex calls/1.324M tokens before `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=scripts/helper.sh`. **Impact: up to 2.65M tokens and 41.9 minutes per repeated pair; confidence: high.**
- **Release authorization fails too late.** Run `35601057512` spent **7,070s** before `gh release create` returned HTTP 403, despite declared `contents: write`. An idempotent PAT fallback would prevent full gate reruns. **Confidence: high.**
- **Prompt cache and memory retrieval are mostly effective, but observability is incomplete.** Derived OpenRouter cache-read share is **78.9%**; sampled memory retrieval hit rate is 100%. However, aggregate `cache_hit_rate` is null, GH API counts are absent, and Serena emitted no probe/query telemetry. **Confidence: medium.**

## Speed Optimizations

1. **Critical path: suppress same-PR/head review runs before they enter concurrency queues.**
   - **Evidence:** `35585215419` waited 4,730s; `35623781454` waited 8,154s and ended `pr_closed`. PRs #4223 and #4229 each had both “Internal: AI Review & Autofix” and “Codex PR Self-Healing Semantic Agent” activity.
   - **Root cause:** active-run visibility races allow a second dispatch; `pr-autofix-<PR>` then queues it with `cancel-in-progress: false`.
   - **Exact change:** before dispatch/reusable invocation, query both review workflow IDs by head SHA, wait 5 seconds and recheck only when initially empty, then skip if another run owns the same PR/head. Reuse the sweep’s cycle-local snapshot logic.
   - **Savings:** 79–136 minutes on observed redundant runs.
   - **Risk:** low; fail open if the snapshot query fails.

2. **Critical path: split CI’s monolithic `lint` job.**
   - **Evidence:** CI p50 40.8 minutes; run `35609244845` took 2,392s and reported `108 passed, 1 failed`.
   - **Root cause:** hundreds of independent static checks and test files run sequentially in one job.
   - **Exact change:** separate static lint, orchestrator-process shards, and remaining contract/unit tests into 3–4 parallel jobs. Keep inventory parity as a final dependent job.
   - **Savings:** estimated 15–25 minutes per CI run.
   - **Risk:** low-medium; total runner-minutes may increase slightly.

3. **Critical path: move deterministic preflights ahead of AI execution.**
   - **Evidence:** implementation runs spent 20–22 minutes before discovering missing `scripts/helper.sh`; review failures spent 59–74 minutes before discovering unusable linked-issue metadata.
   - **Exact change:** validate staged-support manifests, model catalog membership, and linked-issue metadata existence/digest before reviewer/editor/Codex calls.
   - **Savings:** nearly the full 20-minute implementation run and 60-minute failed review run when these conditions recur.
   - **Risk:** low if checks fail closed using current behavior.

4. **Critical path candidate: shorten idle orchestrator polls.**
   - **Evidence:** `orchestrate_poll` p50 is 303s; runs `35632332776` and `35631380621` spent approximately 227s and 299s in `poll`. Several recent runs reported no merge-train releases.
   - **Exact change:** emit `ORCH_POLL_TIMING` with `queue_ms`, `scan_ms`, `api_ms`, `sleep_ms`, and `actions`; exit after the first scan when no actionable state exists.
   - **Savings:** potentially 4–5 minutes per idle poll.
   - **Risk:** medium until phase timing confirms that sleep, rather than useful work, dominates.

5. **Micro-optimization: retry actionlint downloads.**
   - **Evidence:** run `35615284953` failed in 82s on a transient HTTP 504.
   - **Exact change:** add three bounded `curl --retry-all-errors` attempts with exponential backoff and checksum verification unchanged.
   - **Savings:** avoids an entire CI rerun; negligible normal-path latency.
   - **Risk:** low.

## Cost Optimizations

1. **Prevent metadata-integrity reruns before tuning models.**
   - Four PR #4174 failures consumed **45,165,286 token units**.
   - Preserve `linked_issue_metadata.json` outside editor-cleanup scope; verify it immediately before editor execution and reconstruct it once from cached fallback issue numbers before commit.
   - **Expected savings:** 45M tokens for an equivalent four-run recurrence.
   - **Quality risk:** none; retain the scope guard and digest verification.

2. **Reject stale staged-support manifests before Codex.**
   - Runs `35614385686` and `35628923735` repeated identical 12-call, 1.324M-token failures.
   - Add `IMPLEMENT_STAGED_SUPPORT_PREFLIGHT path=... base_exists=...` before the Codex step.
   - **Expected savings:** 2.65M tokens for the observed pair.
   - **Quality risk:** none.

3. **Enable existing risk-tier selection gradually.**
   - `REVIEW_TIER_RESOLVER_ENABLED` and `REVIEWER_RISK_TIER_ENABLED` are disabled while the default panel contains six models.
   - Deep dives show Minimax alone accounting for 36.48M reported token units; clean-review runs `35585215419` and `35623806759` consumed 5.69M and 8.50M.
   - Canary the existing three-model standard tier for low-risk, non-workflow changes; preserve the full panel for `.github/`, `scripts/`, prompts, security, and disagreement.
   - **Expected savings:** approximately 25–45% of reviewer prompt/token usage on eligible PRs.
   - **Quality risk:** medium; retain automatic escalation and rollback variable.

4. **Resume from commit guard failures instead of repeating reviewers.**
   - All 12 sampled review summaries had `resume_restored=false`; three PR #4174 failures shared the same head SHA.
   - Persist the editor patch and scope metadata by head SHA. Classify missing metadata as `commit_guard_metadata`, rebuild it, and continue at commit.
   - **Expected savings:** most reviewer/consolidator tokens on same-head retries.
   - **Quality risk:** medium; require exact head SHA and artifact digests.

5. **Cache observations.**
   - Window totals: 66 Semble queries, 572,245 bytes—about 8.7KB/query. Queries were bounded to 6/12 chunks, so Semble appears to reduce whole-file expansion rather than add material noise.
   - Runtime Semble fallbacks were zero; all 20 fallbacks were contract tests.
   - Serena emitted no queries, responses, tools, fallbacks, or probes; no efficiency comparison is currently possible.
   - Add `source_bytes`, `injected_bytes`, and `prompt_tokens_avoided_estimate` to Semble telemetry.

## Reliability Improvements

1. **Correct the review failure classification and metadata lifecycle.**
   - **Evidence:** all four “editor changes lost” runs first logged `Refusing to commit: linked-issue metadata is unavailable`.
   - **Root category:** state/integrity handling, not necessarily lost editor output.
   - **Fix:** classify this as `commit_guard_metadata`; rebuild metadata once from cached linked-issue data, revalidate its digest, then retry commit.
   - **Logging:** `REVIEW_SCOPE_METADATA stage=commit status=missing|digest_mismatch path=... expected_sha=... actual_sha=... fallback_count=...`.
   - **Expected impact:** eliminate the observed 4/4 review failures.
   - **Rollback:** disable reconstruction and retain current fail-closed guard.

2. **Validate staged-support manifests before implementation.**
   - **Evidence:** both issue #4227 failures ended on missing `scripts/helper.sh`.
   - **Fix:** reject or regenerate stale manifest entries before Codex starts.
   - **Logging:** include manifest source ref, path, base existence and editor existence.
   - **Expected impact:** eliminate the observed 2/2 implementation failures.
   - **Rollback:** retain current commit-stage check.

3. **Make release creation idempotent with existing-token fallback.**
   - **Evidence:** run `35601057512` received HTTP 403 after 117.8 minutes.
   - **Fix:** first check `gh release view`; try `github.token`; on explicit 403 retry once with existing `GH_PAT`, then verify target/tag.
   - **Logging:** `RELEASE_CREATE auth_source=github_token|gh_pat result=... http_status=...`.
   - **Expected impact:** prevents full release-gate reruns for integration-token policy failures.
   - **Rollback:** remove PAT retry without changing tag logic.

4. **Retry transient tool downloads, not test regressions.**
   - Retry actionlint’s HTTP 504.
   - Do **not** retry CI run `35609244845` automatically: its failure was a real regression in `test_security_pass_deleted_branch_repair_accepts_only_exact_sha_create_race` (`branch_create_calls`).
   - Promote the failing test name to a top-level annotation to avoid searching through expected error output.

5. **MCP rollout interpretation.**
   - Semble’s 20 fallbacks were contract-test-only and therefore healthy.
   - Serena has zero probes and recent logs show it disabled/unavailable. This is an observability gap, not evidence of healthy fail-open operation.
   - Emit `SERENA_PROBE result=skipped reason=disabled` whenever disabled, and `failed` with an error class when enabled but unavailable.

6. **Policy pressure signals.**
   - `BREAK_GLASS`: 0.
   - `CONTEXT_BUDGET_WARN`: one occurrence, run `35603036628`, review prompt 91,218/128,000 tokens (71.26%).
   - This indicates isolated prompt-size pressure, not broad rubric/policy break-glass pressure.

## AI Memory Health

Deep-dive sample:

| Metric | Value |
|---|---:|
| Retrieve operations | 14 |
| Retrieves selecting records | 14 (100%) |
| Average selected records | 32.43 |
| Average estimated tokens | 1,416 |
| Average token budget | 1,428.6 |
| Average budget utilization | 99.14% |
| Keyword method | 12 `llm`, 2 `plain`, 0 `none` |
| Disabled retrieves | 0 |
| Zero-record retrieves | 0 |
| Fail-open entries | 20 |
| Pushes requiring >1 attempt | 4/34 |
| Maximum push attempts | 2 |

- Retrieval quality is strong, but token selection leaves almost no safety margin. Target 90–95% budget utilization to absorb formatting variance.
- Sixteen fail-open entries were `force-tick-get/put` failures across review runs `35579063461`, `35586406493`, `35594596407`, and `35604714444`. Four more affected candidate/run-event writes in slow successful reviews.
- Fail-open lines lack actionable reason fields. Add `error_class`, `stderr_tail_hash`, `duration_ms`, and `attempts`.
- All four multi-attempt pushes eventually succeeded; current retry behavior appears bounded and effective.

## GH API Call Audit

- **Collector gap:** no run-level GH API call totals, endpoint counts, retry totals, or rate-limit headers were supplied. No GitHub rate-limit event was observed; the release HTTP 403 was authorization-related.
- **Review sweep is cached but still chatty.** Run `35639523664` processed two candidates with no dispatches. Its code issues a minimum of:
  - one open-PR inventory request;
  - three status requests (`queued`, `in_progress`, `pending`) for each of two workflows.
  - **Minimum: seven requests per sweep**, excluding pagination.
- **Recommendation:** fetch recent runs once per workflow and filter the three statuses locally. Estimated reduction: **7 → 3 requests/run (57%)**, while preserving the existing cycle-local cache.
- **Issue/PR status follows repository rules.** Run `35638115686` used batched GraphQL classification and body/title fallback for issue #4228. No actual batch-failure fallback warning was emitted.
- **Repository rule alignment:** this matches `CLAUDE.md` §15’s requirement for batched GraphQL and cycle-local caches.
- Add a shared wrapper marker:
  - `GH_API_CALL endpoint_family=actions-runs method=GET attempts=1 duration_ms=... status=200 cache=miss rate_remaining=...`
  - Normalize IDs from endpoint names to avoid PII/cardinality problems.

## Prompt Cache & Memory System

- Aggregate `cache_hit_rate` is unavailable, but reported tokens imply a **78.9% weighted cache-read share**: 81.05M cache-read tokens versus 21.62M prompt tokens.
- Usage was available for 146/160 OpenRouter calls (91.25%); treat aggregate cache conclusions as medium confidence.
- Review-run cache rates ranged from **46.6% to 90.9%**, suggesting prefix fragmentation:
  - dynamic PR/run data may precede stable policy text;
  - model-specific prompt variants differ;
  - overflow-file lists vary between runs.
- Place stable system/rubric text first, then a cache breakpoint, followed by PR metadata, timestamps and Semble output.
- `test_and_mark_stable` had a 0% hit rate, 173,763 cache-write tokens and no reads. Reuse one stable release-analysis prefix across its 12 model calls.
- The single context warning in `35603036628` should emit per-component token counts (`foundation`, memory, diff, Semble, logs) so growth is attributable.
- Workspace/partial-resume state was not restored in the sampled review summaries. Target same-head commit-guard recovery before broader cache tuning.

## Orchestrator Health

- Core poller reliability is good: 51/51 successes, p50 303s, p95 455s.
- Health is obscured by long single-step polls and hosted-runner waits. Track queue time separately from active polling.
- The workflow fleet generates substantial skip/no-op traffic: 635/1,000 runs were “other”, with clarify/plan/implement/respond workflows often starting together and skipping in 1–10s.
- Review progression is the main stuck-state source:
  - 22/107 review runs cancelled;
  - two sampled review runs waited 79 and 136 minutes;
  - PR #4174 repeated the same terminal state four times.
- Stall recovery was rarely exercised: one killed reviewer slot in run `35623806759`; no sampled partial finalizations or restored resumes.
- Forward merge fail-open behavior worked: run `35638116796` opened PR #4230 and dispatched review after conflicts. Operational health remains degraded until that fallback PR reaches `main`.

Track:
- active/pending review runs per PR/head;
- terminal reason recurrence;
- queue age;
- poll active versus sleep time;
- resume-restored rate;
- forward-merge fallback age.

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Priority fix |
|---|---|---|---|
| Clarify | Dispatch/no-op overhead | 155 runs; p50 1s; 146 “other” | Tighten event routing after higher-impact fixes |
| Plan | Mostly skipped/no-op plus long tail | 143 runs; p50 1s; p95 369.5s | Emit route reason and active phase |
| Implement | Deterministic post-model guard failure | Two issue #4227 failures at 1,200–1,314s | Preflight staged-support manifest |
| Review/autofix | Queueing, six-model compute, commit-guard reruns | p50 1,056s; p95 5,134.9s; 22 cancellations | Same-head dedup, metadata recovery, risk tiers |
| CI | Serial compute | p50 2,449.5s; p95 2,594.1s | Split into parallel jobs |
| Validate | Sparse evidence | One 511s run; refresh 1,008s | Add stage timers before optimizing |
| Release | Late auth failure | Run `35601057512`, 7,070s, final HTTP 403 | Idempotent release auth fallback |
| Merge propagation | Conflict overhead | PR #4230 opened for five conflicted files | Deterministic handling for generated/changelog files |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review queueing and model execution; serial CI; long release gate.
- **Top failures:** four linked-metadata commit failures, two stale staged-support failures, one release authorization failure, one transient actionlint download failure, one real orchestrator test regression.
- **Highest-cost drivers:** review/autofix used 103.65M OpenRouter token units; implementation used 2.65M Codex tokens.
- **Top three actions:**
  1. Preserve/reconstruct linked-issue metadata and resume at commit.
  2. Add staged-support/model/metadata preflights before AI calls.
  3. Split CI and add same-head active-run suppression for review dispatches.

## Metrics Appendix

### Run outcomes

| Scope | Runs | Success | Failure | Cancelled | Other | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Repository total | 1,000 | 331 | 9 | 25 | 635 | 3s | 2,551s |
| Review/autofix | 107 | 77 | 4 | 22 | 4 | 1,056s | 5,134.9s |
| CI | 38 | 36 | 2 | 0 | 0 | 2,449.5s | 2,594.1s |
| Implement | 144 | 7 | 2 | 3 | 132 | 1s | 836.1s |
| Orchestrate poll | 51 | 51 | 0 | 0 | 0 | 303s | 455s |
| Copilot reviewer | 38 | 38 | 0 | 0 | 0 | 235s | 557.7s |
| Test/mark stable | 1 | 0 | 1 | 0 | 0 | 7,070s | 7,070s |

### AI cost and cache telemetry

| Metric | Value |
|---|---:|
| Runs with log telemetry | 118/1,000 (11.8%) |
| Codex calls / tokens | 24 / 2,648,330 |
| OpenRouter calls | 160 |
| Prompt tokens | 21,622,485 |
| Completion tokens | 1,163,418 |
| Cache-read tokens | 81,051,796 |
| Cache-write tokens | 173,763 |
| Total reported token units | 103,829,426 |
| Usage-available calls | 146/160 |
| Aggregate `cache_hit_rate` | unavailable |
| Derived cache-read share | 78.9% |
| `wall_clock_p50_ms` | 11,000 |
| `wall_clock_p99_ms` | 8,346,330 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 1 |

### MCP telemetry

| Server | Queries | Logged bytes | Fallbacks | Runtime fallbacks |
|---|---:|---:|---:|---:|
| Semble | 66 | 572,245 | 20 | 0 |
| Serena | 0 | 0 | 0 | 0 |

| Serena target | `probe_ok` | `probe_failed` | `probe_skipped` |
|---|---:|---:|---:|
| No target emitted | 0 | 0 | 0 |

- Semble fallbacks: 20 contract-test events, not runtime failures.
- Serena per-tool breakdown: none emitted.
- Other MCP servers observed through structured query/fallback/probe markers: none.
- Playwright MCP startup appeared in Copilot logs, but no query/response telemetry was emitted.

### GH API summary

| Workflow/run | Observed pattern | Result |
|---|---|---|
| Review sweep `35639523664` | Minimum seven inventory/status requests | Two candidates, both skipped active |
| Issue status `35638115686` | Batched linked-issue and classification reads | Issue #4228 finalized successfully |
| Release `35601057512` | One release-create mutation | HTTP 403 |
| Rate-limit events | No structured count available | None observed |

**Material data gaps:** only 7% success-log sampling, no GH API call counters, aggregate cache rate absent, no Serena probe telemetry, and sparse validate/release samples.

## Deep Audit — Workflows & Scripts (2026-09-21)

### Section 1: Bug & Correctness Sweep

Coverage: 50 workflows, 85 shell scripts, and 57 Python scripts. YAML parsing/yamllint, `bash -n`, and Python AST parsing completed without syntax failures. No secret-printing, `set -x`, or direct issue/PR-body interpolation into workflow shell source was found.

#### BUG-001 — Script-reference guard retains a stale exemption
- **File:** `scripts/check_workflow_script_refs.py:30-40,116-123,147-153`; `scripts/stage_workflow_support.sh:45-68`; `.github/workflows/implement.yml:963-970`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** `render_prompt.py` is excluded from missing-reference checks because comments claim it does not exist on `main`. It now exists, is main-primary in support staging, and is required by the implement bootstrap. Deleting it would therefore pass both repository-wide and post-resolver reference checks.
- **Recommended fix:** Remove `render_prompt.py` from `OPTIONAL_REFS`, correct the stale comment, and add a test proving a missing `render_prompt.py` fails the guard.

#### BUG-002 — Consumer drift audit omits a load-bearing wrapper
- **File:** `scripts/audit_consumer_drift.py:202-220,482-493`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** Expected wrappers are discovered only with `ai-*.yml`. This excludes `workflow-templates/review_rb_judge_dispatch.yml`, whose filename is required for autonomous `ai:review-blocked` recovery. The audit can report a consumer as matching while that wrapper is absent or stale.
- **Recommended fix:** Derive the managed wrapper set from `workflow-templates/profiles/full.txt`, or explicitly include `review_rb_judge_dispatch.yml`. Add a test asserting all 17 top-level managed templates are audited.

#### SHELL-001 — Consumer registry iteration permits word splitting and glob expansion
- **File:** `.github/workflows/mark-stable.yml:834-847`; `.github/workflows/test-and-mark-stable.yml:5523-5536`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** `for REPO in $REPOS` intentionally leaves the expansion unquoted. Inference: a malformed registry entry containing whitespace or glob characters could split into multiple dispatch targets.
- **Recommended fix:** Iterate with `while IFS= read -r REPO`, validate `owner/repo` syntax, and reject malformed entries before dispatch.

### Section 2: GitHub API Call Redundancy Audit

#### BATCH-001 — Drift audit performs one contents request per template
- **File:** `scripts/audit_consumer_drift.py:405-480,482-493,538-539`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** Each repository costs one directory request plus up to 16 file-content requests: **17 calls/repository**, or **up to 221 calls** for the current 13-repository registry. A GraphQL blob-alias query could reduce this to **13–26 calls total**. [NEEDS VERIFICATION]
- **Recommended fix:** Add `fetch_workflow_contents_batch(repository, file_names)` using the aliased-query pattern from `_fetch_candidate_issue_details_graphql`. Document payload limits and fall back to current per-file REST reads when an alias or blob is unavailable.

#### BATCH-002 — Staged-support latch release reads each candidate four times
- **File:** `scripts/orchestrate_poll_process.sh:15321-15419`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** For `N` latched issues and `M` candidates reaching live revalidation, reads total **1 + 2N + 2M**: inventory, comments, events, then labels and events again. Worst case is **1 + 4N**.
- **Recommended fix:** Batch initial comments, labels, and label-event timelines in groups of 25, then combine each candidate’s fresh labels and latest event into one revalidation query. Projected count: **1 + ceil(N/25) + M**. Extend the `_fetch_candidate_issue_details_graphql` alias/cache pattern while preserving mutation-time revalidation.

#### BATCH-003 — Security advisory follow-ups are fetched inside a loop
- **File:** `scripts/orchestrate_poll_process.sh:5964-6028`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** The loop issues one issue GET per unchecked follow-up plus one comments request for each blocked issue: **N + B calls**. Data is independent and batchable.
- **Recommended fix:** Prefetch state, labels, and recent comments with aliased GraphQL in batches of 25, reducing the normal path to **ceil(N/25) calls**. Fall back to paginated REST only when the comments connection reports older pages.

#### BATCH-004 — Standalone recovery performs seven label inventories
- **File:** `scripts/orchestrate_poll_process.sh:14398-14438,15504-15528`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** Every standalone-recovery cycle performs seven `gh issue list` calls—one per phase label—plus `_fetch_standalone_marker_issues_graphql`: **8 logical calls** before candidate hydration.
- **Recommended fix:** Extend `_fetch_standalone_marker_issues_graphql` with seven aliased label connections and retain REST pagination fallbacks when `hasNextPage=true`. Normal-path projection: **8 → 1 call**. [NEEDS VERIFICATION]

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Prompt path resolution is duplicated exactly
- **File:** `scripts/assemble_prompt.sh:12-41,51-93`; `scripts/render_prompt.sh:12-41,95-159`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** `resolve_prompt_file`, `resolve_render_prompt_py`, and `resolve_assembly_source_path` have identical implementations in both scripts.
- **Recommended fix:** Move them to `scripts/prompt_path_helpers.sh` with signatures `resolve_prompt_file <path>`, `resolve_render_prompt_py`, and `resolve_assembly_source_path <path>`. Source it from both callers.

#### DUP-002 — Consumer registry and command-executor implementations are duplicated
- **File:** `scripts/audit_consumer_drift.py:51-164`; `scripts/validation_refresh_runner.py:96-163,701-723`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** `CommandExecutor`, its error representation, and `load_target_repositories()` are structurally identical.
- **Recommended fix:** Create `scripts/consumer_repo_utils.py` owning `CommandExecutor.run(...)` and `load_target_repositories(path: Path) -> list[str]`; update both scripts to import it.

#### DUP-003 — Stable-release implementation is duplicated between release workflows
- **File:** `.github/workflows/mark-stable.yml:673-795`; `.github/workflows/test-and-mark-stable.yml:5368-5490`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** The two workflows carry approximately 5,000-character near-identical release blocks. Adjacent dispatch blocks have already drifted in retry behavior.
- **Recommended fix:** Extract `publish_stable_release <version> <source_branch> <tested_sha> <consumer_file> <dry_run>` into `scripts/stable_release_helpers.sh`; keep only workflow-specific wiring in YAML.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Implement support-staging block exceeds the medium-risk threshold
- **File:** `.github/workflows/implement.yml:932-1289`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** The interpolated `run:` scalar is approximately **16,985 characters**, containing three `${{ }}` expressions. It has **4,015 characters of headroom** before GitHub’s 21,000-character limit. No other interpolated block exceeds 15,000 characters.
- **Recommended fix:** Extract the block to `scripts/implement_stage_workflow_support.sh`. Pass repository and feature flags through step `env:` so the `run:` body contains no template interpolation.

No workflow exceeds the 800 KB warning threshold. The largest is `.github/workflows/review_autofix.yml` at approximately 500,755 characters.

### Section 5: Cross-Cutting Concerns

#### CONSIST-001 — Equivalent release dispatch paths use different retry policies
- **File:** `.github/workflows/mark-stable.yml:840-846`; `.github/workflows/test-and-mark-stable.yml:5529-5535`
- **Severity:** Medium
- **Category:** `consistency`
- **Description:** `mark-stable.yml` uses `gh_retry gh api` for consumer dispatches, while `test-and-mark-stable.yml` uses raw `gh api`. The latter can silently skip a consumer on transient API failures.
- **Recommended fix:** Route both through `gh_retry`, preferably inside the shared release helper proposed in DUP-003.

#### DEAD-001 — Reviewer raw-file aliases are assignment-only
- **File:** `scripts/review_run_reviewers.sh:753-777`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE` and `RAW_REVIEWER_SYMBOL_DIFF_SUMMARY_FILE` are assigned but never referenced.
- **Recommended fix:** Remove the aliases, or wire them into the filter rollback path if preserving those originals was intended.

#### DEAD-002 — Review ledger stores an unused floor map
- **File:** `scripts/review_issue_ledger.sh:862-918`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `CURRENT_FLOOR` is declared and populated but never read. `floor_cat` is already consumed directly while generating the issue ID.
- **Recommended fix:** Remove `CURRENT_FLOOR` and its assignment.

#### DEAD-003 — Merge-with-follow-up captures an unused current head SHA
- **File:** `scripts/review_rb_judge.sh:2128-2154`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `PR_HEAD_SHA` is fetched with comments claiming it binds the merge, but all checks and `--match-head-commit` correctly use `RB_JUDGED_HEAD_SHA`.
- **Recommended fix:** Remove `PR_HEAD_SHA` and update the comment to identify `RB_JUDGED_HEAD_SHA` as the authoritative evaluated head.

No `TODO`, `FIXME`, `HACK`, or `XXX` markers were found in scoped workflows or scripts.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 9 | BUG-001, BUG-002, BATCH-001, BATCH-002, BATCH-003, BATCH-004, DUP-003, EXPR-001, CONSIST-001 |
| Low | 6 | SHELL-001, DUP-001, DUP-002, DEAD-001, DEAD-002, DEAD-003 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 2–4 | Large |
| Code modularization | 7–9 | Large |
| Expression size reduction | 2 | Medium |
| Medium/Low fixes | 7–10 | Medium |
