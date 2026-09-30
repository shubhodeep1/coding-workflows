# gh_retry: emit only the successful attempt's stdout

Source issue: shubhodeep1/coding-workflows#5495 (https://github.com/shubhodeep1/coding-workflows/issues/5495)
Base branch: main
Security pass: run

## Summary

`gh_retry` in `scripts/gh_helpers.sh` lets every failed attempt's stdout through to the caller, so a call that fails and then succeeds on retry returns the error bodies in front of the real response. Buffer each attempt's stdout and emit it only for the attempt that succeeds, fixing every caller at once.

## Context

Clarify run 36670937896 (2026-09-30), started by a `/reclarify` on #5016, failed in **Fetch issue metadata** (`.github/workflows/clarify.yml:392`):

```bash
gh_retry gh api "repos/…/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"
```

`gh api` prints the error response body (`{"message":"API rate limit exceeded …"}`) to **stdout** when a call fails. `gh_retry` (`scripts/gh_helpers.sh:439-494`) redirects only stderr per attempt (`"$@" 2>"${stderr_file}"`, line 450), so stdout from all 3 attempts landed in the file: two rate-limit error objects, then the issue. `jq -r '.number'` printed `null`, `null`, `5016`, and the bare `null` line in `$GITHUB_ENV` failed the step with `Invalid format 'null'`.

The same flaw affects every `gh_retry gh … > file` and `$(gh_retry gh …)` capture: the issue counts 25 sites, and `gh_retry gh` appears 557 times across `.github/workflows/` and `scripts/`. It only shows when a retry succeeds after a failure, so the output looks valid but starts with garbage.

The other wrappers in `scripts/gh_helpers.sh` were audited (see AD-1):
- `gh_retry_to_file` (`:505-561`) and `gh_api_json_to_file` (`:610-663`) redirect with `>` on every attempt, so a success leaves only that attempt's output in the file. On final failure `gh_retry_to_file` leaves the last error body in the file, and callers print it as a diagnostic (`scripts/orchestrate_poll_process.sh:2279`, `:2615`, `:2624`).
- `_safe_gh_jq` (`:580-594`) and `curl_gh_api` (`:678-727`) already buffer to a temp file and print only on success.

## Goals

- `gh_retry CMD > file` leaves only the successful attempt's stdout in `file`, whatever failed before it.
- `$(gh_retry CMD)` captures only the successful attempt's stdout.
- A failure that never succeeds (permanent, or retries exhausted) writes nothing to stdout and returns 1, as it does today.
- Return codes, the retry and rate-limit logic (attempt count, backoff, `/rate_limit` wait, Telegram alert, circuit breaker), and the existing stderr lines stay the same.
- No call-site changes.

## Non-goals

- The inline `gh_retry()` copies in `.github/workflows/review_autofix.yml` ("Dispatch standalone validate for orchestrator short-circuit issues" fallback, and the deterministic-skip-merge step). They have their own retry loop and are not `scripts/gh_helpers.sh` (AD-3).
- The `GH_PAT` hourly budget that caused the rate limit (a separate §15 question, per the issue).
- Changing `gh_retry_to_file`, `gh_api_json_to_file`, `_safe_gh_jq`, or `curl_gh_api` (AD-1).
- Rate-limit detection from stdout: detection keeps reading stderr only.

## Constraints

- §5 minimal change: only `gh_retry`'s body changes; the other helpers stay as they are.
- §6 naming immutability: no identifier is renamed. The new local variables (`stdout_file`, `_gh_retry_stdout_bytes`, `_gh_retry_replay_rc`) are local to `gh_retry` and collide with nothing it uses (checked against the function and the file's globals).
- §8: the dropped output is reported as a structured warning line (byte count, attempt), not silently lost.
- §9: tabs, opening braces on a new line; YAML stays 2-space.
- §15: no new GitHub API calls. The fix issues exactly the calls the old code did.
- §20: a `changelog.d/` fragment (`fixed`), since workflow and script behaviour changes.
- §27: no workflow file grows beyond a one-line test registration in `ci.yml` (60,168 bytes).

## Approach

In `gh_retry`, next to the existing stderr temp file, create a stdout temp file (`mktemp "${TMPDIR:-/tmp}/gh_retry_stdout.XXXXXX"`). Each attempt runs `"$@" >"${stdout_file}" 2>"${stderr_file}"`; the `>` truncates the buffer every attempt.

- **Success**: `cat` the buffer to stdout, remove both temp files, and return `cat`'s status: 0 normally, or non-zero if the output could not be delivered, e.g. the reader closed the pipe (AD-4). The command is never re-run once it has succeeded.
- **Failure**: when the buffer is non-empty, print one line to stderr: `::warning::  gh_retry: dropped <N> bytes of stdout from failed attempt <a>/<m>`. The body itself is not printed (AD-2). Then continue exactly as today: the permanent-failure, rate-limit, or backoff branch. The next attempt truncates the buffer.
- **Final failure** (permanent or exhausted): remove both temp files; nothing reaches stdout; return 1.
- **mktemp failure** for the stdout buffer: print an `::error::`, remove the stderr temp file, and return 1 without running the command, like the existing stderr `mktemp` guard (AD-5).

Alternatives considered: fixing each caller by switching to `gh_retry_to_file` (25+ sites, and `$(…)` captures would need temp files); rejected because the issue asks for one fix in `gh_retry`, and the wrapper fix covers every caller, present and future.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the fix is one function body plus its test, changelog fragment, and doc notes, and none of these is useful merged alone.

1. **Phase 1 — buffer `gh_retry` stdout per attempt.**
   - Files: `scripts/gh_helpers.sh`, `tests/test_gh_retry_stdout_isolation.py` [new], `.github/workflows/ci.yml`, `changelog.d/5495-gh-retry-stdout-isolation.md` [new], `README.md`, `agents.md`.
   - Done when: the new test passes and fails against the old `gh_retry`; `tests/test_gh_helpers_parse_reset_header.py`, `tests/test_gh_helpers_list_runs_method.py`, and `tests/test_workflow_gh_retry_fallback_contract.py` still pass; `bash -n scripts/gh_helpers.sh` is clean; `shellcheck` reports nothing new; `yamllint` passes on `ci.yml`.
   - Rollback: revert the phase PR. Callers are unchanged, so a revert restores the old output behaviour exactly.

## Implementation Steps

1. `scripts/gh_helpers.sh:429-494` — update the `gh_retry` header comment (stdout contract) and body as in Approach.
2. `tests/test_gh_retry_stdout_isolation.py` [new] — pytest that sources `scripts/gh_helpers.sh` in `bash` with a fake `gh` first on `PATH` (a counter file drives each attempt), `sleep` stubbed to a no-op, the Telegram variables unset, and `TMPDIR` / `GH_RATE_LIMIT_BREAKER_FILE` inside `tmp_path`. Cases:
   - two rate-limited attempts (JSON body on stdout, `gh: API rate limit exceeded … (HTTP 403)` on stderr), then success: `> file` holds only the success body, and `jq -r .number` prints one line;
   - the same sequence captured with `$(gh_retry …)`;
   - a permanent failure (404 body on stdout, `gh: Not Found (HTTP 404)` on stderr): stdout empty, rc 1, one attempt;
   - transient failures until retries run out (`GH_RETRY_MAX_ATTEMPTS=2`, HTTP 502): stdout empty, rc 1;
   - first-try success: stdout byte-identical (no trailing newline added), rc 0, nothing on stderr;
   - the dropped-bytes warning names the byte count and attempt, and does not contain the body text;
   - no `gh_retry_*` temp files left in `TMPDIR` after success or failure.
3. `.github/workflows/ci.yml` — add the new test file to the "Merge-train and gh_helpers rate-limit tests (CLAUDE.md §15)" step and its comment.
4. `changelog.d/5495-gh-retry-stdout-isolation.md` [new] — `fixed` fragment per §20.
5. `README.md` "GitHub API rate-limit admin alert" section — one paragraph on the `gh_retry` stdout contract. `agents.md` "Operational lessons" — extend the `gh_retry` bullet (§7).

## Files & Modules

- `scripts/gh_helpers.sh`
- `tests/test_gh_retry_stdout_isolation.py` [new]
- `.github/workflows/ci.yml`
- `changelog.d/5495-gh-retry-stdout-isolation.md` [new]
- `README.md`
- `agents.md`

## Tests

- Unit (new): `tests/test_gh_retry_stdout_isolation.py`, run by the `ci.yml` step above. It is shown to fail on the pre-fix `gh_retry` (the capture and file cases get the error bodies) before the fix is applied.
- Regression (existing): `tests/test_gh_helpers_parse_reset_header.py`, `tests/test_gh_helpers_list_runs_method.py`, `tests/test_workflow_gh_retry_fallback_contract.py`, and the pytest files that source `scripts/gh_helpers.sh` through a fake `gh` (`tests/test_review_merge_train.py`, `tests/test_orchestrate_poll_process.py`, and others found by grep).
- Static: `bash -n`, `shellcheck scripts/gh_helpers.sh` (no new findings), `yamllint .github/workflows/ci.yml`.

## Risks & Mitigations

- Large outputs (for example `gh run view --log`) are now written to `TMPDIR` before they are printed. Mitigation: runners have ample disk, the file is removed at once, and `_safe_gh_jq` and `curl_gh_api` already buffer this way.
- Output is no longer streamed as it arrives. No caller reads `gh_retry` output while the command is still running (all 557 `gh_retry gh` sites redirect to a file or capture with `$(…)`, or pipe into `jq` / `head`, which get the same bytes). ACCEPTED.
- A reader that closes the pipe early (`| head -1`) on a large output now makes `cat`, not `gh`, take SIGPIPE, and `gh_retry` returns non-zero without running the command again. Before, `gh` took SIGPIPE, which counted as a failed attempt and re-ran the command. ACCEPTED (AD-4); the old behaviour could repeat a write.
- Callers that read `gh_retry`'s stderr (`orchestrate_poll_process.sh:4195`, `:9650`, `:9666`, `:10615` grep it for `not found` / `protected`) could react to new text. Mitigation: the one new line contains neither word, and the response body is never copied to stderr (AD-2).

## Rollout

Ships to consumer repos on the next `@stable` sync through `scripts/gh_helpers.sh`. No flags, no env vars, no DB changes. Rollback is a revert of the PR.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which wrappers in `scripts/gh_helpers.sh` change? — Picked: A — only `gh_retry`; `gh_retry_to_file`, `gh_api_json_to_file`, `_safe_gh_jq`, and `curl_gh_api` are audited and left unchanged. Alternatives: B — also truncate `gh_retry_to_file`'s output file on final failure. Why: those four already keep failed attempts out of a successful result, and callers print `gh_retry_to_file`'s last error body from the file as a diagnostic (`orchestrate_poll_process.sh:2279`, `:2615`, `:2624`), which B would remove. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What happens to a failed attempt's stdout? — Picked: A — drop it, and print one `::warning::` line with its byte count and the attempt number. Alternatives: B — drop it silently; C — copy the escaped body to stderr. Why: keeps a §8 diagnostic without putting response text on stderr, where four `orchestrate_poll_process.sh` callers grep for `not found` / `protected` to pick a branch; gh already prints the error message on stderr. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Should the inline `gh_retry()` copies in `.github/workflows/review_autofix.yml`, which have the same leak, be fixed here? — Picked: A — no; record them in the log's `## Notes` and the progress comment as a candidate follow-up issue. Alternatives: B — fix them in this PR. Why: the issue scopes the fix to `scripts/gh_helpers.sh` (§5), and those copies live in a workflow near the §27 size guard. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-30] What if the successful attempt's buffered output cannot be written to the caller (for example, the reader closed the pipe)? — Picked: A — return `cat`'s non-zero status, without re-running the command. Alternatives: B — return 0 anyway. Why: the caller did not get the output, so success would be false; and re-running a command that already succeeded could repeat a write. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] What if the stdout temp file cannot be created? — Picked: A — print `::error::`, remove the stderr temp file, and return 1 without running the command. Alternatives: B — run unbuffered (the old behaviour). Why: matches the existing stderr `mktemp` guard in the same function and in `gh_retry_to_file` / `gh_api_json_to_file`; B would silently bring back the corruption. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] What form should the test take? — Picked: A — a pytest file that drives `bash` with a fake `gh`, run in the existing `ci.yml` gh_helpers step. Alternatives: B — a standalone `tests/*.sh` script. Why: matches `tests/test_gh_helpers_parse_reset_header.py` and the CI step that already runs it. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`, so the security pass runs.
- Follow-up candidate (AD-3): the inline `gh_retry()` fallbacks in `.github/workflows/review_autofix.yml` still let failed attempts' stdout through to their `$(…)` captures.

## References

- Issue #5495; clarify run https://github.com/shubhodeep1/coding-workflows/actions/runs/36670937896; #5016 (the stalled `/reclarify`).
