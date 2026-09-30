# Implement-Plan Log — gh_retry: emit only the successful attempt's stdout

- Plan: docs/plans/issue-5495-gh-retry-stdout-isolation-plan.md
- Source issue: shubhodeep1/coding-workflows#5495
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5495-gh-retry-stdout-isolation   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened by session_01PRUpuGPRtMfDoFnj9iAiB7 (/implement-issue-claude #5495)

## Phases
1. [ ] Phase 1 — buffer `gh_retry` stdout per attempt so only the successful attempt's output reaches the caller
   - [ ] `scripts/gh_helpers.sh` `gh_retry`: per-attempt stdout buffer, replay on success, drop + byte-count warning on failure, mktemp guard
   - [ ] `tests/test_gh_retry_stdout_isolation.py` [new]: fake-`gh` cases (rate-limited ×2 then success to a file and to `$(…)`, permanent failure, exhausted retries, first-try success, dropped-bytes warning, temp-file cleanup); fails on the old `gh_retry`
   - [ ] `.github/workflows/ci.yml`: register the new test in the gh_helpers step
   - [ ] `changelog.d/5495-gh-retry-stdout-isolation.md` [new] (`fixed`)
   - [ ] `README.md` / `agents.md`: `gh_retry` stdout contract
   - Done: new + existing gh_helpers tests pass; `bash -n`, shellcheck (no new findings), yamllint clean

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which wrappers in `scripts/gh_helpers.sh` change? — Picked: A — only `gh_retry`; `gh_retry_to_file`, `gh_api_json_to_file`, `_safe_gh_jq`, and `curl_gh_api` are audited and left unchanged. Alternatives: B — also truncate `gh_retry_to_file`'s output file on final failure. Why: those four already keep failed attempts out of a successful result, and callers print `gh_retry_to_file`'s last error body as a diagnostic. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What happens to a failed attempt's stdout? — Picked: A — drop it, and print one `::warning::` line with its byte count and the attempt number. Alternatives: B — drop it silently; C — copy the escaped body to stderr. Why: keeps a §8 diagnostic without putting response text on stderr, which four `orchestrate_poll_process.sh` callers grep. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Should the inline `gh_retry()` copies in `.github/workflows/review_autofix.yml` be fixed here? — Picked: A — no; record them as a follow-up candidate. Alternatives: B — fix them in this PR. Why: the issue scopes the fix to `scripts/gh_helpers.sh` (§5). Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-30] What if the successful attempt's buffered output cannot be delivered? — Picked: A — return `cat`'s non-zero status, without re-running the command. Alternatives: B — return 0 anyway. Why: the caller did not get the output, and re-running could repeat a write. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] What if the stdout temp file cannot be created? — Picked: A — `::error::`, clean up, return 1 without running. Alternatives: B — run unbuffered. Why: matches the existing stderr mktemp guard; B would bring back the corruption. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] What form should the test take? — Picked: A — pytest driving bash with a fake `gh`, in the existing `ci.yml` gh_helpers step. Alternatives: B — a standalone `tests/*.sh` script. Why: matches `tests/test_gh_helpers_parse_reset_header.py`. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue progress comment: 5905562246.
- `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}` → Security pass: run.
- Follow-up candidate (AD-3): the inline `gh_retry()` fallbacks in `.github/workflows/review_autofix.yml` still let failed attempts' stdout through to their `$(…)` captures.
- Checker env: pass `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1` (never empty) in every `check_in_status.py` call; no dedicated verdict bot is configured.
