Download the full log from the URL below, identify the root cause, and ship a fix.

$ARGUMENTS

## Procedure

1. **Fetch the log.** Use `curl --fail-with-body -sSL -o /tmp/<name>.log -w '%{http_code}\n' <url>` and check the status. Retry transient errors (5xx, 429, timeouts, DNS, connection reset) with exponential backoff (up to 4 retries: 2s, 4s, 8s, 16s). Hard failures (401/403/404/410, auth-walled) → record the URL under `Artifacts needed` and stop only if it's the primary log. For GitHub Actions run/job URLs, use `mcp__github__get_workflow_run_logs` / `mcp__github__get_job_logs` instead — `curl` returns HTML. Reference log lines via `nl -ba /tmp/<name>.log` so `L<n>` citations are stable.
2. **Read it end to end.** No skimming. Note errors, stack traces, exit codes, timeouts, dep mismatches, and any references (PRs, issues, SHAs, run IDs, artifact URLs).
3. **Investigate.** Follow every relevant reference: PRs/issues via `mcp__github__pull_request_read` / `mcp__github__issue_read`; workflow YAML; `git blame` / `git log -p` on files in the stack trace; recent commits intersected with implicated files. Read the actual source — never guess at code.
4. **Decide and act.** Apply the [Decision Rule](#decision-rule) below.

## Decision Rule

After investigation, classify each finding as **EVIDENCE-BASED** (fully supported by log + code, plus reproduction when feasible) or **HYPOTHESIS** (plausible but unverified). Then:

- **All findings are EVIDENCE-BASED and no artifacts are missing** → design the fix, apply it, verify it (re-run the repro / failing test when feasible), commit, push, open a PR. Do not ask. Report using the [Output Format](#output-format) afterward.
- **Otherwise** (any HYPOTHESIS finding, any missing artifact, or any inaccessible resource) → stop before editing. Report using the [Output Format](#output-format) and ask the user how to proceed.
- **Environmental failure** (service down, rate limit, runner outage) → no fix; say so explicitly.

## Output Format

Keep it tight. No prose padding.

```
Summary: <1–2 lines: what failed, root cause>

Evidence-based:
- <claim> — log L<n>: "<text>"; <file>:<line>; <SHA/PR if applicable>

Hypothesis (if any):
- <claim> — gap: <what's missing to confirm>

Artifacts needed (if any):
- <exact URL or path> — <what's needed, what's blocked without it>

Fix: <applied / proposed>
- <file>:<line> — <one-line rationale>
```

Omit empty sections. If the fix was applied and pushed, include the branch/PR link in the Fix line.

## Rules

- Download the complete log; never truncate.
- Use `mcp__github__*` for all GitHub interactions (no `gh` CLI). Repo scope: `shubhodeep1/coding-workflows`.
- No citation → no claim. No claim → no fix.
- Forbidden silent moves: editing tests to pass without evidence the test is wrong, broadening `except`/`catch`, suppressing warnings, version-bumping without verified compatibility, adding retries to mask deterministic failures.
- Address the root cause (first meaningful error), not cascading failures. Multiple independent failures → handle each separately.
- Cleanup: delete the temp log from `/tmp/` when done.
