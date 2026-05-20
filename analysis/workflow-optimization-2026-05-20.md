## Executive Summary

- `review_autofix` is the main speed/cost driver: 204 runs, p50 `407s`, p95 `3361s`, with outliers at `5144s` (run `26110263808`), `4807s` (`26094422314`), and `4509s` (`26120104978`). Tiny or no-PR reviews are still paying near-full review cost. **Estimated impact:** save `15-25m` on affected runs. **Confidence:** high.
- `test_and_mark_stable` is the main reliability problem: `26/27` runs failed (`96.3%`), and the only success (`26084231354`) still took `5761s` and logged `126` `gh api` command occurrences, plus retry exhaustion in `step-011-e2e-smoke-test.log`. **Estimated impact:** largest failure/rerun reduction in the fleet. **Confidence:** medium-high (tempered by missing logs on 15 failed runs).
- CI fail-fast ordering is backwards: `ShellCheck static analysis` failed after `789s` (run `26096156079`) and `Python lint (ruff)` failed after `838s` (`26109756163`), while early `Actionlint` failure happened in `18s` (`26097192013`). In `.github/workflows/ci.yml`, ShellCheck and Ruff sit late at lines `477-488`. **Estimated impact:** save `11-14m` on failing CI runs. **Confidence:** high.
- AI memory retrieval is not helping long reviews: I found `6` valid `retrieve` events, with `0/6` hits, `estimated_tokens=0`, and `keyword_method=none` every time. **Estimated impact:** medium cost/quality improvement once fixed. **Confidence:** high.
- Semble looks net-positive, not the bottleneck: `13` real `SEMBLE_QUERY` events across slow `review_autofix` runs totaled only `126,047` bytes and `6,059ms`; the only fallbacks were `5` isolated `SEMBLE_FALLBACK` events in `test_and_mark_stable` run `26084231354`, all due `missing_semble`. **Estimated impact:** medium reliability gain if packaging is fixed; little reason to disable it for cost. **Confidence:** high.
- The orchestrator control plane is noisy and often idle: recent `orchestrate_poll` runs `26142118543` (`58s`), `26139249211` (`53s`), `26136884664` (`56s`), and `26135261030` (`61s`) all completed with no useful work signal, while `clarify`/`plan`/`implement`/`orchestrate_clarify_respond` produced `491` skipped runs combined. **Estimated impact:** medium background cost/queue-pressure reduction. **Confidence:** high.

## Speed Optimizations

1. **Critical path — make small-diff `review_autofix` actually skip or downshift**
   - **Evidence:** `review_autofix` uses six reviewer models by default in `.github/workflows/review_autofix.yml` and sets `REVIEWER_REASONING_EFFORT=xhigh`, `EDITOR_REASONING_EFFORT=xhigh`, and `ENABLE_REVIEWER_TWO_PASS=true`. The file also states the pass-2 diff gate is effectively a no-op because both `REVIEWER_PASS2_REASONING_SMALL` and `..._LARGE` default to `xhigh`. Run `26140956101` still spent `1588s` on PR `2796` after logging `AUTOFIX_GATE_DET_SKIP_EVAL pr=2796 files=1 additions=2 deletions=? ... small_diff=false skip=false`.
   - **Root cause:** the deterministic small-diff gate is too strict when one numeric field is missing, and the second-pass reasoning gate is not actually reducing work.
   - **Exact change:** when the PR fetch succeeds, normalize missing `deletions` to `0`; for qualifying small diffs, either (a) set `DETERMINISTIC_SKIP=true`, or at minimum (b) set `ENABLE_REVIEWER_TWO_PASS=false`, lower `REVIEWER_PASS2_REASONING_SMALL`, and trim reviewer breadth.
   - **Estimated time savings:** `15-25m` on tiny PRs like `2796`; `5-10m` on other small PRs that still need review.
   - **Implementation risk:** low-medium; preserve the existing `[force-review]`/`force-review` label escape hatch.

2. **Critical path — shrink the no-PR `claude/**` review path**
   - **Evidence:** run `26140912111` took `1842s` and run `26140945511` took `1351s`, both dominated by `review-claude-branch-push` after logging `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW_NO_PR ... running reviewer panel + commit-comment path because no PR exists.` The workflow already applies a “lightweight” no-PR profile at `.github/workflows/review_autofix.yml:2317-2324`, but it only lowers reasoning and disables pass-2; it does not reduce the six-model reviewer panel.
   - **Root cause:** a comment-only path still pays near-full multi-model review cost.
   - **Exact change:** in the existing no-PR light-profile step, also override `REVIEWER_MODELS` to the fastest `1-2` reviewers and skip editor/fix application unless reviewer consensus contains a blocking finding.
   - **Estimated time savings:** `10-20m` per no-PR `claude/**` review.
   - **Implementation risk:** medium; acceptable because these runs do not gate mergeability.

3. **Critical path — move ShellCheck and Ruff to the top of CI**
   - **Evidence:** `.github/workflows/ci.yml` places `ShellCheck static analysis` at lines `477-481` and `Python lint (ruff)` at `484-488`, after a long chain of unit tests and coverage gates. Failures hit late: `26096156079` failed at `789s` on ShellCheck; `26109756163` failed at `838s` on Ruff. By contrast, `Actionlint` failed in `18s` on `26097192013`.
   - **Root cause:** cheap static checks run after expensive tests.
   - **Exact change:** move `Shell script syntax check`, `ShellCheck static analysis`, and `Python lint (ruff)` immediately after checkout/setup and before the long Python test matrix.
   - **Estimated time savings:** `11-14m` on failing CI runs of this type.
   - **Implementation risk:** low.

4. **Background speed win — reduce idle `orchestrate_poll` runner starts**
   - **Evidence:** recent `orchestrate_poll` runs `26142118543`, `26139249211`, `26136884664`, and `26135261030` all spent `53-61s` finishing successful no-work polls; summaries/logs show runner assignment plus `has_work=false`/`poll_completed`.
   - **Root cause:** hosted-runner startup dominates no-work cycles.
   - **Exact change:** increase the poll interval after consecutive `has_work=false` cycles, or short-circuit before full checkout when there are no open orchestrator-tracking issues.
   - **Estimated time savings:** `45-60s` per idle poll run.
   - **Implementation risk:** low if backoff resets immediately when work reappears.

5. **Micro-optimization — stop re-checking out workflow support source in multiple steps**
   - **Evidence:** `.github/workflows/clarify.yml:164-203`, `.github/workflows/plan.yml:215-254`, and `.github/workflows/orchestrate.yml:156-188` all duplicate support-source resolution/fallback logic. In `issue_pr_status` run `26142158934`, three substeps (`Fetch memory helper scripts`, `Send PR merged Telegram alert`, `Cleanup tracked Telegram messages`) each went down the “fallback to main” path.
   - **Root cause:** support-source staging is repeated inside the same workflow.
   - **Exact change:** perform support-source staging once per workflow/job and reuse the staged tree.
   - **Estimated time savings:** `5-15s` on affected runs, plus less warning noise.
   - **Implementation risk:** low.

## Cost Optimizations

1. **Make `review_autofix` cost scale with diff size**
   - **Evidence:** `.github/workflows/review_autofix.yml` defaults to six reviewer models, `xhigh` reviewer/editor reasoning, and two-pass review. The pass-2 diff-size gate is explicitly a no-op at defaults. Run `26140956101` spent `1588s` on a `1`-file, `+2`-line PR and still did not skip.
   - **Root cause:** full reviewer fan-out is applied even to trivial diffs.
   - **Exact change:** for small diffs, disable pass-2, drop to `low/medium` reasoning, and use `1-2` reviewers unless the PR touches workflows or carries `force-review`.
   - **Estimated savings:** roughly `30-70%` of model/runtime cost on small PRs.
   - **Quality-risk notes:** medium; mitigate by keeping the current full path for workflow edits, large diffs, or explicit force-review markers.

2. **Treat no-PR `claude/**` reviews as a cheaper “comment-only” class**
   - **Evidence:** no-PR runs `26140912111` and `26140945511` still took `1842s` and `1351s` while only producing reviewer-comment output.
   - **Root cause:** the current light profile still keeps broad reviewer fan-out.
   - **Exact change:** in no-PR mode, cap reviewer count and keep the summarizer on `gpt-5.4-mini`; skip editor/fix steps entirely unless the review finds something severe enough to justify a follow-up PR.
   - **Estimated savings:** `50-80%` of LLM/runtime cost on these runs.
   - **Quality-risk notes:** low-medium because this path is advisory, not merge-gating.

3. **Reduce `workflow_log_analysis` summarization spend before chasing prompt-cache micro-savings**
   - **Evidence:** `workflow_log_analysis` run `26084260324` logged `AI_MEMORY_TELEMETRY` for `summarize_unselected_runs` with model `openai/gpt-5.4-mini`, `summarized=97`, `targeted=100`, and `tokens_used=222612`.
   - **Root cause:** the analysis workflow is paying to summarize a large tail of unselected runs every time.
   - **Exact change:** summarize fewer runs (for example, only failing/cancelled/no-deep-dive runs, or reduce the target from `100` to `50`).
   - **Estimated savings:** `40-60%` of token usage for this workflow.
   - **Quality-risk notes:** low if deep-dive coverage remains intact.

4. **Do not optimize away Semble; fix its packaging**
   - **Evidence:** operational Semble traffic was small: `13` `SEMBLE_QUERY` events total, `126,047` bytes, `6,059ms`. Query targets were focused (`reviewer-context` and a handful of overflow files). (Inference) That is likely cheaper than inlining large file bodies repeatedly into prompts.
   - **Root cause:** Semble itself is not the noisy cost source; the only issue was `5` `SEMBLE_FALLBACK` events in `test_and_mark_stable` run `26084231354`, all due `missing_semble`.
   - **Exact change:** keep Semble enabled for review paths; only fix the validate-scripts fallback packaging.
   - **Estimated savings:** avoids regressing prompt size while removing fallback churn.
   - **Quality-risk notes:** low.

5. **Leave Serena off until it proves it can replace downstream work**
   - **Evidence:** I found `0` operational `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines. Recent `review_autofix` summaries (`26140912111`, `26140945511`, `26140956101`, `26130369634`, `26128991375`) all logged `SERENA_ENABLED: false`.
   - **Root cause:** Serena is not currently participating in the active path.
   - **Exact change:** do not enable it broadly until a narrow path emits real query/probe telemetry and shows it can replace shell/API/model work.
   - **Estimated savings:** avoids speculative cost and complexity.
   - **Quality-risk notes:** none.

6. **Prioritize rerun prevention over prompt-cache guessing**
   - **Evidence:** `test_and_mark_stable` failed `26/27` times, and the one success still consumed `5761s`. That rerun burn is larger than any currently measurable prompt-cache delta. Also, no trustworthy operational prompt/cache token counters were emitted in this window.
   - **Root cause:** avoidable reruns dominate measurable waste.
   - **Exact change:** fix `test_and_mark_stable` reliability first; then add prompt/cache counters and optimize cache behavior from data.
   - **Estimated savings:** one prevented `test_and_mark_stable` rerun saves up to a full `~96m` smoke cycle.
   - **Quality-risk notes:** none.

## Reliability Improvements

1. **Stabilize `test_and_mark_stable` by de-duplicating API polling and preserving diagnostics**
   - **Failure evidence:** the family failed `26/27` times. In the lone success, run `26084231354`, I counted `126` logged `gh api` command occurrences across nine steps, including `74` in `step-011-e2e-smoke-test.log`; that same step logged `gh api failed after 3 attempts` and set `status=retry_dispatch_failed`. `analysis_context` also shows `15` `partial_data:missing_log_archive ... HTTP 404` errors for failed `test_and_mark_stable` runs.
   - **Root cause category:** API-poll-heavy orchestration plus weak diagnostics. **Inference:** this API churn is likely contributing to the high failure rate, but it is not proven for every failed run because many archives are missing.
   - **Exact fix:** cache `pulls`, `actions/runs`, and `jobs` responses per poll iteration; collapse repeated dispatch/poll helpers; always upload a per-phase status artifact/JSON manifest so failures remain diagnosable even when the GitHub log archive 404s.
   - **Expected reliability impact:** highest failure-rate and MTTR reduction in the fleet.
   - **Rollback / fail-open:** keep current direct polling as a fallback path behind an env flag; artifact upload should be `if: always()` and fail-open.

2. **Fix Semble overflow packaging in the validation harness**
   - **Failure evidence:** `5` operational `SEMBLE_FALLBACK` events all occurred in `test_and_mark_stable` run `26084231354`, `step-002-validate-scripts.log`, all `target=overflow`, all with `reason=[Errno 2] ... missing_semble`; affected files were `src/big.py` (`4`) and `src/small.py` (`1`).
   - **Root cause category:** local dependency/path wiring, not fleet-wide Semble instability.
   - **Exact fix:** ensure the validate-scripts step stages the Semble binary/path before running the contract, or explicitly mark the fallback as expected in the harness when `missing_semble` is intentional.
   - **Expected reliability impact:** small-medium; removes noisy false alarms and validates the intended path.
   - **Rollback / fail-open:** keep the current fallback behavior; this is healthy rare fail-open behavior, not evidence of a broken rollout.

3. **Standardize workflow support-source checkout behavior**
   - **Failure evidence:** `issue_pr_status` run `26142158934` took the fallback-to-`main` branch in three substeps. Also, `.github/workflows/clarify.yml` and `plan.yml` hard-fail if `.codex-workflow-src` is missing, while `.github/workflows/orchestrate.yml` only warns and skips run-start memory support.
   - **Root cause category:** inconsistent bootstrap/version-resolution logic.
   - **Exact fix:** use one helper/composite pattern for support-source staging, with a clear list of required vs optional files; add the same “ensure checkout” guarantee everywhere required scripts are mandatory.
   - **Expected reliability impact:** medium; reduces silent version skew and repeated fallback noise.
   - **Rollback / fail-open:** optional helper steps can still warn-and-skip, but required script bundles should fail fast.

4. **Clear the Node 20 deprecation debt now**
   - **Failure evidence:** `review_autofix` runs `26130369634` and `26128991375` both logged `Node.js 20 is deprecated... forced to run on Node.js 24` for `actions/cache/restore@v4`, `actions/cache/save@v4`, and `astral-sh/setup-uv@v3`.
   - **Root cause category:** aging action versions.
   - **Exact fix:** audit and pin Node 24-ready action versions for the warned actions.
   - **Expected reliability impact:** medium future-break prevention.
   - **Rollback / fail-open:** pin specific tested versions so rollback is straightforward.

5. **Repair deterministic-skip field normalization**
   - **Failure evidence:** run `26140956101` logged `files=1 additions=2 deletions=? ... small_diff=false skip=false` and then spent `1588s` in the full review path.
   - **Root cause category:** guard/normalization bug.
   - **Exact fix:** treat a missing `deletions` field as `0` after a successful PR fetch, or validate and re-fetch before deciding `small_diff=false`.
   - **Expected reliability impact:** medium; fewer unnecessary long reviews and fewer operator-triggered reruns.
   - **Rollback / fail-open:** keep `force-review` as an override.

No operational `SERENA_FALLBACK` or `SERENA_PROBE` lines were observed, so there is no evidence of Serena probe instability in this window; this looks like a disabled rollout, not a masked failure.

## AI Memory Health

- I found `22` valid, prefixed `AI_MEMORY_TELEMETRY` JSON events outside `workflow_log_analysis`: `16` `record-run-event` and `6` `retrieve`.
- **Retrieve hit rate:** `0/6 = 0%`.
  - Observed in slow `review_autofix` runs `26094422314`, `26080864487`, `26097192171`, `26083777327`, `26110263808`, and `26120104978`.
  - Every retrieve had `enabled=true`, `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`.
- **Average `estimated_tokens`:** `0`, so retrieval is not consuming budget — but also not returning anything useful.
- **Keyword-method distribution:** `none=6`, `plain=0`, `llm=0`.
- **Flags:**
  - `retrieve` returning 0 records: `6/6`
  - `fail_open: true`: `0`
  - `enabled: false`: `0`
  - high push retry counts in prefixed telemetry: `0`
- I did **not** observe `promote`, `compact`, `finalize-task`, or processed-command telemetry in prefixed lines during this window.
- One write-side memory payload in `review_autofix` run `26110263808` appeared as raw JSON rather than an `AI_MEMORY_TELEMETRY:`-prefixed line, so write-side observability is likely undercounted.

**Recommendation:** when `keyword_method=none`, fall back to plain keywords built from changed file paths, symbols, and PR title; also enforce a single prefixed emission format for all memory ops. The smallest useful SLO is “retrieve hit rate >10% on slow `review_autofix` runs”; it is currently `0%`.

## GH API Call Audit

1. **`test_and_mark_stable` is the clear GH API hotspot**
   - **Evidence:** success run `26084231354` logged `126` `gh api` command occurrences across nine step logs:
     - `step-011-e2e-smoke-test.log`: `74`
     - `step-012-orphan-workflows-test.log`: `15`
     - `step-009-orchestrate-decompose-test.log`: `9`
     - `step-010-e2e-alt-model-test.log`: `8`
     - `step-014-clarify-rejects-unsolvable-test.log`: `6`
     - `step-003-validate-standalone-test.log`: `5`
     - `step-013-workflow-log-analysis-test.log`: `5`
     - `step-008-validate.log`: `3`
     - `step-007-release.log`: `1`
   - **Redundancy pattern:** repeated polling of `actions/runs`, `pulls/{n}`, `issues/{n}`, `issues/{n}/comments`, and `actions/runs/{id}/jobs` inside loops. The workflow file repeats similar helpers around lines such as `494`, `617`, `647`, `699`, `816`, `857`, `1307`, `1374`, `1535`, `1555`, `2033`, `2441`, and `3404-4073`.
   - **Concrete batching/reuse change:** fetch each of `pulls/{PR_NUMBER}`, `actions/runs?...`, and `actions/runs/{RUN_ID}/jobs` once per poll iteration, store the JSON in temp files, and let all subsequent checks read the cached payload.
   - **Estimated call reduction:** `30-50%` for this workflow, likely cutting `40-60` logged `gh api` invocations on a success path like `26084231354`.
   - **Rate-limit risk reduction:** high; this workflow already contains rate-limit backoff scaffolding and `/rate_limit` probing, so pressure is real.

2. **The workflow repeats near-identical dispatch/poll helpers**
   - **Evidence:** `.github/workflows/test-and-mark-stable.yml` contains multiple `PRE -> dispatch -> poll new run id -> poll status` blocks around `3404-4073`, `3553-3838`, and later sections.
   - **Redundancy pattern:** same API choreography reimplemented several times for different workflow files.
   - **Concrete batching/reuse change:** move that logic into one shell/Python helper that accepts workflow file + ref + prior run id, then returns `new_run_id/status/conclusion`.
   - **Estimated call reduction:** `15-25%` in that workflow, plus lower maintenance risk.

3. **`internal-review.yml` has an avoidable extra API call on every `claude/**` push**
   - **Evidence:** `.github/workflows/internal-review.yml:91-101` calls:
     - `repos/${REPOSITORY}/pulls?state=open&head=...`
     - `repos/${REPOSITORY}` for `.default_branch`
   - **Redundancy pattern:** default branch is already available from the event payload.
   - **Concrete batching/reuse change:** replace the second call with `${{ github.event.repository.default_branch || 'main' }}`.
   - **Estimated call reduction:** `1` GH API call per `claude/**` push-triggered review resolution.
   - **Rate-limit risk reduction:** small but free.

4. **The collector side is already behaving well on missing archives**
   - **Evidence:** `tests/test_collect_workflow_logs.py:500-541` verifies `_fetch_run_log_archive` soft-fails and caches missing archives after a single retry (`call_retries == [1]`), and `tests/test_collect_workflow_logs.py:697-728` verifies detail sanitization.
   - **Audit conclusion:** GH API hygiene in the collector is already good; the high redundancy is in workflow runtime, especially `test_and_mark_stable`.

## Prompt Cache & Memory System

- **What is working:** `.github/workflows/review_autofix.yml:1435-1446` pre-assembles a stable static prefix (instructions + AGENTS + trimmed README) specifically for provider-side caching. That is the right structural pattern.
- **What is not measurable:** deep-dive logs repeatedly showed `OPENROUTER_PROMPT_CACHE_DISABLED: false` (for example in `review_autofix` run `26110263808` and `orchestrate_poll` run `26142118543`), but I found no trustworthy operational `prompt_tokens`, `completion_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens`.
- **Observed memory effectiveness:** poor. All `6` observed retrieves returned `0` records, so AI memory is not currently shrinking context or improving reviewer setup.
- **Likely issue:** the main problem is observability, not the existence of a cacheable prefix. Also, write-side memory telemetry is inconsistently prefixed, which makes hit/miss auditing incomplete.
- **Concrete improvements:**
  1. emit per-call prompt/completion/cache-create/cache-read token counters;
  2. emit a stable prefix hash so cache fragmentation can be measured;
  3. keep all dynamic PR/run noise strictly after the static prefix;
  4. add a plain-keyword fallback for AI memory retrieval when `keyword_method=none`.
- **Estimated impact:** token/latency savings are currently unquantifiable, but this is the prerequisite for any serious cache tuning.
- **Reliability impact:** high for observability; low implementation risk.

## Orchestrator Health

- **High skipped-run fan-out:** `clarify` had `127/132` skipped runs, `plan` `121/125`, `implement` `119/125`, and `orchestrate_clarify_respond` `124/125`. That is `491` skipped runs across the phase chain.
- **Interpretation:** phase eligibility is being decided after workflows are invoked, which adds noise and makes p50 repo duration (`2s`) misleading.
- **Idle poller cycles:** recent `orchestrate_poll` runs `26142118543`, `26139249211`, `26136884664`, and `26135261030` all spent `53-61s` on successful no-work polls.
- **Cancellation noise in `review_autofix`:** the family has `44/204` cancelled runs (`21.6%`). **Inference:** some of this is caller/callee accounting noise around reusable workflows rather than true lost work; for example, cancelled `Internal: AI Review & Autofix` run `26120093075` (`4502s`) is paired closely with successful `Codex PR Self-Healing Semantic Agent` run `26120104978` (`4509s`).
- **Merge-health issue:** `forward_merge_stable_to_main` run `26142158852` completed in `22s` but opened fallback conflict PR `#2801`; conflict files included `.github/workflows/review_autofix.yml`, `scripts/orchestrate_poll_process.sh`, and related tests, so stable fixes are currently blocked from reaching `main`.
- **Smallest safe mitigations:**
  - pre-gate phase workflows before dispatch when possible;
  - back off poll cadence when repeated `has_work=false` cycles occur;
  - separate parent vs callee metrics in `review_autofix`;
  - track conflict PR count and hot files for stable→main.
- **Track these indicators:** `has_work=false` poll rate, skipped-run ratio by family, `review_autofix` p95 and cancellation rate, missing log archive count, and forward-merge fallback PR count.

## Pipeline Flow Bottlenecks

1. **Compute bottleneck — `review_autofix`**
   - `review_autofix` is the dominant active-path cost center: p50 `407s`, p95 `3361s`, with many `2000-5000s` runs.
   - Biggest fix: make review depth proportional to diff size and no-PR status.

2. **Retry/API bottleneck — `test_and_mark_stable`**
   - The validation gate is both slow (`5761s` on the only success) and unreliable (`26/27` failures).
   - Biggest fix: de-duplicate API polling, unify dispatch helpers, and persist phase artifacts.

3. **Queueing bottleneck — CI / review / poller**
   - `review_autofix` summaries (`26130369634`) show both `gate` and `codex-agent` waiting for hosted runners.
   - CI success runs (`26140956019`, `26130361628`) and recent `orchestrate_poll` runs also show runner wait dominating setup.
   - Biggest fix: move static CI failures earlier and reduce unnecessary background poll starts.

4. **Control-plane bottleneck — skipped phase workflows and support checkout churn**
   - Hundreds of skipped phase workflows plus repeated support-source staging are adding control-plane noise without useful work.
   - Biggest fix: move routing decisions earlier and stage support source once.

5. **Merge/conflict bottleneck — stable→main propagation**
   - Forward merge is currently opening conflict PRs on hot automation files (`26142158852` / PR `#2801`), delaying rollout of stable fixes.
   - Biggest fix: resolve the current fallback PR quickly and forward-merge hot files more frequently.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` long-tail runtime (`p95 3361s`, outliers `5144s/4807s/4509s`)
  - `test_and_mark_stable` failure storm (`26/27` failed)
  - CI late fail-fast ordering (`ShellCheck`/`Ruff` near end of job)
  - idle `orchestrate_poll` cycles and skipped phase fan-out

- **Top failure modes**
  - API-heavy smoke/test harness with missing log archives (`15` missing archives in the window)
  - late static-analysis CI failures after most of the job already ran
  - support-source fallback/version skew noise
  - forward-merge conflict PRs blocking stable→main propagation

- **Highest-cost drivers**
  - six-reviewer, `xhigh`-reasoning default in `review_autofix`
  - no-PR `claude/**` comment-only reviews still running broad reviewer panels
  - `workflow_log_analysis` summarization spend (`222,612` tokens in run `26084260324`)
  - avoidable reruns from unstable `test_and_mark_stable`

- **Top 3 prioritized actions**
  1. Fix deterministic small-diff handling and reduce no-PR reviewer breadth in `review_autofix`.
  2. Refactor `test_and_mark_stable` polling/dispatch helpers and always upload phase diagnostics.
  3. Move ShellCheck/Ruff to the top of CI and reduce idle poller cadence when `has_work=false`.

## Metrics Appendix

### Repository summary

| Repository | Runs | Success | Failure | Cancelled | Other | Failure rate | p50 duration | p95 duration | Avg duration |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 956 | 383 | 33 | 46 | 494 | 3.5% | 2s | 2293s | 342.6s |

> Repo p50 is distorted by skipped runs; active-path families are better judged by per-family metrics below.

### Key workflow-family metrics

| Workflow family | Runs | Success | Failure | Cancelled | Other/skipped | Failure rate | p50 duration | p95 duration |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `review_autofix` | 204 | 159 | 0 | 44 | 1 | 0.0% | 407s | 3361s |
| `test_and_mark_stable` | 27 | 1 | 26 | 0 | 0 | 96.3% | 0s | 0s |
| `ci` | 79 | 74 | 5 | 0 | 0 | 6.3% | 804s | 875s |
| `orchestrate_poll` | 26 | 26 | 0 | 0 | 0 | 0.0% | 64s | 152s |
| `clarify` | 132 | 5 | 0 | 0 | 127 | 0.0% | 2s | 9s |
| `plan` | 125 | 4 | 0 | 0 | 121 | 0.0% | 1s | 6s |
| `implement` | 125 | 4 | 0 | 2 | 119 | 0.0% | 1s | 7s |
| `orchestrate_clarify_respond` | 125 | 1 | 0 | 0 | 124 | 0.0% | 2s | 3s |
| `issue_pr_status` | 15 | 15 | 0 | 0 | 0 | 0.0% | 15s | 61s |
| `copilot_pull_request_reviewer` | 38 | 37 | 1 | 0 | 0 | 2.6% | 188s | 291s |
| `forward_merge_stable_to_main` | 7 | 7 | 0 | 0 | 0 | 0.0% | 27s | 32s |
| `workflow_log_analysis` | 1 | 1 | 0 | 0 | 0 | 0.0% | 5673s | 5673s |

### Notable outlier runs

| Workflow family | Run ID | Duration | Outcome | Notable evidence |
|---|---:|---:|---|---|
| `review_autofix` | 26110263808 | 5144s | success | longest observed review run; Semble queries + AI memory retrieve miss |
| `review_autofix` | 26094422314 | 4807s | success | slow review; Semble reviewer-context query |
| `review_autofix` | 26120104978 | 4509s | success | slow review; Semble reviewer-context + overflow queries |
| `review_autofix` | 26140912111 | 1842s | success | no-PR `claude/**` comment-only review still expensive |
| `review_autofix` | 26140945511 | 1351s | success | same no-PR path; `REVIEWERS_SUCCESSFUL: 6` |
| `test_and_mark_stable` | 26084231354 | 5761s | success | `126` logged `gh api` occurrences; retry exhaustion; Semble fallback cluster |
| `workflow_log_analysis` | 26084260324 | 5673s | success | `summarize_unselected_runs` used `222,612` tokens |

### Trustworthy token and cache metrics

| Source | Run ID | Metric | Value | Notes |
|---|---:|---|---:|---|
| `workflow_log_analysis` | 26084260324 | `summarize_unselected_runs.tokens_used` | 222,612 | model `openai/gpt-5.4-mini`, `summarized=97`, `targeted=100` |
| `review_autofix` | 26110263808 | `OPENROUTER_PROMPT_CACHE_DISABLED` | false | cache feature enabled, but no hit/miss counters emitted |
| `orchestrate_poll` | 26142118543 | `OPENROUTER_PROMPT_CACHE_DISABLED` | false | same observability gap |
| Global | — | prompt/completion/cache-create/cache-read counters | not observed | cannot compute prompt-cache hit rate from current logs |

### GH API hotspot summary

| Workflow / run | Step / location | Logged `gh api` occurrences | Notes |
|---|---|---:|---|
| `test_and_mark_stable` / `26084231354` | `step-011-e2e-smoke-test.log` | 74 | repeated polling of runs / pulls / jobs / comments; one `gh api failed after 3 attempts` |
| `test_and_mark_stable` / `26084231354` | `step-012-orphan-workflows-test.log` | 15 | repeated workflow/run polling |
| `test_and_mark_stable` / `26084231354` | `step-009-orchestrate-decompose-test.log` | 9 | repeated dispatch/poll logic |
| `test_and_mark_stable` / `26084231354` | `step-010-e2e-alt-model-test.log` | 8 | repeated dispatch/poll logic |
| `test_and_mark_stable` / `26084231354` | all counted steps | 126 total | highest GH API hotspot in the window |
| `.github/workflows/internal-review.yml` | lines `91-101` | 2 per `claude/**` push | one call can be removed by reading `default_branch` from event payload |
| Collector tests | `tests/test_collect_workflow_logs.py:500-541` | 1 retry then cache | confirms good 404 hygiene on missing log archives |

### Semble telemetry

| Workflow family | Event | Count | Bytes | Total ms | Notes |
|---|---|---:|---:|---:|---|
| `review_autofix` | `SEMBLE_QUERY` | 13 | 126,047 | 6,059 | targeted queries only; no operational fallbacks seen in review runs |
| `test_and_mark_stable` | `SEMBLE_FALLBACK` | 5 | — | 0 | all in run `26084231354`, step `validate-scripts`, reason `missing_semble` |

#### Semble query target breakdown

| Target | Queries | Bytes | Total ms | File breakdown |
|---|---:|---:|---:|---|
| `reviewer-context` | 6 | 70,044 | 2,865 | reviewer-context only |
| `overflow` | 7 | 56,003 | 3,194 | `scripts/orchestrate_poll_process.sh` ×3; `tests/test_orchestrate_poll_process.py` ×2; `.github/workflows/review_autofix.yml` ×1; `tests/test_orchestrate_integration_ahead_by_gate.py` ×1 |

### Serena telemetry

| Event | Count | Response bytes | Notes |
|---|---:|---:|---|
| `SERENA_QUERY` | 0 | 0 | no operational lines observed |
| `SERENA_FALLBACK` | 0 | 0 | no operational lines observed |
| `SERENA_PROBE` | 0 | 0 | no operational lines observed |

### AI memory telemetry

| Metric | Value |
|---|---:|
| Valid prefixed `AI_MEMORY_TELEMETRY` events | 22 |
| `record-run-event` | 16 |
| `retrieve` | 6 |
| Retrieve hit rate (`records_selected > 0`) | 0/6 = 0% |
| Avg `estimated_tokens` on retrieve | 0 |
| `keyword_method=none` | 6 |
| `keyword_method=plain` | 0 |
| `keyword_method=llm` | 0 |
| `fail_open: true` observed | 0 |
| `enabled: false` observed | 0 |
| Prefixed events with push attempts > 1 | 0 |

### MCP availability

| Server | Target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---:|---:|---:|---|
| Semble | all | 0 | 0 | 0 | no `SEMBLE_PROBE` lines; `orchestrate_poll` run `26142118543` logged `SEMBLE_AVAILABLE: false`, `SEMBLE_INDEX_AVAILABLE: false` |
| Serena | all | 0 | 0 | 0 | no `SERENA_PROBE` lines; recent `review_autofix` runs logged `SERENA_ENABLED: false` |

**Other MCP servers observed:** none outside generated-analysis text.

## Deep Audit — Workflows & Scripts (2026-05-20)

### Section 1: Bug & Correctness Sweep

No new high-confidence secret-leak or shell-injection path stood out in the current revision. The strongest correctness gap I found is the orchestrator-managed predicate drift below.

- **ID** — BUG-001
  - **File path** — `.github/workflows/plan.yml:1216-1221,1680-1695; .github/workflows/issue_pr_status.yml:264-316,501-512`
  - **Severity** — Medium
  - **Category tag** — bug
  - **Description** — Both workflows establish the authoritative “orchestrator-managed” test as **label OR body marker** (`ai:orchestrator-managed` or `Managed by: AI Orchestrator`), then later notification paths re-check only the body marker with `grep -qF`. In `plan.yml`, that flips alert routing from `tg_send_tracked` to `tg_send_phase_tracked`; in `issue_pr_status.yml`, it can emit a merged alert the poller is supposed to own. If the body marker is absent but the canonical label remains, the later behavior is wrong.
  - **Recommended fix** — Compute `IS_ORCHESTRATOR_MANAGED` once from the existing label-or-body predicate and export it via `$GITHUB_ENV`/step output. Reuse that value in downstream alert/comment steps instead of re-parsing only the issue body.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — BATCH-001
  - **File path** — `.github/workflows/test-and-mark-stable.yml:3399-3471,3575-3615,3633-3680,3698-3738,3787-3849,4047-4089`
  - **Severity** — High
  - **Category tag** — api-batching
  - **Description** — This is the code-level source of the current report’s `test_and_mark_stable` API hotspot: six steps hand-roll the same `PRE -> gh workflow run -> poll actions/workflows/.../runs -> poll actions/runs/{id}` sequence. Even on the best-case path, that is **3 logical GH API calls per watcher** and **18 minimum calls total** before retry polling starts.
  - **Current call count** — Minimum **18** logical calls across the six watcher blocks.
  - **Proposed call count after fix** — Minimum **12** logical calls if a shared helper uses dispatch-start filtering instead of a separate `PRE=` probe and caches each poll payload.
  - **Existing batching pattern to extend** — `scripts/gh_helpers.sh`’s `gh_retry_to_file` / `gh_api_json_to_file`.
  - **Recommended fix** — Extract a shared `scripts/dispatch_and_watch_workflow.sh` helper with inputs like `--repo`, `--workflow-file`, `--deadline-secs`, `--accept-conclusion`, `--field key=value...`. Have it cache registration/status payloads once per poll iteration and remove the duplicated `PRE`/`NEW_ID`/`STATUS` logic from all six blocks.

- **ID** — API-001
  - **File path** — `.github/workflows/internal-review.yml:84-102`
  - **Severity** — Medium
  - **Category tag** — api-redundancy
  - **Description** — `Resolve PR for head branch` makes **2 GH API calls** on every `claude/**` push: one for the open PR lookup and one for `repos/${REPOSITORY}` just to read `.default_branch`. The second call is redundant because the event payload already has `github.event.repository.default_branch`; on failure it also silently falls back to `'main'`, which is incorrect for non-`main` repos.
  - **Current call count** — **2** calls per run of this step.
  - **Proposed call count after fix** — **1** call.
  - **Existing batching pattern to extend** — None; use the workflow event payload instead of a GitHub API helper.
  - **Recommended fix** — Replace `gh api "repos/${REPOSITORY}" --jq '.default_branch'` with `${{ github.event.repository.default_branch || 'main' }}` and keep only the PR lookup call.

- **ID** — API-002
  - **File path** — `.github/workflows/review_autofix.yml:526-553`
  - **Severity** — Medium
  - **Category tag** — api-batching
  - **Description** — In the fallback path where `closingIssuesReferences` is empty, the step fetches PR title/body once and then does `gh issue view ... --json labels` inside the loop for every parsed issue number whose labels are unknown. That path is currently **2 + N** calls: 1 GraphQL PR lookup, 1 PR body/title REST lookup, then **N** per-issue label lookups.
  - **Current call count** — **2 + N** on the fallback path.
  - **Proposed call count after fix** — **3** total on the fallback path.
  - **Existing batching pattern to extend** — The alias-based GraphQL issue batch in `.github/workflows/issue_pr_status.yml:280-317`, or `scripts/orchestrate_poll_process.sh`’s `_fetch_candidate_issue_details_graphql`.
  - **Recommended fix** — After parsing `issue_numbers`, batch-fetch their labels in one aliased GraphQL query and build a label map before entering the loop. That removes the per-issue `gh issue view` calls.

`BUG-001` also has an API-cost angle in `issue_pr_status.yml:501-512` (per-issue body refetch after earlier batched classification), but I did not assign a second ID to avoid double-counting the same defect.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — DUP-001
  - **File path** — `.github/workflows/clarify.yml:215-285; .github/workflows/plan.yml:265-335; .github/workflows/orchestrate.yml:339-424; .github/workflows/review_autofix.yml:929-1205; .github/workflows/validate.yml:210-583`
  - **Severity** — Medium
  - **Category tag** — duplication
  - **Description** — This is broader than the current report’s support-source fallback churn note: the support-source staging implementation itself is hand-copied across reusable workflows. Each block resolves `SCRIPT_REF`, prefers repo-self checkout, falls back to `main`, copies scripts/prompts/schemas, and writes `scripts/.gitignore`; the required-file lists are already diverging.
  - **Proposed shared module** — A new composite action (preferred) or script such as `.github/actions/stage-workflow-support` / `scripts/stage_workflow_support.sh` with inputs/signature like: `script_ref`, `required_scripts_csv`, `required_prompts_csv`, `required_ai_files_csv`, `stage_root`, `write_gitignore`.
  - **Recommended fix** — Move the copy/fallback logic into the shared module and update clarify, plan, orchestrate, review_autofix, validate, implement, and orchestrate_clarify_respond to call it with small per-workflow manifests.

- **ID** — DUP-002
  - **File path** — `scripts/setup_serena.sh:23-149; scripts/install_semble.sh:10-112`
  - **Severity** — Low
  - **Category tag** — duplication
  - **Description** — The Serena and Semble bootstrappers duplicate the same plumbing: `log`, `write_github_env`, `append_github_path`, pinned-version checks, and availability flag handling. They already diverge in small ways (for example, Serena dedupes `GITHUB_PATH` appends while Semble does not), so future fixes will have to be carried twice.
  - **Proposed shared module** — `scripts/tool_install_helpers.sh` with helpers like `write_github_env key value`, `append_github_path dir`, `binary_version_matches actual expected`, `mark_tool_available tool bool`.
  - **Recommended fix** — Keep only tool-specific discovery/install logic in `setup_serena.sh` and `install_semble.sh`; move the common GitHub Actions env/path/version helpers into the shared module.

### Section 4: Expression Size Limit Risk Assessment

No workflow exceeded the 800 KB file-size threshold. The largest workflow is `review_autofix.yml` at 360,085 chars. The longest `if:` expression I found was only 115 chars, so the current risk is concentrated in large interpolated `run:` blocks, not `if:` conditions.

- **ID** — EXPR-001
  - **File path** — `.github/workflows/review_autofix.yml:1497-1886`
  - **Severity** — Medium
  - **Category tag** — expression-limit
  - **Description** — `Collect PR metadata` is still a very large interpolated `run:` block: inline retry helper, metadata fetches, diff capture, and env export logic are all in YAML.
  - **Estimated current character count** — ~17,408 chars
  - **Headroom remaining** — ~3,592 chars to the 21,000-char limit
  - **Recommended fix** — Extract the block to `scripts/review_collect_pr_metadata.sh` and pass only small env vars from YAML.

- **ID** — EXPR-002
  - **File path** — `.github/workflows/validate.yml:210-583`
  - **Severity** — Medium
  - **Category tag** — expression-limit
  - **Description** — `Fetch workflow support files` is another large interpolated `run:` block with clone/fallback/copy helpers and multiple `${{ }}` insertions. Routine support-file additions can push it over the limit.
  - **Estimated current character count** — ~17,416 chars
  - **Headroom remaining** — ~3,584 chars
  - **Recommended fix** — Move the support-fetch logic into the shared support-staging module from `DUP-001`, or split the block into smaller steps.

- **ID** — EXPR-003
  - **File path** — `.github/workflows/review_autofix.yml:929-1205`
  - **Severity** — Medium
  - **Category tag** — expression-limit
  - **Description** — `Stage workflow support files` in `review_autofix.yml` is already over the 15k medium-risk threshold and is one of the fastest-growing blocks in the repo.
  - **Estimated current character count** — ~15,454 chars
  - **Headroom remaining** — ~5,546 chars
  - **Recommended fix** — Route this staging step through the same shared support-staging module, or split prompts/scripts/schema staging into separate steps.

### Section 5: Cross-Cutting Concerns

No `TODO`, `FIXME`, or `HACK` markers were present under `.github/workflows/` or `scripts/` at audit time.

- **ID** — CONSIST-001
  - **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/mark-stable.yml:333-360; .github/workflows/orchestrate_poll.yml:79-113`
  - **Severity** — Medium
  - **Category tag** — consistency
  - **Description** — These workflows embed local `_rl_wait`/`_gh_retry` implementations instead of using the repo’s canonical helpers. The copies already differ in temp-file naming, breaker-file behavior, and logging, so rate-limit and retry behavior can drift away from `scripts/gh_helpers.sh`.
  - **Recommended fix** — Introduce a tiny pre-checkout bootstrap helper/composite action that exports the canonical `gh_retry`, `gh_retry_to_file`, and `gh_api_json_to_file` functions early, then delete the inline copies.

- **ID** — SHELL-001
  - **File path** — `scripts/validate_changed_files_syntax.sh:70-73`
  - **Severity** — Low
  - **Category tag** — shellcheck
  - **Description** — In `case "${file},${basename_lc}"`, the `*.env*` alternation already matches the later `*,*.envrc|*,.env*` cases, so those trailing branches never fire. This is exactly the SC2221/SC2222 warning pair.
  - **Recommended fix** — Collapse the redundant alternates or split path-based and basename-based checks into separate `case` blocks so the intended precedence is explicit.

- **ID** — DEAD-001
  - **File path** — `.github/workflows/orchestrate_poll.yml:7-20`
  - **Severity** — Low
  - **Category tag** — dead-code
  - **Description** — The reusable poller still exposes a `caller_workflow` input even though the description explicitly says the value is ignored and the self-retrigger path was removed. That leaves a dead public input in the callable interface.
  - **Recommended fix** — Once wrapper inventories show no required consumers, remove the dead input and the matching wrapper plumbing. If compatibility must remain for now, emit a notice when a non-default value is supplied so stale callers can be found.

- **ID** — DEBT-001
  - **File path** — `.github/workflows/memory_maintenance.yml:39-55`
  - **Severity** — Low
  - **Category tag** — tech-debt
  - **Description** — `BATCH_API_DISABLED`, `BATCH_API_PROVIDER`, and `BATCH_API_POLL_TIMEOUT_HOURS` are still threaded into the workflow only to be echoed in a `batch_noop` log line; the workflow then immediately asserts it has `"no_llm_path"` / `"no_codex_execution_path"`. They are compatibility scaffolding, not live behavior.
  - **Recommended fix** — Retire the vars once downstream log scrapers are migrated, or move the compatibility echo into a narrower telemetry shim so the reusable workflow surface stops advertising dead knobs.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | BATCH-001 |
| Medium | 8 | BUG-001, API-001, API-002, DUP-001, EXPR-001, EXPR-002, EXPR-003, CONSIST-001 |
| Low | 4 | DUP-002, SHELL-001, DEAD-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2-3 | Medium |
| API call optimization | 3-4 | Medium |
| Code modularization | 7-9 | Large |
| Expression size reduction | 3-5 | Medium |
| Medium/Low fixes | 4-6 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-20)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is proven locally and can be collapsed without changing retry/error/concurrency behavior. `NEEDS_VERIFICATION` means the overlap is real, but a human or follow-on analysis must confirm payload/fallback parity first. `RISKY_SKIP` means the redundancy is visible, but it sits in polling/retry/race-defense code (or another protected path) and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

#### MERGE-001
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path and line ranges** — `scripts/review_rb_judge.sh:319-324,340-356` (top-level linked-issue resolution block)
- **Current call count** — `1` GraphQL call + `1..N` REST issue fetches (`1` in the common case, more if earlier linked issues have empty bodies)
- **Proposed call count** — `1`
- **Endpoint(s)** — GraphQL `repository.pullRequest.closingIssuesReferences`; REST `GET /repos/{repo}/issues/{issue_number}`
- **Evidence**
  ```sh
  ISSUE_NUMBERS="$(gh_retry gh api graphql \
    ... closingIssuesReferences(first: 50) { nodes { number } } ...)"

  ...

  ISSUE_META_JSON="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" || echo '{}')"
  BODY="$(printf '%s' "${ISSUE_META_JSON}" | jq -r '.body // ""' ...)"
  FIRST_ISSUE_LABELS_JSON="$(printf '%s' "${ISSUE_META_JSON}" | jq -c '[(.labels // [])[]?.name]' ...)"
  ```
- **Proposed fix** — Extend the existing GraphQL query at `scripts/review_rb_judge.sh:319-324` to request `body` and `labels(first: 100) { nodes { name } }`, then populate `FIRST_ISSUE`, `FIRST_ISSUE_BODY`, and `FIRST_ISSUE_LABELS_JSON` from that one response. If batching is used, follow the existing aliased-GraphQL pattern documented in `scripts/orchestrate_poll_process.sh` (`_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`).
- **Safety rationale** — `NEEDS_VERIFICATION` because this replaces per-issue REST reads with a richer GraphQL payload, so label/body parity and fail-open behavior must be checked before deleting the REST loop.
- **Downstream signal** — Verify on at least one PR with multiple linked issues and one label-heavy first issue that the merged query reproduces today’s `FIRST_ISSUE`, `FIRST_ISSUE_BODY`, and `FIRST_ISSUE_LABELS_JSON` outputs before removing `repos/{repo}/issues/{issue_number}` fetches.

#### MERGE-002
- **Safety tag** — `RISKY_SKIP`
- **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:2824-2834` (`Phase 7: Close PR and verify cancel_on_pr_close fires`)
- **Current call count** — `2` per poll iteration
- **Proposed call count** — `1` per poll iteration
- **Endpoint(s)** — `GET /repos/{repo}/actions/runs/{run_id}`
- **Evidence**
  ```sh
  while [ "${EXISTING_STATUS}" != "completed" ] && [ "$(date +%s)" -lt "${WAIT_DEADLINE}" ]; do
    sleep 5
    EXISTING_STATUS=$(gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}" \
      --jq '.status // ""' 2>/dev/null || echo "")
    EXISTING_CONCLUSION=$(gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}" \
      --jq '.conclusion // ""' 2>/dev/null || echo "")
  done
  ```
- **Proposed fix** — Poll `actions/runs/{id}` once per iteration into one JSON object (or `--jq '{status, conclusion}'`) and parse both fields locally.
- **Safety rationale** — `RISKY_SKIP` because this is a live polling loop in the release-gate watcher path; changing it can alter poll timing, transient-failure handling, and observable log output.
- **Downstream signal** — Do not auto-implement; manual review must preserve the existing 5s cadence, 600s deadline, empty-string fail-open behavior, and the exact status/conclusion diagnostics emitted by this watcher.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001
- **Safety tag** — `SAFE_TO_MERGE`
- **File path and line ranges** — `scripts/review_rb_judge.sh:294-311,326-329` (early PR guard + PR-body fallback in the same script body)
- **Current call count** — `2` on the `ISSUE_NUMBERS`-empty path
- **Proposed call count** — `1` on the normal path (`2` only if the retained fail-open fallback is needed)
- **Endpoint(s)** — `GET /repos/{repo}/pulls/{pr_number}`
- **Evidence**
  ```sh
  _pr_meta="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" 2>/dev/null || echo '{}')"
  ...
  unset _pr_meta _pr_state _pr_merged

  ...

  PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' || echo "")"
  ```
- **Proposed fix** — Before `unset _pr_meta`, derive and cache `PR_DATA` (or `PR_TITLE_BODY`) from that already-fetched JSON, then use it in the `ISSUE_NUMBERS` fallback. Keep the current `_safe_gh_jq` call only when `_pr_meta` is empty/invalid so error semantics stay fail-open.
- **Safety rationale** — `SAFE_TO_MERGE` because this is the same PR endpoint in the same script body, there is no intervening mutation that changes PR title/body, and retaining the fallback preserves current error handling.
- **Downstream signal** — Reuse the early `_pr_meta` payload for `PR_DATA`, but keep the later `pulls/{PR_NUMBER}` call as a fallback only when `_pr_meta` was empty or invalid.

#### REUSE-002
- **Safety tag** — `SAFE_TO_MERGE`
- **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:443-449` (`Create E2E test issue`)
- **Current call count** — `2`
- **Proposed call count** — `1`
- **Endpoint(s)** — `POST /repos/{repo}/issues`; `GET /repos/{repo}/issues/{issue_number}`
- **Evidence**
  ```sh
  ISSUE_NUMBER=$(gh api "repos/${TEST_REPO}/issues" \
    -f title="${TITLE}" \
    -f body="${BODY}" \
    --jq '.number')

  ISSUE_URL=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '.html_url')
  ```
- **Proposed fix** — Capture the full `POST /issues` response once, then parse both `.number` and `.html_url` from that response locally instead of doing a follow-up GET.
- **Safety rationale** — `SAFE_TO_MERGE` because the created-issue response already contains the URL being re-fetched, both reads happen in the same step, and no mutation occurs between them.
- **Downstream signal** — Capture the `POST /issues` JSON once, extract `.number` and `.html_url`, and delete the follow-up `GET /issues/{ISSUE_NUMBER}`.

### Dead Calls (DEAD-API-###)

#### DEAD-API-001
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path and line ranges** — `.github/workflows/review_autofix.yml:1549-1555` (`Collect PR metadata`, no-PR claude-branch path); supporting caller path `.github/workflows/internal-review.yml:78-82,101-118,123-134`
- **Current call count** — `1` conditional GET site, but `0` reachable executions from current in-repo `force_claude_branch_review` callers
- **Proposed call count** — `0`
- **Endpoint(s)** — `GET /repos/{repo}` for `.default_branch`
- **Evidence**
  ```sh
  # review_autofix.yml
  BASE_REF_OVERRIDE="${BASE_REF_OVERRIDE_INPUT:-}"
  if [ -z "${BASE_REF_OVERRIDE}" ]; then
    BASE_REF_OVERRIDE="$(gh api "repos/${{ github.repository }}" --jq '.default_branch' 2>/dev/null || echo 'main')"
  fi
  ```

  ```sh
  # internal-review.yml
  echo "base_ref=${base_ref}" >> "${GITHUB_OUTPUT}"
  ...
  base_ref_override: ${{ needs.resolve-claude-branch-pr.outputs.base_ref }}
  ```
- **Proposed fix** — Treat `base_ref_override` as required whenever `force_claude_branch_review=true` in this repo’s caller matrix, and replace the silent repo GET fallback with an assertion/log if it is unexpectedly empty.
- **Safety rationale** — `NEEDS_VERIFICATION` because the call is dead for current in-repo wrappers, but the reusable workflow still advertises an “empty means default branch” contract that should be checked before tightening.
- **Downstream signal** — Verify that no in-repo wrapper/test invokes `review_autofix.yml` with `force_claude_branch_review=true` and an empty `base_ref_override`; only then remove the fallback GET or turn it into an explicit assertion.

### Cross-References to Deep Audit Section
- `BATCH-001`: `RISKY_SKIP` — correct hotspot, but it is dispatch/poll helper consolidation inside watcher loops and must preserve retry/log semantics.
- `API-001`: `SAFE_TO_MERGE` — the push event already carries `github.event.repository.default_branch`, so the extra repo GET is redundant in-step.
- `API-002`: `NEEDS_VERIFICATION` — batching fallback label reads into GraphQL is directionally right, but it changes REST-vs-GraphQL shape and needs parity checks first.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 2 | REUSE-001, REUSE-002 |
| NEEDS_VERIFICATION | 2 | MERGE-001, DEAD-API-001 |
| RISKY_SKIP | 1 | MERGE-002 |

### Implement-Stage Handoff
- `REUSE-002`
- `REUSE-001`
