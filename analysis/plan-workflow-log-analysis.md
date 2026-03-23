# Plan Workflow Log Analysis — Issue #106 (atlas-bridge.gd)

**Run Date:** 2026-03-23
**Total Duration:** ~7 min 21s (05:29:42 → 05:37:03)
**Codex Planning Phase:** ~6 min 47s (05:30:13 → 05:37:00)
**Token Usage:** 107,728 tokens
**Model:** openai/gpt-5.3-codex via OpenRouter
**Outcome:** Plan generated successfully on attempt 1, auto-approved

---

## Summary

The workflow completed successfully but consumed significantly more tokens and time than necessary. The Codex agent exhibited several inefficiency patterns that inflate cost and latency. The workflow infrastructure itself has ordering issues and unnecessary dependencies.

---

## Issues & Recommendations

### 1. CRITICAL — `setup-uv` runs before `actions/checkout`, causing warnings and broken caching

**Evidence (log lines):**
```
##[warning]Empty workdir detected. This may cause unexpected behavior.
Could not find file: .../uv.toml
Could not find file: .../pyproject.toml
##[warning]No file matched to [...]. The cache will never get invalidated.
```

**Problem:** `astral-sh/setup-uv@v7` (step 69-70) runs before `actions/checkout@v5` (step 72-76). The working directory is empty, so uv cannot find version pins or cache dependency globs. The uv cache key becomes `no-dependency-glob` and never invalidates properly.

**Recommendation:** Move `actions/checkout` before `astral-sh/setup-uv`, or at minimum set `ignore-empty-workdir: true` and accept the cache will be static. Better yet, since the project uses `pip install` (not uv) for its own dependencies, consider whether the uv action is needed at all — it's only used for `uvx` to run Serena.

**Fix in `plan.yml`:**
```yaml
steps:
  - name: Cache Codex CLI
    ...
  - name: Install Codex CLI
    ...
  - name: Restore Codex CLI from cache
    ...
  - name: Checkout repository        # ← MOVE UP
    uses: actions/checkout@v5
    ...
  - name: Install uv for Serena      # ← AFTER checkout
    uses: astral-sh/setup-uv@v7
```

---

### 2. HIGH — Serena onboarding triggered during planning, wasting tokens

**Evidence (log):**
```
serena.check_onboarding_performed({}) → "Onboarding not performed yet"
serena.onboarding({}) → [full onboarding instructions dumped into context]
```

The Codex agent called `serena.onboarding()` which injected a large onboarding prompt asking it to discover project purpose, tech stack, commands, etc. — work entirely irrelevant to generating a plan. This consumed tokens and distracted the agent.

**Recommendation:**
- Pre-seed Serena memory files during `setup_serena.sh` so `check_onboarding_performed` returns true. A minimal `.serena/memories/` directory with project basics would skip the onboarding flow.
- Alternatively, add `onboarding_performed: true` or equivalent to the Serena project config.
- Or instruct Codex in the planning prompt to skip Serena onboarding: `"Do NOT call serena.onboarding() — skip onboarding and proceed directly to the task."`

---

### 3. HIGH — Codex agent dumped entire files unnecessarily (token waste)

**Evidence:**
```
/bin/bash -lc 'for f in v2/index.html v2/services.html ...; do nl -ba "$f"; echo; done'
```
This single shell exec printed **all 7 V2 HTML files in full** with line numbers — hundreds of lines — when only footers, forms, and icon links were relevant.

Similarly:
```
/bin/bash -lc "sed -n '1,760p' assets/v2.css"
```
Read 760 lines of CSS when only ~10 selectors were relevant.

**Impact:** These bulk reads likely account for 30-40% of the 107k tokens.

**Recommendation:**
- Strengthen the efficiency rules in the planning prompt. The current rules say "request large ranges (200+ lines)" which is counterproductive — it should say "request only the ranges you need."
- Change the prompt's efficiency rules from:
  ```
  When reading files, request large ranges (200+ lines) in a single call
  ```
  to:
  ```
  Only read the specific line ranges you need. Prefer Serena search_for_pattern
  over reading entire files. Never dump full HTML/CSS files.
  ```
- Add a hard limit: `"Total file content read via shell exec must not exceed 500 lines."`

---

### 4. HIGH — Redundant "status update" messages waste output tokens

**Evidence:** The agent produced 8+ nearly identical status messages:
```
"I've parsed the clarification answers (Q1:B, Q2:B, Q3:A, Q4:A, Q5:A, Q6:A, Q7:B)..."
"I've parsed the clarification answers and confirmed they map cleanly to Q1–Q7..."
"I've parsed your clarification answers and started mapping the concrete touchpoints..."
"I've parsed the issue and clarifications..."
```

Each `codex` block is an output turn that consumes tokens. These repetitive status messages provide no value.

**Recommendation:** Add to the planning prompt:
```
Do NOT emit progress status messages. Output only the final plan.
Minimize intermediate reasoning text — every token costs money.
```

---

### 5. MEDIUM — Redundant duplicate Serena searches

**Evidence:**
```
serena.search_for_pattern(v2, "footer|WhatsApp|...") → "too long (29419 chars)"
serena.search_for_pattern(index.html, "footer|WhatsApp|...")
serena.search_for_pattern(contact.html, "footer|WhatsApp|...")
serena.search_for_pattern(app.py, "contact_v2|...")
```
Then ALSO:
```
rg -n "footer|WhatsApp|..." v2/*.html index.html contact.html quote.html app.py
```

The agent searched for the same patterns twice — once via Serena, once via `rg`. The Serena search for `v2/` exceeded the character limit, so the agent fell back to `rg`, but then also ran separate Serena searches per-file that overlapped with the `rg` results.

**Recommendation:**
- Increase `max_answer_chars` in the prompt guidance (suggest 40000+) to avoid Serena truncation forcing fallbacks.
- Add to prompt: `"If a Serena search exceeds max_answer_chars, narrow the pattern — do NOT re-run the same search via shell."`

---

### 6. MEDIUM — `actions/cache@v4` Node.js 20 deprecation warning

**Evidence:**
```
##[warning]Node.js 20 actions are deprecated. The following actions are running on Node.js 20: actions/cache@v4
```

**Recommendation:** Either upgrade to `actions/cache@v5` (if available) or set `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` in the workflow env to suppress warnings and prepare for the June 2026 forced migration.

---

### 7. MEDIUM — Python LSP installed fresh every run (~2s)

**Evidence:**
```
pip install python-lsp-server → Installing collected packages: black-26.3.1, ...
```

The `python-lsp-server` and all its dependencies (black, jedi, parso, etc.) are installed via pip on every workflow run. This isn't cached.

**Recommendation:** Either:
- Add the pip install path to the Codex cache or a separate `actions/cache` step keyed on the LSP version.
- Or use `uv pip install` which benefits from the uv cache that's already being set up.

---

### 8. MEDIUM — Sequential GitHub API calls that could be parallelized

**Evidence:** These steps run sequentially but are independent:
1. Fetch issue metadata (`gh api .../issues/106`)
2. Fetch issue comments (`gh api .../issues/106/comments`)
3. Validate planning phase label (`gh api .../issues/106` again — duplicate fetch!)

**Recommendation:**
- Combine "Fetch issue metadata" and "Fetch issue comments" into a single step using `&` backgrounding.
- The label validation step re-fetches the same issue metadata that was already fetched in step 1. Reuse `ISSUE_META_FILE` instead of making a second API call:
  ```bash
  # Instead of:
  ISSUE_LABELS_JSON="$(gh api "repos/.../issues/${ISSUE_NUMBER}" --jq '[.labels[].name]')"
  # Use:
  ISSUE_LABELS_JSON="$(jq '[.labels[].name]' "${ISSUE_META_FILE}")"
  ```

---

### 9. MEDIUM — Codex retry backoff is aggressive (30s, 60s)

**Evidence:**
```yaml
sleep_secs=$((attempt * 30))  # 30s, 60s
```

If the first attempt fails (e.g., transient 429 from OpenRouter), the workflow sleeps 30s before retry. With a 55-minute timeout, this is fine, but the linear `attempt * 30` formula means attempt 3 waits 90s total in sleep alone.

**Recommendation:** Use exponential backoff with jitter: `sleep_secs=$((2 ** attempt * 5 + RANDOM % 5))` → roughly 10s, 20s, 40s. This retries faster on transient errors while still backing off.

---

### 10. LOW — `ISSUE_BODY` passed via env var has shell injection risk

**Evidence:** The issue body (user-controlled content) is set as `ISSUE_BODY` via `$GITHUB_ENV` heredoc. While the heredoc delimiter prevents most injection, the body is then interpolated into shell commands in later steps via `${ISSUE_BODY}`.

**Recommendation:** Continue using heredoc delimiters (which is already done correctly), but audit all downstream uses of `${ISSUE_BODY}` to ensure they're always quoted. The current code appears safe, but add a comment noting this is a trust boundary.

---

### 11. LOW — Workflow scripts fetched via API on every caller repo run

**Evidence:**
```bash
gh api -H 'Accept: application/vnd.github.raw+json' \
  "repos/${wf_source}/contents/scripts/${f}?ref=stable" > "scripts/${f}"
```

Four API calls to fetch scripts/instructions from `coding-workflows` repo on every run from caller repos.

**Recommendation:** Consider publishing these as a composite action or using `actions/checkout` with a sparse checkout of just the scripts directory from `coding-workflows`. This would also benefit from `actions/cache`.

---

### 12. LOW — `AUTO_IMPLEMENT_ON_CLEAR_PLAN=true` skips human review

**Evidence:**
```
/approved [auto-approved-by-plan]
Auto approval was posted because AUTO_IMPLEMENT_ON_CLEAR_PLAN is enabled.
```

The plan was auto-approved without human review. For a production website with email integration, API key configuration, and content changes across 20+ files, this is risky.

**Recommendation:** Consider defaulting `AUTO_IMPLEMENT_ON_CLEAR_PLAN` to `false` for caller repos, or adding a risk-score heuristic: auto-approve only for plans that touch fewer than N files or don't involve API integrations/secrets.

---

## Performance Breakdown

| Phase | Duration | Notes |
|-------|----------|-------|
| Runner provisioning | ~0s | Cache hit |
| Codex CLI restore | 2s | Cache hit, ~47MB |
| uv setup | 1s | Download + install |
| Checkout | 3s | Shallow clone |
| Script fetch | 1s | 4 API calls |
| Issue metadata + comments | 1s | 2 API calls |
| Validation gates | 3s | Label check, PR check, stale check |
| Context assembly | <1s | |
| Serena setup | 13s | Cache warm + LSP install + health check |
| **Codex planning** | **~407s** | **96% of total runtime** |
| Post-processing | 2s | Parse, post comment, auto-approve |

**Key takeaway:** 96% of runtime is Codex execution. Optimizations to token consumption (recommendations 2-5) will have the highest ROI on both cost and latency.

---

## Estimated Token Savings

| Optimization | Est. Token Reduction |
|-------------|---------------------|
| Skip Serena onboarding | ~2,000 tokens |
| Avoid full HTML/CSS dumps | ~25,000-35,000 tokens |
| Eliminate redundant status messages | ~3,000-5,000 tokens |
| Avoid duplicate searches | ~5,000-8,000 tokens |
| **Total estimated savings** | **~35,000-50,000 tokens (33-46%)** |

---

## Recommended Priority Order

1. **Fix checkout ordering** (item 1) — eliminates warnings, fixes uv caching
2. **Prevent full-file dumps in prompt** (item 3) — biggest token savings
3. **Skip Serena onboarding** (item 2) — avoids context pollution
4. **Suppress redundant status output** (item 4) — easy prompt change
5. **Deduplicate API calls** (item 8) — saves ~1s and one API call
6. **Cache Python LSP** (item 7) — saves ~2s per run
7. **Upgrade actions/cache** (item 6) — preparation for June 2026 deadline
