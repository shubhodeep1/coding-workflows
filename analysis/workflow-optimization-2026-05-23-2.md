## Executive Summary

- `review_autofix` is losing the most wall time to check-run waiting, not model execution. In `shubhodeep1/coding-workflows` run `26332319470`, step `Collect PR check-run failures CI lint autofix context` logged **46 wait loops / 920s sleep** and spanned **942.9s**; runs `26296423247`, `26293979422`, and `26329171639` added another **2,952s** of configured sleep across sample. **Estimated impact:** save **15–20 minutes** on slow `review_autofix` runs. **Confidence:** high.
- The `Workflow Log Analysis` failure was mostly a budget problem. Run `26328477066` spent **1,994.6s (89.9% of run time)** in `analyze-commit-notify`, then `deep-audit / Run deep audit pass` hit **exit 143** with `The runner has received a shutdown signal` after shellcheck retries/timeouts. **Estimated impact:** cut **20–30 minutes/run** and remove a current failure mode. **Confidence:** high.
- The orchestrator is over-dispatching skip-only children. Across `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`, **529/548 runs (96.5%)** ended as `other` (predominantly skipped); at **2026-05-23T13:07:19Z–13:07:23Z**, **20** skip-only runs fired across those four families. **Estimated impact:** remove **500+ no-op workflow launches/window**, reduce queue contention, and simplify state handling. **Confidence:** high.
- Model cost is too high on low-signal work. In `review_autofix` run `26329171639`, the log says `diff is 0 LOC < 200 threshold` but still ran **12 reviewer calls** (two passes × six reviewers) plus **two** `gpt-5.4-mini` summariser calls; in Copilot review run `26333520350`, the workflow built a **102,842-token** prompt and spent **136.7s** building it before session creation. **Estimated impact:** save **40k–80k prompt tokens/run** in Copilot review and roughly **50% reviewer-call cost** on trivial diffs. **Confidence:** medium-high.
- Reliability risk is concentrated in high-value workflows. `Test & Mark Stable Release` run `26328466709` failed on `orphan-workflows-test / Dispatch & watch — update_workflows (allow_workflow_edits=false)` with **HTTP 500** from `gh workflow run`; `review_autofix` also accounts for **9/11 cancellations (81.8%)** across the window. **Estimated impact:** materially lower reruns and release-test flakiness. **Confidence:** medium-high.
- Semble looks net-positive when it is actually active, but AI memory retrieval is not paying off yet. Excluding copied telemetry inside the meta-analysis job, operational `SEMBLE_QUERY` telemetry shows **28 queries / 262,746 bytes / 14,524ms** across six `review_autofix` runs, while AI memory `retrieve` hit rate was **0/8** with `estimated_tokens=0` every time. **Estimated impact:** keep Semble, fix retrieval/promotion before expanding memory use. **Confidence:** high.

## Speed Optimizations

1. **[Critical path] Stop blocking `review_autofix` on long check-run polling**
   - **Evidence:** Run `26332319470` (`review_autofix`) spent **942.9s** in step `Collect PR check-run failures CI lint autofix context`; that step logged **46** `Waiting for ... check-run(s)` messages and **920s** of sleep. Run `26329171639` logged **59 waits / 1,172s sleep**, which was **46.8%** of its **2,504s** total runtime. Runs `26296423247` and `26293979422` logged **45** and **44** waits respectively.
   - **Root cause:** the current flow blocks the review path on repeated same-SHA polling for check-run context.
   - **Exact change:** after a small bounded wait (for example 2–3 polls), continue with the latest available check snapshot; short-circuit entirely when the only remaining in-flight check belongs to the same autofix/review chain.
   - **Estimated time savings:** **14–20 minutes** on slow `review_autofix` runs.
   - **Implementation risk:** **medium**; use a “partial-context” marker so reviewers know check context was incomplete.

2. **[Critical path] Put a hard budget on `workflow_log_analysis`**
   - **Evidence:** Run `26328477066` lasted **2,219s**; step `analyze-commit-notify` consumed **1,994.6s**, then `deep-audit / Run deep audit pass` was killed with **exit 143** and `The runner has received a shutdown signal`. The same step shows repeated shellcheck attempts, including a **20s timeout** run.
   - **Root cause:** unbounded repo-wide audit work is running after a long analysis stage, leaving almost no margin.
   - **Exact change:** cap deep-audit by file set and wall-clock budget, emit partial findings, and skip full-shellcheck sweeps unless the run is manually requested or the changed files are shell-heavy.
   - **Estimated time savings:** **20–30 minutes/run** on this workflow family.
   - **Implementation risk:** **low-medium**; fail with a partial report instead of failing the whole workflow.

3. **[Critical path] Add a trivial-diff fast path to `review_autofix`**
   - **Evidence:** In run `26329171639`, the workflow logged `diff is 0 LOC < 200 threshold` but still executed **pass 1** across six reviewers from **09:47:01Z–09:51:00Z**, then **pass 2** across the same six reviewers from **09:52:36Z–09:55:22Z**, followed by another summariser run; the combined reviewer/summariser window lasted about **692s**.
   - **Root cause:** reviewer fan-out and second-pass depth do not collapse on metadata-only or zero-LOC changes.
   - **Exact change:** if diff LOC is `0` (or only generated metadata/comments changed), skip pass 2 and cut pass 1 to a smaller reviewer set unless there are failing checks or risky file paths.
   - **Estimated time savings:** about **6 minutes** on trivial diffs, plus lower queue pressure.
   - **Implementation risk:** **medium**; keep the full path for risky directories and low-consensus pass-1 results.

4. **[Critical path] Shrink Copilot PR review prompt assembly**
   - **Evidence:** In run `26333520350` (`copilot_pull_request_reviewer`), step `Processing Request Linux` started at **13:07:40Z**, logged `Built prompt with 102842 tokens` at **13:09:57Z**, and created the session immediately after; **136.7s** of the step’s **158.9s** span was spent before the model call.
   - **Root cause:** prompt construction is expanding too much unchanged context/history.
   - **Exact change:** cap historical discussion/context, include only changed-file excerpts plus blocking comments, and move optional background into lazily requested context instead of the initial prompt.
   - **Estimated time savings:** about **1–2 minutes/run** on this workflow, plus token savings.
   - **Implementation risk:** **medium**; preserve changed files and unresolved review comments.

5. **[Control-plane] Pre-gate `clarify` / `plan` / `implement` / `respond` before dispatch**
   - **Evidence:** Those four families produced **529 skip-like runs out of 548 total**; on **2026-05-23T13:07:19Z–13:07:23Z**, there were **20** skip-only runs across the four families, each ending after condition evaluation.
   - **Root cause:** the parent/orchestrator is dispatching children whose conditions are already known false.
   - **Exact change:** evaluate the branch/comment/answer/clarification conditions once in the parent workflow and dispatch only the single eligible child.
   - **Estimated time savings:** low per run, but meaningful end-to-end queue relief and cleaner orchestration.
   - **Implementation risk:** **low**; start with a dry-run metrics mode if needed.

6. **[Micro-optimization] Cache or pre-stage `actionlint` in CI**
   - **Evidence:** `ci` has **p50 1098s / p95 1144s** across **16** runs; sampled runs `26332318086` and `26333154035` say `lint` dominated runtime and both downloaded/installed `actionlint 1.7.12` from the release page.
   - **Root cause:** repeated tool bootstrap inside the slowest CI job.
   - **Exact change:** cache the binary in the existing tool cache or switch the lint step to a reusable setup path that avoids re-download on every run.
   - **Estimated time savings:** **tens of seconds to low minutes/run**.
   - **Implementation risk:** **low**.

## Cost Optimizations

1. **Right-size reviewer fan-out and reasoning on zero-/tiny-diff `review_autofix` runs**
   - **Evidence:** Run `26329171639` logged `diff is 0 LOC < 200 threshold` yet ran **12 reviewer model invocations** (two passes × six reviewers) plus two `gpt-5.4-mini` summariser calls. The pass-1 summariser used **26,157 prompt bytes** and the review summariser used **36,204 prompt bytes**.
   - **Root cause:** fixed multi-model, multi-pass review depth even when the code delta is trivial.
   - **Exact change:** add a `0 LOC / metadata-only` mode that runs a reduced reviewer set and skips pass 2 unless pass 1 finds disagreement or risky files.
   - **Estimated savings:** roughly **50% fewer reviewer calls** and about **357s** on the pass-2 portion alone for zero-diff runs.
   - **Quality-risk notes:** **medium**; keep full fan-out for risky paths, failing checks, or weak consensus.

2. **Cut Copilot PR reviewer prompt size before changing models**
   - **Evidence:** Run `26333520350` built a **102,842-token** prompt for `claude-opus-4.7[ReasoningEffort=medium]`; prompt construction dominated the step (**136.7s** before session creation).
   - **Root cause:** repeated prompt/context expansion, not obviously model latency, is driving both token cost and wall time.
   - **Exact change:** trim unchanged file context, cap legacy conversation/history, and keep only actionable diffs/comments in the first prompt.
   - **Estimated savings:** about **40k–80k prompt tokens/run** and **1–2 minutes** runtime.
   - **Quality-risk notes:** **medium**; trim background first, not changed-file evidence.

3. **Suppress redundant self-triggered follow-up review passes**
   - **Evidence:** In run `26331429148`, step `Re-trigger review via workflow dispatch` logged `Dispatched review_autofix.yml on claude/ecstatic-hawking-cpkPV for PR #2918.`; the gate logs in runs `26331429148`, `26332319470`, and `26332280950` show `AUTOFIX_SKIP_SELF_TRIGGERED:` blank while comments state the default is now `false`. `review_autofix` also has **9 cancelled runs out of 69 (13.0%)**.
   - **Root cause:** self-healing follow-ups are allowed even when the next pass may add little new signal.
   - **Exact change:** suppress only identical-SHA/no-new-signal self-triggered reruns; still allow follow-up review when a fix commit changed the head SHA or when blocking checks changed.
   - **Estimated savings:** one full `review_autofix` pass on redundant chains; exact tokens are not fully observable, but the family’s **p95 is 4,527.8s**.
   - **Quality-risk notes:** **medium**; do not suppress reruns after real code changes.

4. **Narrow `workflow_log_analysis` run summarisation**
   - **Evidence:** Run `26328477066` emitted `AI_MEMORY_TELEMETRY` for `summarize_unselected_runs` using **130,578 tokens** on `openai/gpt-5.4-mini` to summarise **81** runs out of **100** targeted; **19** were skipped because logs were empty.
   - **Root cause:** too many low-value runs are still being targeted for summarisation.
   - **Exact change:** stop targeting skip-only families first, and prefer failed/slow/recent runs with non-empty logs.
   - **Estimated savings:** about **50k–130k mini-model tokens/run**.
   - **Quality-risk notes:** **low**; keep deep-dive coverage for failed and slow runs.

5. **Treat Semble as a compression aid, not a cost target**
   - **Evidence:** Operational `SEMBLE_QUERY` telemetry appeared in **6** `review_autofix` runs with **28** deduped queries total, **262,746 logged bytes** total, and **14,524ms** total latency—about **9.4KB/query** and **519ms/query**. Targets were `overflow` (**21**), `reviewer-context` (**6**), and `conflict-resolver-context` (**1**).
   - **Interpretation:** this is far smaller than the **102,842-token** Copilot prompt and looks like targeted context control rather than noisy low-value expansion. **Inference:** Semble is likely reducing overflow pressure when available.
   - **Exact change:** keep Semble in `review_autofix`; spend optimisation effort on prompt scope and redundant reruns first.
   - **Estimated savings:** avoiding Semble removal probably preserves quality more than it saves cost.
   - **Quality-risk notes:** **low** for keeping it; high if removed blindly.

6. **Serena is not currently contributing either savings or noise in this window**
   - **Evidence:** After excluding copied telemetry inside `workflow_log_analysis`, there were **0** operational `SERENA_QUERY`, `SERENA_FALLBACK`, and `SERENA_PROBE` lines. Sampled review runs also log `SERENA_ENABLED: false`.
   - **Interpretation:** there is no evidence Serena is replacing downstream tool/model work, and no evidence it is adding low-value response bytes.
   - **Exact change:** none for cost; first decide whether Serena should be enabled at all in this pipeline.
   - **Estimated savings:** not measurable from this window.
   - **Quality-risk notes:** **low**.

## Reliability Improvements

1. **Add bounded retry/backoff to `gh workflow run` in release-test dispatch paths**
   - **Failure evidence:** `shubhodeep1/coding-workflows` run `26328466709`, job `orphan-workflows-test`, step `Dispatch & watch — update_workflows (allow_workflow_edits=false)` failed with `could not create workflow dispatch event: HTTP 500: Failed to run workflow dispatch`.
   - **Root cause category:** external API transient / missing retry.
   - **Exact fix:** wrap `gh workflow run` in the same bounded retry/backoff discipline already used for other GH API calls, and apply it to all release-test dispatch sites, not just one.
   - **Expected reliability impact:** high for `test_and_mark_stable` because the family is currently **1/1 failed** in this window.
   - **Rollback / fail-open:** do **not** fail-open on release gates; still hard-fail after bounded retries.

2. **Turn `workflow_log_analysis` into a partial-report workflow instead of a kill-the-run workflow**
   - **Failure evidence:** Run `26328477066` spent **1,994.6s** in `analyze-commit-notify`, then `deep-audit` died with **exit 143** / `shutdown signal`; the step also showed shellcheck retries/timeouts.
   - **Root cause category:** unbounded audit runtime / runner budget exhaustion.
   - **Exact fix:** enforce per-phase budgets, emit partial output when over budget, and mark deep-audit as soft-fail unless explicitly requested.
   - **Expected reliability impact:** high; this family is also **1/1 failed** in the current window.
   - **Rollback / fail-open:** keep hard-fail available behind a manual or nightly-only switch.

3. **Stop generating skip-only child workflows**
   - **Failure evidence:** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` produced **529/548** skip-like runs; **20** of them launched in **4 seconds** on `2026-05-23T13:07:19Z–13:07:23Z`.
   - **Root cause category:** control-plane over-dispatch / state gating in the wrong place.
   - **Exact fix:** perform condition evaluation in the parent/orchestrator and dispatch only the eligible child workflow.
   - **Expected reliability impact:** medium; this should reduce rerun noise, queueing, and accidental orchestration churn.
   - **Rollback / fail-open:** start by logging “would have skipped dispatch” before enforcing.

4. **Treat current `SEMBLE_FALLBACK` events as healthy fail-open validation, not a broken rollout**
   - **Failure evidence:** all observed operational `SEMBLE_FALLBACK` lines were in run `26328466709`, step `validate-scripts`, target `overflow`, count **5**, files `src/big.py` / `src/small.py`, with reason `[Errno 2] No such file or directory: '/tmp/.../missing_semble'`.
   - **Root cause category:** expected fail-open contract testing.
   - **Exact fix:** keep the test, and label this fallback bucket as expected validation telemetry so it does not page operators as a production incident.
   - **Expected reliability impact:** low on workflow outcomes, high on alert quality.
   - **Rollback / fail-open:** none needed; current fail-open behavior is appropriate here.

5. **Reduce reviewer-provider throttling on wide fan-out runs**
   - **Failure evidence:** run `26329171639` logged `ERROR: exceeded retry limit, last status: 429 Too Many Requests` before `mistralai/mistral-small-2603` eventually succeeded on attempt 2.
   - **Root cause category:** upstream model-provider throttling under broad concurrent reviewer fan-out.
   - **Exact fix:** stagger or cap per-provider concurrency, especially on low-signal diffs where the full reviewer set is unnecessary.
   - **Expected reliability impact:** moderate; fewer soft retries and less chance of partial-review churn.
   - **Rollback / fail-open:** keep the existing retry path as fallback.

**Fallback / probe status note:** no operational `SERENA_FALLBACK` or `SERENA_PROBE` lines were observed in the deep-dive logs, so there is no evidence of a masked broken Serena rollout in this window. The only availability-style issue was summary-only poller evidence (`orchestrate_poll` runs `26333486463` and `26332839170`) showing `SEMBLE_AVAILABLE: false` / `SEMBLE_INDEX_AVAILABLE: false`; that is an availability/config problem, not a runtime fallback storm.

## AI Memory Health

Operational AI-memory metrics below exclude copied telemetry echoed inside the failing `workflow_log_analysis` meta-analysis job, so they reflect runtime behavior rather than reprinted logs.

| Metric | Value | Evidence |
|---|---:|---|
| Runs with runtime AI-memory telemetry | 8 | `review_autofix` runs `26292599330`, `26293968972`, `26293979422`, `26296423247`, `26329171639`, `26331429148`, `26332280950`, `26332319470` |
| `retrieve` ops | 8 | One per sampled runtime review path |
| Retrieve hit rate | 0% (0/8) | Every `retrieve` had `records_selected: 0` |
| Avg `estimated_tokens` | 0.0 | Every `retrieve` logged `estimated_tokens: 0`; no budget field was emitted |
| `keyword_method` distribution | `none`: 8/8 | No `llm` or `plain` retrieval method observed |
| `enabled: false` retrieves | 0 | None observed |
| `fail_open: true` in telemetry JSON | 0 | None observed, despite fail-open step names |
| `record-run-event` ops | 16 | Start + completion events across the 8 review runs |
| `record-candidate` ops | 6 | Candidate writes observed in runs `26292599330`, `26293979422`, `26296423247`, `26329171639`, `26331429148`, `26332319470` |
| Push attempts >1 | 0 | All **22** push-bearing ops (`record-run-event` + `record-candidate`) logged `push_attempts: 1` |
| `promote` / `compact` / `finalize-task` / `processed-command-*` ops | 0 observed | Not present in this window |

**Assessment**
- The **write path is healthy**: event and candidate pushes succeeded on the first attempt in every observed runtime case.
- The **retrieve path is ineffective**: every observed retrieval returned zero records, zero estimated tokens, and `keyword_method: none`.
- I did **not** find runtime `AI_MEMORY_TELEMETRY` for `orchestrate_poll`, `clarify`, `plan`, or `implement` in the deep-dive set.

**Recommendation**
- Keep current fail-open behavior, but do not expand memory usage until retrieval is useful.
- The smallest safe fix is to promote successful `record-candidate` outputs into retrievable memory sooner and add stronger retrieval keys (PR number, changed files, workflow family) so review runs stop logging all-zero retrievals.

## GH API Call Audit

No confirmed GitHub REST/GraphQL `429` or secondary-rate-limit hits were observed in the operational deep-dive logs. The current problem is **redundant polling and repeated lookups**, not proven hard throttling.

1. **High-volume hotspot: repeated same-SHA check-run polling in `review_autofix`**
   - **Evidence:** `review_autofix` run `26332319470` step `Collect PR check-run failures CI lint autofix context` logged **46** wait iterations; run `26329171639` logged **59**; runs `26296423247` and `26293979422` logged **45** and **44**.
   - **Redundancy pattern:** repeated status checks on the same head SHA while the current run is blocked.
   - **Concrete change:** cache the last check-run snapshot per `HEAD_SHA`, stop polling when only sibling/self checks remain, and continue with partial context after a bounded wait.
   - **Estimated call-count reduction:** roughly **50–80%** on long blocked reviews.
   - **Rate-limit risk reduction:** high.

2. **Low-volume but easy win: duplicate branch/PR metadata lookups**
   - **Evidence:** `review_autofix` run `26332317273` (`resolve-claude-branch-pr`) used `gh api "repos/${REPOSITORY}/pulls?state=open&head=..."` and `gh api "repos/${REPOSITORY}" --jq '.default_branch'`—**two calls** in a **6s** run.
   - **Redundancy pattern:** lookups that can be passed through as step outputs once the gate has already resolved the branch and PR.
   - **Concrete change:** emit `default_branch` and resolved PR metadata once in the gate/resolve job and reuse them downstream.
   - **Estimated call-count reduction:** **1–2 GH API calls/run** for this path.
   - **Rate-limit risk reduction:** low, but essentially free.

3. **GraphQL linked-issue lookups are clean but could be memoized**
   - **Evidence:** `review_autofix` runs `26333154192` and `26332586791` used `gh api graphql` to fetch closing issue references during post-merge validation dispatch.
   - **Redundancy pattern:** same PR-linked issue data can be re-read by follow-up jobs.
   - **Concrete change:** fetch once per PR and persist the result in job outputs/artifacts for later steps.
   - **Estimated call-count reduction:** **1 call/run** on post-merge validation paths.
   - **Rate-limit risk reduction:** low.

4. **Positive hygiene worth copying: `cancel_on_pr_close` already uses rate-limit-aware helpers**
   - **Evidence:** runs `26333518255` and `26333154161` show `_rl_wait()` calling `/rate_limit`, with no retry triggered; run `26332586788` looped cancel endpoints only when matches existed.
   - **Recommendation:** reuse the same `_rl_wait` / `gh_retry` discipline and bounded-wait behavior in `review_autofix` and poller-side GH API loops.
   - **Estimated effect:** not a direct call reduction by itself, but better bounded behavior under pressure.

5. **Copilot API traffic is currently low-volume**
   - **Evidence:** Copilot review run `26333520350` logged **6** API calls (session PUTs + logs PUTs + progress POST); run `26332252307` showed similarly small traffic.
   - **Recommendation:** do not spend GH API budget effort here first; prompt construction is the bigger issue.

## Prompt Cache & Memory System

- **Prompt cache is enabled but effectively opaque right now.** Outside the meta-analysis workflow, `OPENROUTER_PROMPT_CACHE_DISABLED: false` appeared in **9** deep-dive `review_autofix` runs, but there were **0** observed `cache_creation_input_tokens` lines and **0** observed `cache_read_input_tokens` lines. So cache is on, but hit/miss performance is not measurable from this artifact.
- **Semble looks healthy in `review_autofix`.** Operational telemetry showed **28** deduped `SEMBLE_QUERY` lines across **6** review runs, totaling **262,746 logged bytes** and **14,524ms**. The mix was `overflow` (**21**), `reviewer-context` (**6**), and `conflict-resolver-context` (**1**). **Inference:** this is compact enough to be helping with overflow control rather than adding noisy context.
- **Semble is not consistently available in the poller.** Summary-only evidence for `orchestrate_poll` runs `26333486463` (**171s**) and `26332839170` (**567s**) shows `SEMBLE_ENABLED: true` but `SEMBLE_AVAILABLE: false` and `SEMBLE_INDEX_AVAILABLE: false`.
- **Serena is not active in the observed runtime set.** There were **0** operational `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines after excluding copied telemetry, and sampled review runs log `SERENA_ENABLED: false`.
- **Memory writes succeed; memory reads do not.** The memory system currently behaves like a write-only journal.

**Concrete improvements**
1. Emit cache counters (`cache_creation_input_tokens`, `cache_read_input_tokens`) in runtime logs before changing prompt structure.
2. Keep Semble in `review_autofix`; do **not** cut it first.
3. For `orchestrate_poll`, either build/stage the Semble index before the main `poll` step or skip Semble startup once availability is known false, instead of carrying dead init state through the run.
4. **Inference:** move volatile per-run diagnostics and ephemeral metadata to the tail of prompts, leaving the stable instruction block first, to improve provider-side cache reuse once counters are visible.
5. Promote successful memory candidates into retrievable memory sooner so cache/memory systems work together instead of paying write cost without retrieval benefit.

## Orchestrator Health

- **The biggest orchestrator health problem is non-productive fan-out.** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` were mostly skip-like this window: `clarify` **137/143**, `plan` **130/135**, `implement` **128/135**, `orchestrate_clarify_respond` **134/135**.
- **Poller health is stable but not cheap.** `orchestrate_poll` is **34/34 successful**, but still has **p50 139.5s / p95 188s**, and sampled runs `26333486463` and `26332839170` show hosted-runner wait plus Semble unavailability.
- **Self-heal review chains can be long and churny.** `review_autofix` has **9 cancelled runs out of 69**, and run `26331429148` explicitly dispatched a follow-up review via `workflow_dispatch` after a long healing pass.
- **Wave progression / deferral detail is not well-instrumented in this artifact.** I did not see deep-dive wave counters or conflict-heal retry counters, so I cannot quantify those safely from this window.

**Smallest safe mitigations**
1. Move branch/answer/clarification gating into the parent workflow.
2. Cap review self-heal follow-ups to one per SHA or per short cooldown window.
3. Add explicit orchestrator counters for `child_dispatch_skipped`, `self_heal_followup_dispatched`, `wave_advance`, `wave_blocked`, and `poll_noop_exit`.

**Indicators to track next**
- Skip-only dispatch ratio per family (current four-family combined baseline: **96.5%**).
- `review_autofix` cancellation rate (current baseline: **13.0%**).
- Check-run wait-loop count per `review_autofix` run (sample baseline: **44–59** on slow runs).
- Poller Semble availability (`SEMBLE_AVAILABLE` / `SEMBLE_INDEX_AVAILABLE`).
- AI-memory retrieve hit rate (current baseline: **0%**).
- `workflow_log_analysis` wall time and whether `deep-audit` exits with 143.

## Pipeline Flow Bottlenecks

1. **Clarify → plan → implement → respond: control-plane bottleneck**
   - **Evidence:** **529/548** runs in these four families were skip-like; **20** skip-only runs launched in **4 seconds** on `2026-05-23T13:07:19Z–13:07:23Z`.
   - **Type:** dispatch / queue / orchestration overhead.
   - **Fix order:** pre-dispatch gating first.

2. **Review / autofix: wait-on-checks bottleneck, then model compute bottleneck**
   - **Evidence:** run `26332319470` spent **942.9s** in check-run collection, then **830.4s** in `Run reviewer models`, then **342.6s** in `Apply fixes with editor model`. Run `26331429148` spent **1,151.6s** in reviewer models, **953.7s** in editor fixes, and **457.2s** in conflict resolver/validate/stage.
   - **Type:** first polling, then heavy model compute, then merge/conflict overhead.
   - **Fix order:** remove blocking waits before tuning models.

3. **Validate / orchestrate poller: queue + polling latency**
   - **Evidence:** `orchestrate_poll` is healthy but long (**p50 139.5s**); sampled runs `26333486463` (**171s**) and `26332839170` (**567s**) were dominated by `poll`, included hosted-runner waiting, and had `SEMBLE_AVAILABLE: false`.
   - **Type:** runner queueing + repeated poll work.
   - **Fix order:** reduce no-op launches, then fix Semble bootstrap.

4. **CI: lint dominates the stable long path**
   - **Evidence:** `ci` runs are consistently long (**p50 1098s**, **p95 1144s**, **16/16 success**); sampled runs `26332318086` and `26333154035` say `lint` dominated and reinstalled `actionlint`.
   - **Type:** compute / tool bootstrap overhead.
   - **Fix order:** low-risk caching and lane separation.

5. **Release / analysis side paths: retry and budget bottlenecks**
   - **Evidence:** `test_and_mark_stable` run `26328466709` failed on a single dispatch **HTTP 500**; `workflow_log_analysis` run `26328477066` failed after a **37-minute** run with a killed deep-audit stage.
   - **Type:** transient API failure + unbounded audit runtime.
   - **Fix order:** dispatch retry wrapper, then bounded partial-report audit.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` check-run polling and long reviewer/editor passes (`26332319470`, `26331429148`, `26329171639`)
  - `ci` lint lane (`26332318086`, `26333154035`)
  - Skip-only orchestrator child workflows (`clarify` / `plan` / `implement` / `orchestrate_clarify_respond`)

- **Top failure modes**
  - Transient `gh workflow run` dispatch failure in `test_and_mark_stable` run `26328466709`
  - Over-budget `workflow_log_analysis` run `26328477066` ending in runner shutdown
  - `review_autofix` churn/cancellations (**9/69** runs)

- **Highest-cost drivers**
  - Multi-pass, six-reviewer `review_autofix` runs even on trivial diffs (`26329171639`)
  - Large Copilot review prompt construction (`26333520350`, **102,842 tokens**)
  - Workflow-log-analysis summarisation (`26328477066`, **130,578 tokens** on `gpt-5.4-mini`)

- **Top 3 prioritized actions**
  1. Bound `review_autofix` check-run waiting and allow partial check context.
  2. Pre-gate clarify/plan/implement/respond dispatches in the parent orchestrator.
  3. Add bounded retry/backoff to release-test workflow dispatches and hard budgets to `workflow_log_analysis`.

## Metrics Appendix

*Note: MCP and AI-memory telemetry below exclude copied markers inside `workflow_log_analysis` run `26328477066`, so the tables avoid double-counting reprinted log lines.*

### Run summary

| Scope / family | Runs | Success | Failure | Cancelled | Other | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Overall | 733 | 189 | 2 | 11 | 531 | 125.4 | 1.0 | 1073.0 |
| `review_autofix` | 69 | 58 | 0 | 9 | 2 | 805.3 | 11.0 | 4527.8 |
| `ci` | 16 | 16 | 0 | 0 | 0 | 1099.8 | 1098.0 | 1144.0 |
| `orchestrate_poll` | 34 | 34 | 0 | 0 | 0 | 156.1 | 139.5 | 188.0 |
| `copilot_pull_request_reviewer` | 9 | 9 | 0 | 0 | 0 | 118.8 | 89.0 | 225.2 |
| `clarify` | 143 | 6 | 0 | 0 | 137 | 6.1 | 1.0 | 2.0 |
| `plan` | 135 | 5 | 0 | 0 | 130 | 9.5 | 1.0 | 8.6 |
| `implement` | 135 | 5 | 0 | 2 | 128 | 16.8 | 1.0 | 8.6 |
| `orchestrate_clarify_respond` | 135 | 1 | 0 | 0 | 134 | 1.3 | 1.0 | 2.0 |
| `test_and_mark_stable` | 1 | 0 | 1 | 0 | 0 | 3625.0 | 3625.0 | 3625.0 |
| `workflow_log_analysis` | 1 | 0 | 1 | 0 | 0 | 2219.0 | 2219.0 | 2219.0 |

### Critical-path samples

| Run ID | Family | Step / signal | Measured value |
|---|---|---|---|
| `26332319470` | `review_autofix` | `Collect PR check-run failures CI lint autofix context` | **46 waits / 920s sleep / 942.9s step span** |
| `26329171639` | `review_autofix` | Same-SHA wait loop in `review_codex-agent` | **59 waits / 1172s sleep / 46.8% of 2504s run** |
| `26333520350` | `copilot_pull_request_reviewer` | Prompt build before session creation | **102,842 tokens / 136.7s pre-session / 86.0% of processing step** |
| `26328477066` | `workflow_log_analysis` | `analyze-commit-notify` + `deep-audit` | **1994.6s** analysis stage; `deep-audit` ended with **exit 143** |
| `26328466709` | `test_and_mark_stable` | `Dispatch & watch — update_workflows` | **HTTP 500** dispatch failure after **348.0s** step span |

### Observed token metrics

| Run ID | Family | Metric | Value | Notes |
|---|---|---|---:|---|
| `26333520350` | `copilot_pull_request_reviewer` | Built prompt tokens | 102842 | Prompt assembled before model session creation |
| `26328477066` | `workflow_log_analysis` | `summarize_unselected_runs` tokens_used | 130578 | `openai/gpt-5.4-mini`; 81 summarized / 100 targeted |
| `26331429148` | `review_autofix` | Logged `tokens used` | 183187 | Observed in conflict-resolver/validate/stage step |
| `26329171639` | `review_autofix` | Logged `tokens used` | 139642 | Observed immediately before pass-1 completion; scope not fully disambiguated in log |

### Prompt cache and AI-memory metrics

| Metric | Value | Notes |
|---|---:|---|
| Deep-dive runs with `OPENROUTER_PROMPT_CACHE_DISABLED: false` | 9 | All observed in `review_autofix` |
| Observed `cache_creation_input_tokens` lines | 0 | Not emitted in non-analysis runtime logs |
| Observed `cache_read_input_tokens` lines | 0 | Not emitted in non-analysis runtime logs |
| AI-memory `retrieve` ops | 8 | All in `review_autofix` runtime logs |
| AI-memory retrieve hit rate | 0% | `records_selected: 0` in all 8 |
| Avg `estimated_tokens` on retrieve | 0.0 | No retrieval budget field emitted |
| `keyword_method = none` | 8/8 | No `plain` or `llm` retrieval |
| `record-run-event` ops | 16 | All `push_attempts: 1` |
| `record-candidate` ops | 6 | All `push_attempts: 1` |
| Push attempts >1 | 0 | Across 22 push-bearing AI-memory ops |
| `promote` / `compact` / `finalize-task` / `processed-command-*` | 0 observed | Not present in runtime telemetry |

### GH API / GitHub-adjacent call summary

| Workflow / run | Observed pattern | Count / lower bound | Rate-limit note |
|---|---|---:|---|
| `review_autofix` `26332319470` | Same-SHA check-run polling | 46 wait iterations | No GH `429` observed |
| `review_autofix` `26329171639` | Same-SHA check-run polling | 59 wait iterations | No GH `429`; one model-provider `429` observed |
| `review_autofix` `26296423247` | Same-SHA check-run polling | 45 wait iterations | No GH `429` observed |
| `review_autofix` `26293979422` | Same-SHA check-run polling | 44 wait iterations | No GH `429` observed |
| `review_autofix` `26332317273` | `resolve-claude-branch-pr` GH API lookups | 2 calls | No `429` / secondary rate limit |
| `review_autofix` `26333154192`, `26332586791` | Linked-issue GraphQL lookup | 1 call each | No `429` / secondary rate limit |
| `cancel_on_pr_close` `26333518255`, `26333154161` | Cancel helper + `/rate_limit` helper | Low volume | No retry triggered by rate limit |
| `copilot_pull_request_reviewer` `26333520350` | Copilot API PUT/POST traffic | 6 calls | Not a rate-limit hotspot |

### Semble / Serena / MCP summary

| Server | Query count | Fallback count | Probe count | Logged bytes | Avg bytes/query | Total ms | Avg ms/query | Query runs | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `SEMBLE` | 28 | 5 | 0 | 262746 | 9383.8 | 14524 | 518.7 | 6 | Queries all in `review_autofix`; fallbacks all in one validation test run |
| `SERENA` | 0 | 0 | 0 | 0 | n/a | 0 | n/a | 0 | No operational telemetry observed |
| Other MCP servers observed | 0 | 0 | 0 | 0 | n/a | 0 | n/a | 0 | None observed |

### Semble target breakdown

| Server | Target | Query count | Fallback count |
|---|---|---:|---:|
| `SEMBLE` | `overflow` | 21 | 5 |
| `SEMBLE` | `reviewer-context` | 6 | 0 |
| `SEMBLE` | `conflict-resolver-context` | 1 | 0 |
| `SERENA` | any | 0 | 0 |

### MCP availability rows (`probe_ok` / `probe_failed` / `probe_skipped`)

| Server | Target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---:|---:|---:|---|
| `SEMBLE` | all observed targets | 0 | 0 | 0 | No operational `SEMBLE_PROBE` lines in this artifact |
| `SERENA` | all observed targets | 0 | 0 | 0 | No operational `SERENA_PROBE` lines in this artifact |
| Other MCP servers | none | 0 | 0 | 0 | None observed |
