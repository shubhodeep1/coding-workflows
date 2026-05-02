Download the full logs from every URL in `$ARGUMENTS`, identify the root cause, and ship a fix. `$ARGUMENTS` may contain **one or more log URLs** (newline- or whitespace-separated; mix and match accepted) and an **optional free-form description** of what the user expects you to focus on (suspected cause, what changed recently, which step failed, etc.). The description may appear before, between, or after the URLs.

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS` and fetch every log.** Extract every `https?://...` token as a log URL; treat all remaining (non-URL) text as the user's free-form description. Save the description verbatim — it shapes prioritisation in steps 2–3 (what to focus on, which leads to chase first) but never overrides evidence. If `$ARGUMENTS` contains zero URLs, stop and ask the user for at least one log URL — for ref/ID/prose-only inputs, use `/investigate-issue` instead.

   For each URL: `curl --fail-with-body -sSL -o /tmp/<name>.log -w '%{http_code}\n' <url>` and check the status. Use a **distinct `<name>` per URL** (e.g. derived from run/job ID, or numbered `log-1.log`, `log-2.log`) so concurrent downloads don't overwrite each other. Retry transient errors (5xx, 429, timeouts, DNS, connection reset) with exponential backoff (up to 4 retries: 2s, 4s, 8s, 16s). Hard failures (401/403/404/410, auth-walled) → record the URL under `Artifacts needed`; stop only if all URLs are inaccessible. For GitHub Actions run/job URLs, use `mcp__github__get_workflow_run_logs` / `mcp__github__get_job_logs` instead — `curl` returns HTML. Reference log lines via `nl -ba /tmp/<name>.log` so `L<n>` citations are stable; when multiple logs are in play, cite as `log-<n> L<line>` to disambiguate.
2. **Read every log end to end.** No skimming. Read each downloaded log in the order the user provided them (the first URL is usually the primary failure; subsequent ones are corroborating runs, retries, or related jobs). Note errors, stack traces, exit codes, timeouts, dep mismatches, and any references (PRs, issues, SHAs, run IDs, artifact URLs). Use the user's description to prioritise which signals to chase first, but don't let it cause you to skip parts of any log.
3. **Investigate.** Follow every relevant reference: PRs/issues via `mcp__github__pull_request_read` / `mcp__github__issue_read`; workflow YAML; `git blame` / `git log -p` on files in the stack trace; recent commits intersected with implicated files. Read the actual source — never guess at code.
4. **Decide and act.** Apply the [Decision Rule](#decision-rule) below.

## Decision Rule

After investigation, classify each finding as **EVIDENCE-BASED** (fully supported by log + code, plus reproduction when feasible) or **HYPOTHESIS** (plausible but unverified). Then:

- **All findings are EVIDENCE-BASED and no missing/inaccessible resource blocks root cause or fix verification** → design the fix, apply it, verify it (re-run the repro / failing test when feasible), commit, push, open a PR. Do not ask. Report using the [Output Format](#output-format) afterward. Non-blocking gaps (e.g. an inaccessible linked PR that doesn't affect the diagnosis) still get listed under `Artifacts needed` for transparency.
- **Otherwise** (any HYPOTHESIS finding, or any missing/inaccessible resource that blocks root cause or fix verification) → stop before editing. Report using the [Output Format](#output-format) and ask the user how to proceed.
- **Environmental failure** (service down, rate limit, runner outage) → no fix; say so explicitly.

## Output Format

Keep it tight. No prose padding.

```
Summary: <1–2 lines: what failed, root cause; mention how many logs were analysed and the user's description in one phrase>

Evidence-based:
- <claim> — log-<n> L<line>: "<text>" (or just L<line>: "<text>" if only one log); <file>:<line>; <SHA/PR if applicable>

Hypothesis (if any):
- <claim> — gap: <what's missing to confirm>

Artifacts needed (if any):
- <exact URL or path> — <what's needed, what's blocked without it>

Fix: <applied / proposed>
- <file>:<line> — <one-line rationale>
```

Omit empty sections. If the fix was applied and pushed, include the branch/PR link in the Fix line.

## Rules

- Download the complete log; never truncate. When `$ARGUMENTS` lists multiple URLs, this rule applies to each — download every accessible log in full before drawing conclusions.
- Use `mcp__github__*` for all GitHub interactions (no `gh` CLI). Repo scope: `shubhodeep1/coding-workflows`.
- No citation → no claim. No claim → no fix.
- Forbidden silent moves: editing tests to pass without evidence the test is wrong, broadening `except`/`catch`, suppressing warnings, version-bumping without verified compatibility, adding retries to mask deterministic failures.
- Address the root cause (first meaningful error), not cascading failures. Multiple independent failures → handle each separately.
- Cleanup: delete every temp log written to `/tmp/` during step 1 when done.
