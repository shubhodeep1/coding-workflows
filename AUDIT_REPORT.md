# AUDIT_REPORT.md

Date: 2026-04-16

Audit scope executed:
- Workflow files: `.github/workflows/*.yml` (28 files)
- Scripts: `scripts/*.sh`, `scripts/*.py` (34 top-level files)
- Mandatory context loaded: `README.md`, `agents.md`, `CLAUDE.md`, `.github/ai/*.json`

## 1) Bug & Correctness Sweep

### Finding BUG-001
- **ID**: `BUG-001`
- **File path**: `scripts/orchestrate_poll_process.sh`
- **Line range**: `8027-8030`
- **Severity**: Low
- **Category tag**: `bug`
- **Description**: `_sorted_issue_nums="$(printf '%s\n' ${ISSUE_NUMS} | sort -un)"` expands `${ISSUE_NUMS}` unquoted. This allows word-splitting/glob expansion and can mis-order or drop items if unexpected characters are introduced.
- **Recommended fix**: Quote the expansion and normalize with an array-safe path, e.g. `printf '%s\n' "${ISSUE_NUMS}"` after validating/splitting numeric IDs.

### Finding BUG-002
- **ID**: `BUG-002`
- **File path**: `.github/workflows/test-and-mark-stable.yml`
- **Line range**: `255-282`
- **Severity**: Low
- **Category tag**: `bug`
- **Description**: In Phase 1 polling, comments are fetched twice in the same loop tick (`/comments` for count, then `/comments?per_page=5` for last bot message). This creates a TOCTOU window where stall/failure checks evaluate different snapshots.
- **Recommended fix**: Fetch comments once per tick (descending, bounded page) and derive both count + latest bot-comment analysis from that single JSON payload.

### Finding SEC-001 [NEEDS VERIFICATION]
- **ID**: `SEC-001`
- **File path**: `scripts/orchestrate_poll_process.sh`
- **Line range**: `592-599`
- **Severity**: Low
- **Category tag**: `security`
- **Description**: `_validate_phase_threshold` clears vars via `eval "${var_name}="`. Current callers pass hardcoded names, but this is still an eval-based footgun if future callsites pass untrusted names.
- **Recommended fix**: Replace `eval` with a whitelist + `printf -v "${var_name}" '%s' ''` (or explicit case branches) to remove command-evaluation risk.

## 2) GitHub API Call Redundancy Audit

### Finding API-001
- **ID**: `API-001`
- **File path**: `.github/workflows/review_autofix.yml`
- **Line range**: `96-97`
- **Severity**: Medium
- **Category tag**: `api-redundancy`
- **Description**: Gate step fetches the same PR endpoint twice (`.state`, then `.merged`).
- **Current API call count**: 2 calls/run for `repos/{repo}/pulls/{PR_NUMBER}` in this path.
- **Proposed call count after fix**: 1 call/run.
- **Batching/caching pattern to extend**: Reuse the single-fetch parse model used by `_fetch_pr_json` + `_jq_field` in `scripts/orchestrate_poll_process.sh`.
- **Recommended fix**: Fetch PR JSON once, parse both `state` and `merged` from local JSON.

### Finding API-002
- **ID**: `API-002`
- **File path**: `scripts/orchestrate_poll_process.sh`
- **Line range**: `1063-1077`, `6119-6125`, `6213-6219`, `6306-6313`, `8030-8038`
- **Severity**: High
- **Category tag**: `api-batching`
- **Description**: `_issue_cross_ref_pr_number_last()` re-fetches issue timeline per call. It is invoked repeatedly across multiple loops in one poll cycle (status reconciliation, auto-merge, conflict healing, judge prompt assembly).
- **Current API call count**: At least `2N + R + D` timeline lookups per wave cycle (N = issues in wave, R = ready-to-merge subset, D = in_progress/done subset), plus additional calls in stall/judge paths.
- **Proposed call count after fix**: 1 batched GraphQL fetch per wave cycle (or one prefetch per phase) + in-memory lookups.
- **Batching/caching pattern to extend**: Extend `_fetch_linked_pr_status_graphql` / `_fetch_candidate_issue_details_graphql` style caches already present in this script.
- **Recommended fix**: Prefetch `issue -> linked_pr` map once per cycle and pass cached values into downstream loops.

### Finding API-003
- **ID**: `API-003`
- **File path**: `scripts/orchestrate_poll_process.sh`
- **Line range**: `4389-4393`
- **Severity**: Medium
- **Category tag**: `api-batching`
- **Description**: Standalone stall recovery builds candidate issues by looping six labels and calling `gh issue list` once per label.
- **Current API call count**: 6 calls/cycle for label-based candidate bootstrap.
- **Proposed call count after fix**: 1 call/cycle (single GraphQL/search query with aliased label filters).
- **Batching/caching pattern to extend**: Aliased GraphQL batching pattern used in `_fetch_candidate_issue_details_graphql`.
- **Recommended fix**: Replace per-label looped listing with one batched GraphQL/search query and local union/dedupe.

### Finding API-004
- **ID**: `API-004`
- **File path**: `.github/workflows/test-and-mark-stable.yml`
- **Line range**: `247-281`
- **Severity**: Low
- **Category tag**: `api-redundancy`
- **Description**: Clarify poll loop fetches issue labels and comments count, then performs another comments fetch for bot-error detection.
- **Current API call count**: 3 calls/poll tick (`issue`, `comments`, `comments?per_page=5`).
- **Proposed call count after fix**: 2 calls/poll tick (`issue`, one bounded comments fetch).
- **Batching/caching pattern to extend**: Reuse the same single-comments-payload pattern already used in the plan phase loop in this workflow (`COMMENTS_JSON` reuse).
- **Recommended fix**: Derive count + last bot comment from one comments payload.

### Finding API-005
- **ID**: `API-005`
- **File path**: `.github/workflows/review_autofix.yml`
- **Line range**: `158-167`
- **Severity**: Medium
- **Category tag**: `api-batching`
- **Description**: In post-merge fallback path (`labels_known != true`), the workflow calls `gh issue view` once per linked issue to read labels.
- **Current API call count**: `M` per run in fallback mode (`M` = fallback-linked issue count).
- **Proposed call count after fix**: 1 batched GraphQL call for all fallback issue numbers.
- **Batching/caching pattern to extend**: Extend the existing GraphQL linked-issue fetch in the same step (lines `136-141`) with an alias query for issue labels by number.
- **Recommended fix**: Build one alias GraphQL query for all fallback issue IDs and evaluate labels from that response.

## 3) Code Duplication & Modularization Opportunities

### Finding DUP-001
- **ID**: `DUP-001`
- **File path**: `scripts/label_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/validate_process.sh`, `scripts/review_rb_judge.sh`, `.github/workflows/review_autofix.yml`
- **Line range**: `label_helpers.sh:80-101`, `orchestrate_poll_process.sh:868-931`, `validate_process.sh:440-466`, `review_rb_judge.sh:57-74`, `review_autofix.yml:2969-2984,3056-3067`
- **Severity**: Medium
- **Category tag**: `duplication`
- **Description**: `ensure_label_exists` logic is reimplemented in multiple variants (different retry semantics, color defaults, and existence-check behavior), creating drift risk.
- **Recommended fix**: Consolidate ownership in `scripts/label_helpers.sh` with one stable API:
  - Function signature: `ensure_label_exists <label_name> [repo]`
  - Optional strict mode: `ensure_label_exists_strict <label_name> [repo]`
  - Update callers in `orchestrate_poll_process.sh`, `validate_process.sh`, `review_rb_judge.sh`, and fallback blocks in `review_autofix.yml`.

### Finding DUP-002
- **ID**: `DUP-002`
- **File path**: `.github/workflows/test-and-mark-stable.yml`
- **Line range**: `207-241`, `316-350`, `475-515`, `630-649`, `1059-1070`
- **Severity**: Low
- **Category tag**: `duplication`
- **Description**: `gh_api_safe` and run-ID capture logic are duplicated across multiple phase blocks with slight variations.
- **Recommended fix**: Move polling helpers into a shared script (e.g., `scripts/e2e_poll_helpers.sh`) and source it in each phase step.
  - Function signature examples: `gh_api_safe <args...>`, `capture_run_id <repo> <created_after> <name_regex>`.
  - Callers updated: clarify wait, plan wait, implement wait, review wait, poller wait blocks.

### Finding DUP-003
- **ID**: `DUP-003`
- **File path**: `.github/workflows/review_autofix.yml`, `.github/workflows/issue_pr_status.yml`
- **Line range**: `review_autofix.yml:2990-2993,3075-3078`, `issue_pr_status.yml:195`
- **Severity**: Low
- **Category tag**: `duplication`
- **Description**: Regex-based fallback extraction of issue numbers from PR title/body is repeated in multiple paths.
- **Recommended fix**: Extract to one helper (e.g., `scripts/gh_helpers.sh`):
  - Function signature: `extract_linked_issue_numbers_from_pr_text <repo_slug> <text>`
  - Update callers in review_autofix and issue_pr_status paths.

## 4) Expression Size Limit Risk Assessment

Measurement method used:
- Scanned all `.github/workflows/*.yml` `run: |` blocks containing `${{ ... }}` and measured static expression length per block.
- Checked largest workflow file sizes against 800 KB alert threshold and 1 MB hard limit.

Result summary:
- No `${{ }}` expression block exceeded 15,000 characters.
- Largest measured `run:` interpolation total was 534 chars (`.github/workflows/implement.yml:2344`), leaving 20,466 chars headroom to 21,000.
- Largest workflow file size is 194,289 bytes (`.github/workflows/review_autofix.yml`), leaving 610,511 bytes headroom to 800 KB warning and 854,287 bytes headroom to 1 MB hard limit.

### Finding EXPR-001
- **ID**: `EXPR-001`
- **File path**: `.github/workflows/ci.yml`
- **Line range**: `69-80`
- **Severity**: Low
- **Category tag**: `expression-limit`
- **Description**: CI currently lints YAML/action schema but does not enforce expression-size budgets despite prior historical over-limit incidents.
- **Estimated expression chars / headroom**: Current max observed expression block = 534 chars (headroom 20,466); no immediate overflow risk.
- **Mitigation option**: Add a lightweight guard script in CI to fail at >15,000 chars and warn at >12,000 for any single `${{ ... }}` expression.
- **Recommended fix**: Add a static checker step in `ci.yml` for expression-length budgets.

## 5) Cross-Cutting Concerns

### Finding DEAD-001
- **ID**: `DEAD-001`
- **File path**: `scripts/memory_helpers.sh`
- **Line range**: `57`
- **Severity**: Low
- **Category tag**: `dead-code`
- **Description**: `local token="${GH_TOKEN:-}"` is declared but unused.
- **Recommended fix**: Remove the unused variable or wire it into authenticated remote URL handling if intended.

### Finding DEAD-002 [NEEDS VERIFICATION]
- **ID**: `DEAD-002`
- **File path**: `scripts/orchestrate_poll_process.sh`
- **Line range**: `7250`, `7523`
- **Severity**: Low
- **Category tag**: `dead-code`
- **Description**: `RB_FOLLOWUP_REFUSED` and `IF_BLOCKERS_SOURCE` are assigned but not read in static analysis.
- **Recommended fix**: Confirm no dynamic reads; if none exist, remove assignments to reduce cognitive noise.

### Finding CONS-001
- **ID**: `CONS-001`
- **File path**: `.github/workflows/review_autofix.yml`
- **Line range**: `96-97`, `2921-2928`
- **Severity**: Medium
- **Category tag**: `consistency`
- **Description**: Same workflow mixes raw `gh api` and `gh_retry`/safe-helper patterns for similar PR-read operations, producing inconsistent retry and rate-limit behavior.
- **Recommended fix**: Standardize on helper-backed reads (`gh_retry` + single-response parsing) for all PR metadata fetches in this workflow.

### Finding SH-001 [NEEDS VERIFICATION]
- **ID**: `SH-001`
- **File path**: `scripts/validate_driver.sh`
- **Line range**: `434`, `453`
- **Severity**: Low
- **Category tag**: `shellcheck`
- **Description**: ShellCheck SC2053 flags unquoted RHS in `[[ ... == ${HELPER_PATTERN} ]]` and `[[ ... == ${CANARY_PATTERN} ]]`. If patterns become user-controlled, glob semantics may become broader than expected.
- **Recommended fix**: If literal comparison is intended, quote RHS. If glob matching is intended, switch to explicit `case` blocks and annotate intent.

Additional cross-cutting scan notes:
- TODO/FIXME/HACK markers in audited workflows/scripts: none found.

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
|----------|--------------|-----------------|
| Critical/High bug fixes | 1-3 | Medium |
| API call optimization | 3-6 | Medium-Large |
| Code modularization | 4-8 | Medium-Large |
| Expression size reduction | 1-2 | Small |
| Medium/Low fixes | 6-10 | Small-Medium |

