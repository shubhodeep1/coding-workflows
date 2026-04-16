# AUDIT_REPORT.md

Date: 2026-04-16

Audit scope:
- Workflows: `.github/workflows/*.yml` (28 files)
- Scripts: `scripts/*.sh`, `scripts/*.py` (34 top-level files)
- Mandatory context loaded: `README.md`, `agents.md`, `CLAUDE.md`, `.github/ai/*.json`

## 1) Bug & Correctness Sweep

### Finding BUG-001
- **ID**: `BUG-001`
- **File path**: `scripts/orchestrate_poll_process.sh`
- **Line range**: `8027-8030`
- **Severity**: Low
- **Category tag**: `bug`
- **Description**: `_sorted_issue_nums="$(printf '%s\n' ${ISSUE_NUMS} | sort -un)"` expands `${ISSUE_NUMS}` unquoted, so word-splitting/globbing can corrupt iteration when values are malformed.
- **Recommended fix**: Quote the expansion (`"${ISSUE_NUMS}"`) and/or normalize to an array before sorting.

### Finding BUG-002
- **ID**: `BUG-002`
- **File path**: `.github/workflows/test-and-mark-stable.yml`
- **Line range**: `255-256`, `280-281`
- **Severity**: Low
- **Category tag**: `bug`
- **Description**: The clarify poll loop fetches comments twice in one tick (first for count, then for last bot comment), creating a TOCTOU window where the two checks can evaluate different snapshots.
- **Recommended fix**: Fetch comments once (`?per_page=5&direction=desc`), derive both count and latest bot comment from the same JSON payload.

### Finding SEC-001 [NEEDS VERIFICATION]
- **ID**: `SEC-001`
- **File path**: `scripts/orchestrate_poll_process.sh`
- **Line range**: `592-599`
- **Severity**: Low
- **Category tag**: `security`
- **Description**: `_validate_phase_threshold` clears variables with `eval "${var_name}="`. Current call sites pass constants, but this is still eval-based and unsafe if future call sites pass dynamic names.
- **Recommended fix**: Replace `eval` with explicit whitelist/case handling and `printf -v` assignments.

## 2) GitHub API Call Redundancy Audit

### Finding API-001
- **ID**: `API-001`
- **File path**: `.github/workflows/review_autofix.yml`
- **Line range**: `96-97`
- **Severity**: Medium
- **Category tag**: `api-redundancy`
- **Description**: The same PR endpoint is fetched twice in sequence (`.state` and `.merged`) in one step.
- **Current API call count**: 2 calls/run for `repos/{repo}/pulls/{PR_NUMBER}` in this path.
- **Proposed call count after fix**: 1 call/run.
- **Batching/caching pattern to extend**: Reuse single-response parsing pattern from `_fetch_pr_json` + `_jq_field` in `scripts/orchestrate_poll_process.sh`.
- **Recommended fix**: Fetch PR JSON once and parse all needed fields locally.

### Finding API-002
- **ID**: `API-002`
- **File path**: `scripts/orchestrate_poll_process.sh`
- **Line range**: `1063-1077`, `6119`, `6213`, `6306`, `8030`
- **Severity**: High
- **Category tag**: `api-batching`
- **Description**: `_issue_cross_ref_pr_number_last()` refetches timeline data repeatedly in multiple loops in the same poll cycle.
- **Current API call count**: At least `N + R + D + N` timeline lookups per cycle in common paths (`N` wave issues, `R` ready-to-merge, `D` in-progress/done), plus additional judge/stall paths.
- **Proposed call count after fix**: 1 batched GraphQL prefetch per cycle (or per phase) + in-memory lookups.
- **Batching/caching pattern to extend**: Extend `_fetch_linked_pr_status_graphql` / `_fetch_candidate_issue_details_graphql` cache-first model already present in this script.
- **Recommended fix**: Build `issue_number -> latest_linked_pr` map once and pass it into downstream loops.

### Finding API-003
- **ID**: `API-003`
- **File path**: `scripts/orchestrate_poll_process.sh`
- **Line range**: `4389-4393`
- **Severity**: Medium
- **Category tag**: `api-batching`
- **Description**: Standalone stall-recovery candidate bootstrap loops six labels and runs `gh issue list` once per label.
- **Current API call count**: 6 calls/cycle for label-based bootstrap.
- **Proposed call count after fix**: 1 batched GraphQL/search call.
- **Batching/caching pattern to extend**: Aliased GraphQL helper style used by `_fetch_candidate_issue_details_graphql`.
- **Recommended fix**: Replace per-label listing loop with one aliased GraphQL query and local union/dedupe.

### Finding API-004
- **ID**: `API-004`
- **File path**: `.github/workflows/test-and-mark-stable.yml`
- **Line range**: `247-248`, `255-256`, `280-281`
- **Severity**: Low
- **Category tag**: `api-redundancy`
- **Description**: Clarify polling uses three API reads per tick (issue labels + comments length + comments last bot body), with two calls hitting the comments endpoint.
- **Current API call count**: 3 calls/tick.
- **Proposed call count after fix**: 2 calls/tick.
- **Batching/caching pattern to extend**: Reuse single comments payload in tick-local variables (same pattern already used in other polling phases).
- **Recommended fix**: Fetch comments once per tick and derive both metrics from that payload.

### Finding API-005
- **ID**: `API-005`
- **File path**: `.github/workflows/review_autofix.yml`
- **Line range**: `136-141`, `158-167`
- **Severity**: Medium
- **Category tag**: `api-batching`
- **Description**: Post-merge fallback path calls `gh issue view` per linked issue to fetch labels when labels were not included in the first query.
- **Current API call count**: `M` per run in fallback mode (`M` = fallback-linked issue count).
- **Proposed call count after fix**: 1 batched GraphQL call for all fallback issue numbers.
- **Batching/caching pattern to extend**: Extend the existing linked-issue GraphQL fetch in this same step with aliased issue label lookups.
- **Recommended fix**: Build a single aliased GraphQL query keyed by issue number and parse label membership from one response.

## 3) Code Duplication & Modularization Opportunities

### Finding DUP-001
- **ID**: `DUP-001`
- **File path**: `scripts/label_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/validate_process.sh`, `scripts/review_rb_judge.sh`, `.github/workflows/review_autofix.yml`
- **Line range**: `label_helpers.sh:80-95`, `orchestrate_poll_process.sh:868-929`, `validate_process.sh:440-464`, `review_rb_judge.sh:57-71`, `review_autofix.yml:3020-3034,3133-3144`
- **Severity**: Medium
- **Category tag**: `duplication`
- **Description**: `ensure_label_exists` is reimplemented in at least five places with drift in retry behavior, color/description mapping, and existence checks.
- **Recommended fix**: Centralize in `scripts/label_helpers.sh` and source it everywhere.
  - Proposed shared signature: `ensure_label_exists <label_name> [repo]`
  - Optional strict variant: `ensure_label_exists_strict <label_name> [repo]`
  - Callers to migrate: `orchestrate_poll_process.sh`, `validate_process.sh`, `review_rb_judge.sh`, review_autofix fallback blocks.

### Finding DUP-002
- **ID**: `DUP-002`
- **File path**: `.github/workflows/test-and-mark-stable.yml`
- **Line range**: `207-241`, `316-350`, `475-515`, `630-649`, `1059-1070`
- **Severity**: Low
- **Category tag**: `duplication`
- **Description**: `gh_api_safe` and run-ID polling helpers are repeated across multiple phase blocks with near-identical logic.
- **Recommended fix**: Extract to a shared helper script sourced by these steps.
  - Proposed shared signatures: `gh_api_safe <args...>`, `capture_run_id <repo> <created_after> <name_regex>`
  - Callers to migrate: clarify wait, plan wait, implement wait, review wait, poller wait blocks.

### Finding DUP-003
- **ID**: `DUP-003`
- **File path**: `.github/workflows/review_autofix.yml`, `.github/workflows/issue_pr_status.yml`
- **Line range**: `review_autofix.yml:3043,3154,3637`, `issue_pr_status.yml:197`
- **Severity**: Low
- **Category tag**: `duplication`
- **Description**: The same long regex fallback for issue-number extraction from PR title/body is duplicated in multiple workflow steps.
- **Recommended fix**: Move extraction into one helper in `scripts/gh_helpers.sh`.
  - Proposed shared signature: `extract_linked_issue_numbers_from_pr_text <repo_slug> <text>`
  - Callers to migrate: `review_autofix.yml` fallback paths and `issue_pr_status.yml` fallback path.

## 4) Expression Size Limit Risk Assessment

Assessment method:
- Scanned all workflow `run: |` blocks containing `${{ ... }}` and measured expression token lengths per block.
- Checked workflow file sizes against 800 KB warning and 1 MB hard parser limit.

Measured results:
- Max `${{ ... }}` total in a single interpolated `run` block: **534 chars** (`.github/workflows/implement.yml:2364-2851`).
- Headroom to 21,000 expression cap at current max: **20,466 chars**.
- Blocks over 15,000 chars: **0**.
- Blocks over 18,000 chars: **0**.
- Largest workflow file: **203,300 bytes** (`.github/workflows/review_autofix.yml`).
- Headroom to 800 KB warning: **615,900 bytes**.
- Headroom to 1 MB hard limit: **845,276 bytes**.

### Finding EXPR-001
- **ID**: `EXPR-001`
- **File path**: `.github/workflows/ci.yml`
- **Line range**: `65-87`
- **Severity**: Low
- **Category tag**: `expression-limit`
- **Description**: The repo has prior expression-limit incidents, but CI currently has no dedicated expression-budget check.
- **Estimated expression chars**: Current max observed block = 534 chars.
- **Headroom remaining**: 20,466 chars to 21,000 cap.
- **Mitigation option**: Add a static guard script in CI (warn at 12,000, fail at 15,000 per `${{ ... }}` expression).
- **Recommended fix**: Add an expression-size budget check to `ci.yml` alongside existing lint steps.

## 5) Cross-Cutting Concerns

### Finding DEAD-001
- **ID**: `DEAD-001`
- **File path**: `scripts/memory_helpers.sh`
- **Line range**: `57`
- **Severity**: Low
- **Category tag**: `dead-code`
- **Description**: `local token="${GH_TOKEN:-}"` is declared but never used.
- **Recommended fix**: Remove the variable or wire it into authenticated URL construction if intended.

### Finding DEAD-002 [NEEDS VERIFICATION]
- **ID**: `DEAD-002`
- **File path**: `scripts/orchestrate_poll_process.sh`
- **Line range**: `7250`, `7523`
- **Severity**: Low
- **Category tag**: `dead-code`
- **Description**: `RB_FOLLOWUP_REFUSED` and `IF_BLOCKERS_SOURCE` are assigned but have no reads in static search.
- **Recommended fix**: Confirm no dynamic/indirect reads; if none, remove assignments.

### Finding CONS-001
- **ID**: `CONS-001`
- **File path**: `.github/workflows/review_autofix.yml`
- **Line range**: `96-97`, `440`, `3610`
- **Severity**: Medium
- **Category tag**: `consistency`
- **Description**: Same workflow mixes raw `gh api` and helper-backed `gh_retry` access for equivalent PR state reads, producing inconsistent retry/rate-limit behavior.
- **Recommended fix**: Standardize on one helper-backed PR-read path (single fetch + local parse) across all PR-state checks in this workflow.

### Finding SH-001 [NEEDS VERIFICATION]
- **ID**: `SH-001`
- **File path**: `scripts/validate_driver.sh`
- **Line range**: `558`, `577`
- **Severity**: Low
- **Category tag**: `shellcheck`
- **Description**: ShellCheck SC2053 flags unquoted RHS glob patterns in `[[ ... == ${HELPER_PATTERN} ]]` and `[[ ... == ${CANARY_PATTERN} ]]`.
- **Recommended fix**: If glob semantics are intended, switch to `case` with explicit comments or add targeted ShellCheck suppression; if literal match is intended, quote RHS.

Additional cross-cutting scan notes:
- `TODO`/`FIXME`/`HACK` markers in audited workflow/script files: none found.

## 6) Summary & Severity Matrix

### 6A) Findings Summary Table

| Severity | Count | IDs |
|----------|-------|-----|
| Critical | 0 | — |
| High | 1 | API-002 |
| Medium | 5 | API-001, API-003, API-005, DUP-001, CONS-001 |
| Low | 10 | BUG-001, BUG-002, SEC-001, API-004, DUP-002, DUP-003, EXPR-001, DEAD-001, DEAD-002, SH-001 |

### 6B) Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|----------|---------------|------------------|
| Critical/High bug fixes | 1-3 | Medium |
| API call optimization | 3-6 | Medium-Large |
| Code modularization | 4-8 | Medium-Large |
| Expression size reduction | 1-2 | Small |
| Medium/Low fixes | 6-10 | Small-Medium |
