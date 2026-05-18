## Executive Summary

- `review_autofix` is the dominant bottleneck: 117 runs consumed 96,375s of 139,636s total runtime (69.0%), and in the 6 slow full-log successes the `codex-agent` step occupied 98.0-99.5% of wall time (runs 25993100190, 25995213928, 25999064203, 26000043892, 26001518929, 26006168593). **Estimated impact:** very high. **Confidence:** high.
- Cancelled `review_autofix` runs are burning large amounts of time and likely AI spend: 29 cancelled runs consumed 25,152s total (18.0% of all runtime; avg 867s), with examples 25993094478, 26000038446, 26006960947, and 26007850963. **Estimated impact:** high. **Confidence:** high.
- CI failures are one repeated late-discovered regression: 11 of 23 CI runs failed (47.8%), all at `lint -> Validation self-test unit tests`, ending with `AssertionError: implement.yml missing resolved-ref log output`; in run 25993094414 the failing audit started ~658s after job start, after 175 tests had already passed. **Estimated impact:** high. **Confidence:** high.
- Semble is helping on the happy path but overflow retrieval is noisy: 12 observed operational `SEMBLE_QUERY` lines carried 126,594 logged bytes; 6 were normal `reviewer-context` lookups, but run 25993100190 added 6 overflow queries totaling 39,066 bytes. `SERENA_QUERY/FALLBACK/PROBE` was 0 in operational logs. **Estimated impact:** medium. **Confidence:** medium.
- AI memory is instrumented but not effective yet: 6 parsed `retrieve` ops across slow `review_autofix` runs had 0/6 hits, `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`; prompt-token and prompt-cache counters were not emitted in operational logs. **Estimated impact:** medium. **Confidence:** high.

## Speed Optimizations

1. **[Critical path] Add a pre-`codex-agent` freshness guard and a diff-size reviewer ladder to `review_autofix`.**
   - **Evidence:** `review_autofix` consumed 96,375s total. All 10 unique deep-dive `review_autofix` runs showed hosted-runner wait lines. The 6 slow full-log successes spent 98.0-99.5% of wall time inside the `codex-agent` step. PR 2720 alone consumed 8,562s across 7 sampled `review_autofix` runs (`26006168593`, `26006960947`, `26006966699`, `26007458306`, `26007850963`, `26007857846`, `26008679993`), while `AUTOFIX_GATE_DET_SKIP_EVAL` still reported `small_diff=false` and `additions=1377`.
   - **Root cause:** expensive reviewer/editor fan-out is still launching for stale or very large diffs.
   - **Exact change:** before launching reviewers/editor, re-check whether a newer run exists for the same PR/head and exit neutral if stale; when `small_diff=false` or additions are far above the gate thresholds, drop from the 6-reviewer panel to a smaller ladder and escalate only on disagreement or retry.
   - **Estimated time savings:** eliminate much of the 25,152s cancelled-run waste and likely shave hundreds to >1,000s from large successful runs.
   - **Implementation risk:** medium; mitigate by keeping full fan-out on escalation.

2. **[Critical path] Move `tests/test_workflow_checkout_integration_ref_audit.py` to the front of CI.**
   - **Evidence:** all 11 failing CI runs ended with the same `AssertionError: implement.yml missing resolved-ref log output`. In run `25993094414`, `175 passed, 0 failed` was logged at `14:19:00Z`, `python3 tests/test_workflow_checkout_integration_ref_audit.py` started at `14:19:01Z`, and the assertion failed at `14:19:04Z`—about 662s after job start.
   - **Root cause:** a cheap workflow-contract audit runs after a large self-test sweep.
   - **Exact change:** make the checkout-integration audit a preflight job or run it before the broader test batch.
   - **Estimated time savings:** ~550-660s per failing CI run; 6,910s across the sampled failures.
   - **Implementation risk:** low.

3. **[Critical path] Reduce `check-runs` polling wall time in `review_autofix`.**
   - **Evidence:** recent `review_autofix` runs `26006960947` and `26006966699` logged `CHECK_RUNS_WAIT_TIMEOUT_SECS: 120` and `CHECK_RUNS_POLL_INTERVAL_SECS: 30`. Slow run `25995213928` executed `gh_retry gh api --paginate --slurp "repos/.../commits/${HEAD_SHA}/check-runs?per_page=100"` inside a `while :; do` loop.
   - **Root cause:** repeated polling of the same HEAD SHA before review proceeds.
   - **Exact change:** cache the latest successful `check-runs` snapshot per run, shorten the wait budget on reruns, and snapshot immediately once only non-blocking checks remain.
   - **Estimated time savings:** inference: ~60-120s on runs that currently spend one or more poll intervals waiting.
   - **Implementation risk:** low if fail-open semantics remain unchanged.

4. **[Micro-optimization] Tighten control-plane fan-out triggers, but do not prioritize this over `review_autofix` and CI.**
   - **Evidence:** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` produced 754 skipped runs out of 782 total (96.4%), but only 831s of skipped duration combined.
   - **Root cause:** eager reusable-workflow dispatch on comments/events that immediately evaluate false.
   - **Exact change:** narrow trigger predicates earlier so obvious no-op events do not enter reusable workflows.
   - **Estimated time savings:** low; this is mainly operator-noise cleanup.
   - **Implementation risk:** low.

## Cost Optimizations

1. **Reduce reviewer fan-out on large-diff and repeated-head `review_autofix` runs.**
   - **Evidence:** PR 2720 review runs (`26006168593`, `26006960947`, `26006966699`, `26007850963`, `26007857846`) logged 6 `REVIEWER_MODELS` plus `MODEL_EDITOR: openai/gpt-5.4` and `XPOLL_SUMMARISER_MODEL: openai/gpt-5.4-mini`, while `AUTOFIX_GATE_DET_SKIP_EVAL` showed `files=7 additions=1377 ... small_diff=false skip=false`.
   - **Root cause:** full expensive model panel is used even when the diff is already outside “small diff” territory.
   - **Exact change:** use a cheaper default ladder for `small_diff=false` or repeated same-PR reruns; escalate to the full panel only if the cheaper pass disagrees or fails.
   - **Estimated savings:** inference: reviewer-side model invocations drop by roughly two-thirds on affected runs; likely large cost savings on the heaviest PRs.
   - **Quality-risk notes:** medium; preserve escalation path.

2. **Stop paying for cancelled reruns.**
   - **Evidence:** 29 cancelled `review_autofix` runs consumed 25,152s. Example pairs: `25993094478` cancelled 2574s then `25993100190` success 2569s; `26000038446` cancelled 2275s then `26000043892` success 2270s; `26006960947` cancelled 1216s then `26006966699` success 1209s.
   - **Root cause:** stale runs continue long after they have been superseded.
   - **Exact change:** add a cheap freshness check before reviewer/editor launch and after runner acquisition.
   - **Estimated savings:** very high; this is the clearest avoidable spend proxy in the window.
   - **Quality-risk notes:** low.

3. **Tighten Semble targeting to reduce overflow context spill.**
   - **Evidence:** observed operational `SEMBLE_QUERY` totals were 12 queries / 126,594 bytes. The happy-path shape was 6 `reviewer-context` queries totaling 87,528 bytes. But run `25993100190` added 6 overflow queries totaling 39,066 bytes across `.github/workflows/plan.yml`, `scripts/orchestrate_poll_process.sh`, `scripts/review_apply_fixes.sh`, `scripts/review_rb_judge.sh`, `scripts/review_run_reviewers.sh`, and `scripts/validate_process.sh`.
   - **Root cause:** initial retrieval sometimes misses the most relevant files, forcing late spillover.
   - **Exact change:** seed Semble with changed files and failing-check file hints first, cap overflow to top-K unresolved files, and dedupe files already represented in the initial hit set.
   - **Estimated savings:** medium; in the observed overflow case, ~39 KB of extra retrieval context could likely be avoided.
   - **Quality-risk notes:** low-medium.
   - **Semble verdict:** inference: Semble is probably reducing some raw prompt expansion on the happy path, because 5 of the 6 successful sampled review runs only needed one `reviewer-context` query. The overflow burst shows the targeting degrades on harder cases.
   - **Serena verdict:** no operational `SERENA_QUERY/FALLBACK/PROBE` lines were observed, so Serena is not currently replacing any downstream tool/model work, and no Serena response-byte overhead is measurable.

4. **Emit raw token and prompt-cache counters before doing deeper model/cost tuning.**
   - **Evidence:** 0 operational occurrences of `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens`. The only cache signal found was a normal Actions cache hit in plan run `26005766206` (`codex-v0.114.0-v2`), which is not prompt-cache telemetry.
   - **Root cause:** missing observability prevents precise cost optimization.
   - **Exact change:** emit token and prompt-cache create/read counters per workflow family and model.
   - **Estimated savings:** unquantifiable today, but this is prerequisite instrumentation for confident dollar savings.
   - **Quality-risk notes:** low.

## Reliability Improvements

1. **Break the repeated CI workflow-contract regression loop.**
   - **Failure evidence:** all 11 CI failures (`25993094414` through `25997884756`) failed at `lint -> Validation self-test unit tests` with `AssertionError: implement.yml missing resolved-ref log output`.
   - **Root cause category:** workflow contract drift or stale PR merge ref (inference).
   - **Exact fix:** keep the audit, but require PR branch refresh before rerunning stale workflow-file failures, auto-cancel obsolete CI runs on synchronize, and run this audit first.
   - **Expected reliability impact:** should remove the sampled 47.8% CI failure rate if reruns stop hitting stale refs.
   - **Rollback/fail-open:** audit is read-only; manual rerun path stays available.
   - **Important caveat:** the current workspace already contains the expected line at `.github/workflows/implement.yml:719`, and the contract test expects it at `tests/test_workflow_checkout_integration_ref_audit.py:88`, so the repeated failures may reflect stale branch state rather than a still-broken default branch.

2. **Prevent long `review_autofix` cancellations before they consume minutes.**
   - **Failure evidence:** cancelled `review_autofix` runs include `25993094478` (2574s), `26000038446` (2275s), `26006960947` (1216s), and `26007850963` (1969s), often without a failing step.
   - **Root cause category:** concurrency / rerun churn.
   - **Exact fix:** add stale-run detection before `codex-agent` starts and after runner wait; group concurrency by PR/head.
   - **Expected reliability impact:** lower rerun rate and fewer abandoned long-running runs.
   - **Rollback/fail-open:** only stale runs exit neutral.

3. **Keep Semble fail-open, but separate self-test fallbacks from production incidents.**
   - **Failure evidence:** 5 `SEMBLE_FALLBACK` lines were observed, all in `test_and_mark_stable` run `25996282698`, step `validate-scripts`, target `overflow`, with `missing_semble` reasons on `src/big.py`/`src/small.py`. No operational `SERENA_FALLBACK` or `SERENA_PROBE` lines were observed. Recent `orchestrate_poll` run `26008453323` succeeded in 134s despite `SEMBLE_AVAILABLE: false` and `SEMBLE_INDEX_AVAILABLE: false`.
   - **Root cause category:** self-test fallback coverage and isolated availability miss, not a masked production rollout failure.
   - **Exact fix:** alert only when `SEMBLE_FALLBACK` appears outside `test_and_mark_stable`, or when `SEMBLE_AVAILABLE: false` appears on critical `review_autofix` runs.
   - **Expected reliability impact:** better operational signal without blocking reviews.
   - **Rollback/fail-open:** preserve current fail-open behavior.
   - **Probe vs runtime distinction:** no operational `*_PROBE` lines were emitted, so the observed `26008453323` Semble availability miss is an availability flag, not a runtime fallback.

4. **Remove deprecation and artifact-warning noise before it becomes a real outage.**
   - **Failure evidence:** `Node.js 20 is deprecated` appeared in all 6 slow sampled `review_autofix` successes and in recent runs `26006966699`, `26005766206`, and `26005951398`. Copilot review run `26008674593` logged `Buffer()` deprecation warnings plus `digest-mismatch: error` even though the downloaded SHA matched the expected digest.
   - **Root cause category:** aging action/tool versions and noisy artifact validation.
   - **Exact fix:** move to Node 24-compatible action versions and suppress/reclassify the digest warning path when the post-download SHA matches the expected digest.
   - **Expected reliability impact:** fewer false alarms and lower upstream-breakage risk.
   - **Rollback/fail-open:** version pins are reversible; keep SHA verification strict.

5. **Make AI memory telemetry consistently machine-readable.**
   - **Failure evidence:** recent skipped run `26008492927` logged malformed `AI_MEMORY_TELEMETRY` fragments instead of JSON; recent runs `26008453323` and `26006960947` explicitly reported no AI memory telemetry.
   - **Root cause category:** observability / emitter inconsistency.
   - **Exact fix:** emit JSON-only `AI_MEMORY_TELEMETRY:` lines on stderr on success, cancel, and skip paths; test the formatter.
   - **Expected reliability impact:** improves diagnosis and safer memory rollouts.
   - **Rollback/fail-open:** telemetry-only change.

## AI Memory Health

- **Observed coverage:** parsed operational JSON `AI_MEMORY_TELEMETRY` appeared in 6 slow `review_autofix` runs: `25993100190`, `25995213928`, `25999064203`, `26000043892`, `26001518929`, and `26006168593`.
- **Observed ops:** 24 JSON events total = 12 `record-run-event`, 6 `retrieve`, 6 `record-candidate`.
- **Retrieve effectiveness:** `records_selected > 0` in **0 of 6** retrieves (**0.0% hit rate**). Average `estimated_tokens` was **0**. No budget field was emitted, so budget utilization cannot be computed.
- **Keyword method distribution:** `none=6`, `plain=0`, `llm=0`.
- **Flags:** `enabled=false` **0** times; `fail_open=true` **0** times; `push_attempts > 1` **0** times.
- **Pattern:** the system is writing memory (`record-candidate`, `record-run-event`) but not retrieving anything useful.
- **Coverage gaps:** no AI memory telemetry was reported in recent `orchestrate_poll` run `26008453323` or recent cancelled `review_autofix` run `26006960947`; skipped run `26008492927` produced malformed telemetry.
- **Recommendation:** fix retrieval triggering/keyword extraction first, then expand memory writes. Right now memory is observable but operationally inert.

## GH API Call Audit

The repo already treats API reuse as a contract: `CLAUDE.md §15` requires extending existing calls before adding new ones, and `README.md:1365-1369` says API hygiene should prefer batched calls and cycle-local caches. The sampled workflows still have a few reuse gaps.

1. **`review_autofix` gate is doing repeated PR-scope reads.**
   - **Evidence:** run `26008679993`, step `gate / Evaluate review gate`, executed:
     - `gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" ...`
     - `gh api "repos/${REPOSITORY}/commits/${PR_HEAD_SHA}" ...`
     - `gh api --paginate "repos/${REPOSITORY}/pulls/${PR_NUMBER}/files" ...`
   - **Redundancy:** slow run `25995213928`, step `review_codex-agent`, later re-fetched PR state with `pr_state="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.state' ...)"`
   - **Recommendation:** persist PR JSON/head metadata from gate to later steps instead of re-querying.
   - **Estimated call-count reduction:** inference: at least 117 repeated PR-state calls saved across 117 `review_autofix` runs; likely more if other PR fields are reused.

2. **`review_autofix` `check-runs` polling is the main API hot loop.**
   - **Evidence:** run `25995213928`, `review_codex-agent`, executed `gh_retry gh api --paginate --slurp "repos/shubhodeep1/coding-workflows/commits/${HEAD_SHA}/check-runs?per_page=100"` inside a `while :; do` loop. `README.md:65-67` explicitly notes each poll iteration costs at least one underlying API request and current runs `26006960947` / `26006966699` logged `120s` timeout / `30s` interval.
   - **Redundancy risk:** same HEAD SHA can be re-snapshotted several times in one run.
   - **Recommendation:** cache the latest snapshot for unchanged HEAD SHA, poll less aggressively on reruns, and exit early once required checks settle.
   - **Estimated call-count reduction:** inference: 50-75% of `check-runs` traffic on runs that currently wait multiple intervals.

3. **Copilot review has a smaller but cleanable GH API bundle.**
   - **Evidence:** recent run `26008674593` used GH API for:
     - PR diff fetch (`/pulls/${PR_NUMBER}` with diff accept header)
     - base SHA fetch (`/pulls/${PR_NUMBER} --jq '.base.sha'`)
     - artifact list (`/actions/runs/26008674593/artifacts`)
     - artifact delete (`/actions/artifacts/"$artifact_id"`)
   - **Recommendation:** reuse PR JSON between diff/base-SHA steps and skip cleanup delete when artifact enumeration is empty.
   - **Estimated call-count reduction:** ~1-2 calls per run.

4. **Rate-limit posture is currently okay; redundancy is the actual issue.**
   - **Evidence:** I did not find actual operational secondary-rate-limit or 429 events in the sampled production logs/summaries. The repo’s retry/backoff code is present, but not triggered in the reviewed window.
   - **Recommendation:** focus first on duplicate reads and poll-loop density, not more retry logic.

## Prompt Cache & Memory System

- **Prompt-cache telemetry is missing.** No operational logs emitted prompt token counts or prompt-cache create/read counters, so hit/miss behavior is unknown. The `codex-v0.114.0-v2` hit in plan run `26005766206` is an Actions cache hit, not model prompt-cache data.
- **Most likely cache-fragmenters (inference):** repeated `review_autofix` reruns on the same PR/head, dynamic `check-runs` snapshots, and Semble overflow file lists. The clearest example is PR 2720, which re-entered `review_autofix` repeatedly, plus run `25993100190`, which expanded from one `reviewer-context` query to six overflow file fetches.
- **Concrete improvement:** keep the stable system/repo prefix fixed and canonicalized, then append volatile blocks (live checks, timestamps, overflow file excerpts) at the tail of the prompt. That is the safest layout for future prompt-cache reuse.
- **Memory retrieval is not helping yet.** The current memory system writes `record-candidate` entries but retrieves 0 useful records in sampled runs. Fix retrieval quality before increasing memory write volume.
- **Expected impact:** token/latency savings cannot be quantified until counters are emitted; likely best gains are on repeated same-PR `review_autofix` runs.

## Orchestrator Health

- **Control-plane fan-out is noisy, not slow.** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` accounted for 782 runs, but 754 were skipped and those skips consumed only 831s total. Recent examples: `26008672240` (`plan`) skipped on `/answer` false, `26008672233` (`implement`) skipped on `/approved` false, `26008672223` (`respond`) skipped on false.
- **Poller fail-open behavior looks healthy.** Recent `orchestrate_poll` run `26008453323` succeeded in 134s even with `SEMBLE_AVAILABLE: false` and `SEMBLE_INDEX_AVAILABLE: false`. I did not see evidence of stuck terminal states in the sampled window.
- **The real orchestration pain is repeated entry into `review_autofix`.** PR 2720 alone consumed 8,562s across 7 sampled review/autofix runs. That is a far larger operational problem than clarify/respond skip noise.
- **Track these indicators going forward:** long-cancelled `review_autofix` count and duration, repeated runs per PR/head, hosted-runner wait incidence on `review_autofix`, Semble overflow bytes per run, and AI memory retrieve hit rate.

## Pipeline Flow Bottlenecks

| Stage | Evidence | Bottleneck type | End-to-end impact | Recommended fix |
|---|---|---|---|---|
| Clarify / Plan / Implement / Respond dispatch | 754 skipped runs out of 782; only 831s skipped duration | Control-plane noise | Low | Tighten no-op triggers later; not urgent |
| CI validation | 11 identical failures found ~658s into run `25993094414`; 11/23 CI runs failed | Late compute waste | High | Run workflow-contract audit first |
| `review_autofix` gate | All 10 deep-dive `review_autofix` runs showed hosted-runner waits; gate also does 3 GH API reads/run | Queueing + setup | High | Cancel stale runs before runner/agent launch; reuse gate payload |
| `review_autofix` `codex-agent` | 98.0-99.5% of slow-run time in `codex-agent`; 6 reviewer models + GPT-5.4 editor on large diffs | AI/compute critical path | Very high | Diff-size reviewer ladder + supersession guard |
| `review_autofix` `check-runs` wait | Looping `check-runs` snapshot on same HEAD SHA; `120s` timeout / `30s` interval seen in recent runs | Retry/poll overhead | Medium | Cache snapshot and shorten waits |
| Merge/conflict overhead | No major repo-local merge/conflict failure pattern surfaced in this window | Merge/conflict | Low | Monitor, but do not prioritize |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix`: 117 runs, 96,375s total runtime, p95 2,067s, 69.0% of all runtime.
  - `ci`: 23 runs, 16,116s total runtime, 47.8% failure rate.
  - `orchestrate_poll`: 26 runs, 4,385s total runtime; useful to watch, but not the top sink.

- **Top failure modes**
  - Repeated CI contract failure on `implement.yml` resolved-ref logging (`25993094414` through `25997884756`).
  - Long cancelled `review_autofix` reruns with no explicit failing step (`25993094478`, `26000038446`, `26006960947`, `26007850963`).
  - Partial observability: Semble availability miss on `26008453323`, malformed AI memory telemetry on `26008492927`, no prompt-token/cache counters anywhere operationally.

- **Highest-cost drivers**
  - 6-reviewer `review_autofix` fan-out plus `MODEL_EDITOR: openai/gpt-5.4`.
  - Reprocessing the same PR/head repeatedly, especially PR 2720.
  - Semble overflow spill in hard review runs.

- **Top 3 prioritized actions**
  1. Add stale-run freshness checks and a diff-size reviewer ladder to `review_autofix`.
  2. Move `test_workflow_checkout_integration_ref_audit.py` to a CI preflight step and require branch refresh before rerunning stale workflow-file failures.
  3. Emit prompt-token/prompt-cache counters and fix AI memory retrieval quality before doing deeper model-cost tuning.

## Metrics Appendix

### Overall window

| Scope | Runs | Success | Failure | Cancelled | Other | Success % | Failure % | Avg s | p50 s | p95 s | Total runtime s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 202 | 11 | 31 | 756 | 20.2 | 1.1 | 139.6 | 1.0 | 1216.0 | 139636 |

### Key workflow-family metrics

| Family | Runs | Success | Failure | Cancelled | Other | Success % | Failure % | Avg s | p50 s | p95 s | Total s | Runtime share % |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 117 | 87 | 0 | 29 | 1 | 74.4 | 0.0 | 823.7 | 669.0 | 2067.0 | 96375 | 69.0 |
| ci | 23 | 12 | 11 | 0 | 0 | 52.2 | 47.8 | 700.7 | 676.0 | 800.3 | 16116 | 11.5 |
| plan | 193 | 8 | 0 | 0 | 185 | 4.1 | 0.0 | 15.4 | 1.0 | 7.8 | 2969 | 2.1 |
| implement | 192 | 8 | 0 | 2 | 182 | 4.2 | 0.0 | 21.4 | 1.0 | 7.4 | 4116 | 2.9 |
| clarify | 204 | 9 | 0 | 0 | 195 | 4.4 | 0.0 | 5.9 | 1.0 | 9.0 | 1208 | 0.9 |
| orchestrate_clarify_respond | 193 | 1 | 0 | 0 | 192 | 0.5 | 0.0 | 1.1 | 1.0 | 2.0 | 207 | 0.1 |
| orchestrate_poll | 26 | 26 | 0 | 0 | 0 | 100.0 | 0.0 | 168.7 | 143.0 | 391.8 | 4385 | 3.1 |
| copilot_pull_request_reviewer | 24 | 24 | 0 | 0 | 0 | 100.0 | 0.0 | 163.3 | 149.5 | 299.3 | 3920 | 2.8 |

> Single-run maintenance outliers: `test_and_mark_stable` = 4,640s once; `workflow_log_analysis` = 4,570s once.

### Prompt/token/cache observability

| Metric | Observed value |
|---|---|
| `prompt_tokens` | not emitted in operational deep-dive logs |
| `completion_tokens` | not emitted |
| `total_tokens` | not emitted |
| `cache_creation_input_tokens` | not emitted |
| `cache_read_input_tokens` | not emitted |
| Non-prompt cache signal | plan run `26005766206` logged Actions cache hit `codex-v0.114.0-v2` |

### Semble / Serena / Other MCP telemetry (operational deep-dive logs only)

| Server | Query count | Query bytes | Avg bytes/query | Fallback count | Probe count | Response bytes | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| Semble | 12 | 126594 | 10549.5 | 5 | 0 | n/a | 6 `reviewer-context` queries + 6 `overflow` queries |
| Serena | 0 | 0 | 0 | 0 | 0 | 0 | Disabled / not observed operationally |
| Other MCP servers observed | 0 | 0 | 0 | 0 | 0 | 0 | None |

### Semble target breakdown

| Target | Queries | Bytes | Avg bytes/query | Runs |
|---|---:|---:|---:|---|
| reviewer-context | 6 | 87528 | 14588.0 | `25993100190`, `25995213928`, `25999064203`, `26000043892`, `26001518929`, `26006168593` |
| overflow | 6 | 39066 | 6511.0 | all in `25993100190` |

### MCP availability / probe rows

| Server | Target | probe_ok | probe_failed | probe_skipped | Evidence |
|---|---|---:|---:|---:|---|
| Semble | reviewer-context / overflow | 0 | 0 | 0 | No operational `SEMBLE_PROBE` lines emitted; recent run `26008453323` only showed non-probe availability flags (`SEMBLE_AVAILABLE: false`, `SEMBLE_INDEX_AVAILABLE: false`) |
| Serena | any | 0 | 0 | 0 | No operational `SERENA_PROBE` lines emitted; `SERENA_ENABLED: false` seen in `review_autofix` runs |
| Other MCP servers | none | 0 | 0 | 0 | No other `*_PROBE` lines observed |

### AI memory metrics

| Metric | Value |
|---|---|
| Parsed operational JSON events | 24 |
| Runs with parsed JSON telemetry | `25993100190`, `25995213928`, `25999064203`, `26000043892`, `26001518929`, `26006168593` |
| `record-run-event` ops | 12 |
| `retrieve` ops | 6 |
| `record-candidate` ops | 6 |
| Retrieve hit rate | 0/6 = 0.0% |
| Avg `estimated_tokens` on retrieve | 0 |
| Retrieve budget field | not emitted |
| `keyword_method` distribution | `none=6`, `plain=0`, `llm=0` |
| `enabled=false` retrieves | 0 |
| `fail_open=true` retrieves | 0 |
| Push attempts > 1 | 0 |
| Malformed memory telemetry | recent run `26008492927` log_summary |

### GH API summary

| Workflow / job / step | Evidence | Observed pattern | Estimated reduction |
|---|---|---|---|
| `review_autofix` / `gate` / `Evaluate review gate` | run `26008679993` | 3 GH API reads before skip decision: PR metadata, head commit metadata, paginated PR files | reuse payload across later steps; inference: at least 117 repeated PR-state calls/window removed |
| `review_autofix` / `review_codex-agent` | run `25995213928` | repeated PR state fetch plus looped paginated `check-runs` snapshots | inference: 50-75% of `check-runs` traffic on waiting runs |
| `copilot_pull_request_reviewer` / `Agent` + `Cleanup artifacts` | run `26008674593` | diff fetch, base SHA lookup, artifact list, artifact delete | ~1-2 calls/run if PR payload and empty-cleanup paths are reused |

### Selected run-specific outliers

| Run | Workflow family | Conclusion | Duration s | Notable signal |
|---|---|---:|---:|---|
| `25993094414` | ci | failure | 668 | late contract-audit failure after 175 passing tests |
| `25993100190` | review_autofix | success | 2569 | 1 reviewer-context Semble query + 6 overflow queries |
| `26006168593` | review_autofix | success | 2171 | large-diff PR 2720, 6 reviewer models, Semble available, Serena unavailable |
| `26006960947` | review_autofix | cancelled | 1216 | runner wait + 6-model review config on PR 2720 |
| `26006966699` | review_autofix | success | 1209 | `codex-agent` dominated runtime; Node 20 deprecation warnings |
| `26008453323` | orchestrate_poll | success | 134 | Semble enabled but unavailable; healthy fail-open |
| `26008674593` | copilot_pull_request_reviewer | success | 81 | artifact digest warning noise + GH API cleanup calls |

## Deep Audit — Workflows & Scripts (2026-05-18)

### Section 1: Bug & Correctness Sweep

#### BUG-001
- **File path** — `scripts/review_rb_judge.sh:246-256`; `.github/workflows/review_autofix.yml:4528-4548`
- **Severity** — High
- **Category tag** — bug
- **Description** — Both fallback paths accept broad body/title matches like bare `issues/123` and `issue #123` when `closingIssuesReferences` is empty. `issue_pr_status.yml:196-210` explicitly documents that these broad forms already caused false-positive issue transitions (`#1469`) and therefore tightens its fallback to closing keywords or repo-scoped URLs only. Here, the parsed `ISSUE_NUMBERS` then drive label/state mutation: `review_rb_judge.sh` uses them for `_resilient_phase_swap` and close/reissue actions, and `review_autofix.yml` uses them to set `ai:ready-to-merge`.
- **Recommended fix** — Extract one shared resolver (for example `resolve_closing_issue_numbers`) into `scripts/gh_helpers.sh` and make these call sites reuse the stricter `issue_pr_status.yml:196-210` rule set: GraphQL first, then fallback only for repo-scoped URLs and explicit closing keywords.

#### CONSIST-001
- **File path** — `scripts/label_helpers.sh:146-196`; `.github/workflows/review_autofix.yml:4489-4523`; `.github/workflows/issue_pr_status.yml:240-249`
- **Severity** — Medium
- **Category tag** — consistency
- **Description** — The canonical `set_issue_phase_label_resilient` helper removes old phase labels, computes the replacement set, and `PUT`s the final label list. Both workflow-local fallbacks only `POST` the new label. In `review_autofix.yml:4487-4488`, that fallback is explicitly reachable if staged helper artifacts disappear late in the job, which can leave contradictory phase labels such as `ai:review-blocked` and `ai:ready-to-merge` on the same issue.
- **Recommended fix** — Replace the inline fallbacks with a minimal shared helper that preserves the canonical GET/replace/PUT behavior from `scripts/label_helpers.sh:146-196`, or copy that helper into a protected runtime path that late cleanup steps do not remove.

### Section 2: GitHub API Call Redundancy Audit

#### API-001
- **File path** — `scripts/review_rb_judge.sh:221-223,254-256,305-323,666-667,732-746,792-795,1002-1024`
- **Severity** — Medium
- **Category tag** — api-redundancy
- **Description** — `review_rb_judge.sh` re-fetches the same PR payload repeatedly: early closed/merged guard, fallback title/body lookup, merged guard, mergeability polling, exhausted-fix merge, and merge-with-followup polling. The file already builds `PRELOADED_PR_META` and already calls `gh_pr_with_all_comments`, so most later reads are against data it already had.
  - **Current call count** — 4 fixed PR metadata reads on the common path, +1 optional fallback read at `254-256`, + up to 6 more reads in each mergeability loop (`PR_MERGEABLE_POLL_ATTEMPTS` defaults to 6 at `732-735` and `1002-1004`), + 1 diff fetch.
  - **Proposed call count after fix** — 1 initial PR metadata fetch + 1 conditional freshness refresh + 1 diff fetch on the common path.
  - **Existing batching/prefetch pattern to extend** — `scripts/gh_helpers.sh:735-860` (`gh_pr_with_all_comments`) plus the existing `PRELOADED_PR_META` handoff already built at `307-317`.
- **Recommended fix** — Persist one canonical PR JSON blob before branching, reuse it for title/body/state/head-sha lookups, and only refresh once where a fresh merged/head check is semantically required.

#### API-002
- **File path** — `.github/workflows/issue_pr_status.yml:280-330,503-513`
- **Severity** — Medium
- **Category tag** — api-redundancy
- **Description** — The workflow already classifies linked issues with one aliased GraphQL batch (`295-320`), then later re-fetches linked issue bodies one-by-one in the merged-alert step just to rediscover the same orchestrator marker.
  - **Current call count** — 1 batched GraphQL request + `N` extra REST `issues/{n}` reads, where `N` is the number of linked issues.
  - **Proposed call count after fix** — 1 batched request + 0 extra REST reads.
  - **Existing batching/prefetch pattern to extend** — The existing aliased GraphQL classification block in the same workflow.
- **Recommended fix** — Export `TRACKING_ISSUES`, `MANAGED_ISSUES`, or a boolean like `HAS_ORCHESTRATED_LINKED_ISSUE` from the earlier step via `GITHUB_ENV` or step outputs and reuse it in the alert step.

#### BATCH-001
- **File path** — `scripts/orchestrate_poll_process.sh:8818-8844,9123-9163,10715-10739,10855-10871`
- **Severity** — High
- **Category tag** — api-batching
- **Description** — The poller already has cycle-local batch helpers (`_fetch_issue_labels_batch_graphql`, `_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`), but hot loops still do per-issue REST reads for labels/state/blocker status. That directly conflicts with `CLAUDE.md:415-445` and `agents.md:113-126`, which treat inner-loop per-item API calls as a blocker when batch caches exist.
  - **Current call count** — `(#prior-wave cache misses) + (#current-wave state misses) + (#current-wave issues after review-blocked handling) + (#reissued issues) + (#implementation blockers)` extra REST requests per poll cycle.
  - **Proposed call count after fix** — `ceil(label_refresh_issue_count/25) + ceil(blocker_count/25)` GraphQL calls on mutation paths, and 0 extra calls on cache hits.
  - **Existing batching/prefetch pattern to extend** — `scripts/orchestrate_poll_process.sh:1519-1565`, `6398-6515`, and `6535-6565+`.
- **Recommended fix** — After review-blocked mutations, rebuild `LABELS_JSON` with `_fetch_issue_labels_batch_graphql` over the union of current and reissued issues; batch blocker states through `_fetch_candidate_issue_details_graphql` or a lighter `state-only` variant; keep caches live instead of repopulating them one issue at a time.

#### API-003
- **File path** — `.github/workflows/review_autofix.yml:1473-1567`
- **Severity** — Medium
- **Category tag** — api-redundancy
- **Description** — The “Collect PR metadata” step performs four separate PR-scope REST calls (`/pulls/{n}`, `/issues/{n}/comments`, `/pulls/{n}/reviews`, `/pulls/{n}/comments`) and then reconstructs a consolidated context file locally. The repo already has `gh_pr_with_all_comments` for this shape, but this step bypasses it.
  - **Current call count** — 4 REST calls for PR context hydration + 1 diff fetch + 1 linked-issues GraphQL call per run.
  - **Proposed call count after fix** — 1 consolidated PR-context call + 1 diff fetch + 1 linked-issues GraphQL call.
  - **Existing batching/prefetch pattern to extend** — `scripts/gh_helpers.sh:735-860` (`gh_pr_with_all_comments`).
- **Recommended fix** — Source `scripts/gh_helpers.sh`, extend `gh_pr_with_all_comments` to emit review-state/body fields if needed, and consume that single payload here instead of rebuilding PR context from four separate calls.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **File path** — `.github/workflows/clarify.yml:75-128`; `.github/workflows/plan.yml:126-182`; `.github/workflows/implement.yml:285-338`; `.github/workflows/validate.yml:99-152`; `.github/workflows/orchestrate_clarify_respond.yml:110-163`
- **Severity** — Medium
- **Category tag** — duplication
- **Description** — The same resolver bootstrap is copied across five workflows: stage root creation, auth-header setup, `resolver_git()`, cleanup trap, clone/fetch/checkout, and `bash "${resolver_script}"`. This is the kind of drift surface that forces separate checkout-integration audits.
- **Recommended fix** — Move the staging logic into a shared module such as `scripts/fetch_resolve_integration_ref.sh` with a signature like `fetch_resolve_integration_ref <resolver_repo> <resolver_ref> <issue_number> <output_file>`. Keep `scripts/resolve_integration_ref.sh` as the inner resolver, and have the five workflows call only the wrapper.

#### DUP-002
- **File path** — `.github/workflows/validate.yml:207-583`; `.github/workflows/issue_pr_status.yml:41-173`; `.github/workflows/validation-improvements-intake.yml:48-140`; `.github/workflows/plan.yml:215-335`; `.github/workflows/review_autofix.yml:866-1010`; `.github/workflows/orchestrate_poll.yml:234-340`
- **Severity** — Medium
- **Category tag** — duplication
- **Description** — The repo repeats large support-bootstrap blocks: resolve support ref, clone staged support trees, copy scripts/prompts/schemas, fall back to `main`, and emit `.gitignore`/runtime metadata. The implementations differ slightly per workflow, so fixes have to be propagated manually.
- **Recommended fix** — Extract the shared bootstrap into a module such as `scripts/fetch_workflow_support.sh` with a signature like `fetch_workflow_support <wf_source> <script_ref> <dest_root> <required_csv> [optional_csv]`. Callers to update: at least `validate.yml`, `issue_pr_status.yml`, `validation-improvements-intake.yml`, `plan.yml`, `review_autofix.yml`, and `orchestrate_poll.yml`.

### Section 4: Expression Size Limit Risk Assessment

Measured below as dedented `run:` body length for blocks that contain at least one `${{ }}` interpolation. No `if:` expression exceeded the audit threshold. No workflow exceeded 800 KB; the largest is `review_autofix.yml` at 345,174 bytes.

#### EXPR-001
- **File path** — `.github/workflows/test-and-mark-stable.yml:1203-1587`
- **Severity** — High
- **Category tag** — expression-limit
- **Description** — The “Phase 4: Wait for review & autofix to complete” shell block is already near the hard parser ceiling.
  - **Estimated current character count** — ~19,900
  - **Headroom remaining** — ~1,100
- **Recommended fix** — Extract the wait loop into an external script such as `scripts/e2e_wait_review.sh` and pass only the few dynamic values as env vars.

#### EXPR-002
- **File path** — `.github/workflows/test-and-mark-stable.yml:1673-2078`
- **Severity** — Medium
- **Category tag** — expression-limit
- **Description** — The “Verify editor restored canary” block includes retry dispatch, polling, pytest classification, and multiple `${{ }}` interpolations in one inline shell body.
  - **Estimated current character count** — ~17,409
  - **Headroom remaining** — ~3,591
- **Recommended fix** — Move this logic to `scripts/e2e_verify_bait_removed.sh` and keep the workflow step to env wiring plus a single script invocation.

#### EXPR-003
- **File path** — `.github/workflows/validate.yml:210-583`
- **Severity** — Medium
- **Category tag** — expression-limit
- **Description** — The support-fetch bootstrap is both long and highly interpolated, which makes future edits risky.
  - **Estimated current character count** — ~17,417
  - **Headroom remaining** — ~3,583
- **Recommended fix** — Extract the support bootstrap to `scripts/fetch_workflow_support.sh` or a composite action and keep the workflow body thin.

#### EXPR-004
- **File path** — `.github/workflows/review_autofix.yml:1476-1865`
- **Severity** — Medium
- **Category tag** — expression-limit
- **Description** — The PR-context collector step combines retry helpers, multiple API calls, linked-issue fallback logic, two inline Python blocks, and several `${{ }}` substitutions.
  - **Estimated current character count** — ~17,409
  - **Headroom remaining** — ~3,591
- **Recommended fix** — Extract the whole collector to a script such as `scripts/review_collect_pr_context.sh`, and keep the workflow step to env setup and one invocation.

### Section 5: Cross-Cutting Concerns

#### SHELL-001
- **File path** — `scripts/validate_changed_files_syntax.sh:70-75`
- **Severity** — Low
- **Category tag** — shellcheck
- **Description** — ShellCheck flags this case arm with SC2221/SC2222: `*.env*` already matches before the later `*.envrc` / `.env*` arms, so the later patterns are dead and misleading.
- **Recommended fix** — Collapse the secret-file denylist into one intentional pattern set, or reorder the arms so specific patterns come before the broader `*.env*` arm.

#### DEBT-001
- **File path** — `scripts/orchestrate_lib.py:988-1371`; `agents.md:160-167`; `README.md:1133`
- **Severity** — Low
- **Category tag** — tech-debt
- **Description** — The contradiction-evidence helper stack (`parse_phase_failure_markers`, `choose_most_advanced_conclusive_evidence`, `resolve_label_repair_evidence`, plus `evaluate_phase_failure_resume`) is maintained code but has no external callers in workflows/scripts; docs explicitly say it is reserved and not wired into the active poller.
- **Recommended fix** — Either wire the helper into `reconcile_managed_issue_labels`/resume gating, or move the reserved implementation behind a smaller module boundary with explicit tests and a narrower public surface so active poller code is easier to reason about.

No `TODO`, `FIXME`, or `HACK` markers were present under `.github/workflows` or `scripts`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BUG-001, BATCH-001, EXPR-001 |
| Medium | 9 | CONSIST-001, API-001, API-002, API-003, DUP-001, DUP-002, EXPR-002, EXPR-003, EXPR-004 |
| Low | 2 | SHELL-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3 | Medium |
| API call optimization | 5 | Large |
| Code modularization | 10+ | Large |
| Expression size reduction | 4 | Medium |
| Medium/Low fixes | 3 | Small |
