## Executive Summary

- **The highest-impact reliability issue is MCP/OpenRouter incompatibility in `implement` and reviewer runs.** Failed `implement` runs 25076992830, 25057072163, 25055428237, 25069841009 and others all died in `implement / implement` → `Run Codex implementation`, with logs showing repeated OpenRouter `HTTP 400` on Azure when Codex emitted an invalid MCP tool entry after a failed MCP handshake. This explains most of the `implement` family’s 4.9% failure rate and its worst outliers. **Estimated impact:** cut implement failures by a majority and save 30–80 minutes on bad runs. **Confidence:** high.

- **`review_autofix` is spending large time and cost on doomed reviewer calls plus check-run waiting.** In failed review runs 25045997555 and 25046910871, 3 of 6 reviewer models (`deepseek/deepseek-v4-pro`, `qwen/qwen3.6-plus`, `x-ai/grok-4.1-fast`) consistently 422 on namespaced MCP tools. In a recent “successful” run 25087796721, `REVIEWERS_SUCCESSFUL` was still only 3, and the job also spent ~8m50s polling one in-flight check run before reviewer execution. **Estimated impact:** 20–40% faster `review_autofix`, materially lower token spend, fewer partial-review outcomes. **Confidence:** high.

- **The pipeline has excessive trigger churn and no-op fanout.** Across 1,000 runs, 689 ended as “other” (predominantly skipped). Families with extreme skip/no-op behavior include `clarify` (186/200 other), `plan` (168/183 other), `implement` (155/183 other), and `orchestrate_clarify_respond` (178/182 other). Recent logs show bursts where `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` all launch and finish in 0–2 seconds. **Estimated impact:** lower queue pressure, fewer API calls, less operator noise; small per-run savings but high aggregate. **Confidence:** high.

- **Long-tail latency is dominated by polling and watchdog behavior, not just model compute.** `orphan-workflows-test` in failed stable gate run 25074100587 polled `workflow-log-analysis` for ~25 minutes before timing out, and review run 25087796721 polled check-runs for ~530 seconds. `orchestrate_poll` failures 25058629488 and 25061570578 never reached job logic—they died waiting for hosted runners. **Estimated impact:** 10–25 minutes off affected gate/review paths, plus lower GH API pressure. **Confidence:** high.

- **Serena is helping, but current adoption is too low and instrumentation is inconsistent.** In review run 25087796721, the generated Serena report showed **1,548 Serena tool calls**, **1,681 file-based fallback ops**, and **48% efficiency**, with estimated code-access tokens of **~946k with Serena vs ~1.839M without**. Yet the same run’s token/stats step reported “No Serena tool usage stats found.” **Estimated impact:** another 15–25% token reduction in review/edit phases if fallback reads are reduced and reporting is unified. **Confidence:** medium-high.

- **AI memory retrieval is functioning but underused in many contexts.** Across sampled telemetry there were **100 retrieve operations**, **80% hit rate**, average **36.8 estimated tokens**, and keyword methods split **65 plain / 15 llm / 20 none**. Twenty retrieves returned zero records, especially reviewer retrieval in run 25087796721. **Estimated impact:** modest token savings and better consistency if memory seeding improves; low direct risk. **Confidence:** medium.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

1. **Stop retrying MCP-broken `implement` calls when the failure is deterministic**
   - **Evidence:** Failed `implement` run 25076992830 lasted **4,984s** and logs show repeated OpenRouter/Azure `HTTP 400` due to malformed tool payload after MCP handshake failure. Similar failures occurred in runs 25057072163 (**4,053s**), 25055428237 (**3,818s**), 25069841009 (**3,559s**), 25054349380 (**3,259s**), 25052297978 (**2,143s**). The issue body and run logs explicitly describe “5/5 Codex attempts return exit code 1.”  
   - **Root cause:** Deterministic retry loop on a non-transient provider/tool-shape error.
   - **Exact change:** Add a preflight MCP health gate in `setup_serena.sh` / `implement.yml` so optional MCP servers that fail initialize are omitted from Codex config before the first model call; if the exact malformed-tool/Azure signature appears, fail over immediately instead of consuming full retry budget.
   - **Estimated time savings:** **30–70 minutes** on affected failure runs; **high** critical-path win.
   - **Implementation risk:** **Low-medium.** Safe if limited to optional MCPs and exact known error signatures.

2. **Skip incompatible reviewer models or run them in no-MCP mode**
   - **Evidence:** Failed review runs 25045997555 and 25046910871 document that **3 of 6 reviewers** hard-fail with `HTTP 422 Unprocessable Entity` on namespaced MCP tools. The recent successful review run 25087796721 still ended with `REVIEWERS_SUCCESSFUL: 3`.
   - **Root cause:** Reviewer fleet includes models/providers that reject the Codex/OpenRouter MCP envelope.
   - **Exact change:** Maintain a denylist for MCP-incompatible reviewer slugs and either:
     1. strip all `[mcp_servers.*]` blocks for those reviewers, and  
     2. switch them to a no-MCP prompt variant,  
     or disable those reviewers until provider compatibility is verified.
   - **Estimated time savings:** **5–15 minutes per `review_autofix` run** when all six reviewers are attempted; also reduces retries and partial-review churn.
   - **Implementation risk:** **Low.** Already supported by log-proposed approach; quality risk manageable if compatible reviewers remain.

3. **Reduce or back off fixed-interval check-run polling in `review_autofix`**
   - **Evidence:** In run 25087796721, step `Collect PR check-run failures (CI/lint autofix context)` waited from **02:24:40 to 02:33:30 UTC** polling every **20s** for one in-progress/queued check run—about **8m50s** of pure wait before reviewer execution.
   - **Root cause:** Fixed polling before advisory context collection blocks the critical path.
   - **Exact change:** Use progressive backoff (e.g. 10s → 20s → 40s → 60s cap) and/or lower `CHECK_RUNS_WAIT_TIMEOUT_SECS` for reviewer context from 600s to 180–300s, while keeping fail-open behavior and sentinel output.
   - **Estimated time savings:** **5–8 minutes** on long-tail review runs; **high** critical-path win for review latency.
   - **Implementation risk:** **Low.** Context is advisory; timeout already degrades safely.

4. **Raise polling interval for `orphan-workflows-test` watcher**
   - **Evidence:** In stable gate failure 25074100587, step `orphan-workflows-test` watched workflow-log-analysis run 25074119156 from **19:47:37 to 20:12:46 UTC**, printing status every ~15s for ~25 minutes before timeout.
   - **Root cause:** Tight status polling on a long-running downstream workflow.
   - **Exact change:** Switch to stepped polling (15s for first 2 minutes, then 60s), or derive timeout from downstream workflow p95 rather than a fixed short watch window.
   - **Estimated time savings:** Little compute saved on the watched run itself, but **major GH API reduction** and less parent-job overhead; avoids false negative timeout if combined with longer timeout.
   - **Implementation risk:** **Low.**

5. **Lower planning reasoning level for standard issues or make it adaptive**
   - **Evidence:** Slow `plan` runs 25073268072 (**6,038s**) and 25052390881 (**4,448s**) ran with `MODEL_EDITOR=openai/gpt-5.4` and `MODEL_REASONING_EFFORT=xhigh`.
   - **Root cause:** Expensive reasoning profile on long/complex planning prompts.
   - **Exact change:** Default `plan` to `high` or adaptive reasoning: use `medium/high` for single-issue, low-dependency plans; escalate to `xhigh` only for blocked/multi-wave/orchestrator-bound cases.
   - **Estimated time savings:** **15–35% on slow `plan` runs**; less effect on already-short skipped runs.
   - **Implementation risk:** **Medium.** Needs guardrails to preserve plan quality on difficult issues.

6. **Eliminate no-op fanout before workflow dispatch**
   - **Evidence:** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` frequently launch as skipped/no-op runs; recent windows show multiple 0–2s runs firing in the same second. Across the sample, `clarify` had **186/200 other**, `plan` **168/183 other**, `implement` **155/183 other**, `orchestrate_clarify_respond` **178/182 other**.
   - **Root cause:** Triggers fire broadly and rely on in-workflow guards to no-op.
   - **Exact change:** Move state/label gating to dispatch conditions where possible so only one next-phase workflow is enqueued.
   - **Estimated time savings:** **Small per issue**, but meaningful aggregate queue relief and lower scheduling contention.
   - **Implementation risk:** **Medium.** Must preserve current edge-case semantics.

7. **Review local micro-optimization: avoid heavy runner cleanup on short-lived jobs**
   - **Evidence:** Review logs repeatedly run aggressive disk cleanup (`apt-get remove`, cache cleanup, image pruning) even on runs that later short-circuit or only achieve partial reviewer execution.
   - **Root cause:** Expensive environment prep is unconditional.
   - **Exact change:** Gate deep cleanup behind a disk-pressure threshold or only on long editor/reviewer phases.
   - **Estimated time savings:** **1–3 minutes** on review jobs.
   - **Implementation risk:** **Low-medium.** Must ensure runner disk remains sufficient for worst-case jobs.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

1. **Remove doomed reviewer calls from the fleet**
   - **Evidence:** Review failures 25045997555 and 25046910871 explicitly state that 3 of 6 reviewers fail every attempt with OpenRouter 422; 25087796721 still shows `REVIEWERS_SUCCESSFUL: 3`.
   - **Root cause:** Paying for orchestration/prompt setup around reviewers that cannot complete.
   - **Exact change:** Disable or no-MCP-route the three incompatible reviewer models until they prove healthy.
   - **Estimated savings:** Roughly **up to 50% of reviewer-attempt overhead** for affected runs; also fewer consensus/summarizer inputs built from empty outputs.
   - **Quality-risk notes:** **Low-medium.** Reviewer diversity drops unless no-MCP fallback preserves participation.

2. **Increase Serena adoption above the current 48%**
   - **Evidence:** Run 25087796721 Serena report: **1,548 Serena tool calls**, **1,681 fallback file ops**, **48% efficiency**, **~946,320 estimated tokens with Serena vs ~1,839,300 without**.
   - **Root cause:** Too many broad file-based reads/writes remain in reviewer/editor behavior.
   - **Exact change:** Tighten reviewer/editor prompts and post-run linting to require:
     - `get_symbols_overview` before file reads,
     - `find_symbol`/`find_referencing_symbols` for impact analysis,
     - symbol-body replacements instead of whole-file rewrites.
   - **Estimated savings:** Moving from **48% to ~70%** Serena efficiency should conservatively save **15–25%** of code-navigation/edit tokens in review/edit paths.
   - **Quality-risk notes:** **Low** if fallbacks remain available on Serena errors.

3. **Deduplicate repeated prompt sections to improve token efficiency and cacheability**
   - **Evidence:** In review run 25087796721 `Run reviewer models`, the Serena instruction block appears repeated in the prompt log, along with long GitHub API hygiene material and other static policy text. The prompt is also broadcast across multiple reviewers and passes.
   - **Root cause:** Repeated static guidance in assembled reviewer prompts.
   - **Exact change:** Normalize a single stable prefix file per workflow phase and inject only one copy of shared policy blocks; keep dynamic PR/issue context in the suffix.
   - **Estimated savings:** Likely **tens of thousands of tokens per `review_autofix` run** across 6 reviewers × 2 passes.
   - **Quality-risk notes:** **Low.** This is prompt dedupe, not instruction removal.

4. **Make `plan` reasoning adaptive instead of default `xhigh`**
   - **Evidence:** Slow plan runs 25073268072 and 25052390881 used `openai/gpt-5.4` with `xhigh`; planning p95 is **210s** overall but outliers reached **4,448–6,038s**.
   - **Root cause:** High-cost reasoning applied to all plan cases, including simpler ones.
   - **Exact change:** Use heuristic escalation: `medium/high` by default, `xhigh` only when the issue has cross-workflow dependencies, recovery state, or multi-step acceptance criteria.
   - **Estimated savings:** **15–30%** token and time reduction on non-edge planning runs.
   - **Quality-risk notes:** **Medium.** Keep automatic escalation and rollback flag.

5. **Avoid spending tokens on retry loops after known deterministic provider errors**
   - **Evidence:** Implement failures repeatedly retried the same Azure/MCP invalid-tool failure; logs describe **5/5** attempts failing identically.
   - **Root cause:** Retries assume transient failure.
   - **Exact change:** Short-circuit retries on exact known signatures (`missing field function`, invalid tool object, specific MCP handshake stub failure).
   - **Estimated savings:** Prevents full-run token burn on bad retries; **large savings on bad runs**, especially implement.
   - **Quality-risk notes:** **Low** if signature matching is exact and fail-open fallback exists.

6. **Improve prompt-cache observability before tuning cache policy**
   - **Evidence:** `OPENROUTER_PROMPT_CACHE_DISABLED=false` is consistently set, but sampled usage lines in run 25087796721 show `prompt_tokens=na`, `completion_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`. No usable cache hit/miss totals were present in `summary.json`.
   - **Root cause:** Instrumentation is enabled but not reporting actionable numeric cache results for actual model calls.
   - **Exact change:** Emit numeric cache-read/cache-create tokens for real calls, not just cache probes; record per-phase totals in run metadata.
   - **Estimated savings:** Indirect first; enables precision tuning. Likely unlocks **low-double-digit %** savings once stable prefixes are verified.
   - **Quality-risk notes:** **Low.** Telemetry-only.

7. **Reduce avoidable reruns/no-op runs**
   - **Evidence:** 689/1000 runs are “other”; recent bursts contain many skipped companion runs.
   - **Root cause:** Trigger fanout and late skipping.
   - **Exact change:** Prevent launching no-op workflows when state already makes them impossible to do useful work.
   - **Estimated savings:** Small token savings directly, but meaningful aggregate GH Actions/runtime cost reduction.
   - **Quality-risk notes:** **Low-medium.** Requires careful trigger gating.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

1. **Preflight optional MCP servers and strip failed ones from Codex config**
   - **Failure evidence:** Implement failures 25076992830, 25057072163, 25055428237, 25069841009, 25054349380, 25052297978; logs show Azure/OpenRouter rejecting malformed tool payload after MCP initialize failure.
   - **Root cause category:** Tooling integration / provider compatibility.
   - **Exact fix:** Before any Codex call, handshake each optional MCP server; if initialize fails, omit its config block entirely. Treat Context7/Git/Serena as optional unless the phase truly requires them.
   - **Expected reliability impact:** Should remove the dominant known `implement` failure mode and likely improve `validate`/`review_autofix` too.
   - **Rollback/fail-open:** Keep direct file/git fallback if MCPs are unavailable.

2. **Route or disable MCP-incompatible reviewer models**
   - **Failure evidence:** Review failures 25045997555 and 25046910871 failed in `review / codex-agent (claude-branch-review)` → `Run reviewer models`; logs identify 3 model slugs that 422 every attempt.
   - **Root cause category:** Provider/model incompatibility.
   - **Exact fix:** Maintain `MCP_INCOMPATIBLE_REVIEWER_MODELS` and auto-switch them to no-MCP prompts/config, or temporarily remove them from the reviewer set.
   - **Expected reliability impact:** Eliminates repeatable reviewer hard failures and raises `REVIEWERS_SUCCESSFUL`.
   - **Rollback/fail-open:** Env-controlled denylist; easy revert when provider compatibility changes.

3. **Increase watcher timeouts for downstream deep-audit workflows**
   - **Failure evidence:** `test_and_mark_stable` run 25074100587 failed because `workflow-log-analysis` run 25074119156 stayed `in_progress` until the watcher timed out after ~25 minutes.
   - **Root cause category:** Timeout mismatch between parent watcher and child workflow duration.
   - **Exact fix:** Set watcher timeout from observed child p95 plus setup buffer; current data shows `workflow_log_analysis` avg **1,709s**, p50 **1,444s**, and observed cancelled run **2,344s**.
   - **Expected reliability impact:** Reduces false gate failures in stable-release testing.
   - **Rollback/fail-open:** Longer watch only; no behavior break.

4. **Treat check-run collection as advisory and shorten/block less**
   - **Failure evidence:** Review run 25087796721 spent ~530s waiting for one check run before reviewer execution.
   - **Root cause category:** Over-synchronous dependency on advisory CI context.
   - **Exact fix:** Lower wait timeout and preserve sentinel states (`timeout`, `api_error`, etc.) so review continues earlier.
   - **Expected reliability impact:** Fewer reviews stranded behind unrelated slow checks; lower chance of job timeout in long reviews.
   - **Rollback/fail-open:** Existing sentinel contract already supports fail-open.

5. **Mitigate hosted-runner queue failures for `orchestrate_poll`**
   - **Failure evidence:** `orchestrate_poll` failures 25058629488 and 25061570578 both ended at **903s** without reaching job logic; logs show repeated “Waiting for a runner to pick up this job...”.
   - **Root cause category:** Queueing / runner allocation.
   - **Exact fix:** Reduce unnecessary poller invocations, coalesce duplicated polls, and reserve poller use for active orchestrations only.
   - **Expected reliability impact:** Lowers queue-induced false failures and reduces shared-runner contention.
   - **Rollback/fail-open:** Pure trigger tightening.

6. **Raise observability consistency for Serena and token reporting**
   - **Failure evidence:** In review run 25087796721, step 041 said “No Serena tool usage stats found,” while step 042 still generated a Serena efficiency report.
   - **Root cause category:** Telemetry inconsistency.
   - **Exact fix:** Standardize one stats artifact path and mark the report source explicitly.
   - **Expected reliability impact:** Better diagnostics; faster incident triage.
   - **Rollback/fail-open:** Telemetry-only.

## AI Memory Health

- **Memory telemetry was observed** in sampled deep-dive logs.
- Across sampled logs there were **515 total AI memory operations**:
  - `record-run-event`: **220**
  - `retrieve`: **100**
  - `processed-command-check`: **80**
  - `processed-command-claim`: **80**
  - `record-candidate`: **20**
  - `processed-command-complete`: **15**

### Retrieval effectiveness
- **Retrieve hit rate:** **80%** (80/100 retrieves had `records_selected > 0`)
- **Average estimated tokens per retrieve:** **36.8**
- **Average budget tokens:** **0.0** in sampled telemetry (budget not populated meaningfully)
- **Keyword method distribution:**
  - `plain`: **65%**
  - `llm`: **15%**
  - `none`: **20%**

### Flags
- **Zero-result retrieves:** **20** of 100  
  Example: review run 25087796721, step `Retrieve reviewer memory context fail-open`, emitted `AI_MEMORY_TELEMETRY` with `records_selected: 0`, `estimated_tokens: 0`, `keyword_method: "none"`.
- **`fail_open: true` retrieves:** **0 observed** in sampled telemetry lines.
- **`enabled: false` retrieves:** **0 observed**.
- **High push retry counts:** none surfaced in sampled telemetry; sampled push attempts were typically `1`.

### Assessment
- Memory is **healthy enough operationally**—it is on, telemetry is emitted, and retrieve hit rate is decent.
- The weak spot is **coverage**, especially for reviewer contexts where retrieval sometimes falls back to `keyword_method: none` and returns no records.
- Recommendation:
  1. seed more reviewer-facing records from prior PR review outcomes,
  2. ensure retrieval queries include stable issue/PR identifiers and changed-path hints,
  3. surface retrieval miss reasons in telemetry (`no index hit`, `disabled scope`, `empty ledger`, etc.).

## GH API Call Audit

### High-volume / high-redundancy patterns

1. **Check-run polling in `review_autofix`**
   - **Workflow/job/step:** `review_autofix` / `review_codex-agent_claude-branch-review` / `Collect PR check-run failures (CI/lint autofix context)`
   - **Evidence:** Run 25087796721 polled one SHA repeatedly from **02:24:40** to **02:33:30** every **20s**.
   - **Pattern:** Repeated `GET /repos/{repo}/commits/{sha}/check-runs?per_page=100`
   - **Estimated call count:** About **27** calls in this sample run.
   - **Redundancy:** High; advisory context, same SHA, single in-flight condition.
   - **Recommendation:** Backoff to 60s after 2 minutes, or cap at 180–300s.
   - **Estimated reduction:** **50–70% fewer calls** for long-tail runs.
   - **Rate-limit risk reduction:** Medium-high.

2. **Workflow watcher polling in `test_and_mark_stable`**
   - **Workflow/job/step:** `test_and_mark_stable` / `orphan-workflows-test` / watcher loop
   - **Evidence:** Failed run 25074100587 watched run 25074119156 for ~25 minutes with status prints every ~15s.
   - **Pattern:** Repeated run-status checks on the same downstream workflow.
   - **Estimated call count:** Roughly **~100 status checks** in the sample failure.
   - **Redundancy:** Very high.
   - **Recommendation:** Use stepped polling and timeout derived from child p95.
   - **Estimated reduction:** **70%+** on long watches.
   - **Rate-limit risk reduction:** High.

3. **Metadata fanout in review jobs**
   - **Workflow/job/step:** `review_autofix` / `Collect PR metadata`
   - **Evidence:** Step fetches:
     - PR payload
     - issue comments (`--paginate`)
     - PR reviews (`--paginate`)
     - PR review comments (`--paginate`)
     - linked issues via GraphQL
   - **Pattern:** Several necessary calls, but paginated lists can get expensive on noisy PRs.
   - **Recommendation:** Cache cycle-local outputs once per run and pass file paths downstream; avoid any later step re-fetching the same resources.
   - **Estimated reduction:** **Modest** in sampled runs because some reuse already exists, but important for high-comment PRs.
   - **Rate-limit risk reduction:** Medium.

4. **No-op workflow dispatch churn**
   - **Evidence:** Massive counts of skipped/other runs in `clarify`, `plan`, `implement`, `orchestrate_clarify_respond`.
   - **Pattern:** Triggering workflows that immediately self-skip still incurs GitHub Actions scheduling/event/API overhead.
   - **Recommendation:** Move guard logic earlier, ideally before dispatch.
   - **Estimated reduction:** Potentially **hundreds of run starts per 1,000-run window**.
   - **Rate-limit risk reduction:** Medium.

### Cross-check against repo API hygiene rules
The logs themselves include strong repo rules for **batching**, **cycle-local caches**, and **fail-open behavior**. The main gap is not missing policy—it is that some polling loops still dominate the call volume. Highest-value improvements are therefore:
- backoff in watch/poll loops,
- stronger reuse of already-fetched context,
- eliminating no-op workflow invocations before they start.

## MCP & Serena Efficiency

- **Observed status:** Serena is actively used but not efficiently enough.
- **Best evidence:** Review run 25087796721 generated:
  - **Serena tool calls:** 1,548
  - **Fallback file ops:** 1,681
  - **Serena efficiency:** 48%
  - **Top tools:** `replace_symbol_body` (270), `insert_after_symbol` (270), `get_symbols_overview` (216), `find_symbol` (208), `find_referencing_symbols` (206)
  - **Estimated tokens:** ~946,320 with Serena vs ~1,839,300 without

### Findings
1. **Fallback file operations still exceed Serena tool calls**
   - This means the tool policy is present, but real runs still drift into broad reads/writes.

2. **Instrumentation path is inconsistent**
   - Step `Log token usage and Serena stats` said “No Serena tool usage stats found,” while the next step still computed a Serena efficiency report.

3. **Reviewer compatibility issues reduce Serena usefulness**
   - Half the reviewer fleet appears to be effectively no-op or failing under MCP, which depresses useful Serena-driven semantic work.

### Concrete changes
- Enforce Serena-first workflow with post-run checks:
  - warn when file reads exceed a threshold before `get_symbols_overview`/`find_symbol`,
  - warn on whole-file rewrite when symbol-edit tools were available.
- Split prompt guidance by role:
  - reviewers: read-only Serena navigation,
  - editors: symbol-aware edit tools.
- For MCP-incompatible models, explicitly switch to a no-MCP prompt instead of letting them fail after seeing Serena instructions.
- Parallelize safe metadata fetches and diff summarization before model invocation so Serena time is spent on semantic work, not waiting.

### Expected impact
- **Token efficiency:** medium-high gain
- **Turnaround time:** medium gain
- **Correctness preservation:** high, because fallbacks remain available

## Prompt Cache & Memory System

### Prompt cache
- **Observed behavior:** Cache is enabled (`OPENROUTER_PROMPT_CACHE_DISABLED=false`) across sampled `plan`, `implement`, and `review_autofix` runs.
- **Observed gap:** Sampled usage lines only showed probe events with `prompt_tokens=na`, `completion_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`. No corpus-level cache hit/miss metrics were present in `summary.json`.
- **Assessment:** Cache likely exists, but current telemetry is insufficient to prove hit rate or savings.

### Likely cache-fragmentation causes
1. **Large dynamic prompt bodies**
   - Issue/PR bodies, recent comments, and runtime context vary heavily.
2. **Repeated static instructions duplicated inside assembled prompts**
   - Especially in reviewer prompts.
3. **Per-model/per-pass prompt variance**
   - Six reviewers, two passes, no-MCP vs MCP variants, plus dynamic runtime suffixes.

### Recommendations
1. **Stabilize the prompt prefix**
   - One canonical static prefix per workflow family and role.
2. **Push dynamic noise to the suffix**
   - Issue state, comments, diff stats, check-run snapshot, branch names, etc.
3. **Emit numeric cache metrics for real calls**
   - Especially `cache_read_input_tokens`, `cache_creation_input_tokens`, `prompt_tokens`, `completion_tokens`.
4. **Keep fail-open cache behavior**
   - Current logs already indicate this is the intended behavior; preserve it.

### Memory retrieval effectiveness
- Retrieval hit rate is decent (**80%**), but reviewer retrieval misses are still common.
- Recommendation: better memory seeding and retrieval-key design for reviewer contexts.

### Estimated impact
- **Tokens:** medium
- **Latency:** low-medium
- **Reliability:** medium via better observability and fewer oversized prompts

## Orchestrator Health

### What looks healthy
- Core `orchestrate` runs were successful in the sample, with avg duration **1,405.7s** across 3 runs.
- `orchestrate_poll` had **28/30 successes**, though failures were queue-related rather than logic failures.

### Pain points
1. **Poller queue sensitivity**
   - Failures 25058629488 and 25061570578 never executed useful work.
2. **Compensating stall-recovery behavior**
   - Slow plan logs show auto-answered/stall-recovery text for clarification/planning, indicating the poller is covering for upstream stall conditions.
3. **Workflow fanout churn**
   - Many companion workflows launch only to skip, suggesting orchestrator state transitions are too noisy.

### Smallest safe mitigations
- Coalesce duplicate poller dispatches.
- Record and expose “standalone stall recovery” counts as a first-class metric.
- Skip launching downstream clarify/plan/implement/respond workflows when current labels/state already prove they will no-op.

### Observable indicators to track
- `orchestrate_poll` failure rate
- average poller queue wait
- skipped/no-op runs per issue lifecycle
- count of stall-recovery events
- average judge cycles per orchestration
- downstream workflow start count per successfully completed issue

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

1. **Implement phase failure/retry overhead**
   - `implement` p95 is **1,901.6s**, with multiple 3,000–5,000s failures.
   - Dominant bottleneck is deterministic MCP/provider failure, not codegen speed.

2. **Review/autofix pre-review waiting**
   - `review_autofix` p95 is **1,705.95s**.
   - Sample run 25087796721 lost ~9 minutes before reviews even started due to check-run polling.

3. **Reviewer fleet incompatibility**
   - Half the reviewer fleet is effectively wasted on some runs, adding both latency and cost.

4. **Watcher timeout mismatch in release gating**
   - `test_and_mark_stable` failed because the parent watcher timed out before `workflow-log-analysis` finished.

5. **Queueing overhead**
   - `orchestrate_poll` failures show runner scarcity can fully consume the job budget.
   - No-op workflow bursts likely worsen queue pressure.

6. **Planning outliers**
   - Most plan runs are short or skipped, but the long tail is severe: up to **6,038s**.

### By bottleneck type
- **Queueing:** `orchestrate_poll` hosted-runner waits; noisy no-op runs
- **Compute:** slow `plan`, `implement`, `review_autofix`
- **Retry:** deterministic MCP/provider failures retried too long
- **Merge/conflict overhead:** not strongly evidenced in sampled run logs; monitor separately

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `implement` deterministic MCP/OpenRouter failures causing 30–80 minute bad runs
- `review_autofix` waiting on check-runs plus partially broken reviewer fleet
- high skip/no-op fanout across clarify/plan/implement/respond workflows
- stable gate timeout while watching long `workflow-log-analysis` runs

**Top failure modes**
- `implement / implement` → `Run Codex implementation` with Azure/OpenRouter malformed MCP tool payload
- `review / codex-agent (claude-branch-review)` → `Run reviewer models` with 422 provider incompatibility
- `orchestrate_poll` runner-queue exhaustion
- `test_and_mark_stable` watcher timeout on downstream workflow

**Highest-cost drivers**
- `review_autofix` multi-reviewer, multi-pass runs with only 3 successful reviewers
- `implement` xhigh reasoning plus full retry loops on deterministic failures
- `plan` outliers running `gpt-5.4` at `xhigh`
- repeated static prompt material across reviewers/passes

**Top 3 prioritized actions**
1. **Preflight and strip failed optional MCP servers before any Codex call**
2. **Disable or no-MCP-route incompatible reviewer models immediately**
3. **Back off polling loops and stop dispatching no-op downstream workflows**

## Metrics Appendix

### Overall repository summary

| Repo | Total runs | Success | Failure | Cancelled | Other/skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 263 | 14 | 34 | 689 | 1.4% | 176.5 | 1.0 | 1127.0 |

### Key workflow family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | Other | Avg duration (s) | p50 (s) | p95 (s) | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| implement | 183 | 15 | 9 | 4 | 155 | 222.2 | 1.0 | 1901.6 | Main reliability problem |
| review_autofix | 94 | 65 | 2 | 26 | 1 | 510.0 | 31.5 | 1705.9 | Major cost/time center |
| plan | 183 | 14 | 0 | 1 | 168 | 116.1 | 1.0 | 210.0 | Severe long-tail outliers |
| orchestrate_poll | 30 | 28 | 2 | 0 | 0 | 148.9 | 71.5 | 869.7 | Queue-sensitive |
| orchestrate | 3 | 3 | 0 | 0 | 0 | 1405.7 | 1713.0 | 2217.9 | Small sample |
| test_and_mark_stable | 3 | 0 | 1 | 2 | 0 | 2802.7 | 3542.0 | 3714.8 | Gate timeout issue |
| workflow_log_analysis | 3 | 2 | 0 | 1 | 0 | 1709.3 | 1444.0 | 2254.0 | Child workflow often long |
| ci | 58 | 58 | 0 | 0 | 0 | 610.7 | 615.5 | 651.3 | Stable baseline |

### Deep-dive sampled run evidence

| Run ID | Family | Conclusion | Duration (s) | Key evidence |
|---|---|---|---:|---|
| 25076992830 | implement | failure | 4984 | Azure/OpenRouter 400 on malformed MCP tool payload in `Run Codex implementation` |
| 25057072163 | implement | failure | 4053 | Same MCP/provider failure mode |
| 25055428237 | implement | failure | 3818 | Same MCP/provider failure mode |
| 25069841009 | implement | failure | 3559 | Same MCP/provider failure mode |
| 25045997555 | review_autofix | failure | 668 | 3/6 reviewers 422 on namespaced MCP tools |
| 25046910871 | review_autofix | failure | 697 | Same reviewer incompatibility |
| 25087796721 | review_autofix | success | 1476 | `REVIEWERS_SUCCESSFUL=3`; ~8m50s check-run polling; Serena efficiency 48% |
| 25073268072 | plan | success | 6038 | `gpt-5.4` with `xhigh`; severe planning outlier |
| 25058629488 | orchestrate_poll | failure | 903 | Runner queue exhaustion; no useful execution |
| 25074100587 | test_and_mark_stable | failure | 3542 | `workflow-log-analysis` watcher timed out after ~25 min |

### GH API audit summary

| Workflow / Step | API pattern | Estimated call volume in sample | Issue |
|---|---|---:|---|
| review_autofix / Collect PR metadata | PR + comments + reviews + review comments + GraphQL linked issues | 5+ base calls, paginated | Reasonable, but must be reused downstream |
| review_autofix / Collect PR check-run failures | `GET /commits/{sha}/check-runs` loop | ~27 | High polling overhead |
| test_and_mark_stable / orphan-workflows-test | downstream run status polling | ~100 | Very high polling overhead |
| trigger fanout across clarify/plan/implement/respond | workflow start/skip churn | high aggregate | No-op launches add queue/API noise |

### AI memory telemetry

| Metric | Value |
|---|---:|
| Total memory ops observed | 515 |
| Retrieve ops | 100 |
| Retrieve hit rate | 80% |
| Zero-result retrieves | 20 |
| Avg estimated tokens per retrieve | 36.8 |
| Keyword method: plain | 65 |
| Keyword method: llm | 15 |
| Keyword method: none | 20 |
| `fail_open: true` retrieves | 0 observed |
| `enabled: false` retrieves | 0 observed |

### Serena efficiency (sampled run 25087796721)

| Metric | Value |
|---|---:|
| Serena tool calls | 1548 |
| Fallback file ops | 1681 |
| Serena efficiency | 48% |
| Estimated tokens with Serena | ~946,320 |
| Estimated tokens without Serena | ~1,839,300 |
| Top tools | `replace_symbol_body` (270), `insert_after_symbol` (270), `get_symbols_overview` (216), `find_symbol` (208), `find_referencing_symbols` (206) |

### Token/cache observability status

| Metric | Status |
|---|---|
| Total prompt/completion tokens across corpus | Not available in `summary.json` |
| Cache creation/read totals across corpus | Not available in `summary.json` |
| Sampled OpenRouter probe lines | Present, but numeric fields were `na` |
| Actionable cache hit-rate measurement | Insufficient in current sampled telemetry |

### Sampled log folder coverage used

| Section | Repo/family coverage |
|---|---|
| `errors/` | implement (9), orchestrate_poll (2), review_autofix (2), test_and_mark_stable (1) |
| `slow/` | implement (6), plan (3), test_and_mark_stable (1) |
| `recent/` | cancel_on_pr_close (1), ci (1), clarify (2), implement (2), issue_pr_status (1), orchestrate_clarify_respond (2), orchestrate_poll (1), plan (2), review_autofix (3) |

If you want, I can turn this into a **ranked remediation checklist** with owner, file/workflow targets, and rollout order.
