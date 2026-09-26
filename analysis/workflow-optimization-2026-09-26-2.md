## Executive Summary

- **CI is the dominant reliability problem.** In `shubhodeep1/coding-workflows`, 39 of 47 CI runs failed; 35 failed at `lint / Inventory parity`. Deep-dive logs for runs `36219304455`, `36220264510`, and `36220639422` repeat missing inventory entries for one workflow and three scripts. Update `docs/INVENTORY.md` and run parity before expensive tests. **Impact:** remove this confirmed failure signature and potentially save roughly 15–20 minutes on each affected failing run. **Confidence: high** for the sampled mismatch; **medium** for savings across all 35 runs.
- **Review’s resolver has a recurring fail-closed scope error.** Seven review/autofix failures, including `36221714214` and `36224174449`, end with `Resolver scope check failed closed (ValueError)`. Four inspected resolver logs show model-issued `git add` before that check. Preserve the safety gate, but log a safe reason code and defer staging to a trusted step. **Impact:** faster diagnosis and potentially fewer 475–705-second failed runs. **Confidence: high** for the pattern; **medium** that staging causes every instance.
- **A successful workflow can still lose the intended fix.** Review run `36221120963` took 2,405 seconds and logged `AUTOFIX_FAILURE_REASON: editor_changes_lost` with redispatch skipped for unavailable or exhausted budget. Add a separate logical-outcome signal; check for retained changes before further expensive work. **Impact:** prevent silent hand-back and avoid comparable wasted review cycles. **Confidence: high** for this run.
- **AI cost is concentrated, but coverage is narrow.** The enriched context reports 44,400,792 tokens across 45 usage-recorded calls and an 83.63% cache-hit rate; raw logs cover only 29 of 1,000 runs. Instrument phase-level usage before changing model tiers or reasoning. **Impact:** identify savings without weakening review quality. **Confidence: high** for measured totals, **low** for extrapolation.
- **API and queue bottlenecks cannot yet be ranked.** The supplied logs lack endpoint-level GitHub API call counts and job queue timestamps. Add aggregate diagnostics to existing helpers rather than new API calls. **Impact:** make batching and wait-time recommendations measurable. **Confidence: high** in the data gap.

## Speed Optimizations

1. **Critical path — fail CI parity early.** Run `tests/inventory_parity.py` immediately after checkout/setup, before the long `lint` test sequence in `.github/workflows/ci.yml`. Parity failed in approximately 0.2–0.4 seconds when reached in runs `36220639422` and `36219304455`, but those runs lasted 968 and 1,169 seconds. **Root cause:** a cheap deterministic check runs late. **Estimated saving:** approximately 15–20 minutes per run that would fail parity; implementation risk **low**, provided the full test suite remains unchanged for passing runs. Emit a machine-readable parity summary with missing-entry counts and elapsed time.
2. **Critical path — diagnose resolver scope failures before another cycle.** Review runs `36220574416`, `36221714214`, and `36222494360` failed after 475, 540, and 705 seconds. Inference: staging inside the model turn may change the index that `scripts/review_conflict_resolve.sh` snapshots. Log the scope-check reason code and attempt number; test a resolver-only instruction/tool boundary that leaves staging to the trusted post-check step. **Estimated saving:** up to the failed resolver run’s remaining cycle on prevented recurrences, not a guaranteed per-run speedup. Risk **medium**; never bypass the fail-closed check.
3. **Measure before retuning CI shards.** Successful CI run `36210775910` reached four-shard orchestrate-poll tests around 02:31:55 UTC and emitted shard output around 02:51:01 UTC. Log each shard’s start, end, test count, and slowest test, then rebalance only if skew is demonstrated. **Estimated saving:** unquantifiable until measured; risk **low** for logging, **medium** for changing allocation. Optimizing subsecond parity execution itself would be a micro-optimization, not a priority.

## Cost Optimizations

1. **Prevent expensive work with no retained outcome.** Review run `36221120963` used 26 calls and 29,398,890 reported tokens yet ended with `editor_changes_lost`. Make the trusted change-retention check and budget decision observable before any redispatch or further model invocation. **Potential saving:** up to a comparable run’s usage when a repeat can be safely avoided; **not** a measured recurring saving. Quality risk **low** if validation and human hand-back remain intact.
2. **Attribute usage before changing models or reasoning.** That review configured `openai/gpt-6-sol` for consolidation; run `36224183739` configured `openai/gpt-6-luna` for summarization. Resolver logs show high reasoning. Log calls, tokens, cache reads, duration, and outcome per phase/model; trial a cheaper tier only for measured low-risk summarization, not the conflict resolver. **Savings:** unknown without phase attribution or prices. Quality risk **medium** for tier changes; **none** for instrumentation.
3. **Constrain context only where useful.** The enriched totals include 7,175,139 prompt tokens, 36,643,384 cache-read tokens, zero recorded cache-write tokens, and zero observed context-budget warnings. Preserve stable prompt prefixes; log prefix identity and included-context bytes before trimming any content. **Savings:** unquantified; a 10% reduction in the measured *uncached prompt-token component* would be about 717,500 tokens if quality and call count held constant. Quality risk **medium** for trimming.
4. **Evaluate MCP output against work displaced.** Enriched telemetry reports 33 Semble query observations and 252,554 logged bytes. In the raw deep dives, job/step log duplication reduces 31 observations to 19 job-level queries: 10 `overflow` queries returned 65,740 bytes, seven `conflict-resolver-context` queries 42,604 bytes, and two `reviewer-context` queries 29,260 bytes. Semble’s effect on prompt expansion is **not measurable** without selected-versus-included byte counts. Serena has zero observed queries or tool calls and was configured off in review run `36221120963`; there is no evidence it displaced downstream work. Log selected, included, and discarded bytes by target before changing query volume. Savings and quality risk **unknown**.
5. **Avoidable reruns:** all 47 failed runs show attempt 1 and zero recorded retries, but separate dispatches and heal cycles can still repeat work. Correlate failure fingerprints across run IDs before estimating rerun cost; do not infer that recorded retries of zero mean no repeated AI spending.

## Reliability Improvements

1. **Inventory parity — documentation/validation category.** CI runs `36219304455`, `36220264510`, and `36220639422` each report 11 parity errors, including missing entries for `.github/workflows/claude-issue-intake.yml` and `scripts/claude_issue_handoff.sh`, `scripts/claude_issue_intake.sh`, and `scripts/claude_issue_route.py`. Reconcile `docs/INVENTORY.md` with those files and its references; keep parity enforced and move it early. **Expected impact:** eliminate this verified failure signature, which is the reported failure point in 35 CI runs. **Rollback:** restore test ordering if necessary, never disable parity.
2. **Resolver scope — safety/contract category.** Seven review failures identify `Run Codex resolver, validate, stage, commit`; inspected logs show `ValueError` without its safe classification. In runs `36221714214`, `36221717540`, `36222494360`, and `36224174449`, the model ran `git add` before the error. Inference: an index change may violate the captured merge-state invariant; other ValueError branches remain possible. Add `action`, `attempt`, and enumerated `reason_code` diagnostics without paths or untrusted exception text. Prevent model staging until after trusted verification. **Expected impact:** distinguish causes and reduce repeat failures once confirmed. **Fail-closed and rollback:** retain the current no-commit behavior; revert any staging change that fails scope-contract tests.
3. **Logical failure despite workflow success — outcome category.** Run `36221120963` logged `editor_changes_lost`; PR `4516` later hit `AUTOFIX_FINGERPRINT_CAP_TRIPPED count=3 max=3` and `mergeable_state=dirty` in successful gate run `36224680771`. Emit final `logical_outcome` and terminal reason alongside the Actions conclusion, then hand capped or dirty PRs back without another identical-head fix attempt. **Expected impact:** fewer ineffective cycles and clearer escalation. **Rollback:** keep existing caps and merge guards; do not raise them to obtain a green-looking run.
4. **MCP signals must be classified.** All 64 observed `SEMBLE_FALLBACK` events are `target=overflow`, `context=contract-test` in 16 CI deep-dive runs, four per run; runtime fallback count is zero. This is healthy contract-test fail-open evidence, **not** a 64-event production outage. No runtime Serena probe or fallback was observed, so availability is unknown, not proven healthy. Keep contract-test events separate from runtime alerts and emit availability when Serena is enabled. Observed `BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts are both zero in enriched telemetry; sampled coverage cannot rule out policy or prompt-size pressure elsewhere.

## AI Memory Health

Nine distinct reviewer `retrieve` operations appear in raw job logs; **9/9 selected records**, averaging **1,395.7 estimated tokens against a 1,400-token budget** (99.7%). Keyword method was `llm` in all nine (`plain`: 0; `none`: 0). Runs `36221122176`, `36221714214`, and `36222494360` include failure-event pushes requiring two attempts; none observed required more than two. No observed retrieve selected zero records, set `enabled:false`, or logged `fail_open:true`. The job logs also contain 18 `record-run-event` and two `record-candidate` operations; `finalize-task`, `promote`, `compact`, and processed-command operations were not observed in these deep dives.

**Recommendation:** log retrieval eligibility, selected count, budget utilization, and safe push-retry reason by role; verify telemetry emission in memory-maintenance and issue-status workflows, which lack raw logs here. **Expected impact:** expose low-value near-budget retrieval and intermittent push failures without changing memory contents or fail-open behavior.

## GH API Call Audit

**No call-count hotspot is verified.** Neither the supplied aggregate nor raw logs provide requests by endpoint, job, or step; echoed `gh api` shell code is not evidence that a request ran. No rate-limit event is established by the available telemetry.

The review sweep in run `36224671806` found four PR candidates and dispatched four; run `36222485629` found four, dispatched two, and skipped two active candidates. Instrument its existing enumeration/dispatch path and `scripts/gh_helpers.sh` with an end-of-step summary of normalized endpoint, method, call count, cache hits, retries, elapsed time, and minimum remaining limit—without IDs, URLs containing identifiers, bodies, or credentials. Reuse the already enumerated PR data for dispatch decisions. **Verified call reduction:** unknown. **Conditional bound:** if instrumentation finds four redundant per-PR lookups replaceable by one existing-pattern GraphQL batch, that slice would drop by three calls (75%), reducing rate-limit exposure. Follow `unattended_system_instructions.md` §14: extend an existing fetch or cycle-local cache first, batch only when needed, and fail open to the smallest safe legacy lookup.

## Prompt Cache & Memory System

The enriched cache-hit rate is **83.63%**, calculated from cache reads relative to prompt, cache-read, and cache-write input tokens on usage-covered calls—not across all 1,000 runs. Run `36221120963` reports 85.04% across 26 calls; run `36214926446` reports 79.39% across 13. Zero recorded cache-write tokens alongside substantial reads warrants checking what the provider emits, not assuming writes never occur. There is no direct evidence of cache fragmentation or cache fail-open events.

Keep invariant instructions and retrieved-memory framing ahead of run IDs, timestamps, and variable Semble output; log a non-reversible prefix identifier plus cache read/write and included-memory tokens per call. Compare hit rate and latency before and after any change. **Expected impact:** measurable protection of cached tokens and possibly lower latency; magnitude unknown. The nine observed retrieves nearly fill their budgets, so track selected-record utility rather than expanding the budget. Zero observed `CONTEXT_BUDGET_WARN` does not justify further prompt growth.

## Orchestrator Health

The poller completed **27/27** runs successfully, with p50 **339 seconds** and p95 **631.3 seconds**; its work-versus-wait breakdown is unavailable. Clarify, plan, and implement show many skipped runs—135/141, 129/134, and 129/134 respectively—so their short overall medians must not be read as active-work latency. Log eligibility and skip reason with wave/stage transitions, then measure clarification, deferral, and conflict-heal dwell time before changing cadence.

Two sampled heal-intake summaries, runs `36222541399` and `36224214722`, report `lineage_cap gen=4 max=3`; run `36224680771` reports a dirty PR and a capped autofix fingerprint. Preserve both caps. Emit a deduplicated terminal fingerprint, parent run, terminal reason, and hand-back state so teams can track distinct stalled items rather than counting repeated successful intake runs. **Expected impact:** less ambiguous escalation and fewer identical re-dispatches; no safe failure-rate estimate yet.

## Pipeline Flow Bottlenecks

| Stage or overhead | Evidence | Next diagnostic or fix |
|---|---|---|
| Clarify → plan → implement | Predominantly skipped runs; implement outlier `36213324504` lasted 2,105 seconds | Log eligible/active/skipped reason and phase timings; do not optimize the skip median. |
| CI validation | 35 late `Inventory parity` failures; CI p50 1,124 seconds | Fix inventory and move parity to early preflight. |
| Review/autofix and merge/conflict | Seven resolver-step failures; review p95 1,575.2 seconds; `36221120963` lost editor changes | Add scope reason codes, trusted staging, and logical-outcome reporting. |
| Poll/coordination | Poller p50 339 seconds, with no step-level wait attribution | Log cycle fetch, evaluation, dispatch, and sleep durations. |
| Queueing | All 1,000 run rows have `run_started_at == created_at`; job queue timestamps are absent | Collect job queued/started times before claiming a queue bottleneck. |

These are ordered by observed end-to-end impact. Compute spent before late CI failure and review cycles without a retained change outrank subsecond gate optimizations; API wait, queue time, and merge overhead remain unmeasured.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottleneck:** late CI parity, followed by long review/autofix and poll cycles. **Top failure modes:** 35 CI parity failure points; seven review resolver-step failures; one reported `editor_changes_lost` logical failure in successful run `36221120963`. **Highest measured cost driver:** review/autofix accounts for all 44,400,792 enriched reported tokens.

**Top three actions:** (1) reconcile inventory and fail CI early; (2) add safe resolver scope reason codes and test trusted post-check staging; (3) add logical-outcome, per-phase usage, and existing-helper API summaries. These retain current safety gates and require no new service.

## Metrics Appendix

The enriched `analysis_context.json` includes telemetry beyond the 29 raw-log runs in `summary.json`; the cohorts **overlap and must not be added together**. The 1,000-run outcome and duration window is shared.

| Window / family | Runs | Success | Failure | Cancelled | Skipped | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| Repository total | 1,000 | 350 (35.0%) | 47 (4.7%) | 5 | 598 | 7 / 1,110 s |
| CI | 47 | 7 | 39 (83.0%) | 1 | 0 | 1,124 / 2,660.8 s |
| Review/autofix | 147 | 134 | 8 (5.4%) | 4 | 1 | 37 / 1,575.2 s |
| Orchestrate poll | 27 | 27 | 0 | 0 | 0 | 339 / 631.3 s |

| Usage metric | Enriched context | Raw-log summary |
|---|---:|---:|
| Runs with parsed log telemetry | 124 | 29 |
| OpenRouter usage-recorded calls | 45 | 39 |
| Reported total / prompt / completion tokens | 44,400,792 / 7,175,139 / 585,559 | 39,246,390 / 6,339,289 / 535,989 |
| Cache read / recorded write tokens | 36,643,384 / 0 | 32,373,200 / 0 |
| `cache_hit_rate` | 83.6253% | 83.6247% |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 9,500 / 2,673,340; 120 samples | 1,110,000 / 2,706,960; 29 samples |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 | 0 / 0 |

The wall-clock cohorts differ markedly because of selection and enrichment; neither is a per-model latency percentile. Dollar totals, model-specific pricing, cache-write coverage, and active-only stage durations were not supplied.

| API and MCP signal | Observation |
|---|---|
| GitHub API calls by route; retries; rate-limit events | **Not measured**; call-count and batching savings cannot be verified. |
| Semble collector query observations / logged bytes | Enriched: 33 / 252,554; raw: 31 / 225,488. Raw job-level deduplication: **19 / 137,604**. |
| Semble job-level targets | `overflow`: 10 / 65,740 bytes; `conflict-resolver-context`: 7 / 42,604; `reviewer-context`: 2 / 29,260. |
| Semble fallbacks | 64 `overflow` contract-test events across 16 CI logs; **0 observed runtime fallbacks**. A fallback/query rate would misleadingly mix tests with review queries. |
| Serena | 0 queries, response bytes, tool calls, fallbacks, and query milliseconds observed; per-tool breakdown unavailable because no calls occurred. |
| Other MCP servers observed | None in runtime event lines; continue detecting unknown `*_QUERY`, `*_FALLBACK`, and `*_PROBE` prefixes. |

| MCP availability target | `probe_ok` | `probe_failed` | `probe_skipped` | Interpretation |
|---|---:|---:|---:|---|
| Serena — no target emitted | 0 | 0 | 0 | No runtime probe observed; availability unknown. |
| Semble — no probe emitted | 0 | 0 | 0 | Query and contract-test fallback evidence is not an availability probe. |

**Collection gaps:** raw logs cover 29/1,000 runs; the enriched context covers 124/1,000 with parsed telemetry. Failed CI logs directly verify three matching parity signatures, while the remaining parity count comes from collector failure points. Add safe structured diagnostics at the existing CI, resolver, API-helper, and orchestrator boundaries, then compare the next window against these baselines.

## Deep Audit — Workflows & Scripts (2026-09-26)

### Section 1: Bug & Correctness Sweep

The repository contains 51 workflow YAML files, 96 shell scripts, and 61 Python scripts. All workflow YAML and Python scripts parsed, and `bash -n` found no shell syntax failures. The existing report already covers late CI inventory parity, resolver scope failures, and lost editor changes; those findings are not repeated here.

- **BUG-001** — `.github/workflows/review_autofix_sweep.yml:159-205` — **High** — `bug`. **Description:** Each active-run status fetch ends in `|| true`. If one fetch fails, `jq` can still produce a successful partial snapshot; the sweep then treats absent runs as inactive and may dispatch another review at lines 254-297. This defeats the stated duplicate-dispatch preflight when the API is unavailable. [NEEDS VERIFICATION] **Recommended fix:** Record success separately for all three statuses on both workflows. If any snapshot is incomplete, skip dispatch for that tick and emit a bounded diagnostic; do not interpret missing data as zero active runs.

- **BUG-002** — `.github/workflows/review_autofix_sweep.yml:250-297` — **Medium** — `bug`. **Description:** The active-run map is keyed by PR `head_ref`, but fork PRs are dispatched without `--ref`; the workflow comments explicitly say those runs use the default branch. A subsequent sweep cannot reliably match that run to the fork’s head ref, allowing repeated dispatches or a false match when refs coincide. [NEEDS VERIFICATION] **Recommended fix:** Give fork dispatches a durable per-PR active-run identity and check it before dispatch, rather than applying the same head-branch lookup used for same-repository PRs.

- **BUG-003** — `scripts/label_helpers.sh:203-237` — **High** — `bug`. **Description:** `set_issue_phase_label_resilient` reads all labels, computes a replacement, then `PUT`s the entire set. A concurrent PR-close or escalation label added between GET and PUT can be erased. The judge’s non-terminal path instead adds its target, removes observed old phases, and re-reads terminal state (`scripts/review_rb_judge.sh:812-837`). [NEEDS VERIFICATION] **Recommended fix:** Apply that targeted-mutation and terminal-reconciliation pattern in the shared helper; preserve terminal-label precedence and test an interleaved PR-close update.

- **BUG-004** — `scripts/orchestrate_parse_and_post_answer.sh:280-303` — **Medium** — `bug`. **Description:** Posting `/answer` uses `gh_retry`, which can repeat a successful POST if its response is lost. The processed-command claim at lines 65-104 protects separate runs, but does not establish whether an earlier attempt *within this POST* was accepted. Duplicate command comments could trigger duplicate downstream work. [NEEDS VERIFICATION] **Recommended fix:** Make the POST single-attempt; on an ambiguous result, read comments for a stable answer marker before deciding whether to retry in a later run.

- **BUG-005** — `.github/workflows/workflow-log-analysis.yml:568-590` — **Medium** — `bug`. **Description:** A failed response to the marker comment PATCH immediately falls through to a new POST. If GitHub applied the PATCH but its response was lost, the tracker gains a second weekly comment. The analogous fan-out path has the same pattern (`scripts/workflow_retro_fanout.sh:299-324`). [NEEDS VERIFICATION] **Recommended fix:** Re-read the marked comment after an ambiguous PATCH result and POST only if its absence is confirmed; retain the existing week-scoped lookup.

- **SEC-001** — `scripts/gh_helpers.sh:439-461,488-491` — **High** — `security`. **Description:** `gh_retry` writes unredacted `$*` into failure annotations and writes raw final stderr to logs. Callers pass comment bodies as arguments, including `scripts/orchestrate_parse_and_post_answer.sh:289-290`. On failure this can disclose body contents; embedded newlines in `$*` also bypass the helper’s `_gh_actions_escape` treatment of stderr. No credential exposure was observed in this read-only audit. **Recommended fix:** Log a fixed operation/route identifier and attempt count, never full arguments or raw stderr; redact and escape a bounded error classification before emitting workflow annotations.

### Section 2: GitHub API Call Redundancy Audit

Counts below are **source-path estimates**, not measured request telemetry. Pagination and retries can increase them; proposed counts assume successful batch responses and retain per-item fallback on an unconfirmed result.

- **API-001** — `scripts/orchestrate_poll_process.sh:10431-10443` — **Low** — `api-redundancy`. **Description:** On a final-PR snapshot miss, the same `pulls/${final_pr}` endpoint is fetched once for `.state` and again for `.merged_at`. **Current → proposed:** two GETs → one GET, parsed for both fields. **Recommended fix:** Fetch the full JSON once through `_safe_gh_jq`, validate it, and extract both fields from that snapshot; retain the existing snapshot fast path.

- **API-002** — `scripts/review_merge_train.sh:255-290` — **Low** — `api-redundancy`. **Description:** `_mt_find_marker_comment_id` paginates comments but retains only IDs; `_mt_upsert_comment` then GETs the selected comment to compare its body. **Current → proposed:** one paginated read plus one GET → one paginated read on the existing-marker path. **Recommended fix:** Extend the lookup to return the selected ID *and* body into caller variables, preserving the ID-only interface for existing callers. Follow `_mt_pr_files_into`’s caller-variable pattern so a command substitution does not discard a cache.

- **API-003** — `scripts/gh_helpers.sh:678-725` — **Medium** — `api-redundancy`. **Description:** `curl_gh_api` sends every non-rate-limit failure through its retry branch, including permanent HTTP 404/422 responses, and sleeps even after the final attempt. **Current → proposed:** up to five calls and five sleeps → one call and no sleep for a classified permanent failure. **Recommended fix:** Classify terminal HTTP statuses before backoff, as `gh_retry` does with `_is_gh_permanent_failure`; keep exponential backoff for transient failures and reset-based waits for rate limits.

- **BATCH-001** — `scripts/orchestrate_poll_process.sh:5372-5402` — **Medium** — `api-batching`. **Description:** `security_pass_handle_failed_fix_issue` requests each blocker’s issue state inside a loop. **Current → proposed:** \(N\) issue GETs → `ceil(N / 25)` successful GraphQL calls. **Recommended fix:** Add a lightweight aliased issue-state prefetch using `_fetch_candidate_issue_details_graphql`’s 25-item batching structure (`scripts/orchestrate_poll_process.sh:14686-14756`), with the existing single-issue lookup on a batch miss. [NEEDS VERIFICATION]

- **BATCH-002** — `scripts/review_merge_train.sh:41-53,123-136,202-225` — **Medium** — `api-batching`. **Description:** The train can fetch changed-file pages for each of up to 20 older PRs. Its per-run file cache prevents *repeat* fetches, but not the initial \(N\)-PR fan-out. **Current → proposed:** up to 20 first-page PR-file requests, plus the open-PR list → up to two 10-alias GraphQL requests, plus the list, when each file connection is complete. **Recommended fix:** Extend the `_fetch_linked_pr_status_graphql` alias-building pattern for PR file paths; detect `pageInfo.hasNextPage` and fall back to `_mt_pr_files_into` for those PRs. Preserve the existing cache and fail-open gate. [NEEDS VERIFICATION]

- **BATCH-003** — `scripts/promote_main_cycle.sh:242-264` — **Medium** — `api-batching`. **Description:** `last_cycle_baseline_sha` searches up to ten issues, then GETs comments once per candidate. Its `per_page=100` request is not paginated, so a relevant comment beyond that page can also be missed. **Current → proposed:** one search plus up to ten comment GETs → one search plus one successful 10-alias GraphQL batch, with per-issue pagination fallback where the connection is incomplete. **Recommended fix:** Reuse `_fetch_candidate_issue_details_graphql`’s alias pattern, request the latest comments with author-association fields, preserve the trusted-author filter, and use paginated REST on incomplete results. [NEEDS VERIFICATION]

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — `scripts/review_run_reviewers.sh:1759-1768` — **Low** — `duplication`. **Description:** `append_semble_query_section` has the same body in `scripts/review_conflict_prepare.sh:610-619` and `scripts/review_apply_fixes.sh:993-1002`. **Recommended fix:** Put `append_semble_query_section(label, path, max_bytes=4096)` in the existing `scripts/semble_helpers.sh`; source its trusted staged copy in all three callers while preserving their current missing-file behavior.

- **DUP-002** — `scripts/render_prompt.sh:12-41,95-159` — **Low** — `duplication`. **Description:** `scripts/assemble_prompt.sh:12-41,51-93` repeats `resolve_prompt_file`, `resolve_render_prompt_py`, and `resolve_assembly_source_path`. Divergent path-resolution edits could select different prompt sources. **Recommended fix:** Move those functions into a shared `scripts/prompt_path_helpers.sh`, with signatures `resolve_prompt_file(path, script_dir)`, `resolve_render_prompt_py(script_dir)`, and `resolve_assembly_source_path(path)`; update both callers and their trusted-support staging.

- **DUP-003** — `.github/workflows/review_autofix.yml:5640-5676` — **Low** — `duplication`. **Description:** Similar fallback `set_issue_phase_label_resilient(issue_number, target_label, repo)` definitions recur at lines 2064-2070, 5843-5863, and 7013-7023, and in `.github/workflows/issue_pr_status.yml:320-335`. Their add-only behavior differs from `scripts/label_helpers.sh:187-237`. **Recommended fix:** Retain a trusted runtime copy of `label_helpers.sh` for late review steps and source the existing function; keep a single explicitly degraded add-only fallback for genuinely missing support. Do not remove the fallback until cleanup-path tests cover it.

The small `internal-implement.yml` and `internal-plan.yml` wrappers are structurally similar, but their command predicates and permissions differ (`.github/workflows/internal-implement.yml:7-32`; `.github/workflows/internal-plan.yml:7-34`). They are not recommended for consolidation.

### Section 4: Expression Size Limit Risk Assessment

The measurements use YAML-decoded `run:` values, excluding source-file indentation. Of 745 literal `run: |` blocks scanned, 225 contain `${{ }}` and 520 do not. Expansion of repository variables can change the exact runtime length. No decoded interpolated block exceeds 18,000 characters; no `if:` condition approaches 21,000 characters. No workflow exceeds 800 KB. The stricter repository contract is a **480,000-byte guard**, below its documented 512,000-byte loading limit.

- **EXPR-001** — `.github/workflows/implement.yml:984-1342` — **Medium** — `expression-limit`. **Description:** “Stage workflow support files” has three interpolations and a YAML-decoded body of approximately **16,985 characters**: about **4,015 characters** below 21,000 before dynamic substitution. This crosses the requested 15,000-character medium-risk threshold; its indented source representation is 20,326 characters and should not be mistaken for the decoded body. **Recommended fix:** Extract the body to a script obtained from the already verified `.codex-workflow-src` support checkout, passing its three expression values through step `env:`. Preserve the stage step’s outputs and trusted main-snapshot fallback. [NEEDS VERIFICATION]

The next-largest decoded interpolated block is `.github/workflows/implement.yml:3223-3529` at 14,392 characters, below the reporting threshold. `.github/workflows/review_autofix.yml` is 451,362 bytes—28,638 bytes below the repository’s 480,000-byte guard, addressed separately below.

### Section 5: Cross-Cutting Concerns

- **DEAD-001** — `scripts/orchestrate_poll_process.sh:9384-9459` — **Low** — `dead-code`. **Description:** `BRANCH_REBUILD_SKIP_REASON` and `BRANCH_REBUILD_LAST_REBUILD_AT` are assigned on branch-rebuild paths but have no reads in this script; Shellcheck also reports them unused. **Recommended fix:** Either include the reason and timestamp in a bounded branch-rebuild diagnostic consumed by callers, or remove the assignments after checking source-contract tests.

- **SHELL-001** — `scripts/review_enable_auto_merge.sh:22-26` — **Low** — `shellcheck`. **Description:** Shellcheck reports SC1007 for `CDPATH= cd` in `SCRIPT_DIR` resolution. The neighboring prompt wrappers use `CDPATH='' cd` (`scripts/render_prompt.sh:10`). **Recommended fix:** Use that explicit empty assignment here and retain the quoted directory substitution.

- **SHELL-002** — `.github/workflows/test-and-mark-stable.yml:5873-5885` — **Low** — `shellcheck`. **Description:** `for REPO in $REPOS` deliberately splits JSON-derived text but also permits pathname expansion; malformed entries in the committed consumer list can become unintended dispatch targets. The currently listed entries do not demonstrate that failure. [NEEDS VERIFICATION] **Recommended fix:** Read the JSON array into a Bash array without glob expansion, validate each `owner/repo` value, then iterate with quoted `"${array[@]}"`.

- **DEBT-001** — `.github/workflows/review_autofix.yml:1-14` — **Medium** — `tech-debt`. **Description:** The workflow is **451,362 bytes**, leaving only **28,638 bytes** before the repository’s 480,000-byte CI guard documented in `CLAUDE.md` §27. It is below both that guard and the requested 800 KB warning threshold, so this is growth risk, not a present size failure. **Recommended fix:** On the next substantial addition, move the largest remaining inline step into a trusted `scripts/review_autofix_step_*.sh` file and register it through `scripts/stage_workflow_support.sh:53` and the existing step-script contract tests; do not raise the guard.

No `TODO`, `FIXME`, or `HACK` markers were found in the scoped workflow and script files. The shell-script pass produced no syntax failures or observed SC2086/SC2046/SC2006/SC2015 warnings; it did report the warnings described above.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BUG-001, BUG-003, SEC-001 |
| Medium | 9 | BUG-002, BUG-004, BUG-005, API-003, BATCH-001, BATCH-002, BATCH-003, EXPR-001, DEBT-001 |
| Low | 8 | API-001, API-002, DUP-001, DUP-002, DUP-003, DEAD-001, SHELL-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `review_autofix_sweep.yml`, `label_helpers.sh`, `gh_helpers.sh`; regression tests | Medium |
| API call optimization | `orchestrate_poll_process.sh`, `review_merge_train.sh`, `promote_main_cycle.sh`, `gh_helpers.sh`; batching tests | Large |
| Code modularization | Three Semble callers, two prompt wrappers, `review_autofix.yml`, shared helper/staging files; tests | Medium |
| Expression size reduction | `implement.yml`, a trusted extracted script, support-script registry; tests | Medium |
| Medium/Low fixes | Answer/retro comment paths, sweep fork path, auto-merge helper, poller diagnostics, release workflow; tests | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-09-26)

### Safety Tag Legend

`SAFE_TO_MERGE` is verified for direct implementation; `NEEDS_VERIFICATION` requires the stated checks; `RISKY_SKIP` must not be auto-implemented because a protected API behavior may change. Counts below are source-path estimates, not measured requests.

### Consolidation Candidates (MERGE-###)

- **MERGE-001 — RISKY_SKIP.** **Calls:** `.github/workflows/clarify.yml:581` and `.github/workflows/clarify.yml:583-598`. **Current → proposed:** two GETs → one on the successful, semantic-cache-enabled path; retain the first GET as fallback if the full-history fetch fails. **Endpoint:** `GET /repos/{owner}/{repo}/issues/{issue_number}/comments`. **Evidence:** both reads request ascending, creation-sorted comments; the first retains 50 for the prompt, while the second paginates at 100 per page for thread history. **Proposed fix:** in “Fetch issue comments,” derive the bounded `ISSUE_COMMENTS_FILE` and `THREAD_HISTORY_FILE` from one validated full-history response when semantic caching is enabled; preserve the cache-bypass sentinel and legacy prompt fetch on failure. **Safety rationale:** the second call uses `--paginate`, and replacing the first read can change page-boundary and failure behavior. **Downstream signal:** Do not auto-implement; manually test 0, 50, 51, and more than 100 comments, a failed later page, and concurrent new comments before considering this change.

- **MERGE-002 — RISKY_SKIP.** **Calls:** `scripts/orchestrate_poll_process.sh:13935` and `scripts/orchestrate_poll_process.sh:13936`, in `execute_stall_recovery_action`. **Current → proposed:** two GETs → one on the successful `close_and_reissue` path. **Endpoint:** `GET /repos/{owner}/{repo}/issues/{issue_num}`. **Evidence:** consecutive reads select `.title` and `.body` from the same issue before constructing the replacement. **Proposed fix:** capture one issue JSON snapshot in `execute_stall_recovery_action` and extract both fields, retaining targeted legacy lookups if the snapshot cannot be confirmed. **Safety rationale:** this is an explicit stall-recovery path, and the current independent reads have different partial-failure behavior. **Downstream signal:** Do not auto-implement; manually review concurrent issue edits and test failure of either field lookup before changing recovery behavior.

### Redundant Re-Fetch (REUSE-###)

No findings.

### Dead Calls (DEAD-API-###)

No findings. The inspected discarded GET bodies served existence, freshness, or permission decisions; they were not proven dead.

### Cross-References to Deep Audit Section

- API-001: RISKY_SKIP — The calls are inside `orchestrate_poll_process.sh`; review final-merge race and independent-failure semantics before combining them.
- API-002: RISKY_SKIP — The marker lookup paginates; review page completeness and selected-comment behavior manually.
- API-003: RISKY_SKIP — The proposed change alters a retry/backoff path, including its failure diagnostics.
- BATCH-001: RISKY_SKIP — Blocker-state reads are in the poller; preserve unknown-state deferral and per-item fallback.
- BATCH-002: RISKY_SKIP — The existing changed-file fetch paginates and has a per-run cache contract.
- BATCH-003: NEEDS_VERIFICATION — Verify trusted-author filtering, comment ordering, and incomplete-connection fallback before substituting GraphQL.

### Summary Counts

Counts include the two net-new findings and six Section 2 cross-references.

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 1 | BATCH-003 |
| RISKY_SKIP | 7 | MERGE-001, MERGE-002, API-001, API-002, API-003, BATCH-001, BATCH-002 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
