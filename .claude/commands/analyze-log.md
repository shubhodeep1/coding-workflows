Download the full log from the following URL and analyze it for errors, failures, and issues. Investigate to the depth required to identify the true root cause — follow every relevant reference (issues, PRs, related runner logs, related commits) and propose fixes only on evidence, never on speculation.

$ARGUMENTS

## Steps

1. **Download the log** — Use `curl -sL` to download the raw log content from the provided URL. Save it to a temporary file in `/tmp/` so you can reference it throughout the analysis. If the download fails with a transient error, retry according to the **Retry Rule** in the Rules section. For hard failures (401, 403, 404, 410), record the URL under **Inaccessible Resources** in the final output.

2. **Read the full log carefully** — Do not skim or summarize. Read the entire log from start to finish. Pay attention to:
   - Error messages, stack traces, exceptions
   - Failed assertions or test failures
   - Exit codes, signal kills, OOM errors
   - Timeout or connectivity failures
   - Deprecation warnings that may have escalated to errors
   - Mismatched dependency versions
   - Environment or configuration issues
   - References to PRs (`#1234`), issues, commit SHAs, branches, workflow run IDs, artifact URLs — these are leads to follow in Step 3.

3. **Expand the investigation** — Before proposing any fix, follow every relevant reference surfaced by the log. Do not stop at the log's surface:
   - **PRs / issues** referenced in commit messages, branch names, or log output → fetch with `mcp__github__pull_request_read` / `mcp__github__issue_read`. Read description, comments, linked issues, recent reviews.
   - **Workflow context** (for CI logs) → read the workflow YAML in `.github/workflows/`, the failing job's matrix entry, runner image, cache keys. Inspect related upstream/downstream jobs.
   - **Other runs of the same workflow** → check whether the same failure appears on `main` and on related branches (regression vs. flake).
   - **Failing files** in the stack trace → `git blame` and `git log -p -- <file>` to find the commit/PR that introduced the relevant code.
   - **Recent commits** → intersect files changed in recent commits/PRs with files in the stack trace to identify the likely culprit change.
   - **Retry transient errors when fetching from GitHub or any HTTP source** according to the **Retry Rule** in the Rules section. A transient blip is not an excuse to give up on the investigation.

4. **Identify every distinct issue** — List each unique issue. For each:
   - Exact error message or log line (with line number in the saved log file)
   - Root cause, not symptom
   - Whether it is the primary failure or a cascading/secondary failure

5. **Correlate to source code** — Locate the exact files and lines responsible. Use grep, paths from stack traces, and the references collected in Step 3. **Read the actual files.** Never guess at code structure or function signatures.

6. **Build the Evidence Ledger** — Every claim and every proposed fix must cite:
   - Log line number(s) and exact text
   - Source `file:line`
   - Commit SHA or PR # that introduced the relevant code (when applicable)
   - Test output or reproduction result (when applicable)

   No citation → no claim. No claim → no fix.

7. **Attempt reproduction** — Where feasible, run the failing test/command locally and record the result. A failure to reproduce is itself evidence (environment-specific, flake, cache-poisoned, runner-specific, etc.) — surface it; do not paper over it.

8. **Propose fixes — labeled by confidence** — For each proposed change:
   - `EVIDENCE-BASED` — fully supported by log + code reading + (where applicable) reproduction. Apply per Claude Code's normal edit flow.
   - `HYPOTHESIS` — plausible but unverified. Do **not** apply silently. Surface it, explain the gap, and ask before changing code.

9. **Final output structure** — Always produce, in this order:
   - **Summary** (1–3 lines)
   - **Evidence Ledger** (numbered)
   - **Root Cause(s)** with confidence label
   - **Proposed Fix(es)** with `file:line` and rationale
   - **Inaccessible Resources** — exact URLs the user must open manually, with what's needed from each and what conclusion is blocked without it
   - **Reproduction Result**
   - **Open Questions** — surface any remaining ambiguity rather than guessing

10. **Cleanup** — Remove the temp log file from `/tmp/` when done.

## Rules

- Always download the complete log. Never truncate or skip sections.
- **Retry Rule**: For transient HTTP/GitHub errors (5xx, 429, timeouts, connection resets, DNS failures), retry with exponential backoff (2s, 4s, 8s, 16s — up to 4 retries) before declaring failure. This applies to the initial log download **and** to every follow-up fetch (PRs, issues, workflow files, artifacts, related runs).
- **Inaccessible resources** — If a resource is still unreachable after retries, or returns a hard failure (401, 403, 404, 410), or is auth-walled / expired / private, record the following under **Inaccessible Resources** and continue the investigation with other available evidence:
  - The exact URL
  - What is needed from it
  - What conclusion is blocked without it

  Do not propose a fix whose correctness depends on guessed content from an inaccessible resource — surface that gap under **Open Questions** instead.
- **Use the GitHub MCP tools (`mcp__github__*`) for all GitHub interactions.** The `gh` CLI is not available in this environment. Repo scope is restricted to `shubhodeep1/coding-workflows`.
- Prioritize the root cause — the first meaningful error in the log — over cascading failures.
- If the log contains multiple independent failures, address each separately.
- If a failure is environmental (service was down, rate limit, runner outage) and no code change can fix it, say so explicitly rather than proposing a workaround.
- **Forbidden silent moves**: modifying tests to make them pass (unless the test is genuinely wrong, with evidence), broadening `except`/`catch` blocks, suppressing warnings, version-bumping without verified compatibility, adding retries to mask deterministic failures.
- **No guessing.** Every diagnosis cites evidence; every fix is tied to a specific log line and source location. If you cannot find evidence, ask or surface the gap — do not invent it.
