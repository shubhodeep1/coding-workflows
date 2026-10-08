## Executive Summary

- **Review checkout is the most urgent failure to diagnose.** Three `review / codex-agent` runs failed at `Checkout PR head branch` after **1,648–2,077 seconds of total run time** (37694424988, 37693141615, 37703899777). Add stage and retry timing before changing checkout behavior. **Impact:** up to 91.5 minutes of failed-run time across these three runs is at stake, but the recoverable portion is unknown. **Confidence: high** in the pattern, low in its cause.
- **Check-failure triage repeatedly fails at posting.** Eight of 20 triage runs failed, all at `Post check-failure triage issue`; only one succeeded and 11 had other outcomes. Record whether failure occurred in body validation, `gh_retry`, or response validation. **Impact:** potentially fewer than eight failed triage runs per comparable window. **Confidence: high** in the failure point, low in the proposed cause.
- **CI is a substantial feedback bottleneck:** nine of 18 runs failed; CI p50/p95 duration was **1,130/1,317 seconds**. Failures span contract tests, unit tests, and generated-doc drift, rather than one demonstrated common defect. Log the first failing test and elapsed phase. **Impact:** earlier actionable failures; time saved is unmeasured. **Confidence: high.**
- **Cost and availability conclusions are coverage-limited.** Parsed logs from 124 of 1,000 runs show **48.35 million OpenRouter tokens**, but usage is unavailable for 10 of 75 calls, so aggregate `cache_hit_rate` is null. The supplied full-log directory is absent and the collector reports zero randomly sampled successes. Restore the archive and a successful-run baseline. **Impact:** enables defensible savings and root-cause estimates; no immediate token saving. **Confidence: high.**

## Speed Optimizations

Ranked by observed end-to-end exposure; **run duration is not step duration**, so none of these is a promised saving.

1. **Review checkout — critical path.** Runs 37694424988, 37693141615, and 37703899777 ended at `Checkout PR head branch` after 1,648, 1,765, and 2,077 seconds. The current workflow already retries `git fetch` up to four attempts; its failing operation is unknown without logs. Add one structured checkout summary with `stage`, `duration_ms`, `fetch_attempts`, `git_exit_class`, `head_match`, and `outcome`, plus timings around fetch and branch switch. Preserve the existing stale-head and workspace guards. **Estimated saving:** unknown, bounded above by the 91.5 minutes of combined failed-run elapsed time if these failures can be prevented. **Risk: low** for logging; do not alter retry policy yet.
2. **Poller wait versus work — possible critical path.** All 15 `orchestrate_poll` runs succeeded, but p50/p95 was **705/1,060 seconds**; run 37696614320 took 1,183 seconds. Log per-cycle `wait_ms`, API time, decision time, candidate count, and terminal reason. Change polling cadence only if wait—not required checks—dominates. **Estimated saving:** unquantifiable until that split exists. **Risk: low** for logging; cadence changes need a safety review.
3. **CI failure feedback — critical path for affected PRs.** Run 37697219359 failed in `Targeted file context contract tests` after 1,360 seconds; runs 37697182047 and 37706481621 failed in the review-pipeline plumbing step after 653 and 689 seconds. Emit the first failing test name, its elapsed time, and job-start delay; then move consistently fast-failing checks earlier without removing any gate. **Estimated saving:** up to the remaining CI time after the first failure, not measurable here. **Risk: low** if checks and required conclusions remain intact.
4. **Sync checkout — micro-optimization.** The `log_summary` for successful run 37709349520 attributes roughly **80 of 88 seconds** to checkout. Measure fetch and checkout subphases before testing a narrower checkout against required refs. **Estimated saving:** at most 80 seconds for that observed run, not an established per-run average. **Risk: medium** for changing checkout semantics.

## Cost Optimizations

1. **Prevent expensive failed review work before tuning models.** Failed review run 37706649851 reports **18,281,276 OpenRouter tokens across 24 calls** and four context-budget warnings; its recorded failure point is `Install project dependencies (best-effort)`. Instrument sandbox preparation and failure stage first—these counters do not establish which step consumed the tokens. **Potential saving:** up to that observed token volume *if* an equivalent full rerun is avoided; no such rerun is established. **Quality risk: none** from diagnostics.
2. **Reduce repeated uncached context, conditionally.** Across parsed runs, OpenRouter logged **9,061,174 prompt**, **3,228,070 cache-write**, and **35,463,043 cache-read tokens**. Keep invariant instructions at a stable prompt prefix and put run-specific diagnostics afterward; measure prefix/version and per-call usage before pruning context. A *hypothetical* 10% reduction in prompt-plus-write tokens equals **1.23 million tokens** in this observed sample, not a forecast. **Quality risk: medium** if relevant review context is removed; start with ordering and duplication only. Nine `CONTEXT_BUDGET_WARN` events across review runs 37706649851 (4), 37697219756 (3), and 37691518672 (2) make prompt growth worth measuring.
3. **Measure Semble’s net contribution, not just query size.** Review runs made **6 `SEMBLE_QUERY` calls**, logging **83,594 bytes**, 45 sources, and 744 static-duplicate bytes; six bootstraps took **220,808 ms** total, with none failed or unused. Log per target the context bytes retained versus equivalent static expansion and whether a bootstrap was reused. Present data do **not** show tokens saved by Semble. Implement run 37697468722 instead logged **11 runtime fallbacks and no successful query**; record their target and reason before changing its fail-open path. **Estimated saving:** unknown. **Quality risk: low** for measurement, potentially high for removing retrieved context.
4. **Treat model changes as a controlled experiment.** `issue_pr_status` run 37709348544 used `openai/gpt-6-sol` for an approximately 139-second activation-verification step; plan run 37708452606 configured that editor model with an `openai/gpt-5.6-sol` fallback. Log actual model, reasoning level, usage, and verification outcome, then compare a lower-cost setting only on a bounded non-merge-authorizing path. **Dollar saving:** unavailable—no comparable prices or per-model costs were supplied. **Quality risk: medium.**

Serena recorded **zero queries, tool calls, response bytes, fallbacks, and probes**. There is no evidence that it replaced downstream work—or added noisy response bytes—in this window.

## Reliability Improvements

1. **Triage posting: classify failures before remediation.** Eight failed `check_failure_triage` runs share the posting step, including 37696949470, 37699524692, and 37703547478. `check_failure_triage.sh` already checks for an open issue with the same fingerprint, and the workflow posts through `gh_retry`; neither proves why posting failed. Emit `CHECK_TRIAGE_POST` with `stage`, `attempts`, `error_class`, `issue_created`, and a non-sensitive fingerprint identifier; distinguish validation, authorization, rate-limit, and transport failures. Keep deduplication and the token-scoped posting step. **Expected impact:** diagnose the 8/20 failure cluster and target a safe fix; reduction is unknown. **Rollback:** remove the diagnostic emission without changing posting.
2. **Review checkout: retain fail-closed integrity checks.** The three checkout failures above may reflect fetch, head movement, or a local guard; no step logs distinguish them. Record which guard or git operation exited, its duration, and retry outcome. Retry only a *confirmed* transient transport class under the existing bounds; never retry past a head mismatch as though it were transient. **Expected impact:** reduce repeat failures if transport is confirmed; presently unknown. **Rollback:** revert any retry adjustment while retaining logging.
3. **CI: distinguish independent defects from a shared setup fault.** Nine of 18 CI runs failed; named points include generated-doc drift (37702585144), poll-process tests (37694929386), and review-pipeline plumbing (37697182047, 37706481621). Log test identifier, first exception class, fixture/setup phase, and elapsed time by job. Keep every required test. **Expected impact:** faster diagnosis and fewer speculative reruns, not a quantified failure-rate reduction. **Rollback:** diagnostics are additive.
4. **Separate MCP tests from production availability.** The aggregate has **43 `SEMBLE_FALLBACK` events: 32 classified as CI contract-test fallbacks and 11 runtime fallbacks**, all 11 attributed to successful implement run 37697468722. Contract-test fallbacks are not evidence of a broken deployment; 11 runtime fallbacks without a query warrant inspection but do not establish their cause. Emit per-target reason and attempted-query counts while preserving fail-open behavior. Six bootstraps had **zero failures**. Serena had **no probes**, so its availability is unverified rather than healthy. **Expected impact:** detect a masked rollout if one exists; no failure reduction can yet be estimated.
5. **Prompt pressure, not demonstrated policy pressure.** The nine review `CONTEXT_BUDGET_WARN` events indicate prompt-size risk. `BREAK_GLASS` was **0 in parsed telemetry**, so this sample does not show rubric/policy break-glass pressure. Log prompt size by stable and dynamic component, model context window, and warning threshold; do not weaken review requirements. **Rollback:** logging-only.

## AI Memory Health

No deep-dive step logs were accessible, and the supplied run summaries contain **no `retrieve` operation**. Retrieval hit rate, average `estimated_tokens` versus budget, `keyword_method` distribution, zero-record retrieves, disabled retrieves, and push-retry counts are therefore **not measurable**, not zero.

One summary does quote `finalize-task` on successful `issue_pr_status` run 37709348544: `ok=true`, `enabled=true`, `fail_open=true`, `reason=no_linked_issues`. The flag alone does not indicate a failed operation. Verify that review, implement, and poller steps emit and retain `retrieve` records with selected-count, estimated tokens, budget, method, miss reason, and fail-open status; add a per-run operation summary to the collector so inaccessible archives do not erase the health baseline.

## GH API Call Audit

**Actual GitHub API call counts, endpoint hotspots, retries, and rate-limit events were not supplied.** Do not infer call counts from items examined.

- Successful `cancel_on_pr_close` run 37707986412 spent roughly **48 of 59 seconds** in `Release merge-queued PRs (merge train)`, reporting **38 examined, 0 released**. `scripts/review_merge_train.sh` already fetches the open-PR list once, lazily fetches active runs once, and caches file lists per distinct PR; recommending those same caches again would be redundant. Its release loop can still request file lists for candidates and older PRs. Log candidate disposition, distinct file-list fetches, pages, cache hits, active-listing calls, and elapsed/API time. Only if those counts show avoidable lookups, reuse existing prefetched data or apply a safety-preserving prefilter. **Estimated call reduction: unknown**; 38 examined is not 38 API calls.
- Triage’s duplicate-issue lookup and issue creation are separate operations. For the eight posting failures, add endpoint-*class*, attempt, HTTP/error class, and wait duration to `gh_retry` diagnostics—not response bodies or credentials. This identifies whether retries or rate limits contributed; **call reduction is unknown**.
- Follow `CLAUDE.md` §15: extend existing reads first, retain cycle-local caches, batch per-item reads only where supported, and fail open on cache miss. A job-local `GH_API_AUDIT` summary of calls by endpoint template, retries, rate-limit waits, and cache hits would quantify both call-count and rate-limit-risk reductions without adding an API call. Existing `GH_PAT_BUDGET` snapshots can supplement it, but shared-token usage is not a per-job call count.

## Prompt Cache & Memory System

**Aggregate `cache_hit_rate`: unavailable.** `scripts/cost_audit.py` deliberately returns null when any OpenRouter usage call is unavailable; **10/75 calls (13.3%)** lack usage in this window. Successful review run 37706201852 has the one supplied valid rate, **60.5235% across 24 calls**. It is not a fleet-wide hit rate. Record usage availability and cache read/write per call and preserve a stable prompt prefix; measure whether dynamic PR metadata, warnings, or retrieved context precede that prefix before attributing misses to fragmentation.

The nine review context-budget warnings are a reason to cap or deduplicate *verified* low-value dynamic context, not to suppress memory or Semble blindly. Memory retrieval effectiveness is unknown because no `retrieve` lines were available. These logging and prefix-order changes may improve tokens and latency, but their magnitude—and any reliability gain from avoiding oversized prompts—requires a new measured baseline.

## Orchestrator Health

The 150 clarify, 146 plan, and 145 implement runs are dominated by **“other” outcomes** (146, 139, and 143 respectively); recent examples are skipped, but the summary does not classify every “other” outcome. A skipped trigger is not evidence of a stuck clarification or failed wave. Emit a structured dispatch decision with `workflow_family`, `issue_or_pr_key`, `decision`, and `reason`, then correlate stage transitions rather than treating run counts as completed work.

Poller runs succeeded **15/15** but had 705-second p50 duration; record wave, deferral, conflict-heal, queue, and terminal-state transitions alongside wait time. Successful heal-intake run 37708607843 reported **`lineage_cap gen=4 max=3`**, evidence of an escalation guard firing, not proof that healing succeeded. Track cap hits and subsequent human resolution. Listed failing and slow runs show `run_attempt=1` and `retries=0`; that field cannot rule out internal `gh_retry` or git-fetch attempts.

## Pipeline Flow Bottlenecks

| Stage | Window evidence | Bottleneck type and next diagnostic |
|---|---|---|
| Clarify → plan → implement | Mostly “other”; successful plan run 37708452606 took 631s and implement run 37697468722 took 2,612s | **Flow versus compute unknown:** log dispatch reason and issue-level stage entry/exit. |
| Review/autofix | 250 runs; p50/p95 **476/1,594s**; three checkout-stage failures | **Compute, setup, or retry unknown:** capture job queue delay and timed checkout/reviewer/editor phases before changing the critical path. |
| Validate/CI | 18 runs; p50/p95 **1,130/1,317s**; nine failures | **Compute/failure feedback:** identify first failing test and parallel-job completion gap; retain all gates. |
| Orchestrate/merge | Poller p50 **705s**; merge-train scan 38 examined/0 released in run 37707986412 | **Wait versus API/merge overhead unknown:** log poll sleep, API/cache work, and per-candidate release reason. |

The collector’s `duration_seconds` uses run start-to-update time, so **queueing cannot be separated from execution with the supplied aggregates**. Record created-to-start wait and job/step spans before ranking queue remedies against compute remedies.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix p95 **1,594s**, CI p50 **1,130s**, poller p50 **705s**.
- **Top failure modes:** eight triage posting failures; nine CI failures across distinct steps; three review checkout-stage failures.
- **Highest observed cost:** review/autofix accounts for all **48,349,281 logged OpenRouter tokens**; implement accounts for **1,324,165 of 1,338,353 logged Codex tokens**, concentrated in run 37697468722.
- **Top three actions:** **(1)** instrument triage posting and review checkout failure stages without changing guards; **(2)** restore the full-log artifact and successful-run sampling; **(3)** add timed poll/merge-train and per-call usage/cache summaries before optimizing API calls or prompts.

## Metrics Appendix

*Window: supplied October 7–8, 2026 telemetry for one repository. `insufficient_data=false` describes the assembled window; the stated full-log directory `/home/runner/work/_temp/workflow-log-output` was absent here, so no `summary.json` or `errors/`, `slow/`, or `recent/` step logs could be verified. `summary.errors=[]` is not evidence that failed-run logs were inspected. Rates below use all runs unless noted.*

| Scope | Runs | Success | Failure | Cancelled | Other | p50 / p95 duration |
|---|---:|---:|---:|---:|---:|---:|
| Repository total | 1,000 | 343 (34.3%) | 21 (2.1%) | 2 | 634 | 2 / 868s |
| Review/autofix | 250 | 244 | 4 (1.6%) | 2 | 0 | 476 / 1,594s |
| CI | 18 | 9 | 9 (50%) | 0 | 0 | 1,130 / 1,317s |
| Check-failure triage | 20 | 1 | 8 (40%) | 0 | 11 | 8.5 / 269.5s |
| Orchestrate poll | 15 | 15 | 0 | 0 | 0 | 705 / 1,060s |
| Clarify / plan / implement | 150 / 146 / 145 | 4 / 7 / 2 | 0 / 0 / 0 | 0 | 146 / 139 / 143 | 1 / 11s; 1 / 11s; 1 / 10s |

Among **364 success-or-failure conclusions**, success is **94.2%**; this excludes cancellations and “other” outcomes. The repository-wide 2-second p50 is dominated by short non-success-or-failure runs and is not an active-work p50.

| Logged cost and coverage metric | Observed value |
|---|---:|
| Runs with parsed log telemetry / configured success sample / randomly sampled successes | **124/1,000** / **7%** / **0** |
| OpenRouter calls / usage available / unavailable | **75 / 65 / 10** |
| OpenRouter prompt / completion / cache-write / cache-read tokens | **9,061,174 / 596,994 / 3,228,070 / 35,463,043** |
| OpenRouter total tokens | **48,349,281** |
| Codex calls / logged tokens, reported separately | **31 / 1,338,353** |
| `cache_hit_rate` aggregate / run 37706201852 | **null / 60.5235%** |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` / samples | **1,000 / 2,713,800 / 121** |
| `break_glass_count` / `context_budget_warn_count` | **0 / 9** |
| Semble queries / logged bytes / sources / static-duplicate bytes | **6 / 83,594 / 45 / 744** |
| Semble fallbacks: contract-test / runtime; bootstraps / failed / unused / total time | **32 / 11; 6 / 0 / 0 / 220,808 ms** |
| Serena queries / response bytes / tool calls / fallbacks | **0 / 0 / 0 / 0** |

| GH API and MCP availability | Observed value |
|---|---|
| GH API calls by workflow, endpoint, retry, rate-limit event | **Not collected in supplied context**; merge-train’s **38 examined** in run 37707986412 is an item count, not a call count. |
| Semble runtime fallback rate by target | **Not computable**: runtime fallback targets/reasons are unavailable, and 11 implement fallbacks have no corresponding successful query. |
| Serena per-tool response-byte breakdown | **No tool calls observed**; no breakdown available. |
| Per-target MCP availability: `probe_ok` / `probe_failed` / `probe_skipped` | **No target rows observed**; aggregate Serena **0 / 0 / 0** means availability was not measured. No Semble probe counts were supplied. |
| Other MCP servers observed | **None in supplied summaries**; absent full logs prevent ruling out uncollected prefixes. |

## Deep Audit — Workflows & Scripts (2026-10-08)

### Section 1: Bug & Correctness Sweep

Scope: all 54 `.github/workflows/*.yml` files and 181 `scripts/*.sh`/`scripts/*.py` files received an inventory-wide static scan, followed by targeted source review. All 107 shell scripts passed `bash -n`. Local Python 3.11 parsed 73 of 74 Python scripts; `scripts/workflow_retro.py:791-807` uses an f-string form accepted by the workflow’s pinned Python 3.12 (`.github/workflows/workflow-log-analysis.yml:84-86`), so the local parse failure is not filed as a defect. A YAML parser and `shellcheck` were unavailable; their checks were not claimed as completed. The existing report’s checkout and triage-posting diagnostics are not repeated as findings.

- **BUG-001 — High · `bug` · `scripts/label_helpers.sh:189-239`.** `set_issue_phase_label_resilient` reads labels, calculates a replacement, then PUTs the entire set. **Inference:** if `issue_pr_status.yml:449` adds `ai:merged` between that read and a review job’s `ai:ready-to-merge` update (`review_autofix.yml:6198-6203`), the stale PUT can erase the terminal label. The judge has a narrower, terminal-aware mutation pattern at `scripts/review_rb_judge.sh:922-987`. **Recommended fix:** adapt that add-then-remove-and-reconcile pattern in the shared helper; test a terminal-label insertion between its reads and writes.

- **BUG-002 — Medium · `bug` · `scripts/check_failure_triage.sh:348-359`.** A failed open-triage-issue listing is converted to `[]`. Processing then continues as though no matching fingerprint exists, although the deduplication check was not completed. **Recommended fix:** retain and check the listing’s exit status; defer issue creation on lookup failure instead of treating it as an empty result.

- **BUG-003 — Medium · `bug` · `.github/workflows/review_autofix.yml:5517-5549`.** The editor-summary comment is a POST through `gh_retry`, but this path has no pre-POST marker lookup. **Inference:** if GitHub creates the comment and the response is lost, a retry can post a duplicate. **[NEEDS VERIFICATION]** **Recommended fix:** give the comment a run/head-bound marker and reconcile an existing automation-authored comment before retrying an ambiguous POST.

- **BUG-004 — Medium · `bug` · `scripts/review_merge_train.sh:389-408`.** `_mt_upsert_comment` appends `|| true` to both PATCH and POST, so it reports success after either write fails. The release caller’s failure warning at `scripts/review_merge_train.sh:826-830` therefore cannot fire for those failures. **Recommended fix:** return the write status from `_mt_upsert_comment`; let the caller log its existing warning without changing the label-claim decision.

- **SEC-001 — High · `security` · `scripts/gh_helpers.sh:543-605`; `.github/workflows/review_autofix.yml:5517-5533`.** On failure, `gh_retry` prints unescaped `$*`. The editor-summary POST passes the generated comment body as an argument and leaves retry stderr visible. Thus a failed call can print the body into workflow logs; **inference:** body text containing sensitive material or workflow-command syntax could also be exposed or interpreted there. **[NEEDS VERIFICATION]** **Recommended fix:** log an endpoint class, attempt and error class—not command arguments or bodies—and escape any retained diagnostic text before emitting it.

### Section 2: GitHub API Call Redundancy Audit

Counts below are *logical calls on the stated path*, excluding pagination, transport retries and writes unless specified.

- **API-001 — Low · `api-redundancy` · `scripts/review_merge_train.sh:319-332,389-404`.** For an existing marker, `_mt_find_marker_comment` lists comments and reads each candidate’s body to filter it, but returns only ID/time; `_mt_upsert_comment` then GETs that same comment for its body. **Current → proposed:** two reads → one read per existing-marker upsert. **Recommended fix:** return a structured ID/time/body result from the existing comments listing and pass it to `_mt_upsert_comment`; retain its author check. Extend the existing lookup rather than adding a new batching helper.

- **API-002 — Medium · `api-redundancy` · `scripts/gh_helpers.sh:727-770`.** Unlike `gh_retry`’s permanent-error check (`scripts/gh_helpers.sh:575-581`), `gh_api_json_to_file` retries failed commands without classifying a 404 or 422. With the default attempt bound, one permanent failure can make five requests and incur a sleep even after the final attempt. **Current → proposed:** up to five → one call for a classified permanent failure. **Recommended fix:** reuse `_is_gh_permanent_failure` in this helper and sleep only when another attempt remains; retain rate-limit and transient backoff.

- **BATCH-001 — Medium · `api-batching` · `scripts/orchestrate_poll_process.sh:23-86`.** After one queued-issue listing, `replay_failed_reclarify_commands` makes a live issue GET and a paginated comments GET inside its loop. For **N** queued issues, its read path is one budget read, one listing, one identity read and **2N** per-issue reads. **Current → proposed:** `3 + 2N` → approximately `3 + N + ceil(N/25)` reads, before pagination/retries/fallbacks. **Recommended fix:** extend `_fetch_candidate_issue_details_graphql` (`scripts/orchestrate_poll_process.sh:15929-16062`) with the author/type and completeness fields this trust check requires; batch comments, retain the live issue guard, and use the existing REST comments path whenever history is incomplete or the batch fails. Snapshot freshness before replay needs testing. **[NEEDS VERIFICATION]**

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001 — Low · `duplication` · `.github/workflows/review_autofix.yml:2490-2495,6152-6171,6359-6379,7306-7315`.** Four late-stage blocks define near-identical `set_issue_phase_label_resilient` POST-only fallbacks. **Recommended fix:** make the existing `scripts/label_helpers.sh` own `set_issue_phase_label_resilient(issue_number, target_label, repo)` and stage a verified, durable copy before these steps; update the four callers while preserving their current missing-helper degradation until staging is guaranteed.

- **DUP-002 — Low · `duplication` · `scripts/review_rb_judge.sh:1274-1283`; `scripts/orchestrate_poll_process.sh:22353-22362`.** Both judge paths reproduce the same two REST comment fetches, sorting filters and context-JSON assembly when the preferred helpers are unavailable. **Recommended fix:** put `pr_comments_fallback_snapshot(repo, pr_number, preloaded_meta_json)` in a shared, verified `scripts/pr_checks_lib.sh`; update both callers and preserve the legacy fallback during rollout.

`internal-plan.yml:1-36` and `internal-implement.yml:1-34` share roughly 71% of noncomment lines, but their command gates and permissions differ. Consolidating those job-level gates would not be a safe mechanical deduplication.

### Section 4: Expression Size Limit Risk Assessment

Measurements are **dedented `run:` body characters including literal `${{ }}` placeholders**, not source indentation. Runtime substitutions can change the final length, so headroom is an estimate. Of 836 run blocks scanned, 234 contain interpolation; blocks without interpolation were excluded.

- **EXPR-001 — High · `expression-limit` · `.github/workflows/implement.yml:1005-1395`.** “Stage workflow support files” has three interpolations and an estimated **19,108-character** body: **1,892 characters** of nominal headroom below the stated 21,000-character limit. Its source-indented length is 22,792, which is *not* the measured body length. **[NEEDS VERIFICATION]** **Recommended fix:** extract the staging logic to a trusted script under `scripts/`, bootstrap it from the verified support ref, and pass expression values through step `env:`.

- **EXPR-002 — Medium · `expression-limit` · `.github/workflows/implement.yml:3512-3836`.** The preflight block has one interpolation and an estimated **15,922-character** body: **5,078 characters** of nominal headroom. **[NEEDS VERIFICATION]** **Recommended fix:** move its index and scope-preflight logic to a staged `scripts/implement_preflight_index.sh`, passing the repository value through `env:` and retaining the step’s guards.

- **DEBT-001 — Medium · `tech-debt` · `.github/workflows/review_autofix.yml:6971-7105`.** The workflow is **473,017 bytes**, only **6,983 bytes** below this repository’s 480,000-byte CI guard. The documented/tested hard limit here is **512,000 bytes**, stricter than the prompt’s 1 MB premise (`CLAUDE.md:1613-1627`; `tests/test_workflow_file_size_limit.py:24-27,39-53`). **Recommended fix:** before growing the workflow, extract multiple large inline bodies using its existing `review_autofix_step_*` script pattern until it has the documented 50,000-byte guard headroom.

No workflow exceeds 800 KB. The longest scanned `if:` line is 348 characters (`.github/workflows/clarify.yml:1563`), not near the expression limit.

### Section 5: Cross-Cutting Concerns

- **DEAD-001 — Low · `dead-code` · `scripts/orchestrate_poll_process.sh:14426-14436`.** `stall_recovery_action_is_terminal` has a definition but no call site found in the scoped repository search. An external source-based caller has not been ruled out. **[NEEDS VERIFICATION]** **Recommended fix:** confirm no supported caller sources it, then remove it and its obsolete tests or wire it into the decision path it was intended to classify.

- **CONSIST-001 — Medium · `consistency` · `scripts/activation_verify.sh:250-275,288-297`.** Fix-issue creation and verdict-comment posting use raw, one-attempt `gh api`, while the preceding lookup uses `gh_retry`. A transient write failure returns without a posted verdict. **Recommended fix:** add bounded, marker-aware write reconciliation: on an ambiguous failure, recheck the fix-issue or verdict marker before retrying, so resilience does not create duplicate writes.

- **SHELL-001 — Low · `shellcheck` · `scripts/propagate_consumer_secrets.sh:95-99`.** The `PROPAGATE_TARGETS` expansion deliberately suppresses SC2086, but also permits pathname glob expansion before target validation; a configured token containing a glob can become a different list of targets. **Recommended fix:** split the configured list with a controlled delimiter and `read -r`, without unquoted expansion; retain the registered-repository validation at `scripts/propagate_consumer_secrets.sh:122-135`.

No `TODO`, `FIXME` or `HACK` marker was found in the scoped workflow/script files. Missing `set -euo pipefail` was not treated as a defect in sourced helper libraries solely because they do not set caller shell options.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BUG-001, SEC-001, EXPR-001 |
| Medium | 8 | BUG-002, BUG-003, BUG-004, API-002, BATCH-001, EXPR-002, DEBT-001, CONSIST-001 |
| Low | 5 | API-001, DUP-001, DUP-002, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 3 existing: `scripts/label_helpers.sh`, `scripts/gh_helpers.sh`, `.github/workflows/review_autofix.yml` | Medium |
| API call optimization | 3 existing: `scripts/review_merge_train.sh`, `scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh` | Medium |
| Code modularization | 3 existing callers plus shared helper staging | Large |
| Expression size reduction | `.github/workflows/implement.yml` plus 2 proposed scripts | Large |
| Medium/Low fixes | 6 existing workflow/script files across triage, review, merge train, activation, poller and secret propagation | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-10-08)

### Safety Tag Legend

`SAFE_TO_MERGE` is ready to implement; `NEEDS_VERIFICATION` requires the stated checks first; `RISKY_SKIP` must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

- **MERGE-001 — NEEDS_VERIFICATION.** Calls: `.github/workflows/issue_pr_status.yml:258-263` and `.github/workflows/issue_pr_status.yml:367-378`. **Current → proposed:** two logical calls → one when additional issue lookups are needed; one → one otherwise. **Endpoint:** GraphQL `/graphql`. **Evidence:** the first query reads a PR’s `closingIssuesReferences` with issue bodies and labels; the second queries issue bodies and labels by alias for references not classified by the first response. **Proposed fix:** extend the first query with aliases for independently identifiable PR-title/body and branch issue numbers, then update `classify_orchestrator_issues_from_payload` and the later classification to consume the combined response. Retain the existing REST fallback for incomplete classifications. **Safety rationale:** `SAFE_TO_MERGE` is unproven because the later lookup occurs after classification and `ensure_label_exists`; issue state may also change between the two reads. **Downstream signal:** Verify classification parity for closing references, body-only references, and branch references; test partial GraphQL responses, the 50-item field bounds, and issue changes between the current read points before combining queries.

### Redundant Re-Fetch (REUSE-###)

- **REUSE-001 — RISKY_SKIP.** Calls: `.github/workflows/test-and-mark-stable.yml:1441-1449` and `.github/workflows/test-and-mark-stable.yml:1471-1477`. **Current → proposed:** three PR GETs on a first-attempt stable-head path → two, by retaining full metadata from the second head read. **Endpoint:** REST `GET /repos/{owner}/{repo}/pulls/{pull_number}`. **Evidence:** `HEAD_B` is fetched from that PR immediately before `PR_META` fetches the same PR; the former proves head stability, while the latter guards against a PR closing before bait injection. **Proposed fix:** only after manual review, consider capturing the full `HEAD_B` response and deriving both its SHA and guard fields from it. **Safety rationale:** the head reads sit inside a retry loop, and removing the subsequent live guard weakens its defense against a close or merge after the stable-head check. **Downstream signal:** Do not auto-implement; manually review the race window and demonstrate that the close/merge guard remains live immediately before injection.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: RISKY_SKIP — Its comments listing uses `--paginate`; review page completeness and marker selection manually before removing the subsequent GET.
- API-002: RISKY_SKIP — The proposed change affects a retry loop; review permanent-error classification and rate-limit backoff manually.
- BATCH-001: RISKY_SKIP — This poller replay path uses paginated comments and live race guards; batching requires manual trust-and-freshness review.

### Summary Counts

*Counts cover net-new findings above; Deep Audit cross-references are excluded.*

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 1 | MERGE-001 |
| RISKY_SKIP | 1 | REUSE-001 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
