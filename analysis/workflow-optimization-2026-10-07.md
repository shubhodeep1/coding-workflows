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
