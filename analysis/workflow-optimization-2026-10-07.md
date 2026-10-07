## Executive Summary

- **Review/autofix is the dominant failure and latency concern:** 77 of 343 runs failed (22.45%); duration was p50 504s and p95 1,763s. Many listed failures surfaced in `review / codex-agent / Run Codex resolver, validate, stage, commit`, including runs 37534855733 and 37544217871. **Impact:** avoiding one genuinely duplicate review could save roughly 8 minutes at the family median. **Confidence:** high in the pattern, low in any single root cause.
- **Checkout failures need their own diagnosis:** runs 37548737244, 37542063226, and 37544818160 failed at `Checkout PR head branch` after 1,766–2,238s. The step already retries fetches; more retries are not the first fix. Log the failing guard or Git operation and its elapsed time. **Impact:** potentially substantial per affected run, not yet measurable. **Confidence:** high.
- **Measured AI usage is concentrated, not fleet-wide:** three logged successful review runs account for all 37 measured OpenRouter calls and 25,354,890 reported total tokens; run 37540215406 alone reports 16,000,336. Gate duplicate work by PR **and head SHA** before changing model or reasoning settings. **Impact:** high if identical-head reruns exist; their number is unknown. **Confidence:** medium.
- **Heal chains are reaching a deliberate stop:** intake summaries for runs 37550458207, 37550558366, 37553282175, and 37553711401 report `lineage_cap gen=4 max=3`. Preserve the cap; make stopped lineages and their preceding failure classes easier to correlate. **Impact:** fewer futile follow-on actions, amount unmeasured. **Confidence:** high.
- **Root-cause coverage is materially incomplete despite `insufficient_data=false`:** only 120/1,000 runs have parsed logs; three implement-run archives returned 404, and the advertised full-log directory is unavailable here. GH API call counts, queue time, resolver failure classes, and memory retrieval outcomes cannot be audited reliably from this input. **Impact:** diagnostic rather than quantified runtime savings. **Confidence:** high.

## Speed Optimizations

1. **Critical path — avoid proven same-head repeat reviews.** PR #6209 has failed resolver runs 37534855733 (1,223s), 37544217871 (766s), and 37549838996 (601s); their head SHAs are not supplied. **Inference:** some repeated work may be avoidable. Record `pr`, `head_sha`, review round, dispatch reason, and prior completed/in-flight run at dispatch; suppress only an equivalent run for the *same SHA and review state*. **Estimated saving:** approximately 504s per safely avoided median review run; actual eligible count unknown. **Risk:** medium—never suppress a new head or required review round.
2. **Critical path — distinguish checkout wait from checkout failure.** The three `Checkout PR head branch` failures above took 29–37 minutes end to end. In `.github/workflows/review_autofix.yml`, emit a bounded result for metadata/head checks, workspace checks, ref-health repair, each fetch attempt, and checkout/reset, with elapsed milliseconds and exit class. If evidence identifies a safe read-only check that can run earlier, move *that check*, not the security guards. **Estimated saving:** unknown; at most the work preceding a failure that an earlier check can actually detect. **Risk:** low for logging, medium for reordering.
3. **Critical path — classify resolver retries before tuning them.** Run 37549838996 failed in the resolver step after 601s; the local resolver already has per-attempt limits and no-progress handling. Add one compact per-attempt outcome containing elapsed time, exit class, marker count, fingerprint count, verification tier, and whether restoration succeeded; retain existing fail-closed scope checks. **Estimated saving:** unmeasured until retry classes are counted. **Risk:** low for logging.
4. **Smaller, conditional wins:** `orchestrate_poll` has p50 335.5s/p95 997.1s across 14 runs; `cancel_on_pr_close` run 37553962737 spent about 49 of 62s in merge-train release. Emit phase timings and queue-versus-execution timings before changing polling or merge ordering. **Estimated saving:** unknown; these are lower-confidence targets than review repetition. **Risk:** low.

## Cost Optimizations

1. **First, establish whether expensive review repeats are equivalent.** Runs 37535641362 and 37540215406 both name PR #6475 but report 3,851,553 and 16,000,336 total tokens respectively; the input does **not** establish identical heads or redundant work. Add per-call usage and a non-content prompt/context fingerprint alongside PR head SHA and review round. Suppress only proven duplicates. **Estimated saving:** one avoided run would avoid that run’s measured usage, but no avoidable run is established. **Quality risk:** high if deduplication ignores changed commits or review state.
2. **Improve cache consistency without removing evidence.** Reported aggregate `cache_hit_rate` is 71.97% across the measured calls, but PR #6475 runs show 46.27% (37535641362) versus 80.76% (37540215406). **Inference:** prompt variance may contribute; different tasks or context could also explain it. Keep reusable instructions first, put run-specific data later, and log stable-prefix fingerprint and byte count per call. **Estimated token/dollar saving:** not defensibly quantifiable from run totals; compare matched calls after instrumentation. **Quality risk:** low if content is preserved.
3. **Measure context tools before trimming them.** Review/autofix logged 75 `SEMBLE_QUERY` events and 414,181 result bytes—about 5,522 bytes/query—including 12 queries/74,848 bytes in failed run 37549838996. Target and step breakdowns are absent from the supplied aggregate. Log `target`, result bytes, and whether returned context was used; reuse identical queries within one unchanged head only. It is **not established** that Semble reduced prompt expansion. Serena has zero logged queries, response bytes, and tool calls, so replacement efficiency cannot be assessed. **Estimated saving:** unknown. **Quality risk:** medium if useful retrieval is removed.
4. **Defer model and reasoning changes.** Intake summaries identify `openai/gpt-6-sol`, while the resolver is configured for `high` reasoning; no per-role prices, outcome comparison, or intake usage is supplied. Keep existing settings, then compare per-role calls, latency, tokens, and successful validations before a limited lower-cost trial. **Estimated dollar saving:** unavailable. **Quality risk:** potentially high for conflict resolution.

## Reliability Improvements

1. **Resolver failures — classify before retrying differently.** The review family has 77 failures/343 runs, with many listed at the resolver step, but no accessible full step logs establish whether they were model exits, timeouts, sandbox rejection, scope drift, or validation failures. Emit a single final `failure_class` plus the existing attempt outcomes; count each class by PR/head SHA. **Expected impact:** enables a targeted reduction in repeat failures; no numeric reduction supportable yet. **Rollback:** remove instrumentation without changing retry or fail-closed behavior.
2. **Checkout failures — preserve guards and expose their decision.** Runs 37548737244, 37542063226, and 37544818160 share the failing step. Distinguish stale head, unavailable workspace, ref-health failure, exhausted transient fetch, and checkout/reset error; record attempts and elapsed time without logging credentials or remote URLs. **Expected impact:** faster isolation and fewer misdirected reruns; rate reduction unknown. **Rollback:** retain the existing checkout and four-attempt fetch behavior.
3. **Triage and CI — report the first actionable failure.** Four of ten `check_failure_triage` runs failed, including 37544674251 and 37547343301 at `Collect check-failure context`. Four of eight CI runs failed; runs 37542719844 and 37545637099 identify different contract-test steps. Emit a bounded triage collection outcome (`source`, response class, retry count, ready decision) and CI first-failing test/gate with expected versus observed counts. **Expected impact:** shorter repair cycles, not a measured failure-rate change. **Fail-open:** preserve existing safety and escalation decisions; do not turn absent check evidence into a successful diagnosis or bypass CI.
4. **Separate missing diagnostics from failed execution.** Implement runs 37552973233, 37549847405, and 37548075591 have 0s computed duration and 404 log archives; neither value identifies their execution cause. `scripts/collect_workflow_logs.py` already distinguishes `log_download_status`, `jobs_fetch_status`, and `diagnostic_failure_reason`, but `scripts/analyze_workflow_logs.py` does not carry these into normalized run views. Pass bounded status fields through and report missing duration as unknown where timestamps are absent. **Expected impact:** fewer false root-cause and speed conclusions. **Rollback:** additive fields only.
5. **MCP pressure:** the parsed sample reports zero `BREAK_GLASS` and zero `CONTEXT_BUDGET_WARN`; this does not establish their absence in unparsed runs. Eight Semble fallbacks belong to CI contract tests (parsed CI run 37551498478), with **zero observed runtime fallbacks**. Poller run 37552513928 nevertheless reports Semble enabled but binary/index unavailable: availability gating can prevent a fallback event, so zero is not proof of a healthy rollout. Log availability reason and skipped-query count separately from runtime fallback. Serena has no logged probe or fallback; do not infer availability. **Expected impact:** expose masked rollout failures without changing fail-open behavior.

## AI Memory Health

The available summaries show successful `finalize-task` events with `did_push: true` and `final_state: merged` for issue #6441 in issue-status run 37552972501 and issue #5504 in run 37551497722. No accessible deep-dive `AI_MEMORY_TELEMETRY` logs show `retrieve`, `record-candidate`, `promote`, `compact`, or processed-command outcomes. Consequently **retrieve hit rate, average `estimated_tokens` versus budget, `keyword_method` distribution, zero-record retrieves, `fail_open: true`, `enabled: false`, and push retry counts are all unknown**, not zero. Preserve bounded operation telemetry in the exported excerpts and add a per-run operation/count rollup; verify emission on a review run before judging memory effectiveness. Expected impact is diagnostic coverage, with no supportable token saving yet.

## GH API Call Audit

**No measured per-endpoint, job, or step call counts or rate-limit events were supplied.** Existing `GH_PAT_BUDGET` start/end lines in review/autofix and poller workflows are shared-quota deltas, not attributable request counts; do not present them as such.

| Candidate | Evidence and smallest safe change | Potential call reduction |
| --- | --- | --- |
| Merge-train active-run reads | `scripts/review_merge_train.sh` scans five statuses twice, with pagination, to handle shifting listings. Log pages, unique run IDs, second-pass additions, elapsed time, and rate-limit/retry events before changing this safety mechanism. Run 37553962737 spent ~49s in merge-train release, but its API share is unknown. | None claimed; if equivalence is proven, omitting a redundant pass would remove up to five **base** reads per invocation, plus its pages. |
| Per-item PR/issue lookups | Before adding any lookup in review, triage, or heal loops, audit the existing PR payload and cycle-local caches under `CLAUDE.md` §15. Extend a cached result or batch N independent reads using the repository’s aliased-GraphQL pattern; retain fail-open behavior on prefetch miss. No observed per-item hotspot is quantified here. | Conditional: N reads → roughly `ceil(N/batch_size)` reads; actual N unknown. |
| Log-archive collection | Three specified implement-run log requests returned 404. Retain `missing_archive` distinctly, and avoid retrying a confirmed 404 repeatedly *within the same collection*, while allowing a later collection to recheck. | Up to one call per avoided repeat request; repeat count unknown. |

Instrument existing `gh_retry` call sites with sanitized endpoint class, workflow/job/step, attempt, HTTP class, elapsed time, and rate-limit indication—**without adding an API request**. Cross-check resulting hotspots against `CLAUDE.md` §15’s reuse, batching, cycle-local cache, and fail-open rules.

## Prompt Cache & Memory System

Measured review usage comprises 5,716,508 prompt, 323,919 completion, 18,015,842 cache-read, and 1,299,959 cache-write tokens across 37 calls. The reported 71.97% `cache_hit_rate` is the collector’s cache-read share of prompt-plus-cache input, **not** the percentage of requests that hit a cache. PR #6475’s two measured rates (46.27% and 80.76%) warrant logging stable-prefix identity, dynamic-section bytes, model/role, and per-call reads/writes. Compare matched calls before attributing the difference to fragmentation. Zero sampled `CONTEXT_BUDGET_WARN` events provides no assurance about unsampled prompt growth. Memory retrieval effectiveness remains unknown as described above. Expected token, latency, and reliability improvements require that matched-call baseline; keep prompt content and fail-open semantics unchanged meanwhile.

## Orchestrator Health

The poller completed 14/14 listed runs, but its p95 duration was 997.1s; run 37552513928 completed in 288s while reporting Semble enabled and unavailable. Heal intake completed successfully on runs 37550458207 and 37553711401 while escalating at generation four against a maximum of three. These are **terminal/escalated outcomes, not evidence that the cap malfunctioned**. Log transition counts by lineage root, generation, action (`duplicate`, `escalate`, `budget_exhausted`, `open`), prior issue, and dispatch result; alert on repeated work against a stopped lineage. Track poll phase times, deferred reasons, conflict-heal attempt classes, and terminal age. Clarify and plan each have 119 “other” outcomes, and implement has 118/126; recent examples are `skipped` (clarify 37553592144, plan 37553592137, implement 37553592120). Emit a bounded `skip_reason` and triggering event so intended no-ops are not mistaken for stuck waves. No supplied transition history establishes clarification-loop or wave-progression rates.

## Pipeline Flow Bottlenecks

| Segment | Observed constraint | Next diagnostic/fix |
| --- | --- | --- |
| Clarify → plan → implement | Mostly skipped/“other”; implement also has seven failures, including three 0s rows with missing archives. | Record trigger, skip reason, job-fetch status, and valid execution timestamps; do not optimize their 1s medians as compute time. |
| Review → autofix | 343 runs; p50 504s/p95 1,763s; resolver and checkout failures recur. | Prioritize same-head work identification and phase/attempt failure classes. |
| Validate → merge/conflict heal | CI 4/8 failures; review summaries for runs 37552972542 and 37551497836 warn that no standalone validation was dispatched for merged PRs #6545 and #6137. The warning alone does **not** prove a dispatch API failure or unmet validation requirement. | Log linked-issue eligibility, dispatch attempts, and final reason; retain validation requirements. |
| Orchestrate → cleanup | Poller p95 997.1s; merge-train release ~49s in close-cleanup run 37553962737. | Split listing/API wait, compute, retry, and merge/conflict timings. |

Queueing cannot be separated from compute here: the supplied context lacks job start/wait metrics. `run_attempt=1` and `retries=0` on listed failures describe GitHub run attempts, **not** internal resolver or API retries. Collect both separately before assigning end-to-end savings.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix p95 1,763s (343 runs); CI p50 1,093s (eight runs); poller p95 997s (14 runs). Queue share is unknown.
- **Top failure modes:** 77 review failures, including resolver-step runs 37534855733/37544217871 and three long checkout-step failures; four triage collection failures and four CI failures. Three implement failure archives returned 404, leaving their causes unknown.
- **Highest measured cost:** 25,354,890 reported tokens in three review runs, led by PR #6475 run 37540215406 at 16,000,336; this is **sample usage**, not the repository’s total spend.
- **Top three actions:** (1) emit structured checkout/resolver outcomes and retain collector diagnostic statuses in analysis; (2) correlate review dispatches by PR, head SHA, and round before suppressing repeats; (3) aggregate existing quota lines and instrument sanitized API call counts and phase timings without new requests.

## Metrics Appendix

*Scope: supplied 1,000-run listing, predominantly October 6–7, 2026 UTC; success log sampling was configured at 7%. Parsed-log coverage is 120/1,000. The advertised `/home/runner/work/_temp/workflow-log-output` directory, including `summary.json` and untruncated `errors/`, `slow/`, and `recent/` logs, was unavailable for this audit.*

| Workflow family | Runs | Success | Failure | Cancelled | Other | Failure rate, all runs | Duration p50 / p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **All: shubhodeep1/coding-workflows** | 1,000 | 370 | 92 | 31 | 507 | 9.20% | 10 / 910s |
| Review/autofix | 343 | 266 | 77 | 0 | 0 | 22.45% | 504 / 1,762.7s |
| CI | 8 | 4 | 4 | 0 | 0 | 50.00% | 1,093 / 1,154.5s |
| Check-failure triage | 10 | 0 | 4 | 0 | 6 | 40.00% | 7.5 / 58.7s |
| Implement | 126 | 1 | 7 | 0 | 118 | 5.56% | 1 / 10s |
| Orchestrate poll | 14 | 14 | 0 | 0 | 0 | 0% | 335.5 / 997.1s |
| Failure-heal intake | 89 | 48 | 0 | 31 | 10 | 0% | 65 / 142.8s |

| Parsed-log metric | Overall | Qualification |
| --- | ---: | --- |
| OpenRouter calls; reported total tokens | 37; 25,354,890 | All measured usage is in three successful review runs; not a fleet total. |
| Prompt; completion tokens | 5,716,508; 323,919 | Same measured calls. |
| Cache read; cache write tokens | 18,015,842; 1,299,959 | Reported `cache_hit_rate`: **71.97%**. |
| `wall_clock_p50_ms`; `wall_clock_p99_ms` | 8,000; 3,212,440 | 119 sampled run-wall-clock values, not model-call latency; review subset: 620,500 / 3,274,000ms across 26 samples. |
| `break_glass_count`; `context_budget_warn_count` | 0; 0 | Parsed logs only. |
| GH API calls; rate-limit events | Not supplied; not supplied | Existing job-end quota deltas cannot supply endpoint counts. Three log-archive GETs returned 404. |

| MCP telemetry observed | Queries | Logged bytes | Fallbacks | Probe `ok` / `failed` / `skipped` | Breakdown and limitation |
| --- | ---: | ---: | ---: | ---: | --- |
| Semble, review/autofix | 75 | 414,181 result bytes | 0 runtime | Not reported | Targets and per-step rates unavailable; failed run 37549838996 logged 12 queries/74,848 bytes. |
| Semble, CI contract tests | 0 | 0 | 8 test fallbacks | Not reported | One parsed CI run, 37551498478; exclude from runtime fallback rate. Observed runtime fallback rate is 0/75 queried review paths, **not** an availability rate. |
| Serena | 0 logged | 0 response bytes | 0 logged | 0 / 0 / 0 logged | No target or per-tool rows observed; availability and rollout coverage unknown. |
| **Per-target MCP availability: target not recorded in supplied aggregate** | — | — | — | 0 / 0 / 0 logged | No target-level probe denominator; do not read zeros as successful availability. |

**Other MCP servers observed:** none in the supplied aggregate; unavailable raw logs prevent a complete unknown-server audit.

## Deep Audit — Workflows & Scripts (2026-10-07)

### Section 1: Bug & Correctness Sweep

Read-only scope: 54 workflows, 105 shell scripts, and 73 Python scripts. `bash -n` passed for the shell scripts. Local Python 3.11 cannot parse `scripts/workflow_retro.py:793-807`, but the relevant CI and workflow jobs pin Python 3.12 (`.github/workflows/ci.yml:195-198`; `.github/workflows/workflow-log-analysis.yml:84-86`); this is not counted as a pipeline defect. Checkout, resolver, and log-collector diagnostics already raised in the report’s **Reliability Improvements** section are not repeated here.

- **ID:** BUG-001 · **File:** `scripts/review_merge_train.sh:140-150,163-181,217-249` · **Severity:** High · **Category:** `bug`  
  **Description:** `_mt_pr_files_into` retains only `.filename` from an older PR’s file-list response. The current PR’s diff parser explicitly retains both sides of a rename, but an older PR’s previous filename is lost. **Inference:** a newer PR editing that old path can pass the overlap gate while the older PR renames it.  
  **Recommended fix:** Include `previous_filename` for renamed files in the cached, sorted path set; test both rename directions through `_mt_blockers_for_into`.

- **ID:** BUG-002 · **File:** `scripts/review_merge_train.sh:465-495` · **Severity:** High · **Category:** `bug`  
  **Description:** If adding `ai:merge-queued` fails at lines 471–477, the gate still writes `AUTOFIX_STALE_BASE_SKIP=true` and soft-exits. The release loop selects PRs carrying the queue label (`scripts/review_merge_train.sh:729-736`), so this PR has neither a review nor a label-backed release path.  
  **Recommended fix:** When label persistence fails, do not set the soft-exit variables; continue the full review with a warning, consistent with the gate’s lookup-failure behavior at lines 400–418.

- **ID:** BUG-003 · **File:** `scripts/orchestrate_poll_process.sh:12844-12856` · **Severity:** Medium · **Category:** `bug`  
  **Description:** The deterministic-validation marker is tested with `printf | grep -q` under `pipefail`. A read-only reproduction with long multiline `reason` text returned status 141 despite a matching first line, allowing the code to miss the deterministic short-circuit. Whether production reasons reach that shape remains unmeasured. [NEEDS VERIFICATION]  
  **Recommended fix:** Match `reason` with Bash `[[ … =~ … ]]` and derive the class from `BASH_REMATCH`, avoiding both `grep -q` and the subsequent `grep | head` pipeline.

- **ID:** BUG-004 · **File:** `scripts/review_merge_train.sh:358-377,479-486,792-797` · **Severity:** Medium · **Category:** `bug`  
  **Description:** `_mt_upsert_comment` returns success even when its PATCH or POST fails. Its release caller’s warning branch therefore cannot report that failure. The POST also uses `gh_retry`; if GitHub accepts a comment but its response is lost, retrying can create duplicate marker comments. [NEEDS VERIFICATION]  
  **Recommended fix:** Propagate failed-write status to callers. Make marker POST a single attempt and reconcile an ambiguous result with a fresh marker read before any later post, following the durable-marker approach at `scripts/orchestrate_poll_process.sh:6763-6783`.

- **ID:** SEC-001 · **File:** `scripts/gh_helpers.sh:575-606`; **call path:** `scripts/review_merge_train.sh:466-486` · **Severity:** Medium · **Category:** `security`  
  **Description:** `gh_retry` prints unredacted `"$*"` on failure, including `-f body=...`; the merge-train body incorporates PR changed paths. A failed write can therefore put submitted text into logs, and embedded newlines could produce unintended workflow-command-looking lines. No credential disclosure was established. [NEEDS VERIFICATION]  
  **Recommended fix:** Log a bounded command/endpoint class rather than argument values; redact body, input, and authorization arguments, and escape any remaining untrusted text with `_gh_actions_escape`.

### Section 2: GitHub API Call Redundancy Audit

Counts below are *logical calls on the specified path*, excluding pagination, `gh_retry` attempts, and unrelated writes. The report’s **GH API Call Audit** already explains why quota deltas are not measured endpoint counts.

- **ID:** API-001 · **File:** `scripts/orchestrate_poll_process.sh:11460-11473` · **Severity:** Low · **Category:** `api-redundancy`  
  **Description:** When `final_pr_json_snapshot` does not match the recorded PR, consecutive GETs fetch the same `pulls/${final_pr}` object separately for `.state` and `.merged_at`. **Current → proposed:** 2 → 1 GET on that branch.  
  **Recommended fix:** Fetch one payload, validate its PR number, and extract both fields locally, as the matching-snapshot branch already does. No new batching helper is needed.

- **ID:** API-002 · **File:** `scripts/review_merge_train.sh:288-302,358-373` · **Severity:** Low · **Category:** `api-redundancy`  
  **Description:** `_mt_find_marker_comment` lists comment objects but projects away their bodies; `_mt_upsert_comment` then GETs the selected comment solely to compare its body. **Current → proposed:** one paginated listing plus one individual GET → the listing alone for an existing marker.  
  **Recommended fix:** Extend the marker-list projection to retain the selected body in a structured result and pass it to `_mt_upsert_comment`. This extends the existing merge-train cycle-local reuse pattern rather than adding a new API helper.

- **ID:** API-003 · **File:** `scripts/gh_helpers.sh:727-779` · **Severity:** Medium · **Category:** `api-redundancy`  
  **Description:** `gh_api_json_to_file` retries failed commands without calling the existing `_is_gh_permanent_failure` classifier (`scripts/gh_helpers.sh:165-185`). A known 404 or 422 therefore consumes the default five attempts and backoff sleeps. **Current → proposed:** up to 5 → 1 call for classified permanent failures; retain retries for transient failures and malformed successful responses.  
  **Recommended fix:** Apply that classifier to captured stderr before rate-limit/backoff handling and return immediately for permanent errors, as `gh_retry` does at lines 575–581.

- **ID:** BATCH-001 · **File:** `scripts/orchestrate_poll_process.sh:5925-5963` · **Severity:** Medium · **Category:** `api-batching`  
  **Description:** The security-pass blocker loop issues one issue-state GET per blocker. **Current → proposed:** N → `ceil(N/25)` reads when all aliases return complete data, with individual fallback for misses.  
  **Recommended fix:** Extend the aliased-issue query pattern in `_fetch_candidate_issue_details_graphql` (`scripts/orchestrate_poll_process.sh:15909-15980`) with the blocker numbers and cache keyed states for this pass; retain unknown-state deferral and REST fallback on incomplete batches. [NEEDS VERIFICATION]

- **ID:** BATCH-002 · **File:** `scripts/orchestrate_poll_process.sh:28-46,78-86` · **Severity:** Medium · **Category:** `api-batching`  
  **Description:** Reclarify replay makes a live issue GET and a paginated comments read for each queued issue after its listing and shared identity read. **Current → proposed:** at least `3 + 2N` logical reads → `3 + ceil(N/25)` for issues whose complete required comment history fits an aliased response; additional reads remain necessary for overflow or prefetch failure.  
  **Recommended fix:** Extend `_fetch_candidate_issue_details_graphql` to prefetch the required issue, label, author, and comment fields with explicit completeness checks. Fall back to the current per-issue reads whenever the complete history cannot be established; never authorize replay from a partial cache. [NEEDS VERIFICATION]

- **ID:** BATCH-003 · **File:** `scripts/review_merge_train.sh:138-151,217-250` · **Severity:** Medium · **Category:** `api-batching`  
  **Description:** Each distinct older PR examined by the merge train gets its own paginated `/pulls/{n}/files` read; the per-run cache correctly prevents repeat reads but not the N distinct reads (capped at 20 PRs). **Current → proposed:** N first-page reads → `ceil(N/25)` aliased batch reads, plus required pagination/fallback.  
  **Recommended fix:** Evaluate an aliased changed-files prefetch patterned on `_fetch_linked_pr_status_graphql` (`scripts/orchestrate_poll_process.sh:16063-16118`). Use it only where complete paths—including rename source paths—can be proved; retain `_mt_pr_files_into` for incomplete or unsupported results. GraphQL field and pagination parity need verification. [NEEDS VERIFICATION]

### Section 3: Code Duplication & Modularization Opportunities

- **ID:** DUP-001 · **File:** `.github/workflows/workflow-log-analysis.yml:915-1175,1566-1784,2072-2288` · **Severity:** Low · **Category:** `duplication`  
  **Description:** The analysis, deep-audit, and API-redundancy steps repeat tracking-issue validation, `AI_PHASE_FAILURE_V1` payload construction, comment posting, and label handling, with phase-specific identifiers substituted.  
  **Recommended fix:** Put that shared reporting path in `scripts/workflow_log_analysis_helpers.sh` as `report_log_analysis_failure <failed_step_name> <tracking_issue> <failure_mode> <attempt_count> <summary>`; have all three steps call it while retaining their distinct step names, prompts, and output handling.

- **ID:** DUP-002 · **File:** `.github/workflows/review_autofix.yml:6768-6784,6857-6873` · **Severity:** Low · **Category:** `duplication`  
  **Description:** Two failure-notification steps repeat the same fingerprint-helper invocation, five evidence-file arguments, and marker-suffix construction.  
  **Recommended fix:** Move that calculation to a trusted staged helper under `scripts/`, with signature `review_failure_marker_suffix <runtime_dir> <head_sha> <run_id>`. Update both callers and register the helper in `scripts/stage_workflow_support.sh`.

The normalized non-comment bodies of `internal-implement.yml:1-34` and `internal-plan.yml:1-36` are about 71% similar, but their permissions, predicates, and static reusable-workflow targets differ. They are not proposed for consolidation.

### Section 4: Expression Size Limit Risk Assessment

Measurements remove YAML block indentation and include the `run:` script text containing `${{ }}`. They are estimates **before** context values replace the interpolations; non-interpolated blocks were excluded.

- **ID:** EXPR-001 · **File:** `.github/workflows/implement.yml:1003-1393` · **Severity:** High · **Category:** `expression-limit`  
  **Description:** “Stage workflow support files” contains three interpolations in approximately **19,132** script characters—about **1,868** below the stated 21,000-character limit and above the requested 18,000-character High-risk threshold.  
  **Recommended fix:** Extract the body to a script loaded from the already-checked-out trusted `.codex-workflow-src` support source (`implement.yml:963-989`). Pass the three GitHub expression values through step `env:` and preserve the current support-ref fallback.

- **ID:** EXPR-002 · **File:** `.github/workflows/implement.yml:3509-3829` · **Severity:** Medium · **Category:** `expression-limit`  
  **Description:** The preflight destructive/scope guard contains one interpolation in approximately **15,517** script characters—about **5,483** below the limit and above the requested 15,000-character Medium-risk threshold.  
  **Recommended fix:** Extract it to a trusted staged `scripts/` helper, pass `github.repository` via `env:`, and keep its existing `GIT_INDEX_FILE`, cleanup trap, and fail-closed scope outputs intact.

Across the other workflows, no measured interpolated `run:` block reaches 15,000 dedented characters; the largest in `test-and-mark-stable.yml:1082-1343` is approximately 13,415. The longest measured `if:` body is 947 characters (`internal-clarify.yml:17`), and no workflow exceeds the requested 800 KB flag threshold. **Repository-specific stricter limit:** `CLAUDE.md:1606-1630` documents a 512,000-byte hard limit and a 480,000-byte CI guard, rather than the prompt’s 1 MB overall limit.

### Section 5: Cross-Cutting Concerns

- **ID:** DEBT-001 · **File:** `.github/workflows/review_autofix.yml:1-7647` · **Severity:** Medium · **Category:** `tech-debt`  
  **Description:** The workflow is **463,582 bytes**, leaving only **16,418 bytes** before this repository’s 480,000-byte CI guard (`CLAUDE.md:1606-1630`). It does not breach the guard now, but another substantial inline step could.  
  **Recommended fix:** When next growing this workflow, first extract an eligible inline step using the documented `review_autofix_step_<slug>.sh` staging, registry, and contract-test pattern in `agents.md:932-980`; retain its step metadata and move expressions into `env:`.

- **ID:** SHELL-001 · **File:** `scripts/review_merge_train.sh:700-712`; **gate:** `.github/workflows/ci.yml:365-370` · **Severity:** Low · **Category:** `shellcheck`  
  **Description:** The unquoted `MERGE_TRAIN_DISPATCH_WORKFLOWS` expansion intentionally splits a configured list but also permits pathname expansion, changing the workflow names attempted if a list item matches workspace paths. CI invokes ShellCheck at `--severity=error`, so warning-level unquoted-expansion diagnostics are not enforced. A local ShellCheck diagnostic was not available. [NEEDS VERIFICATION]  
  **Recommended fix:** Parse the configured list into a Bash array and iterate quoted elements; then run ShellCheck at warning severity, documenting intentional exceptions.

No TODO/FIXME/HACK markers were found in the scoped files, and this sweep established no dead function or step confidently enough to recommend removal. The 57 label colors and descriptions in `scripts/label_helpers.sh:23-80` match `.github/ai/label_contract.v1.json`; no catalog-drift finding is raised.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
| --- | ---: | --- |
| Critical | 0 | — |
| High | 3 | BUG-001, BUG-002, EXPR-001 |
| Medium | 9 | BUG-003, BUG-004, SEC-001, API-003, BATCH-001, BATCH-002, BATCH-003, EXPR-002, DEBT-001 |
| Low | 5 | API-001, API-002, DUP-001, DUP-002, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
| --- | --- | --- |
| Critical/High bug fixes | Merge-train script and focused tests: ~2 | Medium |
| API call optimization | Three existing scripts and focused tests: ~5–6 | Medium |
| Code modularization | Two workflows, two new helpers, staging/contract tests: ~5–7 | Large |
| Expression size reduction | `implement.yml`, trusted helpers, staging/contract tests: ~4–6 | Medium |
| Medium/Low fixes | Existing scripts, `review_autofix.yml`, CI gate, and tests: ~5–7 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-10-07)

### Safety Tag Legend

`SAFE_TO_MERGE` is ready for implementation; `NEEDS_VERIFICATION` requires the stated checks first; `RISKY_SKIP` must not be auto-implemented because it touches a protected safety path.

### Consolidation Candidates (MERGE-###)

No findings.

### Redundant Re-Fetch (REUSE-###)

- **ID:** REUSE-001 · **Safety tag:** `RISKY_SKIP` · **Calls:** `scripts/workflow_failure_heal_intake.sh:221-224` and `scripts/workflow_failure_heal_intake.sh:471-479` · **Endpoint:** REST `GET /user` · **Current → proposed:** 2 → 1 logical reads *only* when both conditional reads execute; otherwise unchanged.
  - **Evidence:** The provenance path saves the authenticated login in `PROVENANCE_LOGIN`; the later lineage path fetches `/user` again when `PHASE_COMMENT_AUTHOR` is empty. Both reads can execute for a qualifying report with numeric `SOURCE_GEN`.
  - **Proposed fix:** After manual approval, let the `HEAL_TRUSTED_AUTHOR` assignment reuse a nonempty, verified `PROVENANCE_LOGIN`; retain its existing live-read fallback when that value is absent.
  - **Safety rationale:** `RISKY_SKIP` applies because both reads establish trusted identity in an authentication/provenance flow, where freshness and failure behavior matter.
  - **Downstream signal:** Do not auto-implement. Manually verify that both reads use the same unchanged token and that provenance verification permits reuse of its login for lineage authorization; retain the second read on any uncertainty.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: `RISKY_SKIP` — Both reads are in `orchestrate_poll_process.sh`; review the final-merge race and distinct failure outcomes before consolidating.
- API-002: `RISKY_SKIP` — The source is paginated and authenticates a marker; preserve complete listing and comment-body checks.
- API-003: `RISKY_SKIP` — Changing a retry loop requires manual review of permanent-error classification versus rate-limit backoff.
- BATCH-001: `RISKY_SKIP` — The poller’s blocker-state reads must retain unknown-state deferral on incomplete batches.
- BATCH-002: `RISKY_SKIP` — Paginated comment history is authorization evidence; a partial GraphQL response cannot replace it.
- BATCH-003: `RISKY_SKIP` — Paginated changed-file reads must retain complete path coverage and the cycle-local cache contract.

### Summary Counts

*Counts cover net-new findings only; Deep Audit cross-references are excluded.*

| Tag | Count | IDs |
| --- | ---: | --- |
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 0 | — |
| RISKY_SKIP | 1 | REUSE-001 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
