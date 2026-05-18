## Executive Summary

- `CI` has one dominant failure mode: runs `26029332723`, `26031068317`, `26032757226`, `26034365768`, and `26035468279` all failed in `lint / Validation self-test unit tests` with the same assertion, `implement.yml missing resolved-ref log output`. Estimated impact: remove the observed `17.9%` CI-family failure rate and recover about `3,090s` of failed CI time. Confidence: high.
- CI failure feedback is late. In failed run `26032757226`, `lint / Orchestrate poll process unit tests` consumed `581.9s` before the failing audit step ran for only `3.2s`. Estimated impact: about `9.7 minutes` faster failure feedback per bad run if the audit moves earlier. Confidence: high.
- `review_autofix` is the main latency/cost driver: `133` runs, `821.1s` average, `576s` p50, `2275.8s` p95, and `109,207` of `155,988` observed run-seconds. Small PRs still pay full AI cost: run `26031079673` reviewed a `6-file / 61-addition` PR for `2202s`. Estimated impact: high. Confidence: high.
- `workflow_log_analysis` is the release long tail: run `26009107875` took `3754s`, split roughly into `1605.6s` `analyze-commit-notify`, `1361.1s` `deep-audit`, `676.2s` `api-redundancy`, and `87.4s` `collect-logs`. The workflow file itself says to lower deep-audit reasoning from `xhigh` to `high` first if timeouts recur. Estimated impact: `6-13 minutes` per audit run. Confidence: high.
- GH API pain is mostly redundancy, not outage: `resolve-claude-branch-pr` made two calls in each of `4` observed skip runs; `review_autofix.yml` and `issue_pr_status.yml` contain `5` separate `closingIssuesReferences` query sites; the long `e2e-smoke-test` polling loop likely issued hundreds of run-list calls (inference). No sampled run showed a real `429` or secondary rate-limit incident. Estimated impact: medium. Confidence: medium-high.
- AI memory retrieval is currently not paying off: `5/5` `retrieve` ops returned `0` records, `estimated_tokens=0`, `keyword_method=none`; prompt-cache tuning is blocked because no operational `prompt_tokens` or `cache_*_tokens` counters were emitted anywhere in deep-dive logs. Estimated impact: medium. Confidence: high.
- The repo-wide `p50=1s` is misleading because `725/1000` runs were skipped wrappers. On non-skipped runs, the effective median is about `172s` and p95 about `2165s`, so optimization effort should stay focused on active `review_autofix`, `ci`, and release-tail workflows. Estimated impact: medium on prioritization quality. Confidence: high.

## Speed Optimizations

1. **[Critical path] Right-size `review_autofix` for small diffs**
   - **Evidence:** `review_autofix` is `70%` of observed run-seconds. Run `26031079673` (`Codex PR Self-Healing Semantic Agent`) took `2202s` on PR `#2739` with `6 files / 61 additions`. Run `26033500333` took `1947s` on PR `#2738` with `4 files / 166 additions`. Those runs logged `REVIEWER_REASONING_EFFORT: xhigh`, `EDITOR_REASONING_EFFORT: xhigh`, `ENABLE_REVIEWER_TWO_PASS: true`.
   - **Root cause:** small/medium diffs are still using a full reviewer panel and `xhigh` reasoning. In `review_autofix.yml`, the pass-2 diff-size gate is explicitly a no-op because both `REVIEWER_PASS2_REASONING_SMALL` and `..._LARGE` default to `xhigh`.
   - **Exact change:** make the existing gate real: set `REVIEWER_PASS2_REASONING_SMALL=high` first; if quality holds, reduce the small-diff reviewer set from `6` models to `4` while keeping the current path for large diffs, conflicts, and retries.
   - **Estimated time savings:** inference `15-30%` on qualifying `review_autofix` runs, or roughly `3-10 minutes` on the `20-36 minute` runs observed.
   - **Implementation risk:** medium.

2. **[Critical path] Fail fast on workflow-contract drift in CI**
   - **Evidence:** all `5` CI failures hit the same audit. In run `26032757226`, the slow step was `lint / Orchestrate poll process unit tests` at `581.9s`; the actual failing step `lint / Validation self-test unit tests` lasted only `3.2s`.
   - **Root cause:** a fast workflow-contract check is sequenced after a long unit-test block.
   - **Exact change:** move `tests/test_workflow_checkout_integration_ref_audit.py` to the start of `lint`, or split it into a tiny early `workflow-contracts` job that must pass before the long `test_orchestrate_poll_process.py` step runs.
   - **Estimated time savings:** about `582s` faster failure feedback per broken CI run; about `2,910s` avoided across the `5` observed failures.
   - **Implementation risk:** low.

3. **[Critical path] Lower `workflow_log_analysis` audit reasoning before adding more timeout**
   - **Evidence:** run `26009107875` took `3754s`; `deep-audit` alone took `1361.1s`, `analyze-commit-notify` `1605.6s`. `workflow-log-analysis.yml` already documents: “If audit passes start timing out again, lower this to `high` first.”
   - **Root cause:** `xhigh` reasoning on a very wide prompt surface.
   - **Exact change:** lower the deep-audit Codex reasoning level from `xhigh` to `high` first; if output quality stays acceptable, do the same for `api-redundancy`.
   - **Estimated time savings:** inference `10-20%` on the `3754s` audit, or roughly `6-13 minutes` per run.
   - **Implementation risk:** low-medium.

4. **[Micro / low-control] Trim Copilot code-review artifact overhead only if the workflow is configurable**
   - **Evidence:** sampled Copilot runs `26030256812`, `26031072272`, `26033503051`, and `26034366984` took `173-276s`; summaries point to `actions/runs/<run>/artifacts`, artifact download, and cleanup as the clearest time sinks.
   - **Root cause:** artifact list/download/delete overhead plus runner wait.
   - **Exact change:** if this workflow is repo-configurable, skip artifact enumeration/deletion when there is no `results-agent` artifact, or scope the workflow to label/merge-queue paths instead of every PR.
   - **Estimated time savings:** tens of seconds per Copilot run.
   - **Implementation risk:** medium, and control may be limited.

5. **[Micro] Do not spend time collapsing skipped wrappers yet**
   - **Evidence:** skipped `clarify`/`plan`/`implement`/`orchestrate_clarify_respond` runs totaled `723` runs but only `860s` combined (`0.55%` of all observed run time).
   - **Root cause:** event-filter/UI noise, not compute.
   - **Exact change:** none now; treat this as reporting cleanup, not a speed project.
   - **Estimated time savings:** negligible.
   - **Implementation risk:** none.

## Cost Optimizations

1. **Reduce small-diff AI spend in `review_autofix`**
   - **Evidence:** runs `26031079673` and `26033500333` show modest PR sizes still using `6` reviewer models, `openai/gpt-5.4` editor, and `xhigh` reviewer/editor reasoning. `review_autofix.yml` confirms the small/large pass-2 defaults are both `xhigh`.
   - **Root cause:** full reviewer fan-out and high reasoning are applied too broadly.
   - **Exact change:** set `REVIEWER_PASS2_REASONING_SMALL=high` first; then, if quality holds, use a smaller reviewer set for diffs under the existing pass-2 large-diff threshold.
   - **Estimated savings:** inference `15-35%` of `review_autofix` AI cost on qualifying PRs.
   - **Quality-risk notes:** medium risk; safest rollout is “cheap first pass, full panel on disagreement/conflict/retry.”

2. **Lower `summarize_unselected_runs` breadth**
   - **Evidence:** `workflow_log_analysis` run `26009107875` logged `AI_MEMORY_TELEMETRY` `op=summarize_unselected_runs`, `targeted=100`, `summarized=87`, `tokens_used=150488` on `openai/gpt-5.4-mini`. Workflow defaults are `WORKFLOW_LOG_SUMMARY_MAX_RUNS=100` and token budget `1500000`.
   - **Root cause:** wide newest-first summarization, even when failures/slow/recent runs already have deep evidence.
   - **Exact change:** lower the default max from `100` to `50`, or stop once each active family without a deep dive has at least one summary.
   - **Estimated savings:** about `75k` tokens per workflow-log-analysis run at current density.
   - **Quality-risk notes:** low-medium; keep failures/slow/recent untouched.

3. **Keep Semble; do not spend effort on Serena yet**
   - **Evidence:** there were `8` operational `SEMBLE_QUERY` lines across slow `review_autofix` runs, totaling `87,681` bytes at about `481ms` average. `5` were `target=reviewer-context` queries and `3` were overflow-file lookups. All `5` observed `SEMBLE_FALLBACK` lines came from `test_and_mark_stable` run `26009091997` `validate-scripts`, with `missing_semble` fixture paths. There were `0` operational `SERENA_QUERY` / `SERENA_FALLBACK` / `SERENA_PROBE` lines, and `10` sampled config-bearing runs logged `SERENA_ENABLED: false`.
   - **Root cause:** Semble is the active retrieval path; Serena is effectively not in rollout.
   - **Exact change:** keep Semble enabled, defer Serena optimization until it emits real telemetry, and add per-phase usage counters so Semble’s savings can be measured directly.
   - **Estimated savings:** indirect, but it avoids optimizing a currently disabled system.
   - **Quality-risk notes:** low. Semble currently looks targeted rather than noisy; its `~11 KB/query` average is well below the `102400` byte targeted-context cap logged in review runs.

4. **Do not try to “optimize prompt cache” blind**
   - **Evidence:** operational deep-dive logs emitted `0` occurrences of `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens`. The only prompt-cache signal found was config like `OPENROUTER_PROMPT_CACHE_DISABLED: false` in run `26027445588`.
   - **Root cause:** missing telemetry, not necessarily poor cache behavior.
   - **Exact change:** emit normalized usage and cache counters for reviewer, summarizer, editor, and workflow-log-analysis phases before changing cache settings.
   - **Estimated savings:** not safely quantifiable yet.
   - **Quality-risk notes:** none; this is instrumentation only.

**Cost caution:** do not treat all cancelled `review_autofix` runs as extra AI burn. Of the family’s `32,233` cancelled seconds, `32,011` were on the wrapper `Internal: AI Review & Autofix`; child `Codex PR Self-Healing Semantic Agent` cancellations totaled only `222s`.

## Reliability Improvements

1. **Fix the `implement.yml` vs audit contract drift**
   - **Failure evidence:** CI runs `26029332723`, `26031068317`, `26032757226`, `26034365768`, and `26035468279` all failed in `lint / Validation self-test unit tests`. Deep logs for `26032757226`, `26034365768`, and `26035468279` all ended with `AssertionError: implement.yml missing resolved-ref log output`.
   - **Root cause category:** workflow/test contract drift.
   - **Exact fix:** keep `.github/workflows/implement.yml` and `tests/test_workflow_checkout_integration_ref_audit.py` synchronized around the exact `Resolved fallback ref:` / `PR base ref:` contract; if rollout spans branches, temporarily accept both old and new strings.
   - **Expected reliability impact:** CI family failure rate should drop from `17.86%` (`5/28`) to near zero for this window.
   - **Rollback / fail-open:** widen the matcher before changing log text again.

2. **Stabilize support-script checkout for `issue_pr_status`**
   - **Failure evidence:** recent successful runs `26035586181` and `26035947897` logged `Support checkout ref ... is unavailable; using main`; run `26035586181` also logged `Could not fetch tg_helpers.sh; skipping TG cleanup.`
   - **Root cause category:** support-asset ref skew / optional helper availability.
   - **Exact fix:** ensure the support bundle, especially `tg_helpers.sh`, exists on the selected `script_ref`/`stable`, or intentionally pin that helper checkout to `main` when `stable` lacks it.
   - **Expected reliability impact:** fewer silent notification and cleanup skips on merged-PR flows.
   - **Rollback / fail-open:** keep the current warning-and-continue behavior.

3. **Treat observed Semble fallbacks as healthy test fail-open behavior**
   - **Failure evidence:** `5` `SEMBLE_FALLBACK target=overflow` lines appeared only in `test_and_mark_stable` run `26009091997` step `validate-scripts`, all with `reason=[Errno 2] ... missing_semble` and `ms=0`.
   - **Root cause category:** test-fixture coverage, not production outage.
   - **Exact fix:** classify `missing_semble` fallbacks under `validate-scripts` as expected test coverage and exclude them from production fallback alarms.
   - **Expected reliability impact:** cleaner fallback monitoring; less time chasing non-incidents.
   - **Rollback / fail-open:** preserve the existing fail-open runtime behavior.

4. **Make AI-memory telemetry parsing JSON-strict**
   - **Failure evidence:** deep-dive parsing found `50` non-JSON `AI_MEMORY_TELEMETRY:` matches; `23` were in `workflow_log_analysis` run `26009107875` `analyze-commit-notify`, where report/instruction text echoed the prefix.
   - **Root cause category:** observability/log-format pollution.
   - **Exact fix:** only parse lines that start `AI_MEMORY_TELEMETRY: {`; avoid echoing the raw prefix in generated markdown where possible.
   - **Expected reliability impact:** fewer false positives in AI-memory health reporting.
   - **Rollback / fail-open:** ignore malformed lines; do not fail workflows on them.

5. **Separate wrapper cancellation noise from real `review_autofix` instability**
   - **Failure evidence:** `review_autofix` shows `33` cancelled runs / `32,233s` cancelled time, but `32,011s` of that sits on the wrapper `Internal: AI Review & Autofix`; child cancellations total only `222s`.
   - **Root cause category:** orchestration/accounting noise.
   - **Exact fix:** label wrapper cancellations as `superseded` and track child-workflow reliability separately in dashboards and alerts.
   - **Expected reliability impact:** fewer false rerun investigations and cleaner SLOs.
   - **Rollback / fail-open:** none; this is reporting-only.

**Serena note:** no operational `SERENA_FALLBACK` or `SERENA_PROBE` lines were observed, and sampled config-bearing runs showed `SERENA_ENABLED: false`, so there is no evidence of a masked Serena rollout failure in this window.

## AI Memory Health

- Parsed `21` valid JSON `AI_MEMORY_TELEMETRY` events from deep-dive logs: `10` `record-run-event`, `5` `retrieve`, `5` `record-candidate`, and `1` `summarize_unselected_runs`. No `finalize-task`, `promote`, `compact`, or `processed-command-*` ops were observed.
- **Retrieve hit rate:** `0/5 = 0%`. All five `retrieve` ops came from slow `review_autofix` runs `26027445588`, `26013098223`, `26029342776`, `26014929366`, and `26030256506`, and every one logged `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`, `enabled=true`, `fail_open=false`.
- **Average `estimated_tokens` vs budget:** average was `0`. No retrieval-budget field was emitted, so there is no valid budget comparison yet.
- **Keyword-method distribution:** `none=5`, `plain=0`, `llm=0`.
- **Push health:** all `10` `record-run-event` ops and all `5` `record-candidate` ops logged `push_attempts=1` and `did_push=true`; no high retry counts were observed.
- **Spend hotspot:** `workflow_log_analysis` run `26009107875` logged `summarize_unselected_runs` with `targeted=100`, `summarized=87`, `skipped_empty_logs=13`, and `tokens_used=150488`.
- **Assessment:** the memory system appears mechanically healthy on writes, but retrieval is currently not contributing useful context.
- **Smallest safe next step:** inspect the reviewer retrieval query/record tags, add a retrieval budget field, and enable a plain-keyword fallback before changing write volume or retention policy.

## GH API Call Audit

| Pattern | Evidence | Observed sample volume | Exact change | Estimated reduction |
|---|---|---:|---|---:|
| Redundant PR-skip lookup in `internal-review.yml` `resolve-claude-branch-pr` | Runs `26029329678`, `26031066834`, `26032754907`, `26035465941` each logged both `pulls?state=open&head=...` and repo `default_branch` lookup before immediately skipping | `8` calls across `4` runs | Use `github.event.repository.default_branch` or only fetch default branch on the `proceed=true` path | `1` call saved per run (`50%` of that step) |
| Duplicated `closingIssuesReferences` GraphQL lookups | `review_autofix.yml` has `4` call sites; `issue_pr_status.yml` has `1`; live use is visible in run `26035947411` | `5` code call sites | Fetch linked issues once per PR event and pass the JSON between steps/jobs | `3-4` GraphQL calls per merged-PR flow |
| Fixed-interval review polling in `test-and-mark-stable` | Run `26009091997` had `e2e-smoke-test` at `3667.6s`; workflow polls `actions/runs` every `15s` | Inference: roughly `200-240` run-list polls on a long smoke run before extra PR/job-log calls | Add adaptive backoff (`15s -> 30s -> 60s`) after no state change; only fetch live logs after a run enters `in_progress` or changes state | Inference: `50-70%` fewer poll calls |
| Copilot artifact cleanup | Runs `26030256812`, `26031072272`, `26033503051`, `26034366984` each logged `/actions/runs/<run>/artifacts` in cleanup | `4` observed calls | If configurable, skip artifact enumeration when there is no retained artifact to delete | `1` call per run |
| Rate-limit handling | `cancel_on_pr_close` run `26035947584` hit `/rate_limit` in `_rl_wait` and explicitly logged no `429` / secondary-rate-limit incident | `0` real incidents observed | Keep current retry hygiene; focus on redundancy reduction, not emergency throttling | Risk reduction rather than direct call savings |

Cross-reference to repo API hygiene: `test-and-mark-stable.yml` already documents that staggered dispatch/polling is meant to stay under the `5000/hr` GH_PAT budget. The recommendation above adds headroom; it is not responding to an active rate-limit outage.

## Prompt Cache & Memory System

- **Prompt cache is configured but not observable.** Runs like `26027445588` logged `OPENROUTER_PROMPT_CACHE_DISABLED: false`, but no operational `prompt_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens` counters were emitted anywhere in deep-dive logs. That means hit/miss behavior is currently unknown.
- **Semble looks targeted, not noisy.** The `8` operational `SEMBLE_QUERY` lines were all in slow `review_autofix` runs, totaling `87,681` bytes at ~`481ms` average. `5` `reviewer-context` queries accounted for `65,342` bytes (~`13.1 KB` each), and `3` overflow queries accounted for `22,339` bytes (~`7.4 KB` each). Against the logged `TARGETED_FILE_CONTEXT_MAX_BYTES=102400`, this likely reduces prompt expansion rather than bloating it. This is an inference because token counters are missing.
- **Serena is not participating yet.** No operational `SERENA_QUERY` / `SERENA_FALLBACK` / `SERENA_PROBE` lines were found, and sampled config-bearing runs consistently logged `SERENA_ENABLED: false`. Serena is therefore neither saving cost nor adding noisy context today.
- **Memory retrieval effectiveness is poor.** `5/5` retrieves returned zero records, so prompt-side memory augmentation is probably not helping current review flows.
- **Likely cache-fragmentation sources (inference):** highly dynamic PR metadata, large targeted file context (`102400` byte cap), varying reviewer panels, and injected overflow snippets. If those dynamic blocks are placed before stable system instructions, prompt-cache reuse will be poor even when the task shape repeats.
- **Concrete improvement:** keep a stable prefix first (system prompt, workflow instructions, reviewer checklist), then append dynamic PR metadata, Semble hits, and overflow file context in deterministic order. Emit normalized usage and cache counters per reviewer/editor/summarizer phase so the impact can be measured.

## Orchestrator Health

- **Core poller looks healthy.** `orchestrate_poll` had `23/23` successful runs, `130.1s` average, `136s` p50, `168s` p95. Sample run `26032677838` logged `poll` runtime around `122s`, with some runner wait but no stall/backlog signal.
- **No evidence of a clarification-loop storm.** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` produced many skips (`723` combined) but only `860s` total. That is UI/event noise, not a compute bottleneck.
- **Handoff logic appears sane.** `issue_pr_status` run `26035586181` logged `Orchestrator-managed issue — skipping PR merged alert (poller handles completion)`, which is the right separation of responsibilities.
- **Operational pain points are mostly peripheral:** runner queueing in poller/review flows, support-helper fallback in issue sync, and wrapper cancellation noise in `review_autofix`.
- **Bounded conclusion:** only `2` `orchestrate` bootstrap runs were in the window, and no deep-dive bootstrap logs were captured, so wave-progression conclusions should stay conservative.
- **Track these indicators:** non-skipped run p50/p95, `review_autofix` child latency, wrapper cancelled seconds, poll backlog count, AI-memory retrieve hit rate, and Semble query count per review run.

## Pipeline Flow Bottlenecks

| Flow segment | Evidence | Dominant bottleneck | Ordered fix |
|---|---|---|---|
| Clarify → Plan → Respond wrappers | `723` skipped runs, only `860s` total | Reporting noise, not compute | Deprioritize; do not spend optimization time here yet |
| Implement | `7` successful runs, success-only average `493.1s` | Real compute when active, but limited sample | Leave secondary for now; bigger wins are in review/CI |
| Review / Autofix | `133` runs, `821.1s` average, `2275.8s` p95; small PRs still took `1322-2202s`; slow deep logs show `codex-agent` steps at `1844-2500s` | Compute first, queueing second | Make small-diff reviewer gate real; keep full path only for large/conflict/retry cases |
| Validate / CI | `28` CI runs, `750.8s` average, `5` identical failures; in run `26032757226` a `581.9s` test preceded a `3.2s` failing audit | Failure-feedback ordering | Move the fast workflow audit to the front and fix the drift |
| Release tail (`test_and_mark_stable` / `workflow_log_analysis`) | `test_and_mark_stable` run `26009091997` was dominated by `workflow-log-analysis-test` `3795.9s` and `e2e-smoke-test` `3667.6s`; standalone `workflow_log_analysis` run `26009107875` took `3754s` | Heavy compute plus polling overhead | Lower audit reasoning, cut summary breadth, add adaptive poll backoff |
| Orchestrate poll loop | `23/23` success, `168s` p95 | Healthy | Monitor runner wait only; no major redesign indicated |

**Queueing overhead:** runner-wait messages were visible in `review_autofix` (`26031079673`, `26032757513`, `26035468432`), Copilot review (`26030256812`, `26031072272`), `orchestrate_poll` (`26032677838`), and forward-merge flows.  
**Retry overhead:** `forward_merge_stable_to_main` run `26035946678` logged `git push` retries before succeeding.  
**Merge/conflict overhead:** most “cancelled” `review_autofix` wall time is wrapper-side accounting, not repeated child AI work.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` long tail (`133` runs, `821.1s` avg, `2275.8s` p95)
  - `CI` late-failing workflow contract audit (`5` identical failures)
  - Release tail from `workflow_log_analysis` / `test_and_mark_stable`

- **Top failure modes**
  - `implement.yml` vs workflow-audit drift in CI
  - Support-helper fallback in `issue_pr_status` (`script_ref` unavailable, `tg_helpers.sh` missing once)
  - Observability noise from non-JSON `AI_MEMORY_TELEMETRY:` echoes

- **Highest-cost drivers**
  - Small/medium-diff `review_autofix` runs still using full high-reasoning AI panels
  - `summarize_unselected_runs` (`150,488` tokens in run `26009107875`)
  - Copilot artifact download/cleanup overhead on every sampled Copilot review run

- **Top 3 prioritized actions**
  1. Fix the `implement.yml` audit drift and move `test_workflow_checkout_integration_ref_audit.py` to the front of CI.
  2. Make the existing `review_autofix` small-diff gate real by lowering small-diff reviewer pass-2 reasoning first.
  3. Lower `workflow_log_analysis` deep-audit reasoning from `xhigh` to `high` and cut unselected-run summary breadth.

## Metrics Appendix

### Run summary

| Scope | Runs | Success | Failure | Cancelled | Other/Skipped | Avg s | p50 s | p95 s | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| All runs | 1000 | 234 | 5 | 36 | 725 | 156.0 | 1.0 | 1209.0 | `sampled_success_runs=0` |
| Non-skipped runs | 275 | 234 | 5 | 36 | 0 | 563.5 | 172.0 | 2164.7 | Computed from `workflow_log_report.json` |

### Key workflow families

| Workflow family | Runs | Success | Failure | Cancelled | Other | Avg s | p50 s | p95 s | Note |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| review_autofix | 133 | 99 | 0 | 33 | 1 | 821.1 | 576.0 | 2275.8 | Includes wrapper + child runs |
| ci | 28 | 23 | 5 | 0 | 0 | 750.8 | 781.0 | 802.9 | All failures same step |
| copilot_pull_request_reviewer | 27 | 27 | 0 | 0 | 0 | 182.9 | 172.0 | 299.8 | Artifact-heavy |
| orchestrate_poll | 23 | 23 | 0 | 0 | 0 | 130.1 | 136.0 | 168.0 | Healthy |
| implement | 184 | 7 | 0 | 3 | 174 | 20.1 | 1.0 | 7.2 | Success-only avg `493.1s` |
| plan | 185 | 7 | 0 | 0 | 178 | 10.9 | 1.0 | 7.6 | Success-only avg `257.4s` |
| clarify | 195 | 8 | 0 | 0 | 187 | 5.6 | 1.0 | 9.0 | Success-only avg `107.0s` |
| workflow_log_analysis | 1 | 1 | 0 | 0 | 0 | 3754.0 | 3754.0 | 3754.0 | Heavy release-tail job |
| test_and_mark_stable | 1 | 1 | 0 | 0 | 0 | 3835.0 | 3835.0 | 3835.0 | Dominated by parallel long jobs |

### `review_autofix` cancellation split

| Workflow name | Runs | Success | Cancelled | Total duration s | Cancelled s |
|---|---:|---:|---:|---:|---:|
| Internal: AI Review & Autofix | 78 | 57 | 20 | 77,762 | 32,011 |
| Codex PR Self-Healing Semantic Agent | 34 | 21 | 13 | 31,286 | 222 |
| Internal: AI Review Autofix Sweep | 21 | 21 | 0 | 159 | 0 |

### Token, cache, and AI-memory metrics

| Metric | Value | Evidence |
|---|---:|---|
| `summarize_unselected_runs` tokens | 150,488 | `workflow_log_analysis` run `26009107875` |
| Unselected runs targeted / summarized | 100 / 87 | Same run |
| AI-memory JSON-valid events | 21 | Deep-dive logs |
| AI-memory malformed prefix matches | 50 | Deep-dive logs; mostly report/instruction echoes |
| AI-memory retrieve ops | 5 | Runs `26027445588`, `26013098223`, `26029342776`, `26014929366`, `26030256506` |
| AI-memory retrieve hits | 0 | `0%` hit rate |
| Avg retrieve `estimated_tokens` | 0 | No retrieval-budget field emitted |
| `keyword_method` distribution | `none=5`, `plain=0`, `llm=0` | Retrieve ops only |
| `fail_open:true` retrieves | 0 | None observed |
| `enabled:false` retrieves | 0 | None observed |
| High push retries | 0 | All observed pushes used `push_attempts=1` |
| Operational prompt-token counters emitted | 0 | No `prompt_tokens` / `completion_tokens` / `total_tokens` lines found |
| Operational prompt-cache counters emitted | 0 | No `cache_creation_input_tokens` / `cache_read_input_tokens` lines found |

### GH API observed hot spots

| Pattern | Observed runs / source | Observed sample volume | Notes |
|---|---|---:|---|
| `pulls?state=open&head=...` + repo `default_branch` lookup in `resolve-claude-branch-pr` | `26029329678`, `26031066834`, `26032754907`, `26035465941` | 8 calls | Redundant on skip path |
| `closingIssuesReferences` GraphQL lookup | `review_autofix.yml` (`4` call sites), `issue_pr_status.yml` (`1` call site), live in `26035947411` | 5 code call sites | Cache once per PR event |
| `actions/runs?branch=...` fixed polling in smoke test | `test_and_mark_stable` run `26009091997` | Inference: ~200-240 polls | 15s loop over a 61m step |
| `/actions/runs/<run>/artifacts` cleanup | `26030256812`, `26031072272`, `26033503051`, `26034366984` | 4 calls | Low-control unless workflow is configurable |
| Real `429` / secondary-rate-limit incidents | Sampled deep-dive runs | 0 | `26035947584` explicitly logged none |

### Semble / Serena / MCP telemetry

| Server | Target | Query count | Query bytes | Avg query ms | Fallback count | Probe count | Response bytes | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---|
| Semble | reviewer-context | 5 | 65,342 | 479.2 | 0 | 0 | 0 observed | Slow `review_autofix` reviewer-context lookups |
| Semble | overflow | 3 | 22,339 | 483.3 | 5 | 0 | 0 observed | All `5` fallbacks were test-only in run `26009091997` |
| Serena | n/a | 0 | 0 | n/a | 0 | 0 | 0 | No operational telemetry; `SERENA_ENABLED: false` in `10` sampled config-bearing runs |
| Other MCP servers observed | n/a | 0 | 0 | n/a | 0 | 0 | 0 | None observed |

### MCP availability rows

| Server | Target | probe_ok | probe_failed | probe_skipped | Note |
|---|---|---:|---:|---:|---|
| Semble | reviewer-context | 0 | 0 | 0 | No `SEMBLE_PROBE` telemetry format emitted in this window |
| Semble | overflow | 0 | 0 | 0 | Runtime queries/fallbacks only |
| Serena | n/a | 0 | 0 | 0 | No operational `SERENA_PROBE` lines; server disabled in sampled config-bearing runs |

## Deep Audit — Workflows & Scripts (2026-05-18)

### Section 1: Bug & Correctness Sweep

- **ID** — `BUG-001`
  - **File** — `.github/workflows/issue_pr_status.yml:240-248`; `.github/workflows/review_autofix.yml:4504-4523,4625-4644,5462-5470`
  - **Severity** — High
  - **Category tag** — `bug`
  - **Description** — These workflows define fallback `set_issue_phase_label_resilient()` shims that only `POST` the target label. The canonical helper in `scripts/label_helpers.sh:146-197` first reads the current labels, removes every prior phase label from `_AI_PHASE_LABELS`, and `PUT`s the deduplicated set. If `label_helpers.sh` is missing or fails to source, the fallback can leave contradictory phase labels on the same issue (`ai:done` + `ai:review-blocked`, `ai:merged` + `ai:review-blocked`, etc.), which breaks the repo’s single-phase-label contract.
  - **Recommended fix** — Make `scripts/label_helpers.sh::set_issue_phase_label_resilient <issue_number> <target_label> [repo]` a hard requirement before any phase-label mutation, or vendor its full GET/PUT/POST logic into the fallback. Do not keep the current POST-only shim.

- **ID** — `BUG-002`
  - **File** — `.github/workflows/review_autofix.yml:4528-4538,4650-4658,5478-5486`; `scripts/review_rb_judge.sh:246-256`
  - **Severity** — High
  - **Category tag** — `bug`
  - **Description** — These fallback linked-issue resolvers still accept bare `issues/N` and `issue #N` mentions, even though `.github/workflows/issue_pr_status.yml:46-61` was explicitly hardened to reject those loose references after the `#1469` mislabeling class. In `review_autofix.yml`, the permissive regex feeds label-mutating paths (`ai:ready-to-merge`, `ai:review-blocked`); in `review_rb_judge.sh` it drives judge actions. A PR body that casually mentions another issue can therefore mutate an issue that the close-handler intentionally does not treat as linked.
  - **Recommended fix** — Centralize PR-linked-issue fallback parsing in one helper and use a strict “closing-keyword or repo-scoped URL only” mode for any label/close/judge path. If a looser regex is still useful for context gathering, keep it read-only and separate from state mutations.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`
  - **File** — `.github/workflows/review_autofix.yml:1561-1596`
  - **Severity** — High
  - **Category tag** — `api-redundancy`
  - **Description** — The normal PR path in `Collect PR metadata` makes **5 logical API calls** in one execution path: `pulls/{pr}` (line 1561), issue comments (1562-1563), reviews (1564-1565), review comments (1566-1567), and a separate GraphQL `closingIssuesReferences` fetch (1591-1596). The repo already has a batched PR hydration pattern in `scripts/gh_helpers.sh:761-900` (`gh_pr_with_all_comments`), but this hot path reimplements the same shape inline instead of extending that helper.
  - **Recommended fix** — Extend `gh_pr_with_all_comments owner repo pr_number preloaded_meta_json` to emit `reviews` and `closing_issues` alongside `meta/comments/review_comments`, then replace this 5-call block with **1 GraphQL call**. If you want a staged rollout, an intermediate **2-call** path (helper + separate closing-issues query) is still materially better. Existing batching pattern to extend: `scripts/gh_helpers.sh::gh_pr_with_all_comments`.

- **ID** — `API-002`
  - **File** — `.github/workflows/issue_pr_status.yml:280-349,501-512`
  - **Severity** — Medium
  - **Category tag** — `api-redundancy`
  - **Description** — `Update linked issue labels` already does **1 batched GraphQL call** to classify linked issues as tracking/managed (`ORCH_ALIAS_FRAGMENT`). `Send PR merged Telegram alert` then does up to **N extra REST issue GETs** (lines 505-512) to rediscover whether any linked issue is orchestrator-managed. Worst case, the workflow spends **1 + N** calls to answer one boolean, where `N` is bounded by the earlier `closingIssuesReferences(first: 50)` result set. The later step also only checks the body marker, so it throws away the label-based branch of the earlier classification.
  - **Recommended fix** — Export `MANAGED_ISSUES` or a single `IS_ORCHESTRATED=true|false` flag to `$GITHUB_ENV` in the first step and reuse it in the alert step. Proposed call count: **1 total**. Existing batching pattern to extend: the same `ORCH_ALIAS_FRAGMENT` batch, or the canonical aliased-GraphQL shape used by `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`
  - **File** — `.github/workflows/validate.yml:236-304`; `.github/workflows/issue_pr_status.yml:69-120`; `.github/workflows/validation-improvements-intake.yml:72-140`
  - **Severity** — Medium
  - **Category tag** — `duplication`
  - **Description** — The support-checkout bootstrap is cloned across multiple workflows: `checkout_support_ref`, dual primary/main support roots, `resolved_script_ref`, and `fetch/copy_from_ref_or_local`. The copies already differ in file allowlists and fallback behavior, which makes support-asset drift easy to reintroduce.
  - **Recommended fix** — Extract a shared module, e.g. `scripts/support_checkout.sh`, with functions like `stage_support_repo <wf_source> <script_ref> <workspace_root> <runner_temp>` and `copy_from_support <repo_path> <target_path> [require_remote=false] [allow_main_fallback=true]`; update `validate.yml`, `issue_pr_status.yml`, and `validation-improvements-intake.yml` to call it.

- **ID** — `DUP-002`
  - **File** — `.github/workflows/review_autofix.yml:4504-4523,4625-4644,5462-5470`; `.github/workflows/issue_pr_status.yml:240-248`
  - **Severity** — Medium
  - **Category tag** — `duplication`
  - **Description** — The phase-label fallback helper is duplicated four times in workflow YAML even though the canonical implementation already exists in `scripts/label_helpers.sh:146-197`. This duplication has already drifted semantically; see `BUG-001`.
  - **Recommended fix** — Use `scripts/label_helpers.sh::set_issue_phase_label_resilient <issue_number> <target_label> [repo]` as the only implementation. Fetch/source it once per job, then delete the inline copies in `review_autofix.yml` and `issue_pr_status.yml`.

- **ID** — `DUP-003`
  - **File** — `.github/workflows/review_autofix.yml:4528-4538,4650-4658,5478-5486`; `.github/workflows/issue_pr_status.yml:46-61`; `scripts/review_rb_judge.sh:246-256`
  - **Severity** — Medium
  - **Category tag** — `duplication`
  - **Description** — Linked-issue fallback parsing is copied in three places with different regex policy. That duplication has already produced behavior skew: `issue_pr_status.yml` is strict, while `review_autofix.yml` and `review_rb_judge.sh` are permissive (`BUG-002`).
  - **Recommended fix** — Add one shared helper, e.g. `scripts/gh_helpers.sh::resolve_linked_issue_numbers <repository> <pr_number> <pr_meta_file> <mode>`, returning a JSON array. Update callers so label/close/judge flows use `strict_closing_only`, while any broader context-only parsing uses an explicit read-only mode.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`
  - **File** — `.github/workflows/test-and-mark-stable.yml:1203-1588`
  - **Severity** — High
  - **Category tag** — `expression-limit`
  - **Description** — The `Phase 4: Wait for review & autofix to complete` `run:` block is about **19,899 characters**, leaving only **1,101 characters** of headroom before GitHub’s **21,000-character** expression ceiling. It already embeds helper functions, polling logic, and multiple `${{ steps.* }}` interpolations in one block.
  - **Recommended fix** — Extract the wait loop to a script such as `scripts/e2e_wait_review.sh` and pass inputs via environment variables, or split helper-function setup from the polling loop into separate steps.

- **ID** — `EXPR-002`
  - **File** — `.github/workflows/test-and-mark-stable.yml:1673-2079`
  - **Severity** — Medium
  - **Category tag** — `expression-limit`
  - **Description** — The `Phase 4b: Verify editor restored canary` `run:` block is about **17,408 characters**, leaving **3,592 characters** of headroom. It combines retry helpers, pytest classification, redispatch polling, and several `${{ steps.* }}` references in one step.
  - **Recommended fix** — Move this logic into `scripts/e2e_verify_bait.sh`, or split the gh retry helpers, pytest classifier, and retry poll loop into smaller steps.

- **ID** — `EXPR-003`
  - **File** — `.github/workflows/validate.yml:210-584`
  - **Severity** — Medium
  - **Category tag** — `expression-limit`
  - **Description** — The `Fetch workflow support files` `run:` block is about **17,416 characters**, leaving **3,584 characters** of headroom. It mixes support checkout, fallback logic, large asset lists, and `${{ github.* }}` interpolation in one block.
  - **Recommended fix** — Extract support staging to a shared script or composite action; this also resolves `DUP-001`.

- **ID** — `EXPR-004`
  - **File** — `.github/workflows/review_autofix.yml:1476-1866`
  - **Severity** — Medium
  - **Category tag** — `expression-limit`
  - **Description** — The `Collect PR metadata` `run:` block is about **17,408 characters**, leaving **3,592 characters** of headroom. It combines custom retry code, multiple API fetches, linked-issue fallback logic, inline Python normalization, and several workflow expressions.
  - **Recommended fix** — Extract this step to `scripts/review_collect_pr_metadata.sh` and/or split metadata fetch, linked-issue context building, and comment normalization into separate steps. This also enables `API-001` and `CONSIST-001`.

No workflow exceeded the 800 KB early-warning threshold. The largest audited workflow was `.github/workflows/review_autofix.yml` at **345,188 bytes**.

### Section 5: Cross-Cutting Concerns

- **ID** — `DEAD-001`
  - **File** — `scripts/orchestrate_poll_process.sh:5299-5306`
  - **Severity** — Low
  - **Category tag** — `dead-code`
  - **Description** — `read_standalone_state_json()` is defined but not called. The active standalone-stall path already parses the cached `comments_json` directly via `_extract_standalone_state_json_from_comments` at `scripts/orchestrate_poll_process.sh:7057-7058`, so this wrapper preserves an unused extra-fetch code path.
  - **Recommended fix** — Delete the unused wrapper, or replace the direct parser call with it if you actually want a public helper; do not keep both.

- **ID** — `CONSIST-001`
  - **File** — `.github/workflows/review_autofix.yml:1479-1515`
  - **Severity** — Medium
  - **Category tag** — `consistency`
  - **Description** — `review_autofix.yml` redefines `_rl_wait`/`gh_retry` locally even though `scripts/gh_helpers.sh:381-599` already provides `gh_retry`, `gh_retry_to_file`, `gh_api_json_to_file`, permanent-failure detection, Telegram rate-limit alerts, and breaker-file semantics. The local wrapper retries every non-zero exit, uses a fixed `/tmp/gh_retry_stderr`, and bypasses the repo’s standardized API hygiene behavior.
  - **Recommended fix** — Source `scripts/gh_helpers.sh` in this step and use `gh_retry_to_file` / `gh_api_json_to_file`. If the step needs custom logging, wrap the shared helper instead of forking its semantics.

No `TODO`/`FIXME`/`HACK`/`XXX` markers were present in `.github/workflows/*.yml` or `scripts/*` during this audit.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 4 | BUG-001, BUG-002, API-001, EXPR-001 |
| Medium | 8 | API-002, DUP-001, DUP-002, DUP-003, EXPR-002, EXPR-003, EXPR-004, CONSIST-001 |
| Low | 1 | DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3-4 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 5-6 | Large |
| Expression size reduction | 4-8 | Large |
| Medium/Low fixes | 2 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-18)

### Safety Tag Legend
`SAFE_TO_MERGE` means the consolidation/deletion is statically supported and can be implemented directly with no expected semantic change. `NEEDS_VERIFICATION` means the overlap is real but payload-shape, fallback, or ordering behavior must be checked before changing it. `RISKY_SKIP` means the duplication is visible, but it sits in a retry/poll/stall-recovery or other race-defensive path and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

- **ID** — `MERGE-001`
  - **Safety tag** — `NEEDS_VERIFICATION`
  - **File path and line ranges** — `scripts/review_rb_judge.sh:246-251`, `scripts/review_rb_judge.sh:267-283`
  - **Current call count** — `1` GraphQL call + `1..N` REST issue GETs per judge run with linked issues
  - **Proposed call count** — `1` GraphQL call total
  - **Endpoint(s)** — GraphQL `repository.pullRequest(...).closingIssuesReferences(first: 50)`; REST `GET /repos/{owner}/{repo}/issues/{issue_number}`
  - **Evidence**
    ```sh
    ISSUE_NUMBERS="$(gh_retry gh api graphql \
      ...
      -f query='query($owner:String!, $name:String!, $number:Int!) { repository(owner:$owner, name:$name) { pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number } } } } }' \
      --jq '.data.repository.pullRequest.closingIssuesReferences.nodes[].number' || true)"
    ...
    ISSUE_META_JSON="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" || echo '{}')"
    BODY="$(printf '%s' "${ISSUE_META_JSON}" | jq -r '.body // ""' 2>/dev/null || echo "")"
    FIRST_ISSUE_LABELS_JSON="$(printf '%s' "${ISSUE_META_JSON}" | jq -c '[(.labels // [])[]?.name]' 2>/dev/null || echo '[]')"
    ```
  - **Proposed fix** — Extend the existing `closingIssuesReferences` GraphQL query to request `nodes { number body labels(first: 100) { nodes { name } } }`, then populate `FIRST_ISSUE`, `FIRST_ISSUE_BODY`, and `FIRST_ISSUE_LABELS_JSON` from that payload; keep the current REST loop only as the GraphQL-failure fallback. Existing batching pattern to mirror: `_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql` in `scripts/orchestrate_poll_process.sh:6462-6579,6599-6698`.
  - **Safety rationale** — This is a true batchable overlap, but it changes the response shape and the current “first issue number + first non-empty body” selection behavior, so static reading alone is not enough for `SAFE_TO_MERGE`.
  - **Downstream signal** — Verify with a multi-linked-issue fixture (including “first linked issue has labels but empty body”) that the batched GraphQL path preserves current `FIRST_ISSUE` / `FIRST_ISSUE_BODY` semantics before removing the REST loop.

- **ID** — `MERGE-002`
  - **Safety tag** — `RISKY_SKIP`
  - **File path and line ranges** — `scripts/orchestrate_poll_process.sh:5777-5779`, `scripts/orchestrate_poll_process.sh:7567-7575`
  - **Current call count** — `2` issue GETs per `close_and_reissue` branch (`4` across the mirrored orchestrator-managed + standalone branches)
  - **Proposed call count** — `1` issue GET per branch
  - **Endpoint(s)** — REST `GET /repos/{owner}/{repo}/issues/{issue_num}`
  - **Evidence**
    ```sh
    orig_title="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.title // ""' || echo "")"
    orig_body="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.body // ""' || echo "")"
    ```
  - **Proposed fix** — In each `close_and_reissue` branch, fetch full issue JSON once into a local variable and derive both `.title` and `.body` from it; keep log text and recovery comments unchanged.
  - **Safety rationale** — The duplicate GETs are real, but both sites are inside `orchestrate_poll_process.sh` stall-recovery logic, which is explicitly a `RISKY_SKIP` path.
  - **Downstream signal** — Do not auto-implement; manually review both stall-recovery branches, confirm no recovery-timing or log-contract behavior depends on the second GET, then consolidate to one cached `_safe_gh_jq` read per branch.

### Redundant Re-Fetch (REUSE-###)

- **ID** — `REUSE-001`
  - **Safety tag** — `SAFE_TO_MERGE`
  - **File path and line ranges** — `scripts/review_rb_judge.sh:221-238`, `scripts/review_rb_judge.sh:253-256`
  - **Current call count** — `2` PR GETs on the `ISSUE_NUMBERS`-empty fallback path
  - **Proposed call count** — `1` PR GET on the non-error path
  - **Endpoint(s)** — REST `GET /repos/{owner}/{repo}/pulls/{pr_number}`
  - **Evidence**
    ```sh
    _pr_meta="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" 2>/dev/null || echo '{}')"
    ...
    if [ -z "${ISSUE_NUMBERS}" ]; then
      PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' || echo "")"
    fi
    ```
  - **Proposed fix** — Reuse local PR metadata before re-fetching: build `PR_DATA` from `PR_META_FILE` first (same pattern already used in `.github/workflows/review_autofix.yml:4530-4532,4652-4654,5480-5482`), then from retained `_pr_meta`, and only keep the current line-254 GET as a last-resort fallback.
  - **Safety rationale** — Same endpoint, same script, no intervening mutation, and preserving the current plain `_safe_gh_jq` as the final fallback keeps error-handling semantics intact.
  - **Downstream signal** — Replace the extra `/pulls/${PR_NUMBER}` GET with `PR_META_FILE` → retained `_pr_meta` → existing API fallback, in that order.

- **ID** — `REUSE-002`
  - **Safety tag** — `SAFE_TO_MERGE`
  - **File path and line ranges** — `scripts/implement_diagnose_post_codex_failure.sh:166-172`, `scripts/implement_diagnose_post_codex_failure.sh:261-273`
  - **Current call count** — `2` issue GETs on the `ISSUE_META_FILE`-miss + `ISSUE_BODY_FILE`-miss path
  - **Proposed call count** — `1` issue GET on that path
  - **Endpoint(s)** — REST `GET /repos/{owner}/{repo}/issues/{issue_number}`
  - **Evidence**
    ```sh
    if [ -z "${ISSUE_LABELS_JSON}" ]; then
      ISSUE_LABELS_JSON="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}" --jq '[.labels[].name]' || echo '[]')"
    fi
    ...
    if [ ! -s "${ISSUE_BODY_FILE}" ]; then
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}" --jq '.body // ""' > "${ISSUE_BODY_FILE}" || printf '' > "${ISSUE_BODY_FILE}"
    fi
    ```
  - **Proposed fix** — When `ISSUE_META_FILE` is absent/invalid, fetch full issue JSON once into a local fallback variable/file, derive both labels and body from it, and retain the current body-only GET only if that unified fetch fails.
  - **Safety rationale** — Same endpoint, same top-level script path, no intervening issue mutation, and keeping the body-only GET as cache-miss fallback preserves the current fail-open behavior.
  - **Downstream signal** — Introduce one full-issue fallback fetch and reuse it for both label gating and body extraction before the existing body-only GET.

### Dead Calls (DEAD-API-###)

- **ID** — `DEAD-API-001`
  - **Safety tag** — `SAFE_TO_MERGE`
  - **File path and line ranges** — `.github/workflows/internal-review.yml:98-109`, `.github/workflows/internal-review.yml:121-134`
  - **Current call count** — `2` calls on the observed skip path
  - **Proposed call count** — `1` call on the skip path
  - **Endpoint(s)** — REST `GET /repos/{owner}/{repo}/pulls?state=open&head={owner}:{ref}`; REST `GET /repos/{owner}/{repo}`
  - **Evidence**
    ```sh
    existing_pr="$(gh api \
      "repos/${REPOSITORY}/pulls?state=open&head=${REPOSITORY%/*}:${HEAD_REF}" \
      --jq '[.[] | .number] | first // empty' 2>/dev/null || echo "")"
    base_ref="$(gh api "repos/${REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo 'main')"
    if [ -n "${existing_pr}" ]; then
      echo "proceed=false"
      echo "base_ref=${base_ref}"
    fi
    ```
    ```yaml
    review-claude-branch-push:
      needs: resolve-claude-branch-pr
      if: ${{ github.event_name == 'push' && needs.resolve-claude-branch-pr.outputs.proceed == 'true' }}
    ```
  - **Proposed fix** — Move the repo `default_branch` lookup below the `existing_pr` early-exit, or set a static/event-derived `base_ref` only on the skip branch so the repo GET runs only when `proceed=true`.
  - **Safety rationale** — The repo GET’s result is only fed to `base_ref`, and the only downstream consumer job is already gated on `proceed == 'true'`, so the skip-path fetch is dead.
  - **Downstream signal** — Move the `gh api "repos/${REPOSITORY}" --jq '.default_branch'` call after the `existing_pr` skip branch; keep skip-path output shape with a static/default `base_ref`.

### Cross-References to Deep Audit Section

- `API-001`: `NEEDS_VERIFICATION` — The helper-based GraphQL consolidation is correct in principle, but rollout must preserve `gh_pr_with_all_comments` pagination fail-open behavior and the current `PR_META_FILE` contract.
- `API-002`: `NEEDS_VERIFICATION` — Reusing earlier orchestrator classification is the right direction, but it also changes merged-alert suppression for label-only managed issues, so that behavior needs an explicit verification decision.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 3 | `DEAD-API-001`, `REUSE-001`, `REUSE-002` |
| NEEDS_VERIFICATION | 1 | `MERGE-001` |
| RISKY_SKIP | 1 | `MERGE-002` |

### Implement-Stage Handoff

- `DEAD-API-001`
- `REUSE-001`
- `REUSE-002`
