## Executive Summary

- **Telemetry cost totals are materially overcounted.** Parent job logs and child-step logs duplicate usage events: run `35665803378` reports 32 OpenRouter calls, but parent-only and child-only each contain the same 16; run `35668070395` similarly doubles 8 Semble queries to 16. The unanchored Codex token regex also mistakes prompt/diff text for usage: collector total is 7.945M tokens, while the five failed implement runs contain 1.231M in validated standalone usage lines. **Impact: cost conclusions are unsafe until fixed; confidence: high.**
- **One deterministic defect caused 7 of 13 failures.** Five implement runs (`35614385686`, `35628923735`, `35642366131`, `35656715219`, `35668070395`) completed Codex edits, then failed on `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=scripts/helper.sh`. CI runs `35664916070` and `35670353076` subsequently failed inventory parity on the same nonexistent path. **Potential reduction: 54% of observed failures and 97 minutes of failed implement runtime; confidence: high.**
- **Review/autofix is the dominant latency bottleneck.** Its p50/p95 are 16.8/92.6 minutes. Twenty of 95 runs were cancelled; 16 were cancelled before their first agent step. Cancelled review runs accumulated 13.5 hours of run lifetime, median 35.5 minutes. **Confidence: high.**
- **Deterministic CI checks fail too late.** Inventory failures surfaced after 15.8 and 17.0 minutes; the release-tag contract after 17.0 minutes. CI p50 is 37.4 minutes. **Expected fail-fast saving: 14–16 minutes per affected run; confidence: high.**
- **Release capability was discovered after the 94-minute release gate.** Run `35639756385` failed after three tag-push rejections stating that workflow scope might be required. **Expected saving from an early permission dry-run: up to 90 minutes per recurrence; confidence: high.**
- **AI-memory retrieval is effective, but write failure handling is expensive.** All 13 sampled retrieves found records, but run `35665803378` incurred nine separate 16-attempt push failures caused by missing Git credentials. **Expected saving: roughly 10–16 minutes on an affected run; confidence: medium.**

## Speed Optimizations

1. **Fix the staged-support reinstallation guard — critical path.**
   - **Evidence:** Five implement failures ended after successful Codex edits, on the same missing `scripts/helper.sh`; durations were 1,076–1,314 seconds.
   - **Root cause:** A path not present in the original staged-support overwrite ledger is treated as a missing required support baseline.
   - **Exact change:** In `implement_staged_support_workspace.sh`, fail closed only when a path recorded in `STAGED_SUPPORT_LEDGER` lacks its baseline. Treat other newly introduced paths as editor-owned and emit `IMPLEMENT_STAGED_SUPPORT_EDITOR_SKIPPED_PATH reason=not_staged_support`.
   - **Savings:** About 18–22 minutes per run; 97 minutes across the five failures, excluding reruns.
   - **Risk:** Low-medium; preserve the existing hard failure for actual overwritten support files.

2. **Move cheap deterministic gates ahead of the CI test corpus — critical path.**
   - **Evidence:** Inventory parity failed at the end of runs `35664916070` and `35670353076`; the refspec contract failed late in `35640669723`.
   - **Root cause:** Inventory and release contracts run after most unit and workflow tests.
   - **Exact change:** Run generated-doc drift, actionlint, release-refspec contract, and inventory parity immediately after checkout/setup. Keep the existing later order-contract test.
   - **Savings:** Approximately 14–16 minutes per deterministic failure; about 45 minutes across these three observed runs.
   - **Risk:** Low.

3. **Suppress redundant review dispatches before they enter the concurrency queue — critical path.**
   - **Evidence:** 20 review cancellations, 16 before the first step; median cancelled duration 2,127 seconds.
   - **Root cause:** Per-PR concurrency preserves the running job but newer dispatches replace the single pending job.
   - **Exact change:** Route redispatches through one canonical wrapper and check queued/in-progress runs for the PR before invoking `review_autofix.yml`. Emit `REVIEW_DISPATCH_SUPPRESSED pr=... source=... active_run=...`.
   - **Savings:** Up to 35 minutes median queue lifetime per suppressed duplicate; less run-list and collector noise.
   - **Risk:** Low-medium; fail open if the active-run lookup fails.

4. **Add a release token-capability preflight — critical path.**
   - **Evidence:** Run `35639756385` spent 5,649 seconds before tag publication failed with a workflow-scope rejection.
   - **Exact change:** Before expensive release jobs, perform a non-mutating authenticated push/dry-run against an unambiguous temporary tag ref and classify workflow-scope failures as permanent.
   - **Savings:** Up to roughly 90 minutes per recurrence.
   - **Risk:** Low.

5. **Parallelize the monolithic CI lint job — success-path optimization.**
   - **Evidence:** CI p50/p95 are 37.4/43.3 minutes; run `35609244845` spent 39.9 minutes before one orchestrate-poll shard failed.
   - **Exact change:** Split static/contracts, general unit tests, and orchestrate-poll shards into parallel jobs, sharing only immutable caches.
   - **Savings:** Estimated 10–18 minutes on successful CI.
   - **Risk:** Medium due to ordering and hidden shared-state assumptions.

## Cost Optimizations

1. **Repair usage accounting before making model or budget decisions.**
   - **Evidence:** Run `35665803378` doubles 16 OpenRouter calls into 32; run `35668070395` doubles Semble bytes from 47,443 to 94,886. The Codex regex parses 12 calls from a log containing one genuine standalone token report.
   - **Exact change:** Emit anchored structured lines such as `CODEX_USAGE event_id=... phase=... tokens=...`; parse only those. For OpenRouter/MCP telemetry, parse leaf step logs when available and ignore the composite parent job log.
   - **Savings:** Diagnostic rather than direct; prevents incorrect model downgrades and billing estimates.
   - **Quality risk:** None.

2. **Avoid rerunning successful agent work after harness failures.**
   - **Evidence:** Validated standalone usage in the five failed implement runs totals **1,231,497 tokens**, averaging 246,299 per run; all agents reported success before the support guard failed.
   - **Exact change:** Fix the guard and persist a failure fingerprint containing issue, head SHA, plan hash, changed-file hash, and harness failure class. Do not rerun the model when the productive diff is recoverable.
   - **Savings:** Up to 1.23M validated tokens for this incident class.
   - **Quality risk:** Low if the existing diff and validation artifacts are preserved.

3. **Stop retrying deterministic memory authentication failures.**
   - **Evidence:** Run `35665803378` exhausted 16 attempts nine times for `could not read Username`.
   - **Exact change:** Classify credential/permission errors as permanent after one attempt; retain 16-attempt jitter only for non-fast-forward/ref-lock races.
   - **Savings:** Primarily runner time; roughly 10–16 minutes in the observed run.
   - **Quality risk:** None; memory remains fail-open.

4. **Reduce review prompt pressure selectively, not by globally downgrading models.**
   - **Evidence:** Unique warnings occurred in `35603036628` at 91,218/128,000 tokens and `35665803378` at 167,186/200,000. The latter used 16 unique OpenRouter calls and five successful reviewers.
   - **Exact change:** Log prompt bytes by section and trim repeated historical tails or duplicated diff material first. Enable the existing low-risk review tier only for guarded docs/small-diff paths.
   - **Estimated savings:** Target 5–10% of uncached prompt input after measurement.
   - **Quality risk:** Medium; keep workflow/scripts/security paths on the full panel and `xhigh` reasoning.

5. **Measure Semble avoidance before expanding or reducing it.**
   - **Evidence:** Collector reports 86 queries and 698,311 bytes, but detailed bundles demonstrate duplication. No `source_bytes_avoided` baseline exists.
   - **Exact change:** Add `candidate_bytes`, `returned_bytes`, `chunks_returned`, and `full_file_bytes_avoided` to `SEMBLE_QUERY`.
   - **Quality risk:** None. Current evidence does not justify disabling Semble.

## Reliability Improvements

1. **Correct the `scripts/helper.sh` systemic failure.**
   - **Failure evidence:** Five implement failures plus two inventory-parity failures.
   - **Category:** Deterministic harness/state reconciliation.
   - **Fix:** Apply the ledger-scoped guard described above and run targeted inventory parity immediately after any `agents.md` change.
   - **Expected impact:** Could remove 7/13 observed failures.
   - **Rollback:** Restore hard failure for all paths if unexpected support-file leakage appears.

2. **Treat release permission failures as permanent and preflight them.**
   - **Evidence:** Three identical remote rejections in run `35639756385`.
   - **Category:** Token capability/configuration.
   - **Fix:** Validate the release credential before tests and log `RELEASE_TAG_PREFLIGHT result=... error_class=workflow_scope`.
   - **Impact:** Avoids doomed release runs; retries remain for transient network/ref races.
   - **Rollback:** Disable the preflight while retaining existing publication behavior.

3. **Make state-snapshot persistence succeed if either durable channel succeeds.**
   - **Evidence:** Poll run `35669827207` uploaded 548 bytes, then artifact finalization failed with intermediary HTTP 403; branch publication still ran.
   - **Category:** External artifact-service failure.
   - **Fix:** Give upload and branch publication step IDs, make each best-effort, then fail only if both outcomes fail. Emit size, SHA-256, artifact outcome, and branch outcome.
   - **Impact:** Removes the observed 1/60 poll failure without losing state.
   - **Rollback:** Return to artifact-authoritative behavior if consumers cannot read the branch.

4. **Retry actionlint downloads safely.**
   - **Evidence:** Run `35615284953` failed on one HTTP 504.
   - **Category:** Transient dependency download.
   - **Fix:** Use bounded `curl --retry --retry-all-errors`, preserving pinned version and SHA-256 verification.
   - **Impact:** Likely eliminates this failure class.
   - **Rollback:** None beyond reverting retry flags.

5. **Promote structured failure class over GitHub’s last failed step.**
   - **Evidence:** Run `35604714444` is labeled `Commit changes`, but `REVIEW_AUTOFIX_RUN_SUMMARY_V1` identifies `editor_changes_lost`; commit status was `no_commit`.
   - **Fix:** Have the analyzer extract `finalize_reason` and slot failure class into `root_failure_class`, retaining the GitHub step as `symptom_step`.
   - **Impact:** Better auto-triage and fewer misdirected repairs.
   - **Fail-open:** Fall back to the GitHub step when no structured summary exists.

6. **Fix telemetry de-duplication.**
   - Strip timestamps/ANSI before comparison, or prefer leaf logs over composite logs. Add `event_id` to all structured telemetry.
   - This changes reporting only; rollback is trivial.

`BREAK_GLASS` count was zero. The collector reported three context warnings, but raw de-duplication finds two unique events; this is prompt-size pressure, not break-glass/policy pressure.

## AI Memory Health

- **Retrieval:** 13 sampled retrieves; **100% hit rate** (`records_selected > 0`).
- **Context utilization:** Average estimated tokens **1,459** versus average budget **1,477**—about **98.8%** utilization.
- **Keyword methods:** `llm` 8, `plain` 5, `none` 0.
- **No sampled retrieve returned zero records; no `enabled:false` operation was observed.**
- **Fail-open:** 12 structured fail-open operations were observed. Run `35665803378` is the primary concern: nine distinct writes exhausted 16 push attempts because authentication was absent. Runs `35604714444`, `35628119734`, `35610997224`, and `35644282107` also contained fail-open operations.
- **Recommendation:** Emit `error_class`, `attempt`, cumulative `elapsed_ms`, `auth_configured`, and backoff duration on every memory write. Stop immediately on permanent auth errors; retain fail-open behavior.

## GH API Call Audit

- **Runtime call counts are not collected**, so high-volume endpoints and exact reductions cannot be quantified. No genuine GitHub rate-limit event appeared in the selected full logs.
- **Positive policy alignment:** `CLAUDE.md §15` requires batched GraphQL and cycle-local caches. The implement existing-PR check uses one timeline request plus one open-PR inventory rather than a per-candidate REST loop, and issue metadata is generally cached in `ISSUE_META_FILE`.
- **Audit gap:** Direct calls and `gh_retry` retries emit no aggregate endpoint, pagination, cache-hit, latency, or rate-limit-sleep metrics.
- **Exact instrumentation:** Have `gh_helpers.sh` append local JSONL records and emit one job-end line:
  `GH_API_SUMMARY logical_calls=... requests=... pages=... retries=... cache_hits=... rate_limit_events=... rate_limit_sleep_ms=... endpoints=...`
- **Required fields per logical call:** normalized endpoint template, method, pages, attempts, status class, duration, result count, cache hit/miss. Do not log tokens or full response bodies.
- **Estimated reduction:** Unknown until instrumentation exists. The first target should be repeated same-resource reads within a job; preserve §15’s existing batched poller design.

## Prompt Cache & Memory System

- **Official aggregate `cache_hit_rate`: unavailable** because 22/173 OpenRouter calls lacked usage data.
- The raw aggregate implies an **83.1% apparent cache-read ratio**, but duplicated detailed logs mean this should be treated as directional only.
- Fully reported run rates ranged from **66.25% to 93.73%**; release run `35639756385` was a cold 0% cache-read run.
- High review cache reads suggest stable prefixes are broadly working. There is no direct evidence of severe prefix fragmentation.
- Two unique context-budget warnings show prompt growth is eroding headroom. Add `PROMPT_SECTION_BYTES`, stable-prefix hash, dynamic-suffix bytes, and truncation decisions to each phase.
- Keep reusable instructions and schemas before dynamic run data; place timestamps, run IDs, comments, and diffs after the cached prefix.
- Memory retrieval is effective, but its 98.8% average budget utilization leaves little slack. Monitor relevance and age distribution before increasing the budget.

## Orchestrator Health

- `orchestrate_poll`: 60 runs, 59 successes, one artifact finalization failure; p50/p95 **303/337 seconds**.
- Conflict healing worked: forward-merge run `35667913255` detected a conflict and dispatched review run `35667981432`; subsequent review runs resolved and pushed edits.
- Review sweeps correctly skipped active work—for example, run `35670160209` skipped PRs `#4244` and `#4223`.
- Operational pain remains in pending review churn: 16 review jobs were cancelled before the first agent step.
- Phase fan-out is noisy: clarify, plan, implement, and clarify-response frequently launch together and skip in 0–3 seconds. These account for much of the 660 skipped runs.
- Two successful post-merge runs warned that no standalone validation could be dispatched, although the linked issues did not require validation. Log these as notices unless a validation-required label is present.
- **Track:** dispatch suppressions, pending-slot replacements, active issue counts, phase-transition counts, conflict-heal attempts, validation-required dispatch failures, and per-poll phase timings.

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Priority fix |
|---|---|---|---|
| Clarify → plan | Event fan-out/no-op runs | 151/160 clarify and 139/147 plan runs were non-success “other,” predominantly skipped | Narrow routing or use a single intake dispatcher |
| Implement | Productive work discarded by harness | Five successful Codex edits failed on staged-support restoration | Ledger-scoped reinstallation guard |
| Review/autofix | Compute plus pending-queue churn | p95 92.6m; 20 cancellations; 13.5h cancelled lifetime | Dispatch de-duplication and risk-tier canary |
| CI | Long sequential lint corpus | p50 37.4m; late deterministic failures | Early contracts plus parallel shards |
| Validate/release | Late capability discovery | Release failed after 94m on tag permission | Early dry-run capability probe |
| Orchestrate poll | Fixed ~5m cycle; weak phase detail | p50 303s; poll step dominates | Emit per-phase durations before tuning |
| Merge/conflict | Generally healthy, occasional changes-lost path | Conflict healing succeeded; run `35604714444` ended `editor_changes_lost` | Persist editor diff/status fingerprint |

Queueing, compute, retry, and merge overhead should be reported separately; current wall-clock fields combine them.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix p95 92.6 minutes; CI p50 37.4 minutes; poll p50 5.1 minutes.
- **Top failures:** staged-support `scripts/helper.sh` reconciliation (five implement failures), late inventory drift (two CI failures), release tag permission, artifact finalization 403, and one editor-changes-lost review.
- **Highest-cost drivers:** full multi-model review panels, large cached review prompts, and productive implement calls discarded by the harness. Raw token totals are currently inflated by parser duplication.
- **Top three actions:**
  1. Fix structured telemetry de-duplication and anchored Codex usage emission.
  2. Correct staged-support reconciliation and run inventory checks immediately after affected edits.
  3. De-duplicate review dispatches and short-circuit permanent memory-auth failures.

## Metrics Appendix

### Run outcomes

| Scope | Runs | Success | Failure | Cancelled | Skipped/other | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Repository total | 1,000 | 304 | 13 | 23 | 660 | 6s | 2,392s |
| CI | 35 | 30 | 5 | 0 | 0 | 2,244s | 2,595s |
| Review/autofix | 95 | 69 | 1 | 20 | 5 | 1,005s | 5,557s |
| Implement | 148 | 6 | 5 | 3 | 134 | 2s* | 809s |
| Orchestrate poll | 60 | 59 | 1 | 0 | 0 | 303s | 337s |

\*Implement p50 is dominated by skipped runs. Executed failures lasted 1,076–1,314 seconds.

- All-run failure rate: **1.3%**.
- Active-run success rate, excluding skipped runs: **89.4%**.
- Review cancellation rate: **21.1%**.

### Cost and cache telemetry

| Metric | Collector value | Integrity note |
|---|---:|---|
| Codex tokens/calls | 7,944,990 / 72 | Inflated by unanchored parsing and duplicate logs |
| Validated token lines in five failed implement runs | 1,231,497 / 5 | Deep-dive subset, not full-window total |
| OpenRouter prompt tokens | 25,217,436 | Duplicate parent/child usage exists |
| OpenRouter completion tokens | 1,146,550 | Same caveat |
| OpenRouter cache reads/writes | 124,741,898 / 173,547 | Same caveat |
| OpenRouter calls unavailable | 22/173 (12.7%) | Makes official cache hit rate null |
| Apparent cache-read ratio | 83.1% | Directional only |
| Wall-clock p50/p99 | 12s / 8,986s | 119 samples |
| Break glass | 0 | No policy override pressure |
| Context warnings | Reported 3; unique 2 | Duplicate parent/child warning in `35665803378` |

### Semble and Serena

| System | Queries | Bytes | Fallbacks | Runtime | Contract-test |
|---|---:|---:|---:|---:|---:|
| Semble, collector | 86 | 698,311 | 51 | 3 | 48 |
| Serena | 0 | 0 | 0 | — | — |

Semble’s apparent runtime fallback rate is about **3.4%** of successful queries plus runtime fallbacks, but totals contain known duplication.

| Deep-dive target | Query events | Fallback events | Probe ok/failed/skipped | Coverage |
|---|---:|---:|---:|---|
| Semble `overflow` | 51 | 22 | N/A | Selected bundles only |
| Semble `reviewer-context` | 8 | 0 | N/A | Selected bundles only |
| Serena targets | 0 | 0 | 0 / 0 / 0 | Serena disabled; no skipped probe emitted |

**Serena per-tool breakdown:** none observed.  
**Other MCP servers observed:** none.

### AI memory

| Metric | Value |
|---|---:|
| Sampled retrieves | 13 |
| Retrieve hit rate | 100% |
| Average estimated tokens | 1,459 |
| Average token budget | 1,477 |
| Keyword method | 8 `llm`, 5 `plain`, 0 `none` |
| Zero-record retrieves | 0 |
| Structured fail-open operations | 12 |
| Disabled operations | 0 |
| Worst push-retry incident | Nine 16-attempt failures in run `35665803378` |

### GH API

| Metric | Value |
|---|---|
| Logical calls/requests/pages | Not collected |
| Rate-limit events | None found in selected full logs |
| Retry/sleep totals | Not collected |
| Endpoint hotspots | Not measurable |
| Policy | Batched GraphQL and cycle-local caching required by `CLAUDE.md §15` |

### Material data gaps

- Usage events lack stable event IDs and are duplicated across composite and leaf logs.
- The Codex usage regex is not anchored to structured producer output.
- MCP target/tool maps are calculated internally but not propagated into `analysis_context.json`.
- Queue time is not separated from execution time.
- GitHub API calls, pagination, retries, and cache hits are not counted.
- `success_sample_rate` is configured at 7%, but `sampled_success_runs` is reported as zero.

## Deep Audit — Workflows & Scripts (2026-09-22)

### Section 1: Bug & Correctness Sweep

#### BUG-001 — Transient API failures can select the wrong checkout branch
- **File path and line range:** `scripts/resolve_integration_ref.sh:51-100`; `.github/workflows/clarify.yml:118-129`; `.github/workflows/implement.yml:459-470`
- **Severity:** High
- **Category:** `bug`
- **Description:** Issue and branch metadata use raw `gh api` under `set -euo pipefail`. A transient failure exits the resolver, after which callers write an empty ref and fall back to the default branch. Planning or implementation can therefore run against the default branch instead of the issue’s declared integration branch.
- **Recommended fix:** Source sibling `gh_helpers.sh`, use `gh_retry`/`_safe_gh_jq`, and distinguish confirmed 404 from indeterminate API failure. Make callers fail closed or defer when resolution is indeterminate rather than selecting the default branch.

#### SEC-001 — PAT is interpolated into shell source and persisted in the remote URL
- **File path and line range:** `.github/workflows/implement.yml:4328-4340`; `.github/workflows/review_autofix.yml:5974-6010`
- **Severity:** Medium
- **Category:** `security`
- **Description:** Both steps interpolate `${{ secrets.GH_PAT }}` directly into a `run:` body and store it in `.git/config` through `git remote set-url`. GitHub masking reduces log exposure but does not prevent the credential from entering generated shell source or persisting in repository configuration.
- **Recommended fix:** Pass only `GH_TOKEN` through `env:` and authenticate pushes with a transient `http.extraHeader`, following the `resolver_git()` pattern at `.github/workflows/implement.yml:424-427`. Keep the configured remote credential-free.

### Section 2: GitHub API Call Redundancy Audit

#### API-001 — Reissue paths fetch the same issue twice for title and body
- **File path and line range:** `scripts/orchestrate_poll_process.sh:13731-13733`, `16413-16421`, `21397-21399`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** Three reissue paths make two consecutive `GET /issues/{n}` calls, selecting `.title` and `.body` separately. Current count is **2 calls per reissued issue**; proposed count is **1**.
- **Recommended fix:** Fetch `{title, body}` once and parse both locally. Prefer `_candidate_details_json` when populated; otherwise cache one `_safe_gh_jq` response, following the poller’s cycle-local cache pattern.

#### API-002 — Final-PR state and merge status use duplicate PR reads
- **File path and line range:** `scripts/orchestrate_poll_process.sh:10230-10242`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** On a snapshot miss, `.state` and `.merged_at` are read through two identical `GET /pulls/{n}` calls. Current count is **2**; proposed count is **1**.
- **Recommended fix:** Fetch the PR JSON once and parse both fields, then retain it in `final_pr_json_snapshot` for later checks.

#### API-003 — Merge-train comment upsert discards an already-fetched body
- **File path and line range:** `scripts/review_merge_train.sh:255-290`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** `_mt_find_marker_comment_id` lists comments but returns only the ID; `_mt_upsert_comment` then fetches the selected comment again for its body. Existing-comment paths use **2 calls** instead of **1**.
- **Recommended fix:** Return/cache `{id, body}` from the paginated comment listing. Extend the script’s existing `_MT_FILES_CACHE` approach with a marker-comment cache.

#### API-004 — Inline retry wrappers retry permanent failures
- **File path and line range:** `.github/workflows/review_autofix.yml:1123-1136`, `1326-1338`; `.github/workflows/implement.yml:492-529`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** These wrappers retry every failure without classifying authentication, permission, 404, or validation errors. One logical request can become **4 attempts** in review or **3 attempts** in implement; permanent failures should require **1 attempt**.
- **Recommended fix:** Reuse `gh_helpers.sh::_is_gh_permanent_failure` and the canonical `gh_retry`. If a lightweight job cannot stage the full helper, extract the classifier into a minimal shared helper.

#### BATCH-001 — Merge train performs one files request per older PR
- **File path and line range:** `scripts/review_merge_train.sh:41-59`, `110-137`, `304-329`, `423-458`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** The gate issues one paginated `/pulls/{n}/files` request for each older PR, capped at 20. The file-read phase is currently **N calls, up to 20**; an aliased GraphQL batch could reduce the normal case to **1 batch call**, with REST fallback only for pagination overflow. [NEEDS VERIFICATION]
- **Recommended fix:** Extend `_fetch_candidate_issue_details_graphql`’s alias-builder pattern to batch PR file connections and populate `_MT_FILES_CACHE`. End-to-end gate cost would typically fall from `1 + N` reads to `2`.

#### BATCH-002 — Body-derived linked issues trigger per-issue label reads
- **File path and line range:** `.github/workflows/review_autofix.yml:1144-1240`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** When closing-issue GraphQL data is unavailable and issue numbers are extracted from PR text, the loop calls `gh issue view` once per issue. Current count is **N**; proposed count is **ceil(N/50)**, normally **1**.
- **Recommended fix:** Batch the extracted issue numbers using aliased GraphQL and return labels keyed by issue number, extending `_fetch_candidate_issue_details_graphql`.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Context-budget warning function is copied three times
- **File path and line range:** `scripts/review_apply_fixes.sh:164-202`; `scripts/review_rb_judge.sh:256-294`; `scripts/review_run_reviewers.sh:69-107`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** `emit_context_budget_warn_for_prompt` is byte-equivalent across all three scripts.
- **Recommended fix:** Move it to a new `scripts/prompt_budget_helpers.sh` as `emit_context_budget_warn_for_prompt <phase> <prompt_path> <model>`. Source it from all three callers.

#### DUP-002 — Semble query-section rendering is copied three times
- **File path and line range:** `scripts/review_apply_fixes.sh:909-918`; `scripts/review_conflict_prepare.sh:605-614`; `scripts/review_run_reviewers.sh:1760-1769`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** `append_semble_query_section` has the same implementation in three review paths.
- **Recommended fix:** Add `semble_append_query_section <label> <path> [max_bytes]` to `scripts/semble_helpers.sh` and update all three callers.

#### DUP-003 — Integration-ref bootstrap is repeated across five workflows
- **File path and line range:** `.github/workflows/clarify.yml:57-130`; `.github/workflows/plan.yml:120-196`; `.github/workflows/implement.yml:397-471`; `.github/workflows/orchestrate_clarify_respond.yml:110-184`; `.github/workflows/validate.yml:96-170`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** Each workflow contains approximately 70 lines for cloning the resolver source, configuring auth, redacting logs, executing `resolve_integration_ref.sh`, and applying fallback behavior. Drift here directly affects branch selection.
- **Recommended fix:** Checkout the resolver support source through one shared step pattern and invoke a canonical `resolve_integration_ref_bootstrap <repo> <issue> <resolver_ref>` script. Update all five workflows.

#### DUP-004 — Codex tool-cache persistence is repeated five times
- **File path and line range:** `.github/workflows/plan.yml:90-119`; `.github/workflows/workflow-log-analysis.yml:230-255`, `718-747`, `1405-1434`, `1916-1945`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Identical npm-root discovery, package copying, symlink restoration, and validation blocks are repeated across five jobs.
- **Recommended fix:** Add `scripts/codex_tool_cache.sh persist|restore <tool_cache>` and replace the duplicated shell bodies with calls to that script.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Support-staging expression is within 674 characters of rejection
- **File path and line range:** `.github/workflows/implement.yml:932-1288`
- **Severity:** High
- **Category:** `expression-limit`
- **Description:** The interpolated `Stage workflow support files` block is approximately **20,326 characters**, leaving **674 characters** before the 21,000-character runner limit.
- **Recommended fix:** Extract the body to `scripts/implement_stage_workflow_support.sh` and pass `github.repository` and repository variables through `env:`. Execute the script from the checked-out workflow-support source.

#### EXPR-002 — Preflight guard has limited expression headroom
- **File path and line range:** `.github/workflows/implement.yml:3143-3449`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** The interpolated `Preflight destructive-commit guard` block is approximately **17,313 characters**, leaving **3,687 characters** of headroom.
- **Recommended fix:** Extract it to `scripts/implement_preflight_commit_guard.sh`, passing repository identity through environment variables.

No workflow exceeds the 800 KB warning threshold. The largest is `.github/workflows/review_autofix.yml` at approximately 500,224 characters.

### Section 5: Cross-Cutting Concerns

#### CONSIST-001 — Documented review thread reuse is intentionally ignored
- **File path and line range:** `scripts/review_apply_fixes.sh:1598-1606`; `scripts/review_conflict_resolve.sh:1659-1671`; `README.md:129`
- **Severity:** Medium
- **Category:** `consistency`
- **Description:** `CODEX_THREAD_REUSE_ENABLED` is documented as supporting `review_autofix`, but both the editor and conflict resolver only log that OpenCode will use a fresh full-prompt path. The review-side enablement functions are consequently uncalled.
- **Recommended fix:** Either implement OpenCode continuation/session reuse for these paths or narrow the documented workflow scope and remove the inactive review/conflict helpers under the repository’s compatibility rules.

#### DEAD-001 — Three poller helpers are definition-only
- **File path and line range:** `scripts/orchestrate_poll_process.sh:11348-11356`, `12958-12977`, `13087-13097`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `get_last_validation_run_conclusion`, `read_standalone_state_json`, and `stall_recovery_action_is_terminal` have no callers in the repository.
- **Recommended fix:** Remove them after confirming no sourced external consumer relies on them, or add explicit runtime call sites and tests if they represent intended fallback behavior.

#### DEAD-002 — Label-repair evidence resolver is not wired into production
- **File path and line range:** `scripts/orchestrate_lib.py:2017-2089`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `resolve_label_repair_evidence` is implemented but never called. Documentation explicitly describes it as contract-defined/reserved while active reconciliation uses separate logic.
- **Recommended fix:** Wire it into `reconcile_managed_issue_labels` behind a guarded rollout, or move it to a clearly marked experimental module until activation.

#### SHELL-001 — CI suppresses all warning-level ShellCheck findings
- **File path and line range:** `.github/workflows/ci.yml:976-981`; `.github/workflows/mark-stable.yml:197-202`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** Both gates invoke `shellcheck --severity=error`, so warning-level diagnostics never fail CI. Current examples include SC2043 in `stage_workflow_support.sh` and SC2178/SC2128 in `codex_thread_reuse.sh`, whether intentional or not.
- **Recommended fix:** Raise enforcement to `warning` after adding targeted inline suppressions for intentional constructs. Keep suppressions adjacent to the relevant line with a reason.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, EXPR-001 |
| Medium | 7 | SEC-001, API-004, BATCH-001, BATCH-002, DUP-003, EXPR-002, CONSIST-001 |
| Low | 9 | API-001, API-002, API-003, DUP-001, DUP-002, DUP-004, DEAD-001, DEAD-002, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 4–7 | Medium |
| API call optimization | 4–5 | Medium |
| Code modularization | 8–10 | Large |
| Expression size reduction | 2–3 | Medium |
| Medium/Low fixes | 6–9 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-09-22)

### Safety Tag Legend

`SAFE_TO_MERGE` is implementation-ready; `NEEDS_VERIFICATION` requires the stated checks; `RISKY_SKIP` must not be auto-implemented because pagination, retry, race-defense, or poller semantics are involved.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — Clarify fetches issue comments twice when semantic caching is enabled
- **Safety tag:** `RISKY_SKIP`
- **File path and line ranges:** `.github/workflows/clarify.yml:457-488`, specifically calls at `468` and `472-473`.
- **Current call count:** 2 logical calls.
- **Proposed call count:** 1 logical paginated call.
- **Endpoint(s):** `GET /repos/{owner}/{repo}/issues/{issue_number}/comments`, ordered by creation ascending.
- **Evidence:**
  ```bash
  gh_retry gh api ".../comments?sort=created&direction=asc&per_page=50" > "${ISSUE_COMMENTS_FILE}"

  gh_retry gh api --paginate --slurp \
    ".../comments?sort=created&direction=asc&per_page=100"
  ```
  The second response strictly contains the first call’s first-50-comment snapshot.
- **Proposed fix:** In the `Fetch issue comments` step, capture the paginated response once. Write `add // [] | .[:50]` to `ISSUE_COMMENTS_FILE` and render `THREAD_HISTORY_FILE` from the complete array.
- **Safety rationale:** `RISKY_SKIP` is mandatory because the consolidation changes pagination and could collapse the current fatal first-page failure versus fail-open full-history failure semantics.
- **Downstream signal:** Do not auto-implement; manually test one-page, multi-page, and later-page-failure cases while preserving the fatal/fail-open split.

#### MERGE-002 — PR metadata and three comment surfaces can use the existing consolidated GraphQL helper
- **Safety tag:** `RISKY_SKIP`
- **File path and line ranges:** `scripts/review_collect_pr_metadata.sh:209-226`; existing batching pattern at `scripts/gh_helpers.sh:783-947`.
- **Current call count:** 3 logical calls normally; 4 when `REVIEW_BREAK_GLASS_ENABLED=true`.
- **Proposed call count:** 1 GraphQL call in the non-overflow case.
- **Endpoint(s):**
  - `GET /repos/{repo}/pulls/{pr}`
  - Paginated `GET /repos/{repo}/issues/{pr}/comments`
  - Optional paginated `GET /repos/{repo}/pulls/{pr}/reviews`
  - Paginated `GET /repos/{repo}/pulls/{pr}/comments`
  - Proposed GraphQL: `repository.pullRequest` with `comments`, `reviews`, and nested review comments.
- **Evidence:**
  ```bash
  gh_retry "${PR_PAYLOAD_FILE}" api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"
  gh_retry "${issue_comments_raw}" api --paginate ".../issues/${PR_NUMBER}/comments"
  gh_retry "${reviews_raw}" api --paginate ".../pulls/${PR_NUMBER}/reviews"
  gh_retry "${review_comments_raw}" api --paginate ".../pulls/${PR_NUMBER}/comments"
  ```
  `gh_pr_with_all_comments` already retrieves the overlapping metadata and comment connections in one query.
- **Proposed fix:** Extend `gh_pr_with_all_comments` with additive raw-artifact fields for:
  - PR number/body/head SHA and repository/base metadata.
  - Issue-comment IDs, timestamps, authors, and bodies.
  - Review IDs, state, timestamps, authors, and bodies.
  - Review-comment IDs, timestamps, path, line/original line, author, and body.

  Update `review_collect_pr_metadata.sh` to populate its existing artifact files from that response while retaining REST fallback on any `hasNextPage=true`.
- **Safety rationale:** `RISKY_SKIP` applies because every comment source currently implements pagination, and required PR/comment failures versus optional review failures have different error semantics.
- **Downstream signal:** Do not auto-implement; manually verify every artifact consumer and pagination fixture, including break-glass reviews, thread IDs, materiality comments, resolver retry state, and workflow-heal payloads.

#### MERGE-003 — E2E stability probe immediately re-fetches the same PR
- **Safety tag:** `RISKY_SKIP`
- **File path and line ranges:** `.github/workflows/test-and-mark-stable.yml:1143-1160` and `1181-1187`.
- **Current call count:** `2A + 1`, where `A` is the number of stability attempts; normally 3.
- **Proposed call count:** `2A`; normally 2.
- **Endpoint(s):** `GET /repos/{repo}/pulls/{pr_number}`.
- **Evidence:**
  ```bash
  HEAD_B=$(gh api ".../pulls/${PR_NUMBER}" --jq '.head.sha // ""')
  ...
  PR_META=$(gh api ".../pulls/${PR_NUMBER}")
  ```
- **Proposed fix:** Fetch the full PR JSON for `HEAD_B`, parse its head SHA for the stability comparison, and retain that JSON as the prospective `PR_META`.
- **Safety rationale:** `RISKY_SKIP` applies because the call is inside a retry/race-defense loop, and the final read intentionally detects closure or merge after the stability probe.
- **Downstream signal:** Do not auto-implement; manual review must prove that reusing the second probe cannot miss a merge or close occurring immediately after the loop.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — Partial orchestrator classification causes known issues to be fetched again
- **Safety tag:** `NEEDS_VERIFICATION`
- **File path and line ranges:** `.github/workflows/issue_pr_status.yml:192-206`, `358-426`, and `584-606`; REST calls at `406` and `595-601`.
- **Current call count:** After a failed batch, `N` classification reads plus up to `N` alert-step re-reads.
- **Proposed call count:** `N + U`, where `U` is only the number of unresolved classifications.
- **Endpoint(s):** `GET /repos/{owner}/{repo}/issues/{issue_number}`.
- **Evidence:**
  ```bash
  _orch_meta="$(gh_retry gh api "repos/${REPOSITORY}/issues/${_orch_num}" ...)"
  ```
  The step exports known tracking and managed issues, but not successfully classified standalone or unresolved issues. An incomplete classification therefore causes the Telegram step to scan every linked issue again.
- **Proposed fix:** Extend `export_orchestrator_issue_classification` to export an `ORCHESTRATOR_CLASSIFICATION_JSON` map keyed by issue number with `tracking`, `managed`, `standalone`, or `unknown`. Update `Send PR merged Telegram alert` to reuse known entries and call `_safe_gh_jq` only for `unknown`.
- **Safety rationale:** `NEEDS_VERIFICATION` applies because reuse crosses workflow steps and static reading cannot fully prove that classification inputs remain unchanged between them.
- **Downstream signal:** Verify a partial-failure fixture containing known standalone, known tracking, and unresolved managed issues; confirm only unresolved issues are re-read and alert suppression remains fail-closed.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: RISKY_SKIP — all sites are in `orchestrate_poll_process.sh`, including stall/reissue paths explicitly excluded from automatic consolidation.
- API-002: RISKY_SKIP — the duplicate reads are in the poller’s final-merge race-sensitive path.
- API-003: RISKY_SKIP — the source comment listing is paginated.
- API-004: RISKY_SKIP — changing calls inside retry wrappers requires manual validation of attempt and error-classification semantics.
- BATCH-001: RISKY_SKIP — PR-file reads are paginated and participate in merge-train ordering.
- BATCH-002: NEEDS_VERIFICATION — verify label pagination and per-issue fail-open behavior before replacing the fallback loop with GraphQL.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | REUSE-001, BATCH-002 |
| RISKY_SKIP | 8 | MERGE-001, MERGE-002, MERGE-003, API-001, API-002, API-003, API-004, BATCH-001 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
