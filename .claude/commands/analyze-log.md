Download the full logs from every URL in `$ARGUMENTS`, identify the root cause, and ship a fix. `$ARGUMENTS` may contain **one or more log URLs** (newline- or whitespace-separated; mix and match accepted) and an **optional free-form description** of what the user expects you to focus on (suspected cause, what changed recently, which step failed, etc.). The description may appear before, between, or after the URLs.

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS` and fetch every log.** Extract every `https?://...` token as a log URL; treat all remaining (non-URL) text as the user's free-form description. Save the description verbatim — it shapes prioritisation in steps 2–3 (what to focus on, which leads to chase first) but never overrides evidence. If `$ARGUMENTS` contains zero URLs, stop and ask the user for at least one log URL — for ref/ID/prose-only inputs, use `/investigate-issue` instead.

   For each URL: `curl --fail-with-body -sSL -o /tmp/<name>.log -w '%{http_code}\n' <url>` and check the status. Use a **distinct `<name>` per URL** (e.g. derived from run/job ID, or numbered `log-1.log`, `log-2.log`) so concurrent downloads don't overwrite each other. Retry transient errors (5xx, 429, timeouts, DNS, connection reset) with exponential backoff (up to 4 retries: 2s, 4s, 8s, 16s). Hard failures (401/403/404/410, auth-walled) → record the URL under `Artifacts needed`; stop only if all URLs are inaccessible. For GitHub Actions run/job URLs, use `mcp__github__get_workflow_run_logs` / `mcp__github__get_job_logs` (or `gh run view --log <run-id>` / `gh run view --log --job <job-id>` if `GH_TOKEN` is set — see [Tool Access](#tool-access)) — `curl` against rendered Actions pages returns HTML, not log content. Reference log lines via `nl -ba /tmp/<name>.log` so `L<n>` citations are stable; when multiple logs are in play, cite as `log-<n> L<line>` to disambiguate.
2. **Read every log end to end.** No skimming. Read each downloaded log in the order the user provided them (the first URL is usually the primary failure; subsequent ones are corroborating runs, retries, or related jobs). Note errors, stack traces, exit codes, timeouts, dep mismatches, and any references (PRs, issues, SHAs, run IDs, artifact URLs). Use the user's description to prioritise which signals to chase first, but don't let it cause you to skip parts of any log.
3. **Investigate — keep pulling artifacts until the root cause is nailed down.** Follow every relevant reference: PRs/issues via `mcp__github__pull_request_read` / `mcp__github__issue_read`; workflow YAML; `git blame` / `git log -p` on files in the stack trace; recent commits intersected with implicated files. When a log points at another run / job / artifact / PR / issue, fetch that too — recursively — using the GitHub MCP tools or `gh` CLI (see [Tool Access](#tool-access)). Don't stop investigating because the *first* log was thin: if the root cause isn't yet evidence-based, the next step is to read more, not to guess. Read the actual source — never guess at code.
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

## Tool Access

GitHub reads can go through either of two equivalent paths — pick whichever is exposed in the current session:

- **`mcp__github__*` MCP tools** — always available. Preferred when the schema fits cleanly (e.g. `get_workflow_run_logs`, `get_job_logs`, `pull_request_read`, `issue_read`, `list_commits`, `get_file_contents`).
- **`gh` CLI** — available when `GH_TOKEN` or `GITHUB_TOKEN` is set in the session environment (the SessionStart hook installs `gh` and authenticates with it). **Verify auth state directly with `{ [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; } && gh auth status` (nounset-safe) — don't infer it from the SessionStart log.** That hook's secondary probe (`gh run list`) only checks `actions:read` for this repo and may emit a `NOTE` / `WARNING` even when `gh` works fine for PRs, issues, commits, and file contents; treat those messages as scope hints, not as "gh is dead." Use `gh` for one-off reads where the MCP surface is awkward, or for tools that don't have a clean MCP equivalent in this session — e.g. `gh run view --log <run-id>`, `gh run view --log --job <job-id>`, `gh run view --log-failed <run-id>` (failed-step lines only), `gh api repos/<owner>/<repo>/actions/runs/<id>/logs`. The token has read access across the listed repos, so you can fetch run logs, job logs, PRs, issues, commits, and file contents the same way the MCP tools do.

Repo scope: `shubhodeep1/coding-workflows`. If `gh auth status` succeeds, `gh` is usable; if a *specific* `gh` call returns 401/403/404 for one resource (GitHub returns 404 for many auth-walled / private resources, so treat it as a permission-or-visibility error rather than "resource missing"), or `gh` is missing entirely, fall back to the MCP tool for that call — don't conclude `gh` is broken globally and don't stop the investigation.

**Keep going until the diagnosis is evidence-based.** A single log rarely contains the whole story. If the root cause isn't yet supported by log + source citations, pull the next layer: re-fetch the run with `--log-failed`, fetch sibling/previous runs of the same workflow, fetch the linked PR/issue, fetch the workflow YAML, fetch artifacts. Only stop reading when (a) you have an evidence-based fix, (b) you've hit a [Decision Rule](#decision-rule) blocker that genuinely requires the user, or (c) every reasonable lead is exhausted and recorded under `Artifacts needed`.

## Rules

- Download the complete log; never truncate. When `$ARGUMENTS` lists multiple URLs, this rule applies to each — download every accessible log in full before drawing conclusions.
- Prefer `mcp__github__*` for GitHub reads; fall back to `gh` CLI when `GH_TOKEN` is set and the MCP surface is awkward (see [Tool Access](#tool-access)). Repo scope: `shubhodeep1/coding-workflows`.
- No citation → no claim. No claim → no fix.
- Forbidden silent moves: editing tests to pass without evidence the test is wrong, broadening `except`/`catch`, suppressing warnings, version-bumping without verified compatibility, adding retries to mask deterministic failures.
- Address the root cause (first meaningful error), not cascading failures. Multiple independent failures → handle each separately.
- Cleanup: delete every temp log written to `/tmp/` during step 1 when done.
