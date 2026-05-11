## Executive Summary

- **Fix the `review_autofix` conflict-resolver failure cluster first.** `review_autofix` ran 150 times, with **19 failures** and **71 cancellations**; all 19 recorded failures stopped at `review / codex-agent` → `Run Codex resolver, validate, stage, commit`. Deep-dive runs **25640363259**, **25640364808**, and **25641872477** show merge conflicts that include `scripts/review_conflict_resolve.sh`, followed immediately by a shell syntax error on `<<<`. **Estimated impact:** recover most of the current `review_autofix` failure rate and much of the **75,781s** of failed runtime in this window. **Confidence:** high.

- **The biggest single latency win is removing the 20-minute check-run poll gate from the critical path.** Runs **25640363259** and **25640364808** each spent about **20 minutes** in `Collect PR check-run failures CI lint autofix context` (**1213.9s** and **1220.1s**) before timing out at **1200s** with one check still queued/in progress. **Estimated impact:** **5-20 minutes** faster on affected review runs plus materially fewer GitHub API calls. **Confidence:** high.

- **Reviewer/editor compute is oversized for some small or modest diffs.** In run **25640363259**, `Run reviewer models` took **1783.0s** and `Apply fixes with editor model` took **2154.6s**; in **25640364808**, the same stages took **1538.9s** and **1687.6s**. Run **25642515817** still ran a **1605s** reviewer panel even though the gate logged `files=3 additions=24 ... max_add=10 max_del=10 ... small_diff=false`. **Estimated impact:** **30-60%** lower review latency/cost on qualifying PRs. **Confidence:** medium.

- **CI reliability is currently dominated by one contract drift, not broad test instability.** `ci` failed **15/65 runs (23.1%)**, and every listed CI failure in this window stopped at `lint / Review Semble contract test`. Deep-dive runs **25639158570** and **25634879805** show the same assertion failure while Semble helper/fallback tests still pass. **Estimated impact:** likely remove nearly all current CI failures in this window. **Confidence:** high.

- **AI memory retrieval is effectively inert.** Across the deduped `AI_MEMORY_TELEMETRY` parse for this window, there were **17 retrieve operations**, **0 hits**, **0 average estimated tokens**, and `keyword_method="none"` every time. **Estimated impact:** small direct latency/token savings, but high observability value because the retrieval path is not contributing useful context. **Confidence:** high.

- **Semble is not yet showing measurable production benefit in sampled review runs.** In run **25640363259**, `SEMBLE_AVAILABLE=false` and `SEMBLE_INDEX_AVAILABLE=false`; the only direct Semble signals in deep-dive logs were **10 `SEMBLE_FALLBACK` events** across CI contract-test runs **25639158570** and **25634879805**, all `target=overflow` and all fail-open. **Estimated impact:** medium cost/clarity improvement once availability and byte-level query telemetry are fixed. **Confidence:** medium.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1) Critical-path win: cut the `review_autofix` check-run wait gate

- **Evidence**
  - Run **25640363259**, step `Collect PR check-run failures CI lint autofix context`, ran for **1213.9s** and printed **57** repeated wait messages before warning: `CHECK_RUNS_WAIT_TIMEOUT reached after 1200s`.
  - Run **25640364808**, same step, ran for **1220.1s** with the same timeout pattern.
  - Run **25641872477** shows the same step still taking a material **308.1s**.

- **Root cause**
  - The workflow keeps polling the same PR head SHA for queued/in-progress check-runs on a fixed **20s** interval until either all checks clear or the **1200s** timeout expires.
  - On the worst runs, the workflow eventually proceeds anyway, which means the final 15-20 minutes were pure wait overhead.

- **Exact change**
  - Lower the blocking timeout for this step from **1200s** to something like **180-300s** unless a still-pending check is branch-protection-required.
  - Snapshot current failing checks plus a `pending_checks` list after a short bounded wait, and continue instead of waiting for all queued checks to finish.
  - Replace fixed 20-second polling with a small bounded backoff sequence.
  - If only non-required or long-queued checks remain, stop blocking and mark the context as partial.

- **Estimated time savings**
  - **15-20 minutes** on the worst affected review runs.
  - **5+ minutes** even on moderate cases like **25641872477**.

- **Implementation risk**
  - **Low-medium.**
  - Risk is slightly staler context if a late check fails after the snapshot.
  - Safe mitigation: explicitly record that some checks were still pending, and let later review iterations or CI status updates catch any late failure.

### 2) Critical-path win: expand the small-diff fast lane in `review_autofix`

- **Evidence**
  - Run **25642515817** completed successfully but still took **1605s**.
  - Its gate summary logged: `AUTOFIX_GATE_DET_SKIP_EVAL pr=2470 files=3 additions=24 ... max_add=10 max_del=10 ... small_diff=false skip=false`.
  - In long runs like **25640363259** and **25640364808**, the reviewer/editor stages dominated total runtime.

- **Root cause**
  - The current deterministic small-diff thresholds are very conservative.
  - Even PRs that are small enough to skip editor/commit/judge work can still pay for a long multi-model reviewer panel.

- **Exact change**
  - **Inference:** expand the low-risk/small-diff gate above the current `max_add=10 max_del=10` threshold, using a conservative file/path allowlist.
  - Route qualifying PRs to:
    - a comment-only path with **1-2 reviewers**, or
    - a cheaper reviewer-only path without the full panel and without editor/commit/judge.
  - Keep full review depth for workflows, shell scripts, orchestrator code, or larger diffs.

- **Estimated time savings**
  - **15-25 minutes** on qualifying runs.
  - Roughly **30-60%** latency reduction on those PRs.

- **Implementation risk**
  - **Medium.**
  - Quality risk exists if risky files slip into the fast lane.
  - Mitigate by keeping the fast lane opt-in for low-risk paths only and falling back to full review on workflow/script/orchestrator changes.

### 3) Throughput win: cancel superseded `review_autofix` runs before model work starts

- **Evidence**
  - `review_autofix` had **71 cancelled runs out of 150** total (**47.3%**).
  - Cancelled runs still consumed **15,726s** total, with a **p95 of 912s**.
  - Recent successful review summaries such as **25641900135** and **25642515817** also show runner-start delays before the actual review work begins.

- **Root cause**
  - New PR updates or redispatches are overtaking older runs after they have already queued or begun setup.
  - Some cancelled runs likely reach expensive stages before being superseded.

- **Exact change**
  - Add a stale-run check before Codex installation and again immediately before `Run reviewer models`.
  - Strengthen concurrency keys around PR number + head ref so older runs are cancelled earlier.
  - Preserve the existing self-trigger skip, but move supersession detection earlier in the job.

- **Estimated time savings**
  - Not the biggest single-run latency improvement, but a meaningful queue/capacity win.
  - Up to **4.4 hours** of cancelled runtime eliminated in this sample window.

- **Implementation risk**
  - **Low.**
  - Only cancel when a newer run on the same PR/head is confirmed.

### 4) Micro-optimization: stop scheduling no-op clarify/plan/implement/respond jobs

- **Evidence**
  - `clarify`: **169/174** skipped.
  - `plan`: **163/167** skipped.
  - `implement`: **161/167** skipped.
  - `orchestrate_clarify_respond`: **165/166** skipped.
  - Recent runs **25643131616**, **25643054844**, **25643004034**, and **25643131601** show job-level `if` conditions evaluating false almost immediately.

- **Root cause**
  - Many workflows are still being triggered and assigned runners before a trivial command/body gate skips them.

- **Exact change**
  - Move the skip predicates up to workflow-level `if:` and tighter event filters wherever GitHub Actions supports it.
  - Alternatively, centralize command parsing in one dispatcher and only call downstream workflows when a command is actually present.

- **Estimated time savings**
  - Only **1-5 seconds** per run, so this is not a critical-path fix.
  - The real gain is lower queue noise across **hundreds** of skipped events.

- **Implementation risk**
  - **Low**, as long as the existing conditions are mirrored exactly.

### 5) Micro-optimization: narrow the forward-merge fetch scope

- **Evidence**
  - Run **25643124397** finished in **19s**, but `Checkout main` alone took **9.9s**.
  - The log shows many unrelated `claude/*` refs being fetched before the workflow opened a fallback PR.

- **Root cause**
  - The stable→main forward-merge job is fetching a much wider ref set than it needs.

- **Exact change**
  - Restrict fetches to the branches and tags needed for stable→main merge/fallback behavior instead of fetching the full branch namespace.

- **Estimated time savings**
  - About **5-10 seconds** per forward-merge run.

- **Implementation risk**
  - **Low**, assuming the fallback PR path still has access to `stable`, `main`, and required tags.

### 6) Lower-confidence, off-PR-path win: trim the heavy release-analysis workflows

- **Evidence**
  - `test_and_mark_stable` run **25631690654** took **8953s**, dominated by:
    - `workflow-log-analysis-test`: **8904.6s**
    - `e2e-smoke-test`: **4722.9s**
    - `e2e-alt-model-test`: **2372.9s**
  - `workflow_log_analysis` run **25631704000** took **8865s**, dominated by:
    - `api-redundancy`: **3718.8s**
    - `deep-audit`: **3384.6s**
    - `analyze-commit-notify`: **1659.8s**
  - These step times may overlap across jobs, so they should not be summed.

- **Root cause**
  - Release-quality analysis workloads are large and expensive.

- **Exact change**
  - Run the heaviest audits only on stable/release triggers, or reduce sample/deep-audit scope on non-release runs.
  - Keep job parallelism where it already exists.

- **Estimated time savings**
  - **Tens of minutes to hours** on those workflows.

- **Implementation risk**
  - **Medium**, and confidence is lower because only one sampled run was available for each workflow.

## Cost Optimizations

Actual prompt/completion token totals were mostly **not** available in this window, so the estimates below use **model fan-out, prompt byte size, wall time, failure volume, and rerun volume** as proxies.

### 1) Biggest waste: stop paying for full review runs that always die at the resolver step

- **Evidence**
  - All **19** `review_autofix` failures in the window stopped at `review / codex-agent` → `Run Codex resolver, validate, stage, commit`.
  - Run **25640363259** spent **1783.0s** in `Run reviewer models` and **2154.6s** in `Apply fixes with editor model`, then failed in **0.09s** at the resolver step with a syntax error.
  - Run **25640364808** followed the same pattern: **1538.9s** reviewer + **1687.6s** editor, then resolver failure.
  - Failed `review_autofix` runs consumed **75,781s** total in this sample.

- **Root cause**
  - The workflow is spending for reviewer/editor work before reaching a resolver path that is broken for this failure cluster.

- **Exact change**
  - Add a pre-resolver guard: if the conflicted file set includes `scripts/review_conflict_resolve.sh` or related resolver prompt/support assets, validate them first (`bash -n`, conflict-marker checks).
  - If validation fails, skip the automated resolver and open the manual review-blocked path immediately.

- **Estimated savings**
  - This is likely the **largest cost reduction** in the current window.
  - Exact token/$ savings are unavailable, but it should claw back most of the spend associated with the **19 failed review runs**.

- **Quality-risk notes**
  - **Low risk.**
  - It only diverts already-broken self-conflict cases to a manual path.

### 2) Right-size reviewer model fan-out and reasoning for low-risk diffs

- **Evidence**
  - Run **25643124416** exposed the configured reviewer/editor stack: `REVIEWER_MODELS` included **six reviewer models**, while `MODEL_EDITOR` was `openai/gpt-5.4`.
  - Reviewer/editor wall times are large in failing long runs:
    - **25640363259**: **1783.0s** reviewer + **2154.6s** editor
    - **25640364808**: **1538.9s** reviewer + **1687.6s** editor
  - Run **25642515817** shows that even a **3-file / 24-addition** PR can still miss the small-diff gate and pay for a long reviewer path.

- **Root cause**
  - A broad reviewer panel and expensive editor/consolidator settings are being applied too often.

- **Exact change**
  - For small/low-risk PRs, reduce the reviewer panel from six models to a smaller subset, or use a cheaper first pass and escalate only when disagreement/risk is detected.
  - Reduce reasoning depth on small-diff reviewer/editor paths while preserving the full path for workflow, shell, orchestrator, and larger code changes.

- **Estimated savings**
  - Likely **30-60%** on qualifying `review_autofix` runs.
  - Exact token/$ savings are not directly measurable from the current logs.

- **Quality-risk notes**
  - **Medium risk** if the fast path is too broad.
  - Mitigate with conservative file/path allowlists and an automatic upgrade to the full panel on risky diffs.

### 3) Bound the 300-second zero-output consolidator path

- **Evidence**
  - Run **25640363259** logged:
    - `stage=consolidator model=openai/gpt-5.4 reasoning=xhigh input_bytes=120206 output_bytes=0 wall_secs=300 exit_code=0 ... failopen=1`
  - Run **25640364808** logged the same pattern with `input_bytes=117995`, `output_bytes=0`, `wall_secs=300`, `failopen=1`.
  - This pattern recurs across the failing `review_autofix` cluster.

- **Root cause**
  - The consolidator can consume a large prompt and the full timeout budget, then fail open with no useful output.

- **Exact change**
  - Lower the fail-open timeout for the consolidator to something like **90-120s** when `output_bytes` remains empty.
  - Skip the consolidator entirely when upstream parser/ledger signals show there are no actionable blocks to consolidate.
  - Record zero-output consolidator results as an explicit degraded state so the same run does not repeat the expensive call.

- **Estimated savings**
  - About **3-5 minutes** and one large-model call per affected run.

- **Quality-risk notes**
  - **Low-medium risk.**
  - The current path is already fail-open with zero output, so a shorter timeout should not meaningfully reduce quality.

### 4) Eliminate cancelled `review_autofix` spend earlier

- **Evidence**
  - Cancelled `review_autofix` runs: **71**
  - Total cancelled runtime: **15,726s**
  - Cancelled-run **p95**: **912s**

- **Root cause**
  - Superseded runs are sometimes reaching setup or partial execution before cancellation.

- **Exact change**
  - Cancel stale runs before reviewer/editor phases, not after.
  - Re-check supersession after the wait-heavy context steps and before model invocation.

- **Estimated savings**
  - Potentially most of the **15,726s** of cancelled runtime in the current sample.
  - Exact token/$ savings are unavailable, but some portion of reviewer/editor spend is likely included in the long-tail cancellations.

- **Quality-risk notes**
  - **Low risk.**
  - Cancelling stale runs improves freshness and should not affect the newest run’s quality.

### 5) Remove avoidable CI reruns caused by the Semble contract drift

- **Evidence**
  - All **15** `ci` failures stopped at `lint / Review Semble contract test`.
  - Deep-dive runs **25639158570** and **25634879805** each failed with the same `AssertionError`.
  - These failing runs still take **531-664s** each.

- **Root cause**
  - A contract/assertion mismatch is forcing whole CI reruns for a narrow workflow-wiring issue.

- **Exact change**
  - Update `tests/test_review_semble_contract.py` to match the current workflow wiring, or restore the expected env line if the workflow changed unintentionally.

- **Estimated savings**
  - Roughly **9-11 minutes** per prevented CI rerun.

- **Quality-risk notes**
  - **Low risk** if the smallest safe change is to the assertion only.
  - If the workflow wiring is meant to be preserved, restore it rather than loosening the test.

### 6) Prompt-expansion and Semble note: fix observability before tuning compression logic

- **Evidence**
  - Production deep-dive run **25640363259** shows `OPENROUTER_PROMPT_CACHE_DISABLED=false`, but the `review_autofix_cache_probe` lines still report `prompt_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, and `cache_read_input_tokens=na`.
  - The same run shows `SEMBLE_AVAILABLE=false` and `SEMBLE_INDEX_AVAILABLE=false`.
  - No structured production `SEMBLE_QUERY target=... bytes=...` lines were present in the inspected deep dives.
  - The only direct Semble telemetry was **10 `SEMBLE_FALLBACK` events** in CI contract tests (**25639158570**, **25634879805**), and those lines had **no bytes field**.

- **Root cause**
  - Cost-reduction systems are configured but not measurable in sampled production runs.

- **Exact change**
  - Emit real prompt-cache create/read counters.
  - Emit structured `SEMBLE_QUERY target=... bytes=...` lines whenever Semble is actually used.
  - Keep Semble availability/index status adjacent to reviewer/editor stages so production savings can be measured.

- **Estimated savings**
  - **Unknown today.**
  - This is an observability prerequisite rather than a direct short-term savings lever.

- **Quality-risk notes**
  - **No quality risk**; this is measurement and routing hygiene.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1) Fix the shared `review_autofix` resolver failure cluster

- **Failure evidence**
  - All **19** `review_autofix` failures in the window stop at `review / codex-agent` → `Run Codex resolver, validate, stage, commit`.
  - Deep-dive run **25640363259** shows merge conflicts in:
    - `.github/workflows/test-and-mark-stable.yml`
    - `.github/workflows/workflow-log-analysis.yml`
    - `README.md`
    - `scripts/orchestrate_poll_process.sh`
    - `scripts/review_conflict_resolve.sh`
    - `tests/test_orchestrate_poll_process.py`
  - The same run then fails with `scripts/review_conflict_resolve.sh: line 625: syntax error near unexpected token '<<<'`.
  - Deep-dive runs **25640364808** and **25641872477** show the same resolver-step syntax-error failure class.

- **Root cause category**
  - **Merge/conflict automation failure**, specifically a self-conflicting resolver/support-script path.

- **Exact fix**
  - Before invoking the resolver, validate any conflicted workflow support files that the resolver depends on.
  - If `scripts/review_conflict_resolve.sh` or resolver prompt assets are themselves conflicted or fail shell syntax validation, bypass automated resolution and fail open to the existing manual review-blocked path.

- **Expected reliability impact**
  - This should address the dominant failure cluster in `review_autofix`.

- **Rollback / fail-open considerations**
  - Safe fallback is already available: open the manual conflict path instead of retrying a broken resolver.
  - That is a safer rollback than trying to make the resolver repair its own conflicted script.

### 2) Fix the Semble contract-test drift, but keep fail-open fallback behavior intact

- **Failure evidence**
  - All **15** `ci` failures in the window stopped at `lint / Review Semble contract test`.
  - Runs **25639158570** and **25634879805** show the same failure:
    - `AssertionError` in `tests/test_review_semble_contract.py`
    - expectation around `SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}`
  - In both runs, Semble helper/fallback tests still passed immediately before the failure.

- **Root cause category**
  - **Test/workflow contract drift**, not a general runtime Semble failure.

- **Exact fix**
  - Align the contract test with the intended workflow env wiring, or restore the workflow line if the wiring changed unintentionally.
  - Do **not** remove the fail-open fallback tests.

- **Expected reliability impact**
  - Likely eliminates nearly all current `ci` failures in this sampled window.

- **Rollback / fail-open considerations**
  - Smallest safe mitigation: fix the assertion only, preserving runtime fallback behavior unchanged.

- **`SEMBLE_FALLBACK` quantification**
  - Across the two deep-dive CI runs:
    - **10 total fallback events**
    - workflow: `ci`
    - job/step: `lint / Review Semble contract test`
    - target: `overflow`
    - files: `src/big.py` (8 events) and `src/small.py` (2 events)
    - reason: missing Semble binary path
    - `ms=0`
    - **no bytes field logged**
  - This looks like **healthy test-induced fail-open behavior**, not a masked broken production rollout.

### 3) Add retry/backoff to Copilot reviewer GitHub API calls

- **Failure evidence**
  - No current failures were captured here, but recent runs **25641774946** and **25641877284** show `actions/github-script@v8` using `retries: 0` around `github.rest.pulls.get` / `github.rest.pulls.listFiles`.

- **Root cause category**
  - **Transient GitHub API exposure without retry protection**.

- **Exact fix**
  - Add small retry/backoff settings for `github-script` calls that fetch PR metadata/files.
  - Keep cleanup idempotent so retries are safe.

- **Expected reliability impact**
  - **Low-medium**; protects against intermittent API/network failures.

- **Rollback / fail-open considerations**
  - Very low-risk: retry only on transient failures.

### 4) Make zero-output consolidator fail-open states explicit

- **Failure evidence**
  - Runs **25640363259** and **25640364808** both logged `stage=consolidator ... output_bytes=0 ... wall_secs=300 ... failopen=1`.
  - The current behavior is operationally ambiguous: expensive stage, no output, silent fail-open.

- **Root cause category**
  - **Masked degraded AI stage**, more than a hard workflow crash.

- **Exact fix**
  - Emit a structured degraded-stage marker when consolidator output is empty.
  - Avoid repeated long zero-output consolidator attempts within the same run/iteration.

- **Expected reliability impact**
  - Smaller than items 1-2, but it reduces diagnostic ambiguity and rerun guesswork.

- **Rollback / fail-open considerations**
  - Keep the same fail-open policy; change only timeout/observability behavior.

## AI Memory Health

- I found **`AI_MEMORY_TELEMETRY`** in the deep-dive `review_autofix` logs and deduped the events to **69 unique operations** from **116 raw lines**.

- **Operation mix**
  - `record-run-event`: **33**
  - `record-candidate`: **18**
  - `retrieve`: **17**
  - `summarize_unselected_runs`: **1**

- **Retrieve effectiveness**
  - Retrieve hit rate: **0/17 = 0.0%**
  - Average `estimated_tokens`: **0.0**
  - Average tokens vs budget: **budget could not be computed**, because the sampled retrieve payloads did not expose a token-budget field
  - `keyword_method` distribution:
    - `none`: **17/17 (100%)**
    - `plain`: **0**
    - `llm`: **0**

- **Example**
  - Run **25640363259**, step `Retrieve reviewer memory context fail-open`, emitted:
    - `records_selected: 0`
    - `estimated_tokens: 0`
    - `keyword_method: "none"`
    - `enabled: true`
    - `ok: true`

- **Flags**
  - Retrieves returning 0 records: **17/17**
  - Retrieve entries with `fail_open: true`: **0**
  - Retrieve entries with `enabled: false`: **0**
  - High push retry counts were not widespread; I only saw one push retry above 1 in the parsed telemetry:
    - run **25638352861** had `push_attempts: 2`

- **What looks healthy**
  - The write path is functioning:
    - run **25640363259** successfully recorded both `phase_started` and `phase_failed`
    - both emitted `did_push: true` and `push_attempts: 1`

- **What is missing**
  - I did **not** see sampled deep-dive telemetry for:
    - `promote`
    - `compact`
    - `finalize-task`
    - `processed-command-claim`
    - `processed-command-complete`

- **Recommendation**
  - Keep the memory write path.
  - Repair the retrieval path before expanding usage:
    - short-circuit retrieval when `keyword_method` would be `none`
    - emit token budget and candidate-pool counts
    - measure whether any stored records are actually eligible for selection
  - Right now, memory retrieval is operationally successful but functionally empty.

## GH API Call Audit

No HTTP 429 or secondary rate-limit warnings were visible in the inspected deep dives. The current issue is **redundancy and polling volume**, not an active rate-limit incident.

The repo’s own API hygiene guidance, surfaced in the prompt text captured during run **25640363259** `Apply fixes with editor model`, says to prefer batched GraphQL and avoid per-iteration `gh api` loops. The patterns below are the clearest places where current behavior diverges from that principle.

### 1) `review_autofix` check-run polling loop is the biggest API hotspot

- **Evidence**
  - Run **25640363259**, step `Collect PR check-run failures CI lint autofix context`, looped for **1213.9s** with **57** wait iterations.
  - Run **25640364808** showed the same pattern at **1220.1s**.
  - Each iteration re-checks the same commit’s check-run state.

- **High-volume / high-redundancy pattern**
  - Repeatedly listing check-runs for the same head SHA until timeout.

- **Concrete batching/reuse change**
  - Fetch check-runs once, identify only the required/pending subset, and stop refetching when only non-blocking or long-queued runs remain.
  - Use a bounded backoff sequence rather than fixed 20-second polling.
  - Cache the first snapshot and update only the delta you actually need.

- **Estimated call-count reduction**
  - **Inference:** roughly **45-55 fewer GitHub API calls per affected run**.

- **Rate-limit risk reduction**
  - This is the most important API-thrift change in the repo.

### 2) `cancel_on_pr_close` makes separate list calls for queued and in-progress runs

- **Evidence**
  - Run **25643124423**, step `cancel / cancel-active-runs`, shows:
    - a `/rate_limit` lookup in `_rl_wait()`
    - two `_gh_retry gh api` list calls
    - per-run cancel POST logic

- **High-volume / high-redundancy pattern**
  - Separate branch-scoped listings for `queued` and `in_progress`, then local filtering.

- **Concrete batching/reuse change**
  - Fetch the branch’s active runs once and filter statuses locally, or use a single `gh run list`/REST response that contains both statuses.
  - Only call `/rate_limit` after a retry-worthy failure, not unconditionally.

- **Estimated call-count reduction**
  - **1-2 fewer API calls per closed-PR event**.

- **Rate-limit risk reduction**
  - Small individually, but these are pure control-plane calls and easy to trim safely.

### 3) `closingIssuesReferences` is queried in more than one workflow

- **Evidence**
  - Run **25643124434**, step `sync-status / sync-issue-status`, uses a GraphQL query for `closingIssuesReferences(first: 50)`.
  - Run **25643124416**, step `review / post-merge-validate-dispatch`, uses a similar GraphQL query for `closingIssuesReferences`, but also requests labels.

- **High-volume / high-redundancy pattern**
  - Similar PR-linked-issue lookups are implemented separately in different workflows.

- **Concrete batching/reuse change**
  - Centralize one reusable GraphQL helper that returns:
    - linked issue numbers
    - labels
    - any other downstream-required metadata
  - Reuse the same response shape across post-merge validation and issue-status sync.

- **Estimated call-count reduction**
  - Usually **1 GraphQL call per merged-PR path**.

- **Rate-limit risk reduction**
  - Low individually, but aligns with the repo’s own “extend an existing call before adding a new one” rule.

### 4) Copilot reviewer path is API-light but under-retried

- **Evidence**
  - Recent runs **25641774946** and **25641877284**:
    - `Prepare` uses `github.rest.pulls.get` and paginated `github.rest.pulls.listFiles`
    - `Cleanup artifacts` uses `gh api /repos/.../actions/runs/.../artifacts`
    - `retries: 0`

- **High-volume / high-redundancy pattern**
  - Not especially high volume, but cleanup artifact enumeration is guaranteed overhead even when artifact state is simple.

- **Concrete batching/reuse change**
  - Keep the paginated file fetch, but enable retries and make artifact cleanup conditional when possible.
  - If downstream steps do not need repeated file metadata, reuse the `Prepare` payload instead of re-fetching.

- **Estimated call-count reduction**
  - Small.
  - Main gain is reliability rather than rate-limit prevention.

## Prompt Cache & Memory System

### Current state

- Prompt cache is **intended to be on**:
  - Run **25640363259** shows `OPENROUTER_PROMPT_CACHE_DISABLED=false` in `Install semble`, `Build semble index`, and `Run reviewer models`.

- There is explicit cache-related instrumentation:
  - Run **25640363259**, `Run reviewer models`, logged two `review_autofix_cache_probe` lines with `cache_enabled=true`.

- But the measurement is incomplete:
  - Those same cache-probe lines reported:
    - `prompt_tokens=na`
    - `completion_tokens=na`
    - `total_tokens=na`
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`

- Memory retrieval is present but ineffective:
  - **17/17** retrieve calls returned **0 records** and **0 estimated tokens**.

- Semble is not active in the sampled production review run:
  - Run **25640363259** logged `SEMBLE_AVAILABLE=false` and `SEMBLE_INDEX_AVAILABLE=false`.

### What this means

- The system already has the right *shape* for a cache/memory stack:
  - there is a `Pre-assemble static context cacheable across runs` step
  - there is a prompt-cache probe
  - there is AI memory retrieval and recording
- But in this sample window:
  - prompt-cache **effectiveness cannot be measured**
  - memory retrieval **is not returning anything**
  - Semble compression/querying **is not active in sampled production runs**

### Likely cache-fragmentation causes

- **Inference:** cache reuse is probably being undercut by prompt variance:
  - large dynamic diff/runtime sections
  - run-specific conflict context
  - reviewer outputs that change every run
- That inference is supported by the very large consolidator inputs in failing runs:
  - **120,206 bytes** in **25640363259**
  - **117,995 bytes** in **25640364808**

### Concrete improvements

1. **Emit real cache read/write counters**
   - Make cache creation/read tokens mandatory in the wrapper logs.
   - Without that, the repo cannot tell whether prompt caching is actually saving anything.

2. **Stabilize prompt prefixes**
   - Keep static policy/repo context first.
   - Push dynamic PR/runtime/noise sections to the suffix.
   - Reuse the exact same preassembled static block across reviewer/editor/consolidator stages.

3. **Skip empty memory retrieval work**
   - If retrieval would use `keyword_method="none"`, skip the retrieve call and log that it was intentionally bypassed.

4. **Only evaluate Semble once it is available**
   - When Semble is disabled or no index exists, avoid treating it as a live compression path.
   - When it is active, log `SEMBLE_QUERY target=... bytes=...` so savings can be measured.

### Estimated impact

- **Tokens:** potentially medium, but currently unquantifiable.
- **Latency:** modest to medium once the cache is actually measurable and stable.
- **Reliability:** better degraded-state visibility with very low risk.

## Orchestrator Health

- I did **not** find evidence of unhealthy clarification loops or stuck orchestrator wave progression in the sampled deep dives.
- `orchestrate_poll` looks healthy in this window:
  - **30/30 success**
  - average **109.3s**
  - p95 **129.4s**

### What is unhealthy operationally

1. **Trigger fan-out is very noisy**
   - Across `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`:
     - total runs: **674**
     - skipped runs: **658**
   - Recent examples:
     - **25643131616**: implement skipped because `/approved` gate was false
     - **25643054844**: plan skipped because `/answer` condition was false
     - **25643004034**: respond skipped because `respond.respond.if` was false
     - **25643131601**: clarify skipped because `clarify.clarify.if` was false

2. **`review_autofix` cancellation churn is the real orchestration pain**
   - **71 cancelled** out of **150** `review_autofix` runs.
   - This is not just noise; it is wasted queue/setup/compute.

3. **Runner wait shows up even on tiny jobs**
   - Recent summaries for **25643124423** (`cancel_on_pr_close`), **25643124434** (`issue_pr_status`), **25642515817** (`review_autofix`), **25641900135** (`review_autofix`), and **25641802115** (`ci`) all mention hosted-runner wait.
   - On 7-19 second workflows, queue/setup can dominate useful work.

4. **Forward-merge fallback is healthy, but it marks a manual handoff point**
   - Run **25643124397** opened fallback PR **#2472** with `AHEAD="13"` and `STATUS="conflict"`.
   - That is a good fail-safe, but it is still a human-intervention bottleneck.

### Smallest safe mitigations

- Move command/body gating higher so no-op jobs are never scheduled.
- Cancel superseded `review_autofix` runs before model work begins.
- Keep the forward-merge fallback PR path; it is safer than forcing auto-merge through conflicts.
- Track degraded review states explicitly when the consolidator or resolver fail open.

### Observable indicators to track

- `review_autofix` cancel rate
- `review_autofix` resolver syntax-failure count
- count of `CHECK_RUNS_WAIT_TIMEOUT` events
- percentage of no-op skipped runs across clarify/plan/implement/respond
- AI memory retrieve hit rate
- count of forward-merge fallback PRs
- runner-wait incidence on workflows whose total runtime is under 20 seconds

## Pipeline Flow Bottlenecks

### 1) Clarify → Plan → Implement → Respond
- In this window, these stages are mostly **gating noise**, not compute bottlenecks.
- They contribute to dispatch/setup overhead, but not to end-to-end runtime for real work.
- Evidence: `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` are overwhelmingly skipped.

### 2) Review / Autofix
This is the dominant bottleneck by far.

- **Queue/setup overhead**
  - Recent review summaries **25641900135** and **25642515817** show runner wait before review work begins.

- **Retry/wait overhead**
  - The check-run context step alone can spend **1200s+** waiting:
    - **25640363259**: **1213.9s**
    - **25640364808**: **1220.1s**

- **Compute overhead**
  - Reviewer/editor stages dominate long runs:
    - **25640363259**: **1783.0s reviewer + 2154.6s editor**
    - **25640364808**: **1538.9s reviewer + 1687.6s editor**
    - **25641872477**: reviewer/editor again dominate before the resolver failure

- **Merge/conflict overhead**
  - After the expensive model work, failing runs are dying at the resolver step because the resolver/support scripts are themselves in conflict.

### 3) Validate / Orchestrate Poll
- This path is comparatively healthy.
- `orchestrate_poll` is consistent and successful in the sampled window.
- It is not the primary latency or failure source right now.

### 4) Release / Log-Analysis Loops
- These are very slow, but secondary to the main PR path.
- Evidence:
  - `test_and_mark_stable` **25631690654**: **8953s**
  - `workflow_log_analysis` **25631704000**: **8865s**
- These are large batch workflows rather than the main interactive bottleneck.

### Ordered fixes by end-to-end impact

1. **Fix or bypass the self-conflicting resolver path in `review_autofix`.**
2. **Shrink the check-run wait gate from 1200s and stop polling non-blocking checks.**
3. **Expand the small-diff fast lane and reduce reviewer/editor depth on low-risk PRs.**
4. **Cancel superseded `review_autofix` runs earlier.**
5. **Move no-op orchestration gates higher so skipped jobs are never scheduled.**
6. **Trim/scope the heavy release-analysis workflows on non-release paths.**

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` is the dominant path:
    - **150 runs**
    - **12.7% failure rate**
    - **47.3% cancelled**
    - **p95 4467s**
  - `ci` is the next-highest reliability problem:
    - **65 runs**
    - **23.1% failure rate**
    - failures all concentrated in `Review Semble contract test`
  - Off the main PR path, `test_and_mark_stable` and `workflow_log_analysis` are the longest batch workflows.

- **Top failure modes**
  1. `review_autofix` resolver-step syntax failures after merge conflicts involving workflow support files.
  2. `ci` contract-test drift in `tests/test_review_semble_contract.py`.

- **Highest-cost drivers**
  1. Six-model reviewer panel + expensive editor/consolidator path in `review_autofix`
  2. 20-minute check-run polling before review work
  3. Cancelled `review_autofix` runs that still consume setup/runtime
  4. Large prompt inputs with unmeasured cache effectiveness
  5. No demonstrated production Semble compression in sampled runs

- **Top 3 prioritized actions**
  1. **Guard or bypass automated conflict resolution when `scripts/review_conflict_resolve.sh` or resolver support assets are themselves conflicted or syntactically invalid.**
  2. **Reduce the check-run wait gate from 1200s and only block on truly required checks.**
  3. **Expand the small-diff fast lane and reduce reviewer fan-out for low-risk PRs, while cancelling superseded runs earlier.**

## Metrics Appendix

### Repo summary

| Repo | Total runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 231 | 34 | 73 | 662 | 3.4% | 230.7 | 1.0 | 1475.1 |

### Key workflow-family metrics

| Workflow family | Runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 150 | 57 | 19 | 71 | 3 | 12.7% | 1043.5 | 311.0 | 4467.0 |
| ci | 65 | 50 | 15 | 0 | 0 | 23.1% | 638.3 | 643.0 | 687.4 |
| clarify | 174 | 5 | 0 | 0 | 169 | 0.0% | 4.6 | 1.0 | 3.75 |
| plan | 167 | 4 | 0 | 0 | 163 | 0.0% | 3.6 | 1.0 | 2.0 |
| implement | 167 | 4 | 0 | 2 | 161 | 0.0% | 7.3 | 1.0 | 2.7 |
| orchestrate_clarify_respond | 166 | 1 | 0 | 0 | 165 | 0.0% | 1.1 | 1.0 | 2.0 |
| orchestrate_poll | 30 | 30 | 0 | 0 | 0 | 0.0% | 109.3 | 109.5 | 129.4 |
| test_and_mark_stable | 1 | 1 | 0 | 0 | 0 | 0.0% | 8953.0 | 8953.0 | 8953.0 |
| workflow_log_analysis | 1 | 1 | 0 | 0 | 0 | 0.0% | 8865.0 | 8865.0 | 8865.0 |

### `review_autofix` outcome mix

| Conclusion | Count | Avg duration (s) | p50 (s) | p95 (s) | Total runtime (s) |
|---|---:|---:|---:|---:|---:|
| success | 57 | 1140.2 | 1453 | 2055 | 64991 |
| failure | 19 | 3988.5 | 3905 | 5049 | 75781 |
| cancelled | 71 | 221.5 | 7 | 912 | 15726 |

### AI memory telemetry

| Metric | Value |
|---|---:|
| Raw `AI_MEMORY_TELEMETRY` lines parsed | 116 |
| Deduped unique events | 69 |
| `record-run-event` ops | 33 |
| `record-candidate` ops | 18 |
| `retrieve` ops | 17 |
| `summarize_unselected_runs` ops | 1 |
| Retrieve hit rate | 0 / 17 (0.0%) |
| Avg `estimated_tokens` on retrieve | 0.0 |
| Retrieve `keyword_method=none` | 17 / 17 (100%) |
| Retrieve `keyword_method=plain` | 0 |
| Retrieve `keyword_method=llm` | 0 |
| Retrieve `fail_open=true` | 0 |
| Retrieve `enabled=false` | 0 |
| Push retries > 1 observed | 1 event (`run_id=25638352861`, `push_attempts=2`) |
| `promote` / `compact` / `finalize-task` / `processed-command-*` observed | No |

### Token, prompt-cache, and memory-system metrics

| Metric | Evidence |
|---|---|
| Production prompt/completion/total token totals | **Unavailable** in the sampled window |
| Prompt cache disabled? | `OPENROUTER_PROMPT_CACHE_DISABLED=false` in run **25640363259** |
| Cache-probe lines observed | 2 (`review_autofix_cache_probe`, run **25640363259**) |
| Cache creation/read token counters | `na` in sampled cache-probe lines |
| Semble available in sampled production review run | `SEMBLE_AVAILABLE=false`, `SEMBLE_INDEX_AVAILABLE=false` in **25640363259** |
| Large prompt-size evidence | Consolidator `input_bytes=120206` in **25640363259**; `117995` in **25640364808** |
| Memory retrieve usefulness | 17 retrieves, 0 selected records |

### GH API hotspot summary

| Workflow / run | Job / step | Pattern | Evidence | Estimated reducible calls |
|---|---|---|---|---|
| `review_autofix` / **25640363259** | `review / codex-agent` → `Collect PR check-run failures CI lint autofix context` | Repeated check-run polling | 57 wait iterations over 1213.9s, timeout at 1200s | ~45-55 per affected run *(inference)* |
| `review_autofix` / **25640364808** | same | Same repeated polling | 1220.1s with same timeout pattern | ~45-55 per affected run *(inference)* |
| `cancel_on_pr_close` / **25643124423** | `cancel / cancel-active-runs` | Separate status listings + `/rate_limit` | log shows `/rate_limit` and two `_gh_retry gh api` list calls | 1-2 per run |
| `issue_pr_status` / **25643124434** | `sync-status / sync-issue-status` | GraphQL `closingIssuesReferences` lookup | dedicated GraphQL query | 1 per merged-PR path |
| `review_autofix` / **25643124416** | `review / post-merge-validate-dispatch` | Similar `closingIssuesReferences` lookup with labels | dedicated GraphQL query | 1 per merged-PR path |
| `copilot_pull_request_reviewer` / **25641774946**, **25641877284** | `Prepare`, `Cleanup artifacts` | `pulls.listFiles` pagination + artifact enumeration, retries 0 | evidence from run summaries | Small; bigger gain is resilience |

### Semble telemetry summary

| Scope | Workflow / run(s) | `SEMBLE_QUERY` observed | `SEMBLE_FALLBACK` observed | Target(s) | Logged bytes | Notes |
|---|---|---:|---:|---|---|---|
| Sampled production deep dives | `review_autofix` (e.g. **25640363259**) | 0 | 0 directly observed | n/a | n/a | `SEMBLE_AVAILABLE=false`, `SEMBLE_INDEX_AVAILABLE=false` |
| CI contract tests | `ci` / **25639158570** | 0 structured query lines used for savings analysis | 5 | `overflow` | not present | 4x `src/big.py`, 1x `src/small.py`, missing binary path, `ms=0` |
| CI contract tests | `ci` / **25634879805** | 0 structured query lines used for savings analysis | 5 | `overflow` | not present | 4x `src/big.py`, 1x `src/small.py`, missing binary path, `ms=0` |

### Slow batch-workflow observations

| Workflow / run | Total duration (s) | Dominant observed steps |
|---|---:|---|
| `test_and_mark_stable` / **25631690654** | 8953 | `workflow-log-analysis-test` 8904.6s; `e2e-smoke-test` 4722.9s; `e2e-alt-model-test` 2372.9s |
| `workflow_log_analysis` / **25631704000** | 8865 | `api-redundancy` 3718.8s; `deep-audit` 3384.6s; `analyze-commit-notify` 1659.8s |

If you want, I can turn this report into a prioritized implementation checklist with owner/severity/ETA columns.
