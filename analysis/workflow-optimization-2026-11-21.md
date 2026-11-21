<!-- workflow-log-analysis: window_start=2026-11-20T00:27:06Z window_end=2026-11-21T00:27:02Z run=44843876044 -->
## Executive Summary

- **Data gaps (read first).** Two repositories were truncated: `shubhodeep1/tele-funtoken-msg-scoring` and `shubhodeep1/coding-workflows`. Only part of their runs are listed (`page_limit_reached: true`). The full run-logs folder (`/home/runner/work/_temp/workflow-log-output`) was not mounted. So this report rests on the per-run telemetry and `log_summary` fields, plus the current repository code. `insufficient_data` is false.
- **Two-thirds of the window's failures are one external, consumer-side problem.** 24 of the 36 failures are zero-second runs of the six Netwask "Tier 1" workflows (`tier1-ci-doctor`, `tier1-ci-coach`, `tier1-mcp-inspector`, `tier1-observability-daily`, `tier1-portfolio-analyst`, `tier1-workflow-health-manager`). They ran in `fbc_shutdown` (12) and `mongo-explorer` (12), in two bursts per repo between 10:38 and 10:40 UTC on 2026-11-20. Each of those families failed 100% of the time. These files are not managed by coding-workflows. Fixing them should drop the fleet failure count from 36 to 12. Confidence: high that it is a single cause; low on what that cause is, because the log archives returned 404.
- **The collector misfiles those consumer workflows as `ci`.** `normalize_workflow_family` matches any substring `ci`. This lifts the reported `ci` failure rate to 14.3% (12/84), while coding-workflows' own CI rate is about 5.3% (4/76). A one-line classifier fix gives honest CI metrics. Confidence: high.
- **One review run used 41% of all tokens.** `binance-blessings` AI Review run 44761523221 used 47.77M of the fleet's 115.28M OpenRouter tokens, and 700,995 of 1.53M completion tokens. It made 9 calls over 9,133 s. Review runs have an iteration cap (`MAX_AUTOFIX_ITERATIONS`, default 5) but no token ceiling. A soft cap, off by default, would bound the tail. Confidence: medium.
- **The pre-push gate silently skips tests.** In tele-funtoken PRs #5663 and #5664, the post-merge context lists about 19–20 items as `Unverified … timeout`, including `ruff`. In `prepush_affected_tests.py::_run_one`, an item queued after the gate's 15-minute deadline returns `"timeout"` without ever starting. Separating "never started" from "timed out" would make about 9% coverage loss per PR visible. Confidence: medium.
- **PR-head checkout still fails late in review jobs.** Three consumer review runs failed at "Checkout PR head branch" after 745–961 s: 44721394819, 44721389896 and 44721246213. That is 2,594 runner-seconds. The `REVIEW_HEAD_CHECKOUT` diagnostic line added in the 2026-10-08 work exists, but the collector does not extract it, so the cause is invisible without full logs. Confidence: medium.
- **GH PAT budget telemetry is blind for the deterministic-skip path.** All 5 sampled `deterministic-skip-merge` jobs logged `used_in_job=unknown`. A separate diagnostic line would show why. Confidence: medium.

## Speed Optimizations

1. **Attribute CI wall-clock to queueing versus compute, using data already fetched (critical path).**
   - **Evidence:** coding-workflows CI ran for 7,141 s (44762505940), 4,571 s (44784298840), 4,136 s (44786190015), 3,745 s (44784284993, failed) and 3,540 s (44802559094, failed). CI p50 is 633.5 s and p95 is 3,732.7 s. Every CI job is capped at 15–20 minutes (`.github/workflows/ci.yml` `timeout-minutes` at lines 140, 883, 1534, 2034, 2657, 3464, 4262). Two runs were cancelled at `orchestrate-poll (3)` / "Pre-push affected-test gate tests" after 4,064 s (44770429815) and 3,573 s (44802155801).
   - **Root cause (inference):** a run lasting 3–6× the largest job timeout must be dominated by runner queueing or by `needs:` chains, not by any one job. This fits the §26 account-wide queue pressure. The collector already fetches the jobs payload for these runs (`jobs_fetch_status: success`) but drops `created_at`/`started_at`.
   - **Exact change:** in `scripts/collect_workflow_logs.py`, use the jobs payload already in memory to add `queue_wait_seconds_max` and `critical_path_job` to each run row. This adds no API calls (§14).
   - **Estimated savings:** none directly. It tells you whether queue reduction (§26) or job splitting attacks 30–100 min of CI latency per PR.
   - **Risk:** low; the change is additive to the analysis artefact only.
   - Finding check: reopened — "queue-time separation" was rejected on 2026-10-08 as measurement-only with no defect shown. This window shows the defect: runs of 3,000–7,141 s against job caps of at most 20 minutes, with zero extra API calls needed.

2. **Bound the long tail of review runs (critical path; same finding as Cost #1).**
   - **Evidence:** `review_autofix` p95 is 2,741.8 s and p99 wall-clock is 8,358,100 ms. Slowest runs:

     | Run | Repo | Duration (s) | Tokens |
     |---|---|---|---|
     | 44761523221 | binance | 9,133 | — |
     | 44774959846 | coding-workflows | 6,550 | 6.9M |
     | 44763915568 | binance | 6,043 | 7.3M |
     | 44753055666 | coding-workflows | 5,495 | — |

   - **Exact change:** see Cost #1.
   - **Estimated savings:** up to 2 h on the worst run (inference).
   - Finding check: new — no prior verdict on a per-run token or time ceiling. `review_autofix.yml` has only `MAX_AUTOFIX_ITERATIONS` (line 3303, default `5`) and `codex-agent` `timeout-minutes: 240` (line 2029).

## Cost Optimizations

1. **Add a per-run cumulative token soft cap to review/autofix, off by default.**
   - **Evidence:** run 44761523221 (binance-blessings, AI Review):

     | Metric | Value | Share of fleet |
     |---|---|---|
     | Total tokens | 47,771,869 | 41.4% of 115,276,002 |
     | Completion tokens | 700,995 | 45.9% |
     | Cache read tokens | 44,782,405 | — |
     | Calls | 9 | — |
     | Cache hit rate | 0.951 | — |

     Other telemetry-bearing review runs used 2.3M–9.9M tokens over 6–9 calls (for example 44804411765: 9.89M; 44749115278: 2.29M).
   - **Root cause:** an unbounded agent session. About 78k completion tokens and about 5M cache-read tokens per call point to very long multi-turn sessions (inference). No token ceiling exists in `.github/workflows/review_autofix.yml`.
   - **Exact change, in two steps:**
     1. Run `scripts/cost_audit.py` over run 44761523221. It already aggregates `or_phases` per phase, so it shows which reviewer or editor phase drove the cost.
     2. Add a new env var `REVIEW_RUN_TOKEN_SOFT_CAP` (default `0`, disabled; a new name that does not collide with existing ones). When the running `or_total_tokens` exceeds it, stop starting new autofix iterations and log `REVIEW_TOKEN_SOFT_CAP outcome=stop run_tokens=<n> cap=<n>`. Never abort a call that is in flight.
   - **Estimated savings:** with a cap near 15M, about 30M tokens on this one run. That is about 25% of the window's total.
   - **Quality risk:** a capped run may leave findings unfixed. Default off, fail-open, applies to the next iteration only.
   - Finding check: new — no prior verdict on a token ceiling. The 2026-10-08 Cost #4 rejection concerned model policy "with no cost data"; this window supplies the cost data.

**Other cost signals reviewed (no change recommended):**
- **Prompt cache:** fleet `cache_hit_rate` is 0.874, up from 0.7346 in the 2026-11-18 report. Cache writes are only 3.9% of reads.
- **Semble:** 14 queries totalling 201,733 bytes (about 14.4 KB each) from 102 sources, with `semble_static_dup_bytes_total=0` and `semble_echo_lines_dropped=0`. That is under 1% of prompt volume, so Semble is not inflating prompts.
- **Serena:** made no queries (`SERENA_ENABLED: false` in the review logs), so there is nothing to judge on tool-call replacement.
- **Reruns:** no rerun waste is visible (`retries=0` everywhere).
- **Model usage:** Codex usage is 0, so all model cost is OpenRouter.

## Reliability Improvements

1. **Fix or disable the Netwask Tier 1 workflows in the consumer repos.**
   - **Evidence:** 24 zero-second failures, two bursts per repo.

     | Repo | Run IDs |
     |---|---|
     | `fbc_shutdown` | 44721263574, 44721261868, 44721259077, 44721255077, 44721253186, 44721251415, 44721147855, 44721145021, 44721141536, 44721140125, 44721136754, 44721138550 |
     | `mongo-explorer` | 44721125384, 44721123673, 44721122018, 44721120012, 44721118207, 44721116378, 44720904243, 44720906018, 44720901619, 44720897157, 44720895692, 44720893866 |

     Log archives return HTTP 404 (`partial_data:missing_log_archive`), which means no job ever started.
   - **Root cause category:** workflow-definition or startup failure (inference: a "workflow file issue" on push to the installer branch). The suite is defined by the external Netwask installer: `netwask/tier1-agent-suite.json` names the branch `ai/netwask/tier1-agent-suite` and lists exactly these six agents. No `tier1-*.yml` exists under `workflow-templates/` or `.github/workflows/`.
   - **Exact fix:** in each consumer, open one run's annotations (for example 44721263574) or lint the six files. Then fix them, or remove their `push` trigger until they validate.
   - **Expected impact:** fleet failures drop from 36 to 12; `fbc_shutdown` and `mongo-explorer` failure rates drop from about 6.9% to about 0%.
   - **Rollback / fail-open:** disabling the files is reversible, and nothing in the AI pipeline depends on them.
   - Finding check: unverified — the consumer workflow files cannot be read from this checkout. No prior verdict covers them. The collector's 404 handling is already implemented and is not the problem.

2. **Stop misfiling consumer `tier1-ci-*` workflows as `ci` in the collector.**
   - **Evidence:** the `ci` family shows 12 failures out of 84. Eight of those are `tier1-ci-doctor.yml` / `tier1-ci-coach.yml` zero-second runs (44721255077, 44721253186, 44721145021, 44721141536, 44721125384, 44721116378, 44720904243, 44720901619). The true coding-workflows CI failure rate is 4/76 ≈ 5.3%, not 14.3%.
   - **Root cause:** `scripts/collect_workflow_logs.py::normalize_workflow_family`, line 616, `if "ci" in normalized: return "ci"`, is a substring match on the joined name and path.
   - **Exact fix:** before the `ci` check, return the path-stem family for stems that start with `tier1-`. Better still, require a `ci` token in the workflow file stem itself (`Path(path).stem` in `{"ci", "ci-..."}`). Add a test case in `tests/test_collect_workflow_logs.py`.
   - **Expected impact:** correct CI failure attribution; removes false CI-regression signals from future reports.
   - **Rollback:** a one-line revert. Only `tier1-*` families change.
   - Finding check: new — no prior verdict on family classification; the cause is present at line 616.

3. **Surface the existing `REVIEW_HEAD_CHECKOUT` classification in the collector.**
   - **Evidence:** "review / codex-agent" failed at "Checkout PR head branch" in:

     | Run | Repo | Failed after (s) |
     |---|---|---|
     | 44721394819 | fun-token-multi-chain | 961 |
     | 44721389896 | binance-blessings | 888 |
     | 44721246213 | multi-user-ai-agent | 745 |

     All three fell between 10:40 and 10:41 UTC, the same window as the Tier 1 burst.
   - **Root cause:** unknown from this evidence. `scripts/review_autofix_step_checkout_pr_head_branch.sh` emits `REVIEW_HEAD_CHECKOUT stage=… git_exit_class=<transport_5xx|network|auth|not_found|other>` (lines 152–154). It fails with `exit 1` after 4 fetch attempts (line 266), including for `not_found`. A head branch deleted mid-run would therefore fail the job rather than skip it (inference).
   - **Exact fix:** add a regex for the `REVIEW_HEAD_CHECKOUT` line to `scripts/collect_workflow_logs.py` and attach `stage`, `git_exit_class` and `fetch_attempts` to the run's `failure_point`. If the classes turn out to be `not_found`, a follow-up can treat a closed PR with a deleted branch as `skipped_stale`.
   - **Expected impact:** the root cause becomes visible on every such failure without full logs; this window had 3 failures.
   - **Fail-open:** the change is parser-only.
   - Finding check: recurring — the 2026-10-08 Speed #1 / Reliability #2 diagnostics were `applied`, but checkout failures still occur in this window (three runs above).

4. **Mark pre-push gate items that never started.**
   - **Evidence:** tele-funtoken post-merge contexts for PR #5663 (44843553847, 44843553874) and PR #5664 (44840071983, 44840072173) list about 19–20 `Unverified` items with reason `timeout`. These include `ruff`, `tests/test_withdrawal_flow.py` and `tests/test_wof_spin_outcomes.py`, alongside "(184/190 more)" items that ran.
   - **Root cause:** in `workflow-templates/.claude/hooks/prepush_affected_tests.py::_run_one`, lines 678–680 return `{"status": "timeout"}` when the shared deadline has already passed, before the item starts. Lines 1115–1116 then record it as `reason: "timeout"`. The default budget is `PREPUSH_TEST_GATE_TIMEOUT_MINUTES` = 15 (`scripts/prepush_test_gate_run.sh:78`). Units are submitted in list order (`run_units`, lines 702–709), so fast checks such as `ruff` can be starved by slow ones (inference).
   - **Exact fix:** this is the diagnostic addition.
     - Add `"not_started": True` to the early-return dict.
     - Propagate it to the `unverified` entry as an additive field, keeping `reason: "timeout"` for compatibility (§10).
     - Log `PREPUSH_TEST_GATE outcome=budget_exhausted not_started=<n> timed_out=<n>`.
     - Optionally, as a follow-up, submit lint units (for example `ruff`) first.
   - **Expected impact:** about 9% per-PR coverage loss becomes visible and actionable.
   - **Fail-open:** the field is additive and the gate exit semantics are unchanged.
   - Finding check: new — no prior verdict on gate budget starvation; the cause is present at `prepush_affected_tests.py:678-680`.

5. **Clear the npm audit advisory in `multi-user-ai-agent`.**
   - **Evidence:** "npm Audit Gate" / `npm-audit` / "Run npm run audit:ci" failed 2 of 2 runs: 44721247712 (112 s) and 44721601722 (255 s).
   - **Root cause category:** dependency advisory (inference).
   - **Exact fix:** update the flagged dependency in the consumer repo.
   - **Expected impact:** that family's failure rate goes from 100% to 0.
   - Finding check: unverified — consumer repo code and audit output are not readable here.

6. **Retire Node.js 20 action pins in consumer-local workflows (low priority).**
   - **Evidence:** `##[warning]Node.js 20 is deprecated` for:
     - `actions/checkout@11d5960a…` in AI Auto-Heal Trusted Dispatch (44843556751, 44840075140) and Secret Scan (44842281537);
     - `actions/checkout@v4` in Conditional deploy monitor (44840070768).
   - **Exact fix:** bump to a Node 24 release of `actions/checkout` in those consumer workflows.
   - **Expected impact:** avoids a hard break when Node 20 is removed.
   - Finding check: unverified — these workflows live in the consumer repos and are not readable here.

**BREAK_GLASS / CONTEXT_BUDGET_WARN:** 0 / 0 across all 126 runs with telemetry, so there is no rubric or prompt-size pressure.

**Semble / Serena fallbacks and probes:**
- `semble_fallbacks=29`, made of 24 contract-test fallbacks (4 per CI run × 6 runs: healthy, already classified separately) and 5 runtime fallbacks, all in one Internal: AI Implement run, 44765822629 (7,026 s). One run is rare fail-open behaviour, not a broken rollout.
- `serena_fallbacks=2` in AI Validate 44786114968, with zero probes. It is not possible to tell probe availability apart from runtime fallback without the logs.

## AI Memory Health

1. **Confirm `AI_MEMORY_TELEMETRY` emission once logs are mounted.**
   - **Evidence:** no `AI_MEMORY_TELEMETRY:` lines appear in any `log_summary`; many summaries say explicitly that none were visible. The deep-dive log folder was absent, so retrieve hit rate, `estimated_tokens` versus budget, `keyword_method` mix, `fail_open` and `enabled:false` cannot be computed.
   - **Exact change:** no code change. On the next window with mounted logs, grep `errors/` and `slow/` for `AI_MEMORY_TELEMETRY:` in implement and review runs (for example 44765822629 and 44761523221) before concluding that emission is missing.
   - Finding check: unverified — the run-logs folder was unreadable this window. The 2026-10-08 verdict records that `AI_MEMORY_TELEMETRY` is already emitted.

## GH API Call Audit

1. **Diagnose `used_in_job=unknown` on `deterministic-skip-merge`.**
   - **Evidence:** `GH_PAT_BUDGET phase=end workflow=review_autofix job=deterministic-skip-merge … used_in_job=unknown` in 44843472735, 44841055733, 44841051379, 44841039331 and 44841023176. That is 5 of the 5 sampled, while `codex-agent`, `post-merge-*` and `sweep` jobs report numbers (357, 18, 11, 7, 158).
   - **Root cause (inference):**
     - The start step (`review_autofix.yml:1242-1256`) writes the snapshot file only when `gh api rate_limit` returns a valid reading.
     - The end step (`review_autofix.yml:1651-1669`) needs the same `reset` value and `initial >= remaining`.
     - So either the start reading failed, or the reset differs between the two reads.
   - **Exact change:** when `used=unknown`, also print a separate line: `GH_PAT_BUDGET_DIAG job=deterministic-skip-merge reason=<start_missing|reset_changed|remaining_increased|end_unreadable>`. Do not append fields to the existing line: the collector regex `PAT_BUDGET_LINE` (`scripts/collect_workflow_logs.py:115-119`) is `$`-anchored right after `used_in_job=`, so extra fields would break parsing.
   - **Estimated call change:** 0 additional calls.
   - **Risk:** none.
   - Finding check: new — prior verdicts rejected per-endpoint counters, but none addresses this broken per-job metric.

**Observed volumes:**

| Run | Repo | Job | Calls used | Remaining after |
|---|---|---|---|---|
| 44841156138 | tele-funtoken | `codex-agent` | 357 | 871 |
| 44840928727 | coding-workflows | Internal sweep | 158 | 1,395 |
| 44843553847 | tele-funtoken | `post-merge-validate-dispatch` | 18 | — |
| 44840072173 | tele-funtoken | `post-merge-force-poll` | 11 | 1,889 |
| 44840072173 | tele-funtoken | `post-merge-validate-dispatch` | 7 | 1,893 |

- The `codex-agent` run and the sweep share `reset=1795220255`, which implies a shared PAT.
- No HTTP 429 or secondary rate-limit events were seen.
- Merge-train listing retries (`MERGE_TRAIN_RUNS_LISTING outcome=retry reason=listing_shifted … attempt=2/2` in 44843553850) behave as designed.
- Hygiene rules (§14): no new per-item loop calls were observed.

## Prompt Cache & Memory System

- **No change recommended this window.** Fleet `cache_hit_rate` is 0.874212, with 99.44M cache-read and 3.85M cache-write tokens.
  - Per repo: binance 0.916, tele-funtoken 0.816, coding-workflows 0.811.
  - Lowest single run: 0.627 (44749115278: 1.41M read / 0.30M write).
  - This is up from 0.7346 in `analysis/workflow-optimization-2026-11-18.md`.
  - `CONTEXT_BUDGET_WARN` count is 0, so prompt growth is not eroding cache value.
  - Cache fragmentation cannot be judged without logs. Prefix identity already exists (`REVIEWER_PROMPT_COMPONENTS … static_prefix_sha256`), so no new instrumentation is warranted.
- **Memory retrieval:** see AI Memory Health; no telemetry was available.

## Orchestrator Health

1. **Check why tracking #5558 was validated four times in one day.**
   - **Evidence:** AI Validate [tracking:5558] runs:

     | Run | Duration (s) | Outcome |
     |---|---|---|
     | 44765892940 | 3,405 | — |
     | 44786114968 | 1,516 | failed at "validate / validate" → "Enforce validation outcome" |
     | 44796966597 | 2,991 | — |
     | 44809026440 | 3,115 | — |

     Together that is about 3.0 h of runner time. `validate` p50 is 3,053 s.
   - **Exact change:** confirm from the validate dispatch logs whether each run followed a new integration-branch head. If two ran on the same head, deduplicate the dispatch by head SHA. This is a recommendation to investigate only.
   - Finding check: unverified — the validate dispatch code path was not read in this run, and the logs are unavailable.

**Observable indicators to track:**
- **Poller:** `orchestrate_poll` had 706 runs: 670 success, 36 cancelled, 0 failures. p50 is 262.5 s and p95 is 1,091 s. The slowest were 2,295 s (44757332975) and 2,255 s (44734907679).
- **Merge train:** `MERGE_TRAIN_RELEASE_SUMMARY … overlap=1 … oldest_queued_age_secs=1272` (44843553850) shows PR 5670 blocked by #5669 on overlapping paths, which is expected behaviour.
- **Clarify loops:** `orchestrate_clarify_respond` had 137 runs, all skipped, and no clarify failures.
- **Lineage:** issue #5662 closed with `ai:merged` after PR #5664 merged (44840071983), and the lineage looks healthy.

## Pipeline Flow Bottlenecks

**Runner-time share** (avg duration × runs; total about 309 h):

| Family | Share | Approx. hours |
|---|---|---|
| review/autofix | 46% | 143 |
| orchestrate_poll | 24% | 75 |
| CI | 9.5% | 29 |
| implement | 3.4% | 10.7 |
| workflow_integrity | 3.3% | 10.1 |
| validate | 1.4% | 4.2 |

**By overhead type:**
- **Compute:** review/autofix dominates; its long tail runs 4,500–9,133 s.
- **Queueing:** probably dominant for CI (inference), since CI wall-clock reaches 3–6× the job caps. Summaries repeatedly log "Job is waiting for a hosted runner to come online".
- **Retry:** negligible; `retries=0` everywhere.
- **Merge/conflict:** small; one overlap block of 1,272 s.
- **Skipped runs:** 1,446 skipped or other runs (check-failure triage 555, failure heal 270, clarify 147, plan 139, clarify-respond 137, implement 115) add noise but almost no runner time. The prior `queue-noop-fanout-reduction` item is recorded as intentionally deferred; it is not re-raised here.

**Recommendations, in end-to-end order:**

1. **The 10:38–10:43 UTC correlated burst on 2026-11-20.** 30 of 36 failures (24 Tier 1, 3 head-checkout, 2 npm-audit, 1 Finalize Project 26 at "Verify Phase 3 parity", 44721601597) fell within 5 minutes across 6 repos. Fix the Tier 1 workflows first (Reliability #1), then classify the checkout failures (Reliability #3).
   - Finding check: unverified — same finding as Reliability #1; consumer code is not readable.
2. **Measure CI queue wait** (Speed #1) before any CI restructuring.
   - Finding check: reopened — same finding as Speed #1.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows (truncated: 1,000 runs listed)
- **Bottlenecks:** review/autofix runs of 4,700–6,550 s (44774959846, 44753055666, 44753929019); CI up to 7,141 s.
- **Failures:** CI failed 4 times:
  - 44784284993 at "Fetch base commits for the shallow checkout (hooks and orchestrator)". That step already retries three times (`ci.yml:926-941`).
  - 44802559094 and 44754348968 at "System-Python test shard" (shards 17 and 8).
  - 44769284558 at `orchestrate-poll (3)` unit tests.
- **Cost:** 38.1M tokens over 56 calls; hit rate 0.811.
- **Actions:**
  1. Fix the CI family classifier.
     - Finding check: new — `collect_workflow_logs.py:616` substring match.
  2. Add CI queue-wait attribution.
     - Finding check: reopened — see Speed #1.
  3. Add the `GH_PAT_BUDGET_DIAG` line for `deterministic-skip-merge`.
     - Finding check: new — 5/5 runs show `used_in_job=unknown`.

### shubhodeep1/tele-funtoken-msg-scoring (truncated: 1,000 runs listed)
- **Bottlenecks:** validate runs of 3,000–3,542 s; review runs of 2,400–2,986 s.
- **Failures:** validate 44786114968; review gate 44731611279 at "Evaluate review gate" (366 s).
- **Cost:** 8.0M tokens over 16 calls; hit rate 0.816.
- **Actions:**
  1. Mark pre-push gate items that never started.
     - Finding check: new — `prepush_affected_tests.py:678-680`.
  2. Check the repeated tracking #5558 validations.
     - Finding check: unverified — validate dispatch code not read.
  3. Keep an eye on PAT headroom (871 remaining after `codex-agent` 44841156138); no code change.
     - Finding check: unverified — per-endpoint counters were rejected before, and there is no new defect.

### shubhodeep1/binance-blessings
- **Bottleneck:** review run 44761523221 (9,133 s).
- **Failure:** head checkout 44721389896.
- **Cost:** 69.1M tokens, the largest of any repo, with hit rate 0.916.
- **Actions:**
  1. Attribute the outlier by phase and add the soft cap.
     - Finding check: new — no token ceiling in `review_autofix.yml`.
  2. Parse `REVIEW_HEAD_CHECKOUT`.
     - Finding check: recurring — 2026-10-08 diagnostics applied; failures continue.
  3. Bump the Node 20 checkout in Secret Scan.
     - Finding check: unverified — consumer workflow.

### shubhodeep1/fun-token-multi-chain
- **Failure:** head checkout 44721394819 (961 s). Failure rate 0.57%; no token telemetry.
- **Action:** parse `REVIEW_HEAD_CHECKOUT`.
  - Finding check: recurring — as above.

### shubhodeep1/multi-user-ai-agent
- **Failures:** npm audit ×2 (44721247712, 44721601722); Finalize Project 26 "Verify Phase 3 parity" (44721601597); head checkout 44721246213.
- **Actions:**
  1. Fix the npm advisory.
     - Finding check: unverified — consumer code.
  2. Parse `REVIEW_HEAD_CHECKOUT`.
     - Finding check: recurring — as above.

### shubhodeep1/fbc_shutdown
- **Failures:** 12 of 12 are zero-second Tier 1 runs, with 5 log archives missing (404).
- **Action:** fix or disable the six `tier1-*.yml` files.
  - Finding check: unverified — consumer code.

### shubhodeep1/mongo-explorer
- **Failures:** 12 of 12 are zero-second Tier 1 runs.
- **Action:** fix or disable the six `tier1-*.yml` files.
  - Finding check: unverified — consumer code.

### shubhodeep1/bitsafe.io
- **Health:** 0 failures in 160 runs; the poller (44843014961, 166 s) is healthy.
- **Action:** none.
  - Finding check: unverified — no run in this window supports a finding.

## Metrics Appendix

**Runs and outcomes**

| Scope | Runs | Success | Failure | Cancelled | Other / skipped | p50 (s) | p95 (s) |
|---|---|---|---|---|---|---|---|
| Fleet | 3,739 | 2,213 (59.2%) | 36 (0.96%) | 44 | 1,446 | 45 | 1,587 |
| coding-workflows | 1,000 | 592 | 4 | 21 | 383 | 58 | 2,393 |
| tele-funtoken-msg-scoring | 1,000 | 411 | 2 | 11 | 576 | 9 | 1,442 |
| binance-blessings | 871 | 434 | 1 | 9 | 427 | 10 | 1,209 |
| multi-user-ai-agent | 184 | 150 | 4 | 0 | 30 | 164.5 | 745 |
| fun-token-multi-chain | 176 | 167 | 1 | 0 | 8 | 236.5 | 793 |
| fbc_shutdown | 175 | 154 | 12 | 1 | 8 | 117 | 631 |
| mongo-explorer | 173 | 153 | 12 | 1 | 7 | 113 | 628 |
| bitsafe.io | 160 | 152 | 0 | 1 | 7 | 183.5 | 679 |
| `ci` family (as reported) | 84 | 69 | 12 (8 are Tier 1) | 3 | 0 | 633.5 | 3,733 |
| review_autofix | 645 | 639 | 4 | 2 | 0 | 255 | 2,742 |
| orchestrate_poll | 706 | 670 | 0 | 36 | 0 | 262.5 | 1,091 |
| validate | 6 | 5 | 1 | 0 | 0 | 3,053 | 3,508 |

**Tokens and cache (OpenRouter; 101 calls, all with usage available)**

| Metric | Value |
|---|---|
| Prompt / completion tokens | 10,463,032 / 1,526,291 |
| Cache read / write tokens | 99,442,657 / 3,845,553 |
| Total tokens | 115,276,002 (binance 69.1M, coding-workflows 38.1M, tele-funtoken 8.0M) |
| `cache_hit_rate` | 0.874212 |
| `codex_calls` / `codex_tokens_used` | 0 / 0 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (fleet, 122 samples) | 13,000 / 7,116,850 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (review_autofix) | 366,000 / 8,358,100 |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |

**GH API (GH_PAT_BUDGET end lines in `log_summary`)**

| Run | Job | used_in_job | remaining |
|---|---|---|---|
| 44841156138 | codex-agent | 357 | 871 |
| 44840928727 | sweep | 158 | 1,395 |
| 44843553847 | post-merge-validate-dispatch | 18 | 4,703 |
| 44840072173 | post-merge-force-poll / post-merge-validate-dispatch | 11 / 7 | 1,889 / 1,893 |
| 44843472735, 44841055733, 44841051379, 44841039331, 44841023176 | deterministic-skip-merge | unknown ×5 | 4,836–4,965 |
| 44841635555 | codex-agent | unknown | 4,737 |

HTTP 429 or secondary rate-limit events: 0 visible.

**Semble / Serena**

| Metric | Value |
|---|---|
| `semble_query_calls` / `semble_query_bytes` | 14 / 201,733 (≈14.4 KB per query) |
| `semble_sources_total` / `semble_static_dup_bytes_total` / `semble_echo_lines_dropped` | 102 / 0 / 0 |
| `semble_bootstraps` / failed / unused / total ms | 14 / 0 / 0 / 875,406 (≈62.5 s each) |
| `semble_fallbacks` (contract-test / runtime) | 29 (24 / 5); runtime all in 44765822629 |
| Serena query / tool calls / response bytes / ms | 0 / 0 / 0 / 0 |
| `serena_fallbacks` | 2 (44786114968) |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (all targets) | 0 | 0 | 0 |
| Semble (per-target breakdown) | not available in context | — | — |

Other MCP servers observed: none.

**Findings check**

| Status | Count |
|---|---|
| new | 4 |
| recurring | 1 |
| reopened | 1 |
| unverified | 5 |
| dropped: already fixed on this ref | 4 (contract-test vs runtime Semble split; 404 missing-archive classification; merge-train `listing_shifted` re-read; CI shallow-fetch retry) |
| dropped: previously decided | 3 (per-endpoint GH API counters; poller `SEMBLE_AVAILABLE=false` under lazy mode; prompt-prefix telemetry) |

**Data gaps**
- `repositories_truncated`: tele-funtoken-msg-scoring and coding-workflows.
- The run-logs folder was not mounted. A missing log-output artifact was rejected on 2026-10-08 as runtime state, not a code defect.
- Five `fbc_shutdown` log archives returned 404.
- `analysis/recommendation-processing-report.md` is readable, but its newest ledger entry covers `workflow-optimization-2026-10-08.md`. Later docs, including 2026-11-18, have no recorded verdicts.
- `success_sample_rate` was 0.07.