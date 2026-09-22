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
