## Executive Summary

- `review_autofix` is the clear bottleneck: 102 active runs consumed 113,167s of 168,518 active workflow-seconds (67.2%), with p50 1,147.5s and p95 3,069.2s. The biggest low-risk win is the check-run wait loop in `.github/workflows/review_autofix.yml:1888-2013`; sampled runs 26242205286, 26246026439, 26248621568, 26269646416, and 26271444771 each spent 40-59 poll sleeps there. Estimated impact: 8-15 minutes saved on slow review runs. Confidence: high.
- Small PRs are still taking the expensive review path. `.github/workflows/review_autofix.yml:109-118` explicitly says the pass-2 gate is a no-op at defaults, and runs 26271444771 (PR 2889, 3 files / 4 additions) and 26269646416 (PR 2890, 5 files / 5 additions) still logged `small_diff=false`. Estimated impact: 20-40% lower review latency/token spend on small PRs. Confidence: high.
- One of only two hard failures in 1,000 runs is a deterministic AI-memory ref-lock race: plan run 26268304639 failed in `plan / plan / Check and claim /answer command` after 5 push attempts to `refs/heads/ai-memory` (`errors/.../26268304639/step-001-plan_plan.log:1258-1260`). Estimated impact: removes a known plan-blocking failure mode. Confidence: high.
- CI fast-fail is missing. CI run 26242204999 spent 1,099s before `ruff` failed on two `F841` errors in `scripts/verify_integration_fingerprints.py` (`errors/.../26242204999/step-001-lint.log:2320-2344`), while `.github/workflows/ci.yml:501-505` places `ruff` late in the job. Estimated impact: up to ~18 minutes saved on lint regressions. Confidence: high.
- Prompt/context bloat is a major cost driver. `review_autofix` editor prompts reached 323,987-684,542 bytes (runs 26226389847, 26246026439, 26250888345, 26271444771), and runs 26242205286 and 26250888345 logged 552,811 and 514,997 tokens respectively, with context compaction and one provider-side 429 in 26250888345. Estimated impact: six-figure token savings on heavy review runs. Confidence: high.
- Orchestrator wrapper fan-out is noisy but cheap: 688 skipped `clarify`/`plan`/`implement`/`orchestrate_clarify_respond` runs consumed only 965s total, with recent examples at 2026-05-22 07:52 UTC triggered by `<!-- ORCHESTRATOR_STATE_V2 ... -->` comments. Estimated impact: modest runner savings but meaningful queue/UI cleanup. Confidence: high.

## Speed Optimizations

### Critical-path wins

1. **Cut `review_autofix` check-run waiting**
   - **Evidence:** `.github/workflows/review_autofix.yml:1938-2012` polls every 20s up to 1200s. Current-window runs logged:
     - 26269646416: 59 waits and an explicit `CHECK_RUNS_WAIT_TIMEOUT reached after 1200s` (`slow/.../26269646416/step-001-review_codex-agent.log:4410`)
     - 26242205286: 47 waits (minimum 940s sleep)
     - 26246026439: 47 waits (minimum 940s sleep)
     - 26248621568: 46 waits (minimum 920s sleep)
     - 26271444771: 40 waits (minimum 800s sleep)
   - **Root cause:** the review path blocks on sibling check-runs before building autofix context.
   - **Exact change:** lower `CHECK_RUNS_WAIT_TIMEOUT_SECS` to 180-300 for normal PRs, allow `0-60` for small diffs, and switch the loop to adaptive backoff when the in-flight count is unchanged (for example 20s → 40s → 80s → 120s cap) while reusing the last successful snapshot.
   - **Estimated time savings:** 8-15 minutes on affected slow `review_autofix` runs.
   - **Implementation risk:** low-medium; the step already fails open and writes a sentinel snapshot.

2. **Restore a real small-diff fast path**
   - **Evidence:** `.github/workflows/review_autofix.yml:109-118` documents that `REVIEWER_PASS2_REASONING_SMALL` and `..._LARGE` both default to `xhigh`. Gate logs show:
     - 26271444771: `AUTOFIX_GATE_DET_SKIP_EVAL pr=2889 files=3 additions=4 deletions=? ... small_diff=false skip=false` (`step-002-review_gate.log:464`)
     - 26269646416: `... pr=2890 files=5 additions=5 deletions=? ... small_diff=false skip=false` (`step-002-review_gate.log:426`)
     - Pass-2 still ran at `xhigh` on 2 LOC (26250888345), 66 LOC (26226389847), 74 LOC (26271444771), and 164 LOC (26246026439).
   - **Root cause:** the configuration makes small and large diff branches identical, and the size gate falls back to “not small” when deletion data is missing.
   - **Exact change:** immediately set `REVIEWER_PASS2_REASONING_SMALL=medium` or `high` while keeping `REVIEWER_PASS2_REASONING_LARGE=xhigh`; separately instrument/fix the missing-deletions path so `SMALL_DIFF` can classify tiny PRs reliably.
   - **Estimated time savings:** 2-8 minutes per small PR.
   - **Implementation risk:** medium; keep pass-1, force-review override, and CI/lint failure handling unchanged.

3. **Move `ruff` to the front of CI**
   - **Evidence:** run 26242204999 failed after 1,099s on two trivial `F841` violations (`errors/.../26242204999/step-001-lint.log:2320-2344`). In `.github/workflows/ci.yml:480-505`, shell syntax, export validation, and ShellCheck all run before `ruff`.
   - **Root cause:** the cheapest deterministic Python failure check runs too late.
   - **Exact change:** move `Python lint (ruff)` ahead of slower validations, or split it into its own early parallel job.
   - **Estimated time savings:** up to ~18 minutes on lint-failing runs.
   - **Implementation risk:** low.

### Micro-optimizations

4. **Cache tool bootstrap in CI**
   - **Evidence:** CI runs 26274115196 and 26274552286 logged a fresh `actionlint` download/install; `.github/workflows/ci.yml:504` also `pip install -q ruff` each run.
   - **Root cause:** per-run tool bootstrap.
   - **Exact change:** restore tool caches or use setup actions for `actionlint` and `ruff`.
   - **Estimated time savings:** seconds per CI run.
   - **Implementation risk:** low.

## Cost Optimizations

1. **Shrink the `review_autofix` editor prompt before changing models**
   - **Evidence:** `scripts/review_apply_fixes.sh:632-693` inlines large sections: PR diff, last-run diff, changed-file lists, targeted file context, reviewer consensus, comments, check-runs, and reviewer bundle. Observed prompt sizes:
     - 26246026439: pass1 20,888 bytes; review 19,353; editor 684,542
     - 26242205286: pass1 23,594; review 40,926; editor 590,217; tokens used 552,811
     - 26250888345: pass1 15,109; review 11,176; editor 570,338; tokens used 514,997; context compacted; provider-side 429
     - 26271444771: editor 327,003
   - **Root cause:** overlapping high-cap inline artifacts and a very large editor prompt budget.
   - **Exact change:** lower caps on comments/check-run tails/reviewer bundle for small PRs, dedupe overlapping sources (`PR_DIFF_FILE`, `LAST_RUN_DIFF_FILE`, targeted file context), and prefer compact summaries when the same facts are already inlined elsewhere.
   - **Estimated savings:** roughly 150k-300k tokens on heavy `review_autofix` runs.
   - **Quality-risk notes:** medium; keep CI/lint failures and reviewer consensus authoritative.

2. **Use cheaper reasoning on small diffs instead of swapping core models**
   - **Evidence:** sampled slow runs already use `XPOLL_SUMMARISER_MODEL: openai/gpt-5.4-mini` (e.g. 26272215424, 26272218890), so summarisation is already on a cheaper tier. The waste is the `xhigh` second-pass reviewer path on tiny diffs.
   - **Root cause:** reasoning policy, not summariser model choice.
   - **Exact change:** set `REVIEWER_PASS2_REASONING_SMALL=medium` or `high`; leave editor model selection alone for now.
   - **Estimated savings:** meaningful token and latency reduction on small PRs without a broad quality downgrade.
   - **Quality-risk notes:** low-medium if pass-1 and CI/lint context stay intact.

3. **Keep Semble reviewer-context; cap Semble overflow**
   - **Evidence:** strict runtime telemetry showed 16 production `SEMBLE_QUERY` lines, all in `review_autofix`: 7 `reviewer-context` queries totaling 115,052 bytes and 9 `overflow` queries totaling 76,372 bytes. Run 26246026439 alone issued five overflow fetches at 8,377 bytes each after a reviewer-context query.
   - **Root cause:** Semble is being used both for useful bounded reviewer-context and for extra overflow fetches that often look redundant.
   - **Exact change:** keep `reviewer-context`, but dedupe overflow files across phases, cap overflow fetch count per iteration, and suppress overflow fetches when the file is already covered by targeted file context.
   - **Estimated savings:** tens of KB per heavy review run; lower downstream prompt growth.
   - **Quality-risk notes:** low if dedupe happens before any hard cap.
   - **Semble verdict:** *Inference:* Semble is reducing raw prompt expansion on the reviewer-context path; the cost problem is the overflow path, not Semble itself.

4. **Instrument prompt cache before trying to optimize it**
   - **Evidence:** `OPENROUTER_PROMPT_CACHE_DISABLED=false` is emitted in sampled plan/review/orchestrate logs, and code already supports cache-usage reporting (`scripts/review_run_reviewers.sh:107-117`) plus a probe path (`scripts/review_run_reviewers.sh:121-165`). But no deep-dive logs contained `cache_creation_input_tokens`, `cache_read_input_tokens`, or `CACHE_PROBE_OK`.
   - **Root cause:** cache exists in code, not in telemetry.
   - **Exact change:** enable the existing `REVIEWER_CACHE_PROBE_ENABLED` on canary `review_autofix` runs and emit the same usage line for normal reviewer/editor calls.
   - **Estimated savings:** unquantified until measured; enables higher-confidence future savings.
   - **Quality-risk notes:** low.

5. **Treat canceled `review_autofix` runs as cost to classify first**
   - **Evidence:** 16 canceled `review_autofix` runs consumed 20,523s total (avg 1,282.7s, p50 1,508s, max 3,161s).
   - **Root cause:** unclear from current telemetry; PR-backed `review_autofix` explicitly sets `cancel-in-progress: false` in `.github/workflows/review_autofix.yml:731-755`.
   - **Exact change:** add cancellation-cause tagging before changing concurrency behavior.
   - **Estimated savings:** potentially large, but not safely quantifiable yet.
   - **Quality-risk notes:** low.

**Serena note:** no production `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines were observed, so Serena is not currently replacing downstream tool/model work in this window.

## Reliability Improvements

1. **Fix AI-memory processed-command claim contention**
   - **Failure evidence:** run 26268304639 (`Internal: AI Plan`) failed in `plan / plan / Check and claim /answer command` with `AI_MEMORY_ERROR: Failed to push memory branch after 5 attempts` and `cannot lock ref 'refs/heads/ai-memory'` (`errors/.../26268304639/step-001-plan_plan.log:1258-1260`).
   - **Root cause category:** shared-ref concurrency / optimistic push collision.
   - **Exact fix:** add jitter/backoff to `persist_memory_operation` (`scripts/ai_memory_lib.py:1860-1894`), retry only on retryable ref-lock failures, preserve the duplicate-check recovery in `scripts/ai_memory.py:1229-1268`, and emit `processed-command-claim` telemetry on both success and exception paths.
   - **Expected reliability impact:** high; this removes a proven hard-failure path in plan execution.
   - **Rollback/fail-open:** keep claim operations fail-closed so duplicate `/answer` processing is still impossible.

2. **Lower prompt pressure to reduce provider-side retry failures**
   - **Failure evidence:** `review_autofix` run 26250888345 logged `context compacted`, then `ERROR: exceeded retry limit, last status: 429 Too Many Requests`, after 514,997 tokens; 26242205286 also compacted context at 552,811 tokens.
   - **Root cause category:** oversized prompt / provider throttling.
   - **Exact fix:** combine the prompt-size reductions and small-diff reasoning downgrade above.
   - **Expected reliability impact:** medium; fewer retry-limit exhaustions and less compaction-induced brittleness.
   - **Rollback/fail-open:** drive via existing vars (`TARGETED_FILE_CONTEXT_MAX_BYTES`, reasoning vars) so rollback is a config change.

3. **Instrument cancellation causes before changing `review_autofix` concurrency**
   - **Failure evidence:** 16 canceled `review_autofix` runs consumed 20,523s, but the present logs do not say whether the cancels came from PR closure, manual action, workflow replacement, or runner loss.
   - **Root cause category:** observability gap.
   - **Exact fix:** extend the run-end failure/cancel hook in `.github/workflows/review_autofix.yml:5809-5825` to record cancellation reason, sibling run ID, PR state, and SHA.
   - **Expected reliability impact:** medium; it converts hidden rerun waste into fixable categories.
   - **Rollback/fail-open:** telemetry only.

4. **MCP fail-open behavior looks healthy in production**
   - **Failure evidence:** none in production. Strict runtime telemetry showed 16 production `SEMBLE_QUERY` lines and 0 production `SEMBLE_FALLBACK`; the only fallback lines were 5 test-fixture `missing_semble` events in `test_and_mark_stable` run 26268664618. No production Serena probe/query/fallback lines appeared.
   - **Root cause category:** n/a.
   - **Exact fix:** no urgent production change; keep current fail-open behavior and keep the test coverage.
   - **Expected reliability impact:** avoids unnecessary churn.
   - **Rollback/fail-open:** current behavior is already fail-open.

## AI Memory Health

- **Telemetry coverage found:** 30 `AI_MEMORY_TELEMETRY` events across 8 deep-dive runs.
- **Operation mix:** 15 `record-run-event`, 7 `record-candidate`, 7 `retrieve`, 1 `processed-command-check`.
- **Retrieve effectiveness:** 7/7 retrieve events returned `records_selected=0`; hit rate **0%**. All were reviewer-role retrieves from `review_autofix` runs 26226389847, 26242205286, 26246026439, 26248621568, 26250888345, 26269646416, and 26271444771.
- **Budget use:** average `estimated_tokens=0`, against the configured reviewer budget of **1400** tokens in `ai-memory/config/retrieval_profiles.v1.json:59-76`.
- **Keyword method distribution:** `keyword_method=none` in 7/7 retrieves; `plain=0`, `llm=0`.
- **Fail-open / disabled telemetry:** no sampled entries had `fail_open: true`; no sampled entries had `enabled: false`.
- **Push retry signal:** emitted telemetry only showed `push_attempts=1` on successful memory writes. The highest-value failure path did **not** emit structured claim telemetry: run 26268304639 failed after 5 push attempts, but only `processed-command-check` was logged before the error.
- **Recommendation:** make reviewer retrieval actually spend part of its budget by enabling a conservative keyword path (`plain` first, `llm` only if needed), and add exception-path `processed-command-claim` telemetry with `push_attempts`, `failure_reason`, and `did_push`.

## GH API Call Audit

1. **Hotspot: `review_autofix / Collect PR check-run failures`**
   - **Evidence:** `.github/workflows/review_autofix.yml:1888-2013` polls `commits/{sha}/check-runs?per_page=100` in a loop. Current-window sampled runs logged 9-59 waits each, with 46-59 waits on four slow runs and an explicit 1200s timeout in 26269646416.
   - **Why it matters:** README already warns this is at least one logical snapshot per iteration and may fan out under pagination/retries (`README.md:65-68`). That conflicts with the repo’s API hygiene rule to prefer cycle-local reuse over repeated inner-loop API calls (`CLAUDE.md:426-440`).
   - **Concrete change:** cache the last snapshot locally, increase the sleep interval when the in-flight count is unchanged, and shorten the nominal wait budget.
   - **Estimated call-count reduction:** roughly 70-85% on the slowest sampled runs.
   - **Rate-limit risk reduction:** high.
   - **Supporting analysis-only evidence:** workflow-log-analysis run 26268682225 also estimated `>=176` logical snapshots across five sampled slow `review_autofix` runs; I treat that as supporting evidence, not primary proof for this window.

2. **Good pattern to preserve: PR gate metadata fetch**
   - **Evidence:** `.github/workflows/review_autofix.yml:252-259` extends the existing PR fetch to include state, labels, additions, and deletions, explicitly avoiding a separate `/files` call on the small-diff path.
   - **Assessment:** this matches the repo’s “check first, add second” rule.
   - **Recommendation:** keep this pattern; fix the downstream missing-deletions path rather than adding new API calls.

3. **Good pattern to preserve: implement existing-PR check**
   - **Evidence:** `.github/workflows/implement.yml:137-169` uses one paginated issue timeline query to detect open cross-referenced PRs and explicitly avoids fuzzy `gh pr list --search`.
   - **Assessment:** this is good API hygiene and not a hotspot.
   - **Recommendation:** no change.

4. **Minor redundancy: `review_autofix` post-commit dispatch fallback chain**
   - **Evidence:** `.github/workflows/review_autofix.yml:5289-5381` intentionally sleeps, checks for peer runs, then may try `gh workflow run review_autofix.yml` and one or more caller workflow fallbacks.
   - **Assessment:** not the main API problem, but it can add extra dispatch calls and UI noise on busy PRs.
   - **Recommendation:** log the exact dispatch path and suppress fallback attempts when the direct target is known unavailable.
   - **Estimated call-count reduction:** small.

**Direct GitHub API 429 note:** no direct GitHub API 429 or secondary-rate-limit runtime lines were observed in current-window production deep dives.

## Prompt Cache & Memory System

1. **Prompt-cache telemetry is missing, not necessarily prompt-cache functionality**
   - `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears in sampled runs, and `scripts/openrouter_prompt_cache.py:77-97` plus `scripts/review_run_reviewers.sh:107-117` already know how to normalize and print cache usage. But no deep-dive logs showed `cache_creation_input_tokens`, `cache_read_input_tokens`, or `CACHE_PROBE_OK`.
   - **Recommendation:** enable the existing `REVIEWER_CACHE_PROBE_ENABLED` canary path (`scripts/review_run_reviewers.sh:121-165`) and emit the same usage line for normal reviewer/editor calls.
   - **Impact:** better token/latency visibility with near-zero behavior risk.

2. **Cache fragmentation is likely coming from large volatile editor inputs**
   - **Evidence:** `scripts/review_apply_fixes.sh:632-693` inlines highly variable artifacts after PR metadata, including full diffs, comments, check-run tails, and reviewer bundles. Observed editor prompts ranged from 323,987 to 684,542 bytes on sampled `review_autofix` runs.
   - **Inference:** even if provider cache is enabled, these large changing sections will fragment reuse.
   - **Recommendation:** keep the existing load-bearing instruction placement in `scripts/review_apply_fixes.sh:479-576`, but reduce variability by deduping overlapping sections and moving volatile detail to capped summaries or on-demand reads.
   - **Impact:** lower token spend, lower compaction risk, better cacheability.

3. **Memory retrieval is currently ineffective**
   - All sampled reviewer retrieves were zero-hit, zero-token, `keyword_method=none`.
   - **Recommendation:** for reviewer-role retrieval, add a conservative keyword path and a minimal fallback selection so the 1400-token reviewer budget can actually surface prior incidents/patterns.
   - **Impact:** moderate token efficiency and reliability gain if retrieval becomes useful.

4. **Semble is helping selectively**
   - Reviewer-context Semble usage looks efficient; overflow fetches are the noisy extension.
   - **Recommendation:** keep reviewer-context, dedupe overflow, and do not invest in Serena tuning until Serena is actually enabled and emitting runtime telemetry.

## Orchestrator Health

- **Wrapper fan-out noise is real:** 688 skipped `clarify`/`plan`/`implement`/`orchestrate_clarify_respond` runs consumed only 965s, but recent runs 26275551821, 26275551866, 26275551823, and 26275551867 all evaluated slash-command predicates against an `<!-- ORCHESTRATOR_STATE_V2 ... -->` comment and skipped.
  - **Smallest safe mitigation:** add a caller-side prefilter for orchestrator state comments before dispatching wrapper workflows.
  - **Track:** skipped-wrapper count, skipped-wrapper seconds, runner-pickup count for skipped jobs.

- **Plan progression has one sharp failure edge:** run 26268304639 shows the `/answer` claim path can still hard-stop the plan stage.
  - **Smallest safe mitigation:** fix the claim retry path and add failure-path telemetry.
  - **Track:** `processed-command-claim` failures and push-attempt p95.

- **Merge/finalization friction is visible:** forward-merge run 26275972594 completed in 25s but opened a manual conflict PR after `AHEAD="12"` and 17 conflicted files (`recent/.../26275972594/step-001-forward-merge.log:2209-2271`).
  - **Smallest safe mitigation:** alert when stable→main drift grows large and run the existing forward-merge path before conflict sets widen.
  - **Track:** `AHEAD`, conflict-file count, forward-merge conflict PR count.

- **Tool availability is uneven but not currently breaking orchestration:** orchestrate_poll run 26275462752 logged `SEMBLE_ENABLED: true` with `SEMBLE_AVAILABLE: false` and `SEMBLE_INDEX_AVAILABLE: false`; no Serena runtime telemetry was present.
  - **Smallest safe mitigation:** add an availability-rate metric by workflow; do not treat this as a first-wave fix until it correlates with failures.

## Pipeline Flow Bottlenecks

1. **Queueing / control-plane**
   - The noisiest stage is not compute; it is wrapper dispatch. `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` produced 688 skipped runs from comment-trigger evaluation, especially around orchestrator state comments on 2026-05-22 07:52 UTC.
   - Many short workflows also logged runner pickup waits (`cancel_on_pr_close`, `integration_pr_readiness`, `issue_pr_status`).

2. **Compute**
   - `review_autofix` dominates end-to-end compute: 67.2% of all active seconds.
   - CI is second: 25,802 active seconds (15.3%), with `lint` dominating many runs (for example 26272696899, 26274115196, 26274552286).

3. **Retry / wait overhead**
   - `review_autofix` check-run polling adds 3-20 minutes of pure wait on slow runs.
   - The AI-memory claim race caused one hard plan failure.
   - Provider-side context compaction/429 showed up on at least one heavy review run (26250888345).

4. **Merge / conflict overhead**
   - Forward-merge conflict handling is not a big runner-time cost in this window, but it is a human-flow blocker when drift reaches 12 commits and 17 files conflict (run 26275972594).

**Recommended fix order by end-to-end impact**
1. Shorten and back off the `review_autofix` check-run wait loop.
2. Restore small-diff differentiation and shrink editor prompts.
3. Fix AI-memory processed-command claim contention.
4. Prefilter orchestrator-state comments before wrapper dispatch.
5. Track stable→main drift and conflict count to keep forward-merges small.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` consumed 113,167 active seconds (67.2% share).
- CI consumed 25,802 active seconds (15.3% share), mostly in `lint`.
- Wrapper control-plane noise generated 688 skipped runs, but only 965s total.

**Top failure modes**
- Hard failure 1: CI run 26242204999, `lint / Python lint (ruff)`, two `F841` errors in `scripts/verify_integration_fingerprints.py`.
- Hard failure 2: plan run 26268304639, `/answer` command claim failed on `ai-memory` ref-lock contention.

**Highest-cost drivers**
- Oversized `review_autofix` prompts and `xhigh` pass-2 reasoning on tiny diffs.
- Long `check-runs` polling before the editor starts.
- Opaque canceled `review_autofix` runs (20,523s total).

**Top 3 prioritized actions**
1. Reduce `review_autofix` check-run waiting (`CHECK_RUNS_WAIT_TIMEOUT_SECS` + adaptive backoff).
2. Fix AI-memory claim contention and add failure-path telemetry.
3. Re-enable small-diff differentiation (`REVIEWER_PASS2_REASONING_SMALL`) and trim editor prompt caps/deduplication.

## Metrics Appendix

### Overall window metrics

| Repo | Total runs | Success | Failure | Cancelled | Other/skipped | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 288 | 2 | 19 | 691 | 170.634 | 2.0 | 1261.0 |

### Active workflow-family metrics

| Workflow family | Active runs | Success | Failure | Cancelled | Active seconds | Share of active seconds | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 102 | 86 | 0 | 16 | 113167 | 67.2% | 1109.5 | 1147.5 | 3069.2 |
| ci | 25 | 24 | 1 | 0 | 25802 | 15.3% | 1032.1 | 1059.0 | 1116.4 |
| copilot_pull_request_reviewer | 27 | 26 | 0 | 1 | 4915 | 2.9% | 182.0 | 189.0 | 289.6 |
| orchestrate_poll | 25 | 25 | 0 | 0 | 4409 | 2.6% | 176.4 | 136.0 | 477.8 |
| implement | 12 | 10 | 0 | 2 | 3704 | 2.2% | 308.7 | 285.0 | 578.9 |
| plan | 15 | 14 | 1 | 0 | 2997 | 1.8% | 199.8 | 184.0 | 479.8 |
| clarify | 13 | 13 | 0 | 0 | 1548 | 0.9% | 119.1 | 102.0 | 211.4 |
| orchestrate | 2 | 2 | 0 | 0 | 1494 | 0.9% | 747.0 | 747.0 | 1209.6 |

### Wrapper control-plane noise

| Workflow family | Skipped/other runs | Total skipped seconds | Avg skipped duration (s) |
|---|---:|---:|---:|
| clarify | 180 | 238 | 1.32 |
| plan | 166 | 229 | 1.38 |
| implement | 166 | 252 | 1.52 |
| orchestrate_clarify_respond | 176 | 246 | 1.40 |
| **Total** | **688** | **965** | **1.40** |

### Selected prompt and token metrics (`review_autofix`)

| Run ID | Pass1 prompt bytes | Review prompt bytes | Editor prompt bytes | Tokens used | Notable effect |
|---|---:|---:|---:|---:|---|
| 26242205286 | 23594 | 40926 | 590217 | 552811 | context compacted |
| 26250888345 | 15109 | 11176 | 570338 | 514997 | context compacted; provider 429 |
| 26246026439 | 20888 | 19353 | 684542 | n/a | 5 overflow Semble fetches |
| 26271444771 | 18028 | 26851 | 327003 | n/a | 74 LOC diff still used xhigh pass-2 |
| 26226389847 | 15766 | 25296 | 323987 | n/a | 66 LOC diff still used xhigh pass-2 |

### AI memory telemetry

| Metric | Value |
|---|---:|
| Telemetry events observed | 30 |
| Runs with telemetry | 8 |
| `record-run-event` | 15 |
| `record-candidate` | 7 |
| `retrieve` | 7 |
| `processed-command-check` | 1 |
| Retrieve hit rate | 0 / 7 (0%) |
| Avg retrieve `estimated_tokens` | 0 |
| Reviewer token budget configured | 1400 |
| `keyword_method=none` | 7 |
| `keyword_method=plain` | 0 |
| `keyword_method=llm` | 0 |
| `fail_open: true` observed | 0 |
| `enabled: false` observed | 0 |
| Max emitted `push_attempts` | 1 |
| Highest real retry count seen in logs | 5 (run 26268304639, unstructured error path) |

### GH API hotspot summary

| Workflow / step | Run ID | Wait lines observed | Minimum sleep implied | Explicit runtime timeout line | Notes |
|---|---:|---:|---:|---|---|
| review_autofix / Collect PR check-run failures | 26269646416 | 59 | 1180s | yes | timed out after 1200s |
| review_autofix / Collect PR check-run failures | 26242205286 | 47 | 940s | no | long poll loop before review work |
| review_autofix / Collect PR check-run failures | 26246026439 | 47 | 940s | no | long poll loop before review work |
| review_autofix / Collect PR check-run failures | 26248621568 | 46 | 920s | no | long poll loop before review work |
| review_autofix / Collect PR check-run failures | 26271444771 | 40 | 800s | no | long poll loop before review work |
| review_autofix / Collect PR check-run failures | 26226389847 | 9 | 180s | no | still non-trivial wait |

*Supporting secondary evidence:* workflow-log-analysis run 26268682225 estimated `>=176` logical check-run snapshots across five sampled slow `review_autofix` runs.

### Semble / Serena telemetry (strict runtime lines only)

| Server event | Production count | Logged bytes | Response bytes | Notes |
|---|---:|---:|---:|---|
| `SEMBLE_QUERY` | 16 | 191424 | 0 | 7 `reviewer-context` queries = 115052 bytes; 9 `overflow` queries = 76372 bytes |
| `SEMBLE_FALLBACK` | 0 | 0 | 0 | 5 fallback lines existed only in `test_and_mark_stable` run 26268664618 (`missing_semble` fixture) |
| `SERENA_QUERY` | 0 | 0 | 0 | none observed |
| `SERENA_FALLBACK` | 0 | 0 | 0 | none observed |
| `SERENA_PROBE` | 0 | 0 | 0 | none observed |

### MCP availability rows (observed probe telemetry)

| Server | Target | Probe ok | Probe failed | Probe skipped | Notes |
|---|---|---:|---:|---:|---|
| Semble | reviewer-context | 0 | 0 | 0 | no probe telemetry; queries succeeded in 7 sampled runs |
| Semble | overflow | 0 | 0 | 0 | no probe telemetry; 9 overflow query lines, 0 production fallbacks |
| Serena | n/a | 0 | 0 | 0 | no production probe/query/fallback telemetry observed |

**Other MCP servers observed:** none in strict runtime telemetry.

### Prompt cache metrics

| Metric | Observed value | Note |
|---|---:|---|
| `OPENROUTER_PROMPT_CACHE_DISABLED=false` | yes | emitted in sampled plan/review/orchestrate runs |
| `cache_creation_input_tokens` lines | 0 | no usable provider cache-hit/miss telemetry in this window |
| `cache_read_input_tokens` lines | 0 | no usable provider cache-hit/miss telemetry in this window |
| `CACHE_PROBE_OK` lines | 0 | probe path exists in code but was not exercised |
| Existing cache probe support | yes | `scripts/review_run_reviewers.sh:121-165` |


## Deep Audit — Workflows & Scripts (2026-05-22)

### Section 1: Bug & Correctness Sweep

- **ID:** BUG-001  
  **File path:** `.github/workflows/orchestrate.yml:966-980`  
  **Severity:** Medium  
  **Category:** `bug`  
  **Description:** The fallback `_safe_gh_jq() { gh api "$@"; }` at line 969 is not equivalent to the canonical helper in `scripts/gh_helpers.sh:532-545`. The canonical version suppresses stdout on failure because `gh api --jq` can emit raw error JSON instead of filtered output. This step feeds `_safe_gh_jq` directly into `DEFAULT_BRANCH` and `BASE_SHA` command substitutions at lines 971 and 979, so if `scripts/gh_helpers.sh` fails to source, transient API failures can contaminate ref variables with error payloads and break integration-branch creation.  
  **Recommended fix:** Remove the fallback or replace it with the canonical temp-file implementation already used in `scripts/gh_helpers.sh:532-545` and `.github/workflows/implement.yml:4303-4312`.

- **ID:** BUG-002  
  **File path:** `scripts/run_validation_repo_checks.sh:14-22`  
  **Severity:** Low  
  **Category:** `bug`  
  **Description:** The override path turns each positional argument into a full shell program (`CHECK_COMMANDS=("$@")`) and then executes each entry via `/bin/sh -c`. A normal argv-style call such as `scripts/run_validation_repo_checks.sh python3 tests/test_x.py` therefore runs `python3` and `tests/test_x.py` as two separate checks, and any shell metacharacters in an override are evaluated rather than passed literally. That makes the override contract easy to misuse and broader than a normal CLI contract. [NEEDS VERIFICATION]  
  **Recommended fix:** Make the interface explicit: either accept repeated `--command '<shell snippet>'` flags and document that they are shell-evaluated, or accept command + argv and execute them directly with arrays/`timeout` without `/bin/sh -c`.

### Section 2: GitHub API Call Redundancy Audit

- **ID:** API-001  
  **File path:** `scripts/review_rb_judge.sh:450-488`  
  **Severity:** Medium  
  **Category:** `api-batching`  
  **Description:** The judge already does one GraphQL fetch for `closingIssuesReferences` at lines 450-455, then falls back to per-issue REST hydration inside the loop at lines 471-488 to recover issue body and labels. Worst-case, the path costs one GraphQL call plus one REST `repos/.../issues/{n}` call per linked issue until a non-empty body is found.  
  **Current call count:** `1 GraphQL + up to N REST`  
  **Proposed call count:** `1 GraphQL`  
  **Pattern to extend:** `.github/workflows/review_autofix.yml:1600-1618` or `scripts/orchestrate_poll_process.sh:7943-8064`  
  **Recommended fix:** Extend the initial GraphQL selection to request `body` and `labels(first: 100)` and read the first usable node locally.

- **ID:** API-002  
  **File path:** `scripts/orchestrate_poll_process.sh:7850-7898,8459-8466`  
  **Severity:** Medium  
  **Category:** `api-batching`  
  **Description:** `run_standalone_stall_recovery` still performs one `gh issue list --label ...` call per pipeline label at lines 8462-8466, even though the same file already contains an aliased GraphQL search helper for marker discovery at lines 7850-7898. The label list is fixed at seven values, so every standalone recovery cycle pays seven REST list calls before candidate processing even starts.  
  **Current call count:** `7 REST list calls`  
  **Proposed call count:** `1 GraphQL aliased search call`  
  **Pattern to extend:** `scripts/orchestrate_poll_process.sh:_fetch_standalone_marker_issues_graphql`  
  **Recommended fix:** Add a batched helper for pipeline-label searches (for example `_fetch_pipeline_label_issues_graphql <labels_json>`) and have `run_standalone_stall_recovery` consume that merged result instead of looping over `gh issue list`.

- **ID:** API-003  
  **File path:** `.github/workflows/review_autofix.yml:1641-1699`  
  **Severity:** Medium  
  **Category:** `api-batching`  
  **Description:** When `closingIssuesReferences` is empty, the linked-issue context fallback parses issue numbers from the PR body and then hydrates each issue one-by-one with `gh api repos/.../issues/{n}` at lines 1684-1698. The fallback is capped at 20 issues, but it is still a per-item REST loop on the review path.  
  **Current call count:** `1 GraphQL + up to 20 REST`  
  **Proposed call count:** `2 GraphQL calls total`  
  **Pattern to extend:** `scripts/orchestrate_poll_process.sh:_fetch_candidate_issue_details_graphql`  
  **Recommended fix:** Keep the existing body parser, but batch `_fallback_numbers` into one aliased GraphQL query that returns `{number,title,body}` for all parsed issues.

- **ID:** API-004  
  **File path:** `.github/workflows/review_autofix.yml:527-579`  
  **Severity:** Medium  
  **Category:** `api-batching`  
  **Description:** In the post-merge validate dispatch path, the workflow first fetches linked issues with GraphQL at lines 527-532, then falls back to PR-body parsing at lines 534-545, and finally re-fetches labels one issue at a time with `gh issue view` inside the loop at lines 552-559 when `labels_known != true`. That makes fallback-linked issues cost one extra API call each.  
  **Current call count:** `1 GraphQL + 1 PR fetch + up to N issue-view calls`  
  **Proposed call count:** `1 GraphQL + 1 PR fetch + 1 batched GraphQL labels call`  
  **Pattern to extend:** the label shape already used at `.github/workflows/review_autofix.yml:531-532`, or `scripts/orchestrate_poll_process.sh:_fetch_candidate_issue_details_graphql`  
  **Recommended fix:** After fallback number extraction, batch-hydrate labels once before the loop and keep the loop purely local.

### Section 3: Code Duplication & Modularization Opportunities

- **ID:** DUP-001  
  **File path:** `.github/workflows/clarify.yml:214-286; .github/workflows/plan.yml:265-336; .github/workflows/orchestrate.yml:339-425; .github/workflows/orchestrate_poll.yml:287-397; .github/workflows/orchestrate_clarify_respond.yml:257-348; .github/workflows/implement.yml:809-998; .github/workflows/review_autofix.yml:929-1206; .github/workflows/validate.yml:207-583`  
  **Severity:** Medium  
  **Category:** `duplication`  
  **Description:** Eight workflows hand-roll near-identical support-file staging blocks: create support directories, prefer `.codex-workflow-src` then `.codex-workflow-src-main`, install scripts, copy ai-memory schemas, optionally write `scripts/.gitignore`, and stage prompts/JSON assets. The copies have already diverged in meaningful ways (`review_autofix` stages out-of-tree, `validate` clones refs manually, others stage in-tree), so every support-asset change now requires multi-file edits.  
  **Shared module:** `scripts/stage_workflow_support.sh`  
  **Signature:** `stage_workflow_support --workflow <name> --script-ref <ref> --dest-root <dir> --required-script <file>... --optional-script <file>... --prompt <file>... --schema <file>... [--out-of-tree]`  
  **Callers:** `clarify.yml`, `plan.yml`, `orchestrate.yml`, `orchestrate_poll.yml`, `orchestrate_clarify_respond.yml`, `implement.yml`, `review_autofix.yml`, `validate.yml`  
  **Recommended fix:** Extract the shared fetch/install logic into the module above and leave only workflow-specific asset lists and env exports inline.

- **ID:** DUP-002  
  **File path:** `scripts/gh_helpers.sh:391-545; .github/workflows/orchestrate.yml:968-969; .github/workflows/orchestrate_poll.yml:96-113; .github/workflows/mark-stable.yml:356-369,505-518; .github/workflows/cancel_on_pr_close.yml:40-53; .github/workflows/review_autofix.yml:615-624,1518-1539; scripts/label_helpers.sh:18-20; scripts/review_run_reviewers.sh:13-15; scripts/orchestrate_poll_process.sh:122-124; scripts/validate_process.sh:244-246; scripts/review_apply_fixes.sh:16-18; scripts/review_rb_judge.sh:34-36`  
  **Severity:** Medium  
  **Category:** `duplication`  
  **Description:** The canonical `gh_retry`/`_safe_gh_jq` implementations live in `scripts/gh_helpers.sh`, but multiple workflows and scripts still redefine one-line fallbacks or custom retry wrappers. That duplication has already drifted into a correctness bug: `orchestrate.yml`’s local `_safe_gh_jq` fallback is weaker than the canonical helper (BUG-001).  
  **Shared module:** `scripts/gh_bootstrap.sh`  
  **Signature:** `bootstrap_gh_helpers [<support_scripts_dir>]`  
  **Callers:** the workflows/scripts above that currently inline `gh_retry` or `_safe_gh_jq` fallbacks  
  **Recommended fix:** Add a tiny bootstrap helper that sources `gh_helpers.sh` when present and installs the canonical fallbacks otherwise; replace inline wrapper definitions with one bootstrap call.

- **ID:** DUP-003  
  **File path:** `.github/workflows/review_autofix.yml:534-545,1641-1705,4647-4660,4769-4782,5696-5709; scripts/review_rb_judge.sh:457-488`  
  **Severity:** Medium  
  **Category:** `duplication`  
  **Description:** Linked-issue fallback parsing and hydration logic is copied several times: PR-body regex extraction in multiple `review_autofix` branches, linked-issue context hydration in `review_autofix`, and a near-identical fallback in `review_rb_judge.sh`. Any change to closing-keyword parsing, repo escaping, or issue-detail batching has to be patched in several places.  
  **Shared module:** `scripts/linked_issue_helpers.sh`  
  **Signature:** `get_linked_issue_numbers <repo> <pr_meta_file>` and `fetch_linked_issue_details_graphql <repo> <numbers_json> [--include-body] [--include-labels]`  
  **Callers:** `review_autofix.yml`, `scripts/review_rb_judge.sh`  
  **Recommended fix:** Centralize both the regex extraction and the batched hydration helpers, then have each workflow branch call the shared helpers instead of repeating the regex and per-issue fetch logic.

### Section 4: Expression Size Limit Risk Assessment

No workflow exceeded the 800 KB watch threshold; the largest was `.github/workflows/review_autofix.yml` at 359,610 characters. I did not find any `if:` expression near the 15,000-character watch threshold. The only `run:` blocks with `${{ }}` interpolation above 15,000 characters were the three below.

- **ID:** EXPR-001  
  **File path:** `.github/workflows/review_autofix.yml:1498-1887`  
  **Severity:** Medium  
  **Category:** `expression-limit`  
  **Description:** The `Collect PR metadata` step embeds a large interpolated shell block. Estimated current size is about `17,408` characters, leaving roughly `3,592` characters of headroom before GitHub’s 21,000-character template-expression limit. The block already mixes PR fetches, linked-issue context building, body-text fallback parsing, and env export.  
  **Recommended fix:** Extract the step body to `scripts/review_collect_pr_metadata.sh` or split it into smaller steps such as `Fetch PR JSON`, `Build linked issue context`, and `Export metadata`.

- **ID:** EXPR-002  
  **File path:** `.github/workflows/review_autofix.yml:930-1206`  
  **Severity:** Medium  
  **Category:** `expression-limit`  
  **Description:** The `Stage workflow support files` step is also a large interpolated shell block. Estimated current size is about `15,510` characters, leaving roughly `5,490` characters of headroom. Because this block grows whenever support assets are added, it has steady upward pressure.  
  **Recommended fix:** Move the staging logic into a shared script such as `scripts/stage_workflow_support.sh` and keep only minimal env/setup in YAML.

- **ID:** EXPR-003  
  **File path:** `.github/workflows/validate.yml:210-583`  
  **Severity:** Medium  
  **Category:** `expression-limit`  
  **Description:** The `Fetch workflow support files` step in `validate.yml` is estimated at about `17,416` characters, leaving roughly `3,584` characters of headroom. It inlines clone helpers, ref fallback logic, asset copying, and consumer/self-repo branching in one interpolated block.  
  **Recommended fix:** Extract the support-bootstrap logic to `scripts/stage_workflow_support.sh` or split it into separate steps for checkout, copy, and output emission.

### Section 5: Cross-Cutting Concerns

I did not find any `TODO`, `FIXME`, or `HACK` markers under `.github/workflows/*.yml` or `scripts/*.{sh,py}`.

- **ID:** DEAD-001  
  **File path:** `scripts/orchestrate_poll_process.sh:4200-4275,12238-12268,12679-12735; scripts/review_conflict_resolve.sh:342-350`  
  **Severity:** Low  
  **Category:** `dead-code`  
  **Description:** Several variables are assigned but never read. Repo-wide search found no subsequent reads of `BRANCH_REBUILD_SKIP_REASON`, `BRANCH_REBUILD_LAST_REBUILD_AT`, `RB_FOLLOWUP_REFUSED`, `IF_BLOCKERS_SOURCE`, or `RESOLVER_ESCALATION_COMMENT_MARKER`. These writes currently do not affect control flow or emitted telemetry, so they behave as inert state.  
  **Recommended fix:** Either wire these values into logs/outputs/telemetry where callers can consume them, or remove the assignments/constants to reduce misleading state.

- **ID:** SHELL-001  
  **File path:** `scripts/validate_process.sh:230-238`  
  **Severity:** Low  
  **Category:** `shellcheck`  
  **Description:** `local msg="$1$(_tg_link_suffix)"` triggers ShellCheck SC2155. If `_tg_link_suffix` fails, the failure is masked by the declaration/assignment, so the function can continue with a truncated Telegram message instead of surfacing the helper failure.  
  **Recommended fix:** Split the declaration and assignment: declare `msg` first, then assign `msg="$1$(_tg_link_suffix)"`.

- **ID:** SHELL-002  
  **File path:** `scripts/validate_changed_files_syntax.sh:70-74`  
  **Severity:** Low  
  **Category:** `shellcheck`  
  **Description:** The case arm `*.env*` on line 71 already matches both `*.envrc` and `.env*`, so the later patterns on line 73 are unreachable (ShellCheck SC2221/SC2222). That makes the redaction rule harder to reason about and easier to accidentally drift from the comment above it.  
  **Recommended fix:** Remove the unreachable alternatives or tighten the earlier pattern and document the intended precedence if the overlap is deliberate.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 11 | BUG-001, API-001, API-002, API-003, API-004, DUP-001, DUP-002, DUP-003, EXPR-001, EXPR-002, EXPR-003 |
| Low | 4 | BUG-002, DEAD-001, SHELL-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 3 files | Medium |
| Code modularization | 8 workflows + 2 scripts | Large |
| Expression size reduction | 2 workflows + 1 shared script | Medium |
| Medium/Low fixes | 5 files | Small |
