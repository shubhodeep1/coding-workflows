# Implement-Plan Log — gh_retry: emit only the successful attempt's stdout

- Plan: docs/plans/issue-5495-gh-retry-stdout-isolation-plan.md
- Source issue: shubhodeep1/coding-workflows#5495
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5495-gh-retry-stdout-isolation   Final PR: #5509 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (review by review_autofix.yml, Claude-fixer mode)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 implemented and verified by session_01PRUpuGPRtMfDoFnj9iAiB7; phase PR opened against the project branch

## Phases
1. [ ] Phase 1 — buffer `gh_retry` stdout per attempt so only the successful attempt's output reaches the caller
   - [x] `scripts/gh_helpers.sh` `gh_retry` (`:452-521`; buffer `:461-472`, drop warning `:475-478`): per-attempt stdout buffer, replay on success, drop + byte-count warning on failure, mktemp guard
   - [x] `tests/test_gh_retry_stdout_isolation.py` [new] (16 tests: 8 for `gh_retry`, 7 of which failed before the fix, plus 4 cases for each of the two inline `review_autofix.yml` wrappers added in review round 1): fake-`gh` cases (rate-limited ×2 then success to a file and to `$(…)`, permanent failure, exhausted retries, first-try success, dropped-bytes warning, temp-file cleanup); fails on the old `gh_retry`
   - [x] `.github/workflows/ci.yml`: register the new test in the gh_helpers step
   - [x] `changelog.d/5495-gh-retry-stdout-isolation.md` [new] (`fixed`)
   - [x] `README.md` / `agents.md`: `gh_retry` stdout contract
   - Done: new + existing gh_helpers tests pass; `bash -n`, shellcheck (no new findings), yamllint clean

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which wrappers in `scripts/gh_helpers.sh` change? — Picked: A — only `gh_retry`; `gh_retry_to_file`, `gh_api_json_to_file`, `_safe_gh_jq`, and `curl_gh_api` are audited and left unchanged. Alternatives: B — also truncate `gh_retry_to_file`'s output file on final failure. Why: those four already keep failed attempts out of a successful result, and callers print `gh_retry_to_file`'s last error body as a diagnostic. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What happens to a failed attempt's stdout? — Picked: A — drop it, and print one `::warning::` line with its byte count and the attempt number. Alternatives: B — drop it silently; C — copy the escaped body to stderr. Why: keeps a §8 diagnostic without putting response text on stderr, which four `orchestrate_poll_process.sh` callers grep. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Should the inline `gh_retry()` copies in `.github/workflows/review_autofix.yml` be fixed here? — Picked: A — no; record them as a follow-up candidate. Alternatives: B — fix them in this PR. Why: the issue scopes the fix to `scripts/gh_helpers.sh` (§5). Applied in: no code change. Status: changed to B (2026-10-01, PR #5509): review round 1 flagged the inline copies as a task gap of #5495, so both now buffer stdout like `gh_retry`
- AD-4 [plan, 2026-09-30] What if the successful attempt's buffered output cannot be delivered? — Picked: A — return `cat`'s non-zero status, without re-running the command. Alternatives: B — return 0 anyway. Why: the caller did not get the output, and re-running could repeat a write. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] What if the stdout temp file cannot be created? — Picked: A — `::error::`, clean up, return 1 without running. Alternatives: B — run unbuffered. Why: matches the existing stderr mktemp guard; B would bring back the corruption. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] What form should the test take? — Picked: A — pytest driving bash with a fake `gh`, in the existing `ci.yml` gh_helpers step. Alternatives: B — a standalone `tests/*.sh` script. Why: matches `tests/test_gh_helpers_parse_reset_header.py`. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Phase 1 verification (2026-09-30): new test 8/8 (15 repeated runs, all green) and 7/8 failing on the pre-fix `gh_retry`; the 47 test files that source `gh_helpers.sh` or mention `gh_retry` gave 1707 passed, 1 failed at `-n 4`. The failure, `test_implement_post_codex_recovery.py::test_review_pipeline_integration_chain_module_runs_clean`, is `gawk: command not found` in `scripts/review_issue_ledger.sh` (the container has no gawk; unrelated). At `-n 8` the 26 extra failures in `test_orchestrate_final_merge_required_checks_gate.py` and others were 60 s subprocess timeouts under load (sourcing the poller takes ~30 s); they pass at `-n 4` and alone. `bash -n` clean; shellcheck finding counts identical before and after; yamllint `ci.yml` clean; changelog contract tests 47/47.
- Issue progress comment: 5905562246.
- `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}` → Security pass: run.
- AD-3 follow-up closed in PR #5509: the two inline `gh_retry()` retry loops in `.github/workflows/review_autofix.yml` (standalone-validate dispatch fallback, deterministic-skip-merge step) now buffer each attempt's stdout and print only the successful attempt's, with the same `::error::` / `::warning::` lines as `scripts/gh_helpers.sh`. No follow-up issue is needed.
- Checker env: pass `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1` (never empty) in every `check_in_status.py` call; no dedicated verdict bot is configured.
