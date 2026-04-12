## Executive Summary

- **`review_autofix` is the dominant bottleneck and risk surface**: it represents only **204/1834 runs (11.1%)** but has **26/27 failures (96.3%)**, **all 88 cancellations**, **545s avg duration**, and **1753.9s p95**.  
  **Estimated impact:** focusing optimizations here can cut end-to-end CI time and token burn for AI workflows by **30–50%**.

- **Merge-conflict handling failures are the primary reliability issue**: in the provided failing-run sample, most failures occur at `Detect merge conflicts` / `Resolve merge conflicts with Codex` / `Commit changes` (e.g., run IDs `24236729072`, `24234088484`, `24236785237`).  
  **Estimated impact:** preflight conflict checks + early exit can reduce review failures by **40–60%** and save **10–25 min** on worst-case failed runs.

- **High-cost long cancellations are avoidable**: multiple `review_autofix` runs are cancelled after ~30–37 minutes (e.g., `24283785673` 2220s, `24287026660` 2194s).  
  **Estimated impact:** stricter concurrency + stale-run cancellation can reclaim **20–35%** of `review_autofix` runtime/token spend.

- **Repo concentration is high**: `shubhodeep1/tele-funtoken-msg-scoring` alone has **24 failures**, **6.47% failure rate**, and **1120.5s p95**.  
  **Estimated impact:** repo-targeted fixes here should deliver the largest immediate reliability and cost improvements.

---

## Speed Optimizations

### 1) Add PR/branch concurrency guards for `review_autofix` (highest time savings)

- **Evidence**  
  - `review_autofix`: **88 cancelled runs**, all workflow-family cancellations.  
  - Slow cancellations >1600s across repos (e.g., `24283785673` 2220s, `24287672028` 2093s, `24285422496` 2022s).
- **Root cause**  
  Multiple overlapping runs continue long after becoming stale.
- **Exact change**
```yaml
# In AI Review workflow
concurrency:
  group: ai-review-${{ github.repository }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true
```
- **Estimated impact**  
  **20–35% reduction** in `review_autofix` elapsed compute time; major p95 improvement.

---

### 2) Move merge-conflict detection to an early deterministic preflight

- **Evidence**  
  Repeated failures at:
  - `Detect merge conflicts` (e.g., `24236729072`, `24235724364`, `24235266854`, `24236738902`)
  - `Resolve merge conflicts with Codex` (`24234088484`, `24233938850`, etc.)
  - `Commit changes` (`24236785237`, `24236761890`)  
  Several failures occur after long durations (up to **2249s**).
- **Root cause**  
  Expensive AI steps run before cheap Git feasibility checks.
- **Exact change**
```yaml
- name: Preflight mergeability
  run: |
    git fetch origin ${{ github.base_ref }} --depth=1
    # Fast conflict check before invoking agent
    if ! git merge-tree $(git merge-base HEAD origin/${{ github.base_ref }}) HEAD origin/${{ github.base_ref }} | grep -q '<<<<<<<'; then
      echo "merge_conflict=false" >> $GITHUB_OUTPUT
    else
      echo "merge_conflict=true" >> $GITHUB_OUTPUT
    fi
  id: preflight

- name: Skip AI conflict resolution if hard conflict
  if: steps.preflight.outputs.merge_conflict == 'true'
  run: |
    echo "Hard merge conflict detected; exiting early for manual resolution."
    exit 0
```
- **Estimated impact**  
  Save **8–25 min** on doomed runs; **30–45% p95 reduction** for failure path.

---

### 3) Add step-level timeouts + circuit breaker around conflict-resolution loop

- **Evidence**  
  Long-running failures in conflict steps (up to 2249s), plus repeated failures in short window.
- **Root cause**  
  Conflict resolution can spin too long before failing.
- **Exact change**
```yaml
jobs:
  review:
    timeout-minutes: 35
    steps:
      - name: Resolve merge conflicts with Codex
        timeout-minutes: 10
      - name: Detect merge conflicts
        timeout-minutes: 5
```
- **Estimated impact**  
  **10–20% faster failure turnaround**, less queue blocking.

---

### 4) Fast-fail non-critical post-processing (`Telegram success`, artifact cleanup)

- **Evidence**  
  Failures at `Telegram success` (`24300264370`) and `Cleanup artifacts / Get artifact IDs` (`24234611370`).
- **Root cause**  
  Non-core steps can fail the full workflow.
- **Exact change**
```yaml
- name: Telegram success
  continue-on-error: true

- name: Cleanup artifacts
  continue-on-error: true
```
- **Estimated impact**  
  Prevents avoidable reruns; small but immediate speed gain on tail failures.

---

## Cost Optimizations

> **Data gap:** no per-run token usage was provided. Estimates below use runtime/failure/cancellation as token-burn proxy.

### 1) Stop spending tokens on stale `review_autofix` runs (largest savings)

- **Evidence**  
  88 cancelled `review_autofix` runs; many cancel after ~30+ minutes.
- **Root cause**  
  AI work continues on superseded commits/PR states.
- **Exact change**  
  Concurrency cancellation (above) + start-of-job freshness check:
```yaml
- name: Abort if PR head changed
  run: |
    CURRENT_SHA=$(gh pr view ${{ github.event.pull_request.number }} --json headRefOid -q .headRefOid)
    if [ "$CURRENT_SHA" != "${{ github.event.pull_request.head.sha }}" ]; then
      echo "Stale run; exiting."
      exit 0
    fi
```
- **Estimated impact**  
  **25–40% token spend reduction** for `review_autofix`.

---

### 2) Route model/thinking level by step type (deterministic vs generative)

- **Evidence**  
  Failures cluster in Git-operational steps (detect/resolve/commit), not pure reasoning tasks.
- **Root cause**  
  Expensive reasoning likely used where shell/git checks suffice.
- **Current vs proposed behavior**

| Area | Current (inferred) | Proposed |
|---|---|---|
| Conflict detection | AI-involved path | Deterministic git preflight (no model) |
| Conflict resolution attempt | Always full reasoning | Only when preflight says resolvable; cap attempts |
| Notifications/summaries | Same model tier | Lower-cost model / low thinking |
| Autofix patch generation | Uniform model tier | Keep higher-capability model only here |

- **Exact change**  
  Add per-step model config inputs (`model_tier`, `thinking_level`) and default to low for classify/detect/notify.
- **Estimated impact**  
  **15–30% token reduction** with low quality risk.

---

### 3) Avoid repeated context on recurring conflict failures

- **Evidence**  
  Same repo (`tele-funtoken-msg-scoring`) repeatedly fails same steps within hours (multiple run IDs between 08:11–09:47 UTC).
- **Root cause**  
  Re-running equivalent prompts/context on unresolved underlying merge state.
- **Exact change**
```yaml
- name: Conflict fingerprint cache key
  id: fp
  run: |
    echo "key=$(git rev-parse HEAD)-$(git rev-parse origin/${{ github.base_ref }})" >> $GITHUB_OUTPUT

# Skip AI resolution if same fingerprint failed recently (store in workflow artifact/cache)
```
- **Estimated impact**  
  **10–20% token savings** in high-churn repos.

---

### 4) Ensure skipped workflows do not initialize AI context

- **Evidence**  
  Large `other/skipped` volume with 1s durations (e.g., many recent runs in `digital_pa`, `coding-workflows`).
- **Root cause**  
  Trigger noise can still incur minimal orchestration overhead; guardrails may reduce unnecessary setup tokens.
- **Exact change**
```yaml
if: github.event_name == 'pull_request' && !github.event.pull_request.draft
```
and strict `paths`/`paths-ignore` filters on AI workflows.
- **Estimated impact**  
  Small per-run savings, meaningful at scale.

---

## Reliability Improvements

### 1) Harden merge conflict lifecycle (largest failure-rate reduction)

- **Evidence (failure pattern)**  
  Majority of sampled failures are merge/commit lifecycle (`Detect merge conflicts`, `Resolve merge conflicts with Codex`, `Commit changes`).
- **Root cause**  
  Conflict handling occurs too late and may be attempted when unresolvable.
- **Exact change**  
  - Deterministic preflight mergeability check  
  - Branch early-exit on hard conflicts  
  - Limit AI conflict-repair attempts to 1–2
- **Expected reliability impact**  
  `review_autofix` failure rate from **12.7%** toward **5–8%**.

---

### 2) Add robust git write prechecks for commit/push steps

- **Evidence**
