# security_audit.sh: print a sanitized, size-capped tail of Codex stderr on a codex-execution failure

Source issue: shubhodeep1/coding-workflows#5785 (https://github.com/shubhodeep1/coding-workflows/issues/5785)
Base branch: main
Security pass: run

## Summary

When Codex exits nonzero inside `scripts/security_audit.sh`, the job log shows only `error=Codex\ exited\ nonzero`. This plan prints a sanitized tail of Codex's stderr, capped at 40 lines and 4 KiB, and adds a `provider=` field to the failure line. A stage session can then tell a provider outage (HTTP 402, 401, 429, 5xx) from a code failure without reading raw logs.

## Context

- On 2026-09-30 three `security-audit.yml` runs (36748018397, 36749008453, 36754588874) failed at **Run security audit** about 8 s after Codex started. The cause was an OpenRouter credit outage (HTTP 402). The #4867 project stage could not tell that from the log, stopped with Q13/Q14, and waited about 7 hours for a human.
- `scripts/security_audit.sh:1160-1174` sends Codex stderr to `${CODEX_ERROR_FILE}`. On failure it calls `security_audit_emit_path_diagnostic` (`:53-61`), which prints only `No such file`-style lines, and then `security_audit_emit_failure` (`:40-51`).
- `security_audit_sanitize_log_value` (`:25-38`) already flattens whitespace, strips non-printables, masks URL userinfo, `Authorization:`, `Bearer`, `github_pat_` / `gh[pousr]_`, and `sk-or-` values, and returns a `printf %q`-quoted value.
- `codex exec` echoes the prompt to stderr. `tests/test_security_audit_workflow_contract.py:316-330` (`_assert_security_audit_failure_context`) asserts that failure output never contains the prompt text (`Chief Security Officer`) or the test `OPENROUTER_API_KEY` / `GH_TOKEN` values.
- The orchestrator's security pass runs the same script (`scripts/orchestrate_poll_process.sh:6935-6938`) and keeps its stderr in a file. Nothing parses the `security-audit: phase=` line (repo-wide grep), so adding a field breaks no reader.
- Related: #4867 (the blocker that asked for this), #5773 (provider-outage handling that will consume `provider=`).

## Goals

- On a `codex-execution` failure, the job log contains a block framed by `security-audit: codex-stderr-tail begin …` and `security-audit: codex-stderr-tail end`. The block holds at most the last 40 non-blank stderr lines and at most 4096 bytes of sanitized line content.
- Every printed tail line is masked for `sk-…` keys, long hex runs (32+), long base64/token runs (40+), and the literal values of secret-named environment variables. It then passes through `security_audit_sanitize_log_value`.
- Lines that echo the rendered prompt verbatim are not printed.
- The `phase=codex-execution` failure line ends with `provider=<402|401|429|5xx|unknown>`.
- A successful Codex run prints no tail. Every other failure line is unchanged.

## Non-goals

- Retrying or resuming on a provider outage (#5773).
- Changing the failure output of any other phase, or the shared sanitizer.
- Changing `security-audit.yml`, the consumer wrapper, or the orchestrator's security pass.

## Constraints

- §1: never print the raw stderr file, environment values, or the Codex config. Mask first, then sanitize, then cap (issue item 3).
- §5: minimal change. Only the `codex-execution` failure branch and one optional argument to `security_audit_emit_failure` change.
- §6: no rename. `security_audit_emit_failure` keeps its three required arguments and gains an optional fourth. New function names are unique in the script: `security_audit_mask_stderr_line`, `security_audit_emit_codex_stderr_tail`, `security_audit_classify_codex_provider`.
- §9: tabs in the bash script and Python tests.
- §15: no new GitHub API calls.
- §20: one `changelog.d/` fragment. §7: one `agents.md` note.

## Approach

Add three helpers to `scripts/security_audit.sh`. Call them only in the `codex-execution` failure branch:

1. **`security_audit_mask_stderr_line <line>`** (pure bash plus one `sed`) masks:
   - the literal value of every exported variable whose name matches `(TOKEN|PAT|API_KEY|SECRET|PASSWORD)` and is 8 or more characters long, using bash `${line//"$value"/[redacted]}`, so values never appear in a process argv;
   - `sk-` keys at a word boundary;
   - hex runs of 32 or more characters;
   - token runs `[A-Za-z0-9+_-]{40,}={0,2}`;
   - padded base64 `[A-Za-z0-9+/]{40,}={1,2}`.
   It prints the masked line. The caller then passes it through `security_audit_sanitize_log_value`, which adds the existing masks and `%q` quoting.
2. **`security_audit_emit_codex_stderr_tail <stderr-file> <prompt-file>`**:
   - reads at most the last 64 KiB of the stderr file (`tail -c`). When the file is larger, it replaces the leading non-space run of the cut first line with `[cut]`, so a token cut at the boundary is never printed without its prefix and the rest of that line still counts;
   - drops blank lines and lines that exactly match a line of the rendered prompt (`grep -vxF -f`);
   - keeps the last 40 lines, then masks and sanitizes each one;
   - walks from the newest line back, keeping lines while their total sanitized size is at most 4096 bytes. When the newest line alone is larger, it keeps the first 4096 bytes of that line;
   - prints `begin lines=<n> omitted_lines=<m>`, one `security-audit: codex-stderr-tail line=<value>` per kept line (oldest first), then `end`, all on stderr. Every line starts with `security-audit:`, so no line can start a GitHub Actions `::` workflow command;
   - sets the provider class (helper 3) from the same filtered 40-line window.
3. **`security_audit_classify_codex_provider`** scans the window case-insensitively. The latest matching line wins. Within one line the order is 402, 401, 429, 5xx:
   - **402**: `payment required`, `insufficient credits`, or a 402 status;
   - **401**: `unauthorized`, or a 401 status;
   - **429**: `too many requests`, `rate limit` (but not `rate limiting`), or a 429 status;
   - **5xx**: `internal server error`, `bad gateway`, `service unavailable`, `gateway timeout`, or a `5[0-9][0-9]` status.
   A numeric code counts only after `http`, `status`, `code`, or `error`, with at most 16 non-alphanumeric characters between them. Anything else is `unknown`.

`security_audit_emit_failure` gains an optional fourth argument, `provider`. When it is non-empty, ` provider=<sanitized value>` is appended to the line. The `codex-execution` branch then runs:
- `security_audit_emit_path_diagnostic` (unchanged);
- the tail;
- the failure line with `provider=`;
- `exit` with the original status.

Every helper is fail-open: an unreadable stderr file prints `begin lines=0 omitted_lines=0` / `end` and `provider=unknown`, and the original exit status is always preserved.

Alternatives considered:
- Extending the shared sanitizer would redact commit SHAs in other failure lines (AD-1).
- Relaxing the existing no-prompt-text test was rejected (AD-2).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan. The change is one function group in one script plus its tests, docs, and changelog fragment, and it cannot be split into independently useful PRs.

1. **Phase 1: Codex stderr tail and provider class on codex-execution failures.**
   - Files: `scripts/security_audit.sh`, `tests/test_security_audit_workflow_contract.py`, `agents.md`, `changelog.d/5785-security-audit-codex-stderr-tail.md`.
   - Done when the extended contract tests pass under `python3 tests/test_security_audit_workflow_contract.py`, `bash -n scripts/security_audit.sh` is clean, and the success path still prints nothing on stderr.
   - Rollback: revert the phase PR. No state, workflow, or consumer change is involved.

## Implementation Steps

1. `scripts/security_audit.sh:40-51`: add the optional `provider` argument to `security_audit_emit_failure`.
2. `scripts/security_audit.sh` (after `:61`): add `security_audit_mask_stderr_line`, `security_audit_classify_codex_provider`, and `security_audit_emit_codex_stderr_tail`.
3. `scripts/security_audit.sh:1169-1174`: call the tail helper, then emit the failure line with `provider=`.
4. `tests/test_security_audit_workflow_contract.py`: update the existing codex-failure test, and add tests for:
   - the 402 tail with an `sk-` key masked and `provider=402`;
   - a large stderr capped to 40 lines and 4096 bytes;
   - prompt-echo lines dropped;
   - the 401, 429, 5xx, and unknown classes;
   - secret environment values masked;
   - success printing no tail.
5. `agents.md` (security-audit bullet near line 2173): add one note on the failure output.
6. `changelog.d/5785-security-audit-codex-stderr-tail.md`: add a `changed` fragment.

## Files & Modules

- `scripts/security_audit.sh`
- `tests/test_security_audit_workflow_contract.py`
- `agents.md`
- `changelog.d/5785-security-audit-codex-stderr-tail.md` [new]
- `docs/plans/issue-5785-security-audit-codex-stderr-tail-plan.md` [new]
- `docs/implement-plan/issue-5785-security-audit-codex-stderr-tail.md` [new]

## Tests

- Unit and contract tests in `tests/test_security_audit_workflow_contract.py`, which already runs in the `ci.yml` "Validation self-test unit tests" step. The tests use its mock `codex` (`MOCK_CODEX_STDERR`, `MOCK_CODEX_EXIT_CODE`):
  - **402 case**: the tail is present, the fake `sk-or-v1-…` key is absent while `[redacted]` is present, the failure line ends with `provider=402`, and the exit status is preserved.
  - **Cap case**: 200 lines of 300 bytes give at most 40 tail lines, at most 4096 bytes of content, and `omitted_lines>0`.
  - **Prompt-echo case**: a stderr line copied from the rendered prompt is not printed.
  - **Class cases**: 401, 429, 503 (`5xx`), and unknown.
  - **Env case**: the `OPENROUTER_API_KEY` / `GH_TOKEN` values are never printed.
  - **Success**: stderr stays empty (existing `test_security_audit_success_path_retains_codex_and_tracker_behavior`).
- `bash -n scripts/security_audit.sh`, plus `shellcheck` when installed.

## Risks & Mitigations

- A secret that matches no mask (short, unusual shape) could be printed. Mitigation: the literal values of secret-named environment variables are masked, the token-run masks cover random keys, and Codex does not print its config. ACCEPTED: unknown secret shapes not held in the environment.
- Classification false positives, for example audit reasoning text that says "rate limit". Mitigation: phrase and status-context matching, the prompt-echo filter, and latest-line-wins.
- Masking can hide useful long identifiers, such as SHAs in the tail. ACCEPTED: the tail is a diagnostic, and the masks apply to the tail only.

## Rollout

The change ships with the next `@stable` sync. Consumer runs stage `scripts/` from the `@stable` support checkout, so they pick it up with no wrapper change. There is no flag. Roll back by reverting the PR.

## Auto-decisions

- AD-1 [plan, 2026-10-01] Where should the extra masks (`sk-`, long hex/base64, secret environment values) live?
  - Picked: A, a tail-only `security_audit_mask_stderr_line` applied before the existing `security_audit_sanitize_log_value`.
  - Alternatives: B, extend the shared sanitizer, which would also redact SHAs and paths in every other failure line; C, use only the existing sanitizer, which misses generic `sk-` keys and the test environment values.
  - Why: this meets issue item 1 without changing any other phase's output (§5).
  - Applied in: phase 1 PR. Status: pending review.
- AD-2 [plan, 2026-10-01] `codex exec` echoes the prompt to stderr, and the existing contract test forbids prompt text and environment values in failure output. How should the tail respect that?
  - Picked: A, drop tail lines that exactly match a rendered-prompt line, and mask the literal values of secret-named environment variables.
  - Alternatives: B, relax the existing test assertions for `codex-execution`.
  - Why: this keeps the existing invariant and issue item 3 ("never print … environment values"), and stops prompt text from skewing the provider class.
  - Applied in: phase 1 PR. Status: pending review.
- AD-3 [plan, 2026-10-01] How should provider failures be matched?
  - Picked: A, reason phrases plus 401/402/429/5xx codes only in `http|status|code|error` context; the latest matching line wins, and within a line the order is 402, 401, 429, 5xx.
  - Alternatives: B, match bare numbers anywhere in the tail.
  - Why: bare 3-digit numbers (token counts, ids) would misclassify, and the newest error is the cause.
  - Applied in: phase 1 PR. Status: pending review.
- AD-4 [plan, 2026-10-01] Where should the tests go?
  - Picked: A, extend `tests/test_security_audit_workflow_contract.py`.
  - Alternatives: B, a new `tests/test_security_audit_codex_stderr_tail.py` registered in `ci.yml`.
  - Why: the file already has the fake-Codex harness, already runs in CI, and holds the codex-failure test that must change anyway (§5).
  - Applied in: phase 1 PR. Status: pending review.
- AD-5 [plan, 2026-10-01] The issue asks for an `agents.md` note only if the security-audit section describes failure output, and it does not. Add one?
  - Picked: A, add a one-sentence note to the security-audit bullet.
  - Alternatives: B, no `agents.md` change.
  - Why: §7 requires documenting failure-mode changes.
  - Applied in: phase 1 PR. Status: pending review.
- AD-6 [plan, 2026-10-01] How should input be bounded and the 4 KiB cap applied?
  - Picked: A:
    - read at most the last 64 KiB, replacing the cut leading token of the first line with `[cut]` when the file is larger;
    - drop blank lines;
    - count the 4096-byte budget over sanitized line content, keeping the newest lines;
    - truncate a single over-budget newest line to its first 4096 bytes.
  - Alternatives: B, cap the raw bytes before sanitizing.
  - Why: issue item 3 says sanitize first, then cap; replacing the cut token stops a boundary-cut token from escaping the prefix masks, and keeps the error text of a single over-long line.
  - Applied in: phase 1 PR. Status: pending review.

## References

- Issue #5785; blocker #4867 (comment 5916157551); #5773.
- Runs 36748018397, 36749008453, 36754588874.
