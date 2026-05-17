## Executive Summary

- **Fix the CI contract drift first.** All 19 `ci` runs failed (100%) in `shubhodeep1/coding-workflows`, each at `lint / Validation self-test unit tests` with `AssertionError: implement.yml missing resolved-ref log output` (for example runs `25979656195`, `25979048558`, `25979101442`, `25979995047`). The mismatch is literal: `.github/workflows/implement.yml` prints `Resolved fallback ref:` while `tests/test_workflow_checkout_integration_ref_audit.py` requires `Resolved ref:`. **Estimated impact:** remove the current 100% CI failure mode and recover `11,900s` of failed runtime in this window. **Confidence:** high.
- **`review_autofix` is the dominant runtime and cost center.** It consumed `68,104s` (`73.4%`) of all sampled runtime; slow run `25979383060` spent `1535.9s` in `Run reviewer models` and `677.4s` in `Apply fixes with editor model`. **Estimated impact:** this is the highest-leverage family for both speed and cost work. **Confidence:** high.
- **Self-triggered autofix follow-up runs look unnecessarily expensive.** In deep-dive `review_autofix` logs, there were 8 deduped `AUTOFIX_CONTINUATION_DISPATCH_ISSUED` events and 8 matching `AUTOFIX_DISPATCH_ISSUED ... continuation=true` events, but 0 `AUTOFIX_GATE_SKIP reason=self_triggered_autofix` events. `.github/workflows/review_autofix.yml` documents this skip as worth `~7 LLM calls per follow-up run` and `~50%` of autofix LLM spend per fix cycle. **Estimated impact:** major cost reduction and shorter PR fix cycles if `AUTOFIX_SKIP_SELF_TRIGGERED=true` is restored. **Confidence:** high.
- **AI memory telemetry is present but not helping yet.** In the 10 slow `review_autofix` deep dives, 8 runs emitted valid `AI_MEMORY_TELEMETRY`; all 8 deduped `retrieve` operations returned `0` records, `estimated_tokens=0`, and `keyword_method=none`. **Estimated impact:** moderate quality/cost upside once retrieval actually returns useful context. **Confidence:** high.
- **Semble is contributing bounded context, not obvious noise.** The deduped deep-dive sample showed 10 `SEMBLE_QUERY` events, `130,302` logged bytes total, `13,030.2` bytes/query average, `491.4ms` average latency, and 0 `SEMBLE_FALLBACK` events. Serena telemetry was absent (`SERENA_QUERY/FALLBACK/PROBE = 0`). **Estimated impact:** keep Semble in reviewer paths; fix or disable unavailable Semble config where it is not actually present. **Confidence:** medium-high.
- **Skipped fanout workflows are noisy in count but cheap in runtime.** The sample had 815 skipped runs, but they consumed only `971s` total (`1.0%` of runtime). The four high-count fanout families (`clarify`, `plan`, `implement`, `orchestrate_clarify_respond`) accounted for 814 skipped runs but only `931s`. **Estimated impact:** low; do not prioritize skip suppression ahead of CI and autofix fixes. **Confidence:** high.

## Speed Optimizations

1. **Critical path: skip redundant self-triggered autofix synchronize runs**
   - **Evidence:** `review_autofix` accounted for `68,104s` and 101 runs. Slow run `25979383060` spent `1535.9s` in `Run reviewer models`, `677.4s` in `Apply fixes with editor model`, and `19.1s` in `Re-trigger review via workflow_dispatch`. Deep-dive logs showed 8 deduped `AUTOFIX_CONTINUATION_DISPATCH_ISSUED` events (`25965072457`, `25965358554`, `25966185659`, `25966190742`, `25967061106`, `25978047174`, `25979101486`, `25979383060`) and 0 `AUTOFIX_GATE_SKIP reason=self_triggered_autofix` events.
   - **Root cause:** self-authored `[ai-autofix]` pushes are still allowing the synchronize-triggered review path to run instead of being gated out.
   - **Exact change:** set repository variable `AUTOFIX_SKIP_SELF_TRIGGERED=true` and leave `AUTOFIX_CONTINUATION_ENABLED=true` so the intended `workflow_dispatch` successor still runs.
   - **Estimated time savings:** **inference:** roughly one redundant `review_autofix` run per affected cycle; recent successful `review_autofix` runs ranged from `272s` to `936s`, with outliers above `1300s`.
   - **Implementation risk:** low-medium. The workflow comments already document the safety boundary: only bot-authored `[ai-autofix]` synchronize events are eligible, and `workflow_dispatch` plus stall-cron backstops still run.

2. **Critical path: fix the `implement.yml` log string and move the audit earlier**
   - **Evidence:** all 19 `ci` runs failed, totaling `11,900s`. Error logs for runs `25979656195`, `25979048558`, `25979101442`, and `25979995047` show `AssertionError: implement.yml missing resolved-ref log output`. `.github/workflows/implement.yml` currently echoes `Resolved fallback ref: ...`; `tests/test_workflow_checkout_integration_ref_audit.py` requires `Resolved ref: ...`. In the CI error corpus, the failing step started `609.2s` after run start on average, and the preceding `Orchestrate poll process unit tests` step averaged `587.4s`.
   - **Root cause:** workflow/test contract drift plus late execution of the audit test.
   - **Exact change:** rename the log line in `.github/workflows/implement.yml` to `Resolved ref:` and move `tests/test_workflow_checkout_integration_ref_audit.py` into an earlier “workflow contract” step or a parallel fail-fast job in `.github/workflows/ci.yml`.
   - **Estimated time savings:** immediate removal of the current CI block; on future regressions, about `10.1` minutes earlier feedback per broken run.
   - **Implementation risk:** low. This is a log-string correction plus test reordering.

3. **Critical path: reduce `review_autofix` check-run settle waiting**
   - **Evidence:** run `25979101486` spent `119.6s` in `Collect PR check-run failures CI lint autofix context`; the step logged repeated 30s waits for queued/in-progress check runs on the same SHA. Run `25979383060` spent `30.8s` in the same step. In `25979383060`, `Log token usage` showed `CHECK_RUNS_WAIT_TIMEOUT_SECS=120` and `CHECK_RUNS_POLL_INTERVAL_SECS=30`.
   - **Root cause:** the reviewer path blocks up to 120s for check-run stabilization before starting expensive model work.
   - **Exact change:** lower `CHECK_RUNS_WAIT_TIMEOUT_SECS` (for example to 60), capture an immediate snapshot of completed checks, and proceed fail-open with partial check context once the shorter deadline expires.
   - **Estimated time savings:** `30-90s` on affected `review_autofix` runs.
   - **Implementation risk:** medium. Mitigate by clearly labeling incomplete check coverage in the prompt/comment.

4. **Micro-optimization: make `Free disk space` conditional**
   - **Evidence:** `Free disk space` took `86.8s` in run `25979383060` and `99.3s` in run `25979101486`.
   - **Root cause:** an expensive cleanup step sits on the hot path regardless of actual free-space pressure.
   - **Exact change:** only run the cleanup when post-checkout free space drops below a measured threshold.
   - **Estimated time savings:** about `90s` on long `review_autofix` runs where the step can be skipped.
   - **Implementation risk:** medium and **inference-based**. Keep the existing step as fallback if disk pressure is detected.

**Queueing note:** hosted-runner wait messages appeared in `issue_pr_status` run `25980223791`, `forward_merge_stable_to_main` run `25980223697`, `copilot_pull_request_reviewer` run `25979383345`, `review_autofix` run `25980000037`, and `orchestrate_poll` run `25980107616`. Because adding infrastructure is out of scope, the best queue-time reduction is to remove redundant runs and shorten the hot paths above.

## Cost Optimizations

1. **Highest impact: stop paying for self-triggered verification reruns**
   - **Evidence:** `.github/workflows/review_autofix.yml` explicitly documents self-triggered `[ai-autofix]` follow-up runs as “pure cost” worth `~7 LLM calls per follow-up run`, and says skipping them cuts `~50%` of autofix LLM spend per fix cycle. The deep-dive sample showed 8 deduped continuation dispatches and 0 self-trigger skip events.
   - **Root cause:** `AUTOFIX_SKIP_SELF_TRIGGERED` is effectively off in the sampled window.
   - **Exact change:** set `AUTOFIX_SKIP_SELF_TRIGGERED=true`.
   - **Estimated savings:** about `56` avoided LLM calls in this sample window (`8 * ~7`), and roughly half of autofix LLM spend on affected cycles.
   - **Quality-risk notes:** low. The workflow still runs the intended `workflow_dispatch` successor, and the skip is limited to bot-authored `[ai-autofix]` synchronize events.

2. **Remove the telemetry blind spot before model-right-sizing anything else**
   - **Evidence:** run `25979383060` contains a `Log token usage` step and shows `OPENROUTER_PROMPT_CACHE_DISABLED=false`, but sampled logs contained no `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, or `openrouter usage` lines.
   - **Root cause:** cost/usage telemetry is not being emitted to logs or artifacts in a machine-readable way.
   - **Exact change:** emit structured per-call usage JSON for review/autofix, implement, and poller model calls: model name, prompt/completion totals, cache create/read counts, and step name.
   - **Estimated savings:** not directly quantifiable yet; this is the prerequisite for safe token and model optimization.
   - **Quality-risk notes:** none if the emission stays fail-open and non-blocking.

3. **Keep Semble in reviewer paths; stop carrying dead Semble config elsewhere**
   - **Evidence:** deduped deep-dive Semble telemetry showed 10 `SEMBLE_QUERY` events, 0 `SEMBLE_FALLBACK`, `130,302` logged bytes total, and `491.4ms` average latency; target split was `reviewer-context` 8 queries / `116,052` bytes and `overflow` 2 / `14,250` bytes. **Inference:** that looks like bounded context selection, not noisy prompt expansion. In contrast, `orchestrate_poll` run `25980107616` logged `SEMBLE_ENABLED=true` with `SEMBLE_AVAILABLE=false` and `SEMBLE_INDEX_AVAILABLE=false`.
   - **Root cause:** Semble is being configured as available in at least one path where it is not actually usable.
   - **Exact change:** keep Semble enabled in `review_autofix`; in poller paths, preflight availability first and disable the feature flag when the binary/index is unavailable.
   - **Estimated savings:** small direct compute savings in poller; more importantly, no evidence supports removing Semble from reviewer flows.
   - **Quality-risk notes:** low if the unavailable path keeps its current fail-open behavior.

4. **Phase-2 only, and only after telemetry exists: tighten reviewer fanout on truly small diffs**
   - **Evidence:** run `25979383060` logged `REVIEWERS_SUCCESSFUL: 5` and spent `1535.9s` in `Run reviewer models`. Run `25980000037` logged `AUTOFIX_GATE_DET_SKIP_EVAL pr=2669 files=4 additions=28 ... small_diff=false skip=false`, so the current small-diff gate did not exclude a modest change set. Secondary roles are already somewhat right-sized: the same run used `XPOLL_SUMMARISER_MODEL=openai/gpt-5.4-mini`.
   - **Root cause:** **inference:** reviewer-panel fanout is more likely to dominate spend than summarizer-model choice.
   - **Exact change:** after token telemetry lands, pilot a smaller reviewer set or lower reasoning effort for only the smallest, lowest-risk diffs.
   - **Estimated savings:** potentially material, but unquantified in this window.
   - **Quality-risk notes:** medium-high. Do not change the default editor/reviewer path blindly.

**Serena note:** no `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines were observed, and no Serena response-byte telemetry was present. In this sample Serena is neither replacing downstream tool/model work nor adding noisy response bytes.

## Reliability Improvements

1. **CI workflow contract drift**
   - **Failure evidence:** all 19 `ci` runs failed at `lint / Validation self-test unit tests`; examples: `25979656195`, `25979048558`, `25979101442`, `25979995047`.
   - **Root cause category:** workflow/test contract drift.
   - **Exact fix:** change `.github/workflows/implement.yml` to emit `Resolved ref:` instead of `Resolved fallback ref:` and keep the audit test aligned to the canonical wording.
   - **Expected reliability impact:** removes the current 100% `ci` family failure mode.
   - **Rollback/fail-open:** trivial; the change affects audit logging only, not checkout behavior.

2. **Late failure detection in CI**
   - **Failure evidence:** the failing audit step started `609.2s` after run start on average, after `587.4s` of `Orchestrate poll process unit tests`.
   - **Root cause category:** fail-fast ordering gap.
   - **Exact fix:** move `tests/test_workflow_checkout_integration_ref_audit.py` into an early CI step or separate fail-fast job.
   - **Expected reliability impact:** lowers rerun pressure and wasted compute when workflow/test contracts drift again.
   - **Rollback/fail-open:** low risk; step ordering only.

3. **Auxiliary support-script / Telegram-helper drift in `issue_pr_status`**
   - **Failure evidence:** recent `issue_pr_status` run `25980223791` succeeded in `16s` but its evidence-grade `log_summary` reported `Support checkout ref ${script_ref} is unavailable; using main.` and `Could not fetch tg_helpers.sh; skipping TG cleanup.`
   - **Root cause category:** auxiliary dependency-resolution drift.
   - **Exact fix:** resolve the support ref once at job start, carry `resolved_script_ref` through all support-file fetches, and use the existing local fallback path without repeating warning-generating branches.
   - **Expected reliability impact:** lowers notification/cleanup fragility and warning noise without changing the main path.
   - **Rollback/fail-open:** preserve the current `main` fallback and skip cleanup if helpers still cannot be fetched.

4. **Noisy fail-open metadata warnings on already-skipped fanout runs**
   - **Failure evidence:** recent skipped runs `25980142042` (`plan`), `25980142049` (`clarify`), `25980142032` (`implement`), and `25980151243` (`implement`) were correctly skipped by gate conditions such as `plan.if`, `clarify.if`, or `/approved` checks. Similar recent summaries also reported checkout-context metadata warnings on runs that never executed the expensive path.
   - **Root cause category:** noisy fail-open instrumentation.
   - **Exact fix:** defer metadata-fetch work until after the gate passes, or emit one consolidated warning once per run when the workflow is already skipping.
   - **Expected reliability impact:** small direct failure reduction, but materially better operator signal-to-noise.
   - **Rollback/fail-open:** no behavior change for real execution paths.

5. **MCP rollout status is healthy; fix the one obvious config drift**
   - **Failure evidence:** sampled deep dives showed `SEMBLE_FALLBACK=0`, `SERENA_FALLBACK=0`, and `SERENA_PROBE=0`; no repeated fallback storm or probe-failure loop was visible. However, `orchestrate_poll` run `25980107616` reported `SEMBLE_ENABLED=true` with `SEMBLE_AVAILABLE=false` and `SEMBLE_INDEX_AVAILABLE=false`.
   - **Root cause category:** configuration drift, not runtime outage.
   - **Exact fix:** disable Semble in poller until the binary/index is actually present, or gate the feature flag behind an availability preflight.
   - **Expected reliability impact:** small but safe; prevents a masked partial rollout.
   - **Rollback/fail-open:** existing behavior already fails open.

## AI Memory Health

| Metric | Observed value | Scope |
|---|---:|---|
| Runs with valid `AI_MEMORY_TELEMETRY` | `8 / 10` | Slow `review_autofix` deep dives |
| Deduped telemetry events | `32` | Same sample |
| `record-run-event` ops | `16` | Same sample |
| `retrieve` ops | `8` | Same sample |
| `record-candidate` ops | `8` | Same sample |
| Retrieve hit rate | `0 / 8` (`0%`) | Same sample |
| Avg `estimated_tokens` | `0.0` | Same sample |
| `keyword_method` distribution | `none: 8` | Same sample |
| `enabled: false` retrieves | `0` | Same sample |
| `fail_open: true` retrieves | `0` | Same sample |
| High push retry counts | none observed | Push events that reported `push_attempts` succeeded with `push_attempts=1` |

- **Example:** run `25979383060`, step `Retrieve reviewer memory context fail-open`, logged `AI_MEMORY_TELEMETRY: {"enabled": true, "estimated_tokens": 0, "keyword_method": "none", "ok": true, "op": "retrieve", "records_selected": 0, "role": "reviewer"}`.
- **Interpretation:** emission works, but retrieval is effectively inert. The system is recording run/candidate events yet retrieving no reviewer memory in the sampled slow paths.
- **Budget gap:** no retrieval budget field was emitted, so `estimated_tokens` can only be compared against zero, not against a configured ceiling.
- **Smallest safe fix:** verify that `record-candidate` / `record-run-event` entries land in the namespace queried by reviewer retrieval, and emit a single notice when `N` consecutive retrieves return zero records.
- **Do not change:** keep fail-open semantics; there were no hard memory failures in the sample.

## GH API Call Audit

`CLAUDE.md` §15 already sets the right guardrails: reuse existing calls, prefer batched GraphQL, and use cycle-local caches. The strongest findings are below.

| Workflow / run | Step / area | Evidence | Audit verdict | Exact change | Estimated call reduction |
|---|---|---|---|---|---:|
| `issue_pr_status.yml` | linked-issue/orchestrator lookup | Workflow comments state a “Single batched GraphQL call (one API request regardless of issue count)” with REST fallback only on batch failure | **Good existing hygiene** | Keep this as the reference pattern for multi-issue lookups | Avoids N-per-issue REST calls |
| `review_autofix` runs `25978047174`, `25966190742`, `25967061106`, `25979101486`, `25979383060` | linked-issue reuse | Logs show `Linked issues already cached from early fetch.` later in the run | **Good existing hygiene** | Keep reusing the early GraphQL fetch instead of re-querying later steps | Prevents repeated linked-issue fetches within the run |
| `review_autofix` run `25979101486` | `Collect PR check-run failures CI lint autofix context` | The step spent `119.6s` and logged at least three repeated waits on the same SHA before the deadline | **Redundant polling hotspot** | Shorten the wait window and reuse the first snapshot of completed checks instead of polling the same SHA for 120s | Roughly `2-3` check-run polls on affected runs |
| `copilot_pull_request_reviewer` run `25979160103` | `Prepare` and `Cleanup artifacts` | Evidence-grade `log_summary` reported `github.paginate(github.rest.pulls.listFiles)`, `github.rest.pulls.get`, and `/actions/runs/25979160103/artifacts`; `actions/github-script@v8` used `retries: 0` | **API reuse + resilience gap** | Carry PR metadata/file lists between steps, pass artifact IDs forward instead of re-listing, and add small retries/backoff for GH-script API calls | About `2-3` GH API calls per run, plus lower transient-failure risk |

**Call-count caveat:** exact per-run API totals were not emitted in the sampled logs, so the table above summarizes observed call sites and hotspot patterns rather than full request counts.

## Prompt Cache & Memory System

1. **Prompt-cache observability is missing**
   - **Evidence:** run `25979383060` step `Log token usage` printed environment state, including `OPENROUTER_PROMPT_CACHE_DISABLED=false`, but no prompt-token or cache read/write counters. Sample-wide grep found no `cache_creation_input_tokens` or `cache_read_input_tokens`.
   - **Assessment:** the design intent is there (`Pre-assemble static context cacheable across runs` exists in slow `review_autofix` runs), but cache behavior is currently unauditable.
   - **Recommendation:** emit structured cache metrics plus a stable “cacheable prefix hash” so cache fragmentation can be diagnosed.
   - **Impact:** high observability value; enables safe token, latency, and reliability tuning.

2. **Review-ledger cache is partially effective**
   - **Evidence:** across the 8 slow `review_autofix` runs with restore attempts, cache restore hit rate was `3 / 8` (`25967061106`, `25978047174`, `25979101486`) and miss rate was `5 / 8` (`25965072457`, `25965358554`, `25966185659`, `25966190742`, `25979383060`); all 8 saved a ledger.
   - **Assessment:** the PR-level restore key is working sometimes; misses look more like cold starts/new PRs than a total design failure.
   - **Recommendation:** keep the current PR-prefix restore path, but skip saves when the ledger content is unchanged and surface hit/miss in the step summary.
   - **Impact:** modest latency reduction and less cache churn.

3. **Memory retrieval is not feeding the prompt**
   - **Evidence:** `AI_MEMORY_TELEMETRY` retrieve hit rate was `0 / 8`, with `estimated_tokens=0` every time.
   - **Assessment:** the prompt system is doing the retrieval call, but it is not retrieving reusable memory context.
   - **Recommendation:** validate record storage/query compatibility and add a low-noise alert for repeated zero-hit retrieves.
   - **Impact:** moderate future token and quality upside once fixed.

4. **Semble is the only observed MCP prompt-compaction layer**
   - **Evidence:** deduped sample: `SEMBLE_QUERY=10`, `SEMBLE_FALLBACK=0`; `SERENA_QUERY=0`, `SERENA_FALLBACK=0`, `SERENA_PROBE=0`.
   - **Assessment:** **inference:** Semble appears to be reducing prompt expansion rather than adding noise in `review_autofix`; Serena is currently absent, so it is neither saving nor costing model work here.
   - **Recommendation:** keep Semble on the reviewer path, but disable or preflight it where unavailable; do not spend effort on Serena tuning until it actually emits telemetry.
   - **Impact:** small direct savings, better configuration clarity.

5. **Likely prompt-cache fragmentation cause, if one exists**
   - **Evidence gap:** no prompt-cache hit/miss metrics were emitted, so fragmentation is not directly proven.
   - **Assessment:** **inference:** if cache misses remain high once metrics are added, the likely culprit will be run-specific noise (timestamps, SHAs, temp paths) leaking into the prefix ahead of the “static context” block.
   - **Recommendation:** keep dynamic values in the suffix, not the cacheable prefix.
   - **Impact:** potentially meaningful token and latency savings, but only after instrumentation lands.

## Orchestrator Health

- **Fanout gating is healthy and cheap.** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` produced 814 skipped runs but only `931s` total runtime. Examples: `plan` runs `25980224422` and `25980151257` skipped because `plan.if` was false; `implement` run `25980142032` skipped because the comment was not `/approved`; `clarify` run `25980142049` skipped because `clarify.if` was false.
- **Terminal-state cleanup looks healthy.** `implement` run `25980151243` skipped because the comment body was an auto-close message from `close_merged_issues_sweep`, which indicates the poller’s merged-issue cleanup path is functioning.
- **The real operational pain is `review_autofix` tail behavior.** The family had `10` cancelled runs consuming `7717s`, and its p95 (`2404s`) is far above its p50 (`489s`). The smallest safe mitigations are still: skip redundant self-triggered synchronize runs and shorten the check-run wait window.
- **Retry behavior is mostly healthy where it exists.** `forward_merge_stable_to_main` run `25980223697` retried `git push` and a verification fetch once with `2s` backoff and still completed successfully via direct forward merge. That looks like healthy fail-open / retry behavior, not a broken rollout.
- **No stuck-state storm was visible in the sampled window.** There were no `SEMBLE_FALLBACK`, `SERENA_FALLBACK`, or `SERENA_PROBE` loops, and no sampled logs showed repeated conflict-heal retries or terminal-state thrash.
- **Observable indicators to track next:**  
  `AUTOFIX_GATE_SKIP reason=self_triggered_autofix` rate, cancelled `review_autofix` runtime, `review_autofix` p95 duration, total check-run wait seconds, poller `SEMBLE_AVAILABLE=false` count, and time-to-fail for workflow contract tests.
- **Data gap:** the current window does not expose explicit wave-progression or deferral counters, so orchestrator-health tracking is currently inferential rather than state-machine-driven.

## Pipeline Flow Bottlenecks

1. **Validation/CI bottleneck — hard blocker**
   - `ci` is only `19` runs but all `19` failed, wasting `11,900s`.
   - The entire failure set comes from one trivial audit mismatch in `implement.yml`.
   - **Fix first:** repair the log string and move the test earlier.

2. **Review/autofix compute bottleneck — dominant end-to-end cost**
   - `review_autofix` used `68,104s` (`73.4%`) of total runtime.
   - Slow run `25979383060` spent `1535.9s` in reviewer models and `677.4s` in editor work.
   - **Fix next:** remove redundant reruns and shorten the pre-review wait path.

3. **Review/autofix cancellation / duplicate-work bottleneck**
   - `review_autofix` cancellations burned `7717s` (`11.3%` of family runtime, `8.3%` of total runtime).
   - Deep-dive continuation dispatches with no self-trigger skips reinforce that duplicate follow-up work is a real source of churn.
   - **Fix:** enable `AUTOFIX_SKIP_SELF_TRIGGERED` and keep the continuation successor.

4. **Implement active-run bottleneck — real, but under-instrumented**
   - `implement` consumed `4622s` total, but 201 skipped runs used only `214s`; the three successful runs consumed the remaining `4408s`: `25964683452` (`1983s`), `25964655745` (`1265s`), and `25979048822` (`1160s`).
   - **Evidence gap:** no `slow/implement/` deep-dive folders were present, so root-cause optimization is not evidence-ready yet.
   - **Next collection step:** include slow implement runs in the deep-dive set before changing workflow logic.

5. **Queueing bottleneck — visible but secondary**
   - Hosted-runner waits appeared across `issue_pr_status`, `forward_merge_stable_to_main`, `copilot_pull_request_reviewer`, `review_autofix`, and `orchestrate_poll`.
   - **Fix inside current constraints:** reduce job count and hot-path duration; do not spend time on skip-only workflows.

6. **Clarify → plan → respond fanout is not the bottleneck**
   - These workflows dominate run count, not runtime.
   - **Do not optimize first:** they are already cheap due to aggressive gate conditions.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix`: `68,104s` runtime (`73.4%` share), p95 `2404s`
  - `ci`: `11,900s` failed runtime (`12.8%` share), `19/19` failed
  - `implement`: `4622s` runtime (`5.0%` share), mostly from 3 long successful runs

- **Top failure modes**
  - Workflow/test contract drift: `ci` run failures all trace to `implement.yml` logging `Resolved fallback ref:` instead of `Resolved ref:`
  - Auxiliary support-script drift: `issue_pr_status` run `25980223791` had support-ref and TG-helper warnings
  - No MCP fallback storm: `SEMBLE_FALLBACK=0`, `SERENA_FALLBACK=0`, `SERENA_PROBE=0` in sampled deep dives

- **Highest-cost drivers**
  - Reviewer/editor model time in `review_autofix`
  - Self-triggered follow-up review cycles
  - Cancelled `review_autofix` runs
  - Long successful `implement` runs with insufficient deep-dive evidence

- **Top 3 prioritized actions**
  1. **Fix `.github/workflows/implement.yml` and reorder the CI audit** so `ci` stops failing and future drift fails in seconds, not ~10 minutes.
  2. **Set `AUTOFIX_SKIP_SELF_TRIGGERED=true`** to remove redundant self-triggered verification passes while keeping the intended `workflow_dispatch` successor.
  3. **Shorten hot-path `review_autofix` waits and improve observability**: reduce `CHECK_RUNS_WAIT_TIMEOUT_SECS`, make `Free disk space` conditional, and emit structured token/prompt-cache metrics.

## Metrics Appendix

### Repo summary

| Repo | Runs | Success | Failure | Cancelled | Skipped/Other | Success rate | Failure rate | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 156 | 19 | 10 | 815 | 15.6% | 1.9% | 92.8 | 1.0 | 630.2 |

### Runtime by conclusion

| Conclusion | Runs | Runtime s | Runtime share |
|---|---:|---:|---:|
| Success | 156 | 72,229 | 77.8% |
| Failure | 19 | 11,900 | 12.8% |
| Cancelled | 10 | 7,717 | 8.3% |
| Skipped | 815 | 971 | 1.0% |

### Workflow family summary

| Family | Runs | S | F | C | O | Avg s | p50 s | p95 s | Runtime share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `review_autofix` | 101 | 90 | 0 | 10 | 1 | 674.3 | 489.0 | 2404.0 | 73.4% |
| `ci` | 19 | 0 | 19 | 0 | 0 | 626.3 | 646.0 | 660.0 | 12.8% |
| `implement` | 204 | 3 | 0 | 0 | 201 | 22.7 | 1.0 | 2.0 | 5.0% |
| `orchestrate_poll` | 26 | 26 | 0 | 0 | 0 | 152.3 | 125.0 | 178.5 | 4.3% |
| `copilot_pull_request_reviewer` | 14 | 14 | 0 | 0 | 0 | 131.5 | 119.0 | 193.0 | 2.0% |
| `plan` | 205 | 2 | 0 | 0 | 203 | 6.8 | 1.0 | 2.0 | 1.5% |
| `clarify` | 207 | 2 | 0 | 0 | 205 | 1.9 | 1.0 | 2.0 | 0.4% |
| `orchestrate_clarify_respond` | 205 | 0 | 0 | 0 | 205 | 1.1 | 1.0 | 2.0 | 0.2% |
| `issue_pr_status` | 7 | 7 | 0 | 0 | 0 | 18.7 | 13.0 | 46.8 | 0.1% |
| `nightly_validation_selftest` | 1 | 1 | 0 | 0 | 0 | 115.0 | 115.0 | 115.0 | 0.1% |
| `forward_merge_stable_to_main` | 4 | 4 | 0 | 0 | 0 | 19.8 | 19.5 | 21.7 | 0.1% |
| `cancel_on_pr_close` | 7 | 7 | 0 | 0 | 0 | 6.4 | 6.0 | 7.7 | 0.0% |

> `S/F/C/O` = success / failure / cancelled / other(skipped)

### Token, cache, and memory metrics

| Metric | Value | Notes |
|---|---:|---|
| Prompt tokens total | unavailable | No runtime token usage lines emitted |
| Completion tokens total | unavailable | No runtime token usage lines emitted |
| Total model tokens | unavailable | No runtime token usage lines emitted |
| Prompt-cache create tokens | unavailable | No `cache_creation_input_tokens` lines found |
| Prompt-cache read tokens | unavailable | No `cache_read_input_tokens` lines found |
| Review-ledger cache restores | 8 | Slow `review_autofix` deep-dive sample |
| Review-ledger cache hits | 3 | Runs `25967061106`, `25978047174`, `25979101486` |
| Review-ledger cache misses | 5 | Runs `25965072457`, `25965358554`, `25966185659`, `25966190742`, `25979383060` |
| Review-ledger cache saves | 8 | Same sample |
| AI memory telemetry coverage | 8 / 10 runs | Slow `review_autofix` deep-dive sample |
| AI memory deduped events | 32 | 16 `record-run-event`, 8 `retrieve`, 8 `record-candidate` |
| AI memory retrieve hit rate | 0 / 8 | `keyword_method=none` every time |
| AI memory avg `estimated_tokens` | 0.0 | No retrieve budget field emitted |

### MCP / Semble / Serena summary

| Server | Queries | Fallbacks | Probes | Logged bytes | Avg bytes / query | Avg ms / query | Response bytes | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Semble | 10 | 0 | 0 | 130,302 | 13,030.2 | 491.4 | n/a | `reviewer-context`: 8 / 116,052 bytes; `overflow`: 2 / 14,250 bytes |
| Serena | 0 | 0 | 0 | 0 | n/a | n/a | 0 | No per-tool or response-byte telemetry emitted |
| Other MCP servers observed | 0 | 0 | 0 | 0 | n/a | n/a | 0 | None observed |

### Per-target MCP availability rows

| Server | Target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---:|---:|---:|---|
| Semble | `reviewer-context` | 0 | 0 | 0 | No `SEMBLE_PROBE` lines; queries only |
| Semble | `overflow` | 0 | 0 | 0 | No `SEMBLE_PROBE` lines; queries only |
| Serena | n/a | 0 | 0 | 0 | No `SERENA_PROBE` lines |
| Other MCP servers | none | 0 | 0 | 0 | None observed |

### GH API call summaries

| Workflow / run | Step / area | Observed API activity | Observed count / interval | Note |
|---|---|---|---|---|
| `issue_pr_status.yml` | orchestrator status lookup | batched GraphQL + REST fallback | 1 batch call path in code comments | Good reference implementation |
| `review_autofix` `25979101486` | `Collect PR check-run failures CI lint autofix context` | repeated polling on same SHA while waiting for check runs | at least 3 waits over `119.6s` | Clear poll hotspot |
| `review_autofix` `25978047174`, `25966190742`, `25967061106`, `25979101486`, `25979383060` | linked-issue reuse | `Linked issues already cached from early fetch.` | observed in 5 named runs (also seen in other slow samples) | Good intra-run reuse |
| `copilot_pull_request_reviewer` `25979160103` | `Prepare` / `Cleanup artifacts` | `pulls.listFiles`, `pulls.get`, artifact listing endpoint | 3 hotspot call sites in `log_summary` | Best candidate for call consolidation |

