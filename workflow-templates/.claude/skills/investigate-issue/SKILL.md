---
name: investigate-issue
description: Investigate a PR, issue, or workflow log from a consumer repo by tracing back through the stable-tagged version of shubhodeep1/coding-workflows (workflows, scripts, prompts, related PRs/issues/runs/comments) and produce an evidence-backed proposed fix. Read-only — never edits files. The user takes the proposed fix to a session in the appropriate target repo based on the fix label — `shubhodeep1/coding-workflows` for `[UPSTREAM]` fixes, the consumer repo for `[CONSUMER]` fixes, or both for `[BOTH]` fixes — to verify and implement.
---

# investigate-issue

Investigate a problem reported against this consumer repo whose root cause likely lives in the upstream workflow library `shubhodeep1/coding-workflows`. Trace back through every relevant reference (PRs, issues, Actions logs, comments, commits) and produce an evidence-backed proposed fix. Investigation reads against the **stable-tagged** version of the upstream repo, not `main`.

This skill is **read-only**. It produces a structured report; it never edits files in this consumer repo. Take the proposed fix to a session in `shubhodeep1/coding-workflows` to verify and implement.

$ARGUMENTS

## Steps

1. **Parse the input** — `$ARGUMENTS` is free-form prose. Scan it for every actionable lead:
   - GitHub PR URLs / `#1234` references / `owner/repo#1234` references
   - GitHub issue URLs / `#1234` references
   - Actions log URLs (e.g. `https://github.com/<owner>/<repo>/actions/runs/<id>`, raw log URLs, job log URLs)
   - Workflow run IDs, job IDs, artifact URLs
   - Commit SHAs (full or short)
   - Branch names, tag names
   - File paths and stack traces
   - Error messages quoted in prose

   Restate the parsed leads back to the user in the **Summary** so any miss is visible.

2. **Resolve the upstream ref the consumer is actually running** — All subsequent reads of `shubhodeep1/coding-workflows` MUST be pinned to the ref the consumer's failing run was executing. Pinning to the *current* `stable` tag without checking the consumer's pin is wrong: if the consumer is pinned to `@v1.2.3` and `stable` has since moved to `@v1.3.0`, reading at `stable` analyses code the consumer is not running. Resolve once, up front, into **two distinct fields**:

   - `UPSTREAM_TAG` — the human-readable label the consumer is pinned to (e.g. `v1.2.3`, `stable`, `main`, or the literal SHA if pinned by SHA). Used for citations and the report.
   - `UPSTREAM_SHA` — the resolved commit SHA the tag/branch/ref points at. **This is the single canonical value passed as `ref=` to every GitHub MCP call.** Pinning to the SHA (not the tag name) makes the analysis stable even if a moving pointer like `stable` is updated mid-investigation.

   Resolution procedure:

   - **First, inspect the consumer's wrapper YAML** (the workflows under `.github/workflows/<name>.yml` in this repo, plus any composite actions they delegate to) to find every `uses:` / `repository:` / `ref:` entry that references `shubhodeep1/coding-workflows`. The exact `ref` (tag, branch, or SHA) is the consumer's pin and is the authoritative input.
     - If the wrapper pins a specific tag (e.g. `@v1.2.3`) → `UPSTREAM_TAG = v1.2.3`; resolve to `UPSTREAM_SHA` via `mcp__github__get_tag` (or `list_tags`).
     - If the wrapper pins a SHA directly → `UPSTREAM_TAG = <short-sha>`; `UPSTREAM_SHA = <full-sha>`.
     - If the wrapper pins the moving `stable` pointer (e.g. `@stable`) → `UPSTREAM_TAG = stable`; resolve `UPSTREAM_SHA` via `mcp__github__list_tags` (this repo uses a moving `stable` pointer set by `scripts/mark-stable.sh`).
     - If the wrapper pins a branch (e.g. `@main`) → `UPSTREAM_TAG = main`; set `UPSTREAM_SHA` to the SHA of the failing workflow run's head commit (read from the run metadata, not the current branch tip). Note this case explicitly under **Open Questions** because the consumer is not on a stable release.
   - **Fallback when the consumer pin cannot be determined** (e.g. the wrapper file is inaccessible after retries): try `mcp__github__list_tags` for `stable`, else the highest semver tag (`vX.Y.Z`), else `mcp__github__get_latest_release`. Set `UPSTREAM_TAG` and `UPSTREAM_SHA` from whichever succeeded. Record the fallback path and the assumption under **Open Questions**.
   - **Also resolve `PREVIOUS_UPSTREAM_TAG` and `PREVIOUS_UPSTREAM_SHA`** — the tag immediately preceding `UPSTREAM_TAG` in semver order, plus its resolved SHA. List tags with `mcp__github__list_tags`, sort by semver, pick the entry below `UPSTREAM_TAG`. This is needed in Step 4 to scope the regression search ("changes merged between `PREVIOUS_UPSTREAM_SHA` and `UPSTREAM_SHA`"). If `UPSTREAM_TAG` is the lowest tag (or is a branch / direct SHA), set both `PREVIOUS_*` fields to null and skip the regression-search step.
   - Record `UPSTREAM_TAG`, `UPSTREAM_SHA`, `PREVIOUS_UPSTREAM_TAG`, `PREVIOUS_UPSTREAM_SHA` in the **Evidence Ledger**. Every later upstream tool call MUST pass `ref=<UPSTREAM_SHA>`. Every citation should be written as `<owner>/<repo>@<UPSTREAM_TAG> (<short-sha>):<file>:<line>` so the reader sees both the human label and the canonical SHA.
   - If `UPSTREAM_SHA` cannot be resolved at all after retries (see **Retry Rule**), record this under **Inaccessible Resources** and stop — the skill cannot produce a pinned analysis without a SHA. Do not silently fall back to `main`.

3. **Download any logs** referenced in the input — Choose the fetch tool by URL shape; `curl` against a rendered GitHub page returns HTML, not log content.
   - **GitHub Actions run / job URLs** (e.g. `https://github.com/<owner>/<repo>/actions/runs/<id>`, `.../runs/<id>/job/<id>`, `.../runs/<id>/attempts/<n>`): use the appropriate GitHub MCP tool (e.g. `mcp__github__get_workflow_run_logs`, `mcp__github__get_job_logs`, or whichever workflow-log tool is exposed in the current session — search `mcp__github__*` for `log`). These return the raw log payload. Do NOT `curl` these URLs.
   - **Raw log URLs / artifact URLs / external (non-GitHub) URLs**: use `curl --fail-with-body -sSL -o /tmp/<unique-name>.log -w '%{http_code}\n' <url>` so HTTP errors are detected reliably; plain `curl -sL` exits 0 on 4xx/5xx and silently downloads the server's error page.

   Verify the status / payload before treating the result as a log:
   - `2xx` → proceed.
   - `5xx`, `429`, network/timeout/connection-reset/DNS errors → transient; retry per the **Retry Rule**.
   - `401`, `403`, `404`, `410` → hard failure; record under **Inaccessible Resources** and follow the **Inaccessible resources** rule.

   Read each log **in full**. Do not skim. Note: error messages, stack traces, exit codes, OOM/signal kills, timeouts, deprecation warnings, dependency mismatches, environment/config issues, and every embedded reference to PRs / issues / commits / run IDs / artifact URLs — these are leads to follow in Step 4.

4. **Expand the investigation** — Before proposing any fix, follow every lead. Be exhaustive about the upstream side because that is what the user can actually change.

   **In this consumer repo (current working directory):**
   - PRs / issues referenced → fetch via `mcp__github__pull_request_read` / `mcp__github__issue_read`. Read description, comments, linked issues, review threads, status checks.
   - Workflow YAML used by the failing run → read the consumer-repo wrapper at `.github/workflows/<name>.yml` to identify which upstream reusable workflow / action / script it calls and at what ref.
   - `.github/ai/consumer_repos.json` and any consumer-side config that affects which upstream behaviour is active.
   - `git blame` / `git log -p -- <file>` on consumer-repo files in stack traces.
   - Other recent runs of the same workflow (regression vs. flake).

   **In the upstream `shubhodeep1/coding-workflows` repo, pinned to `UPSTREAM_SHA`:**
   - Use `mcp__github__get_file_contents` with `ref=<UPSTREAM_SHA>` for every read. Never read upstream files at `main` — a fix proposed against `main` may not apply against the consumer's pinned wrapper.
   - Reusable workflow that the consumer wrapper calls (under `workflow-templates/` or `.github/workflows/`).
   - Scripts invoked by that workflow (under `scripts/`).
   - Prompt files invoked by those scripts (under `prompts/`).
   - Any contracts (`db/contracts/*.yml`), agents config (`agents.md`), or `codex_system_instructions.md` rules referenced by the failing path.
   - PRs / issues in `shubhodeep1/coding-workflows` that touched the relevant files between `PREVIOUS_UPSTREAM_SHA` and `UPSTREAM_SHA` (resolved in Step 2) — these are the most likely culprits if the regression is upstream-introduced. Use `mcp__github__list_commits` with `path=<file>` (and a `since` / `sha` window bounded by the two refs) and `mcp__github__search_issues` / `mcp__github__search_pull_requests` scoped to the repo. If `PREVIOUS_UPSTREAM_SHA` is null (Step 2 found `UPSTREAM_TAG` was the lowest tag, or the consumer is pinned to a branch / direct SHA), skip this regression-window search and note it under **Open Questions**.
   - Cross-reference: intersect files changed in upstream PRs merged between `PREVIOUS_UPSTREAM_SHA` and `UPSTREAM_SHA` with the files implicated by the consumer-side stack trace / log.

   **Retry transient errors** (5xx, 429, timeouts, connection resets, DNS failures) on every fetch — log download, GitHub MCP, raw HTTP — per the **Retry Rule**. A transient blip is not an excuse to give up on a lead.

5. **Identify every distinct issue** — List each unique issue. For each:
   - Exact error message or log line (with line number in the saved log file)
   - Root cause, not symptom
   - Whether it is the primary failure or a cascading/secondary failure
   - Whether the root cause is **upstream** (in `shubhodeep1/coding-workflows@<UPSTREAM_TAG> (<short-sha>)`), **consumer-side**, or **environmental** (runner / network / external service)

6. **Correlate to source code** — Locate the exact files and lines responsible. **Read the actual files at `UPSTREAM_SHA`.** Never guess at code structure, function signatures, env vars, or workflow inputs. Cite upstream files as `<owner>/<repo>@<UPSTREAM_TAG> (<short-sha>):<path>:<line>`; cite consumer-repo files as `<path>:<line>`.

7. **Build the Evidence Ledger** — Every claim and every proposed fix must cite:
   - `UPSTREAM_TAG`, `UPSTREAM_SHA`, `PREVIOUS_UPSTREAM_TAG`, `PREVIOUS_UPSTREAM_SHA` (the four fields resolved in Step 2) once at the top
   - Log line number(s) and exact text (with the `/tmp/` path of the saved log)
   - Source location: `<repo>@<UPSTREAM_TAG> (<short-sha>):<file>:<line>` for upstream; `<file>:<line>` for consumer
   - Commit SHA / PR # that introduced the relevant code (when applicable)
   - Test output or reproduction result (when applicable)

   No citation → no claim. No claim → no fix.

8. **Attempt reproduction (where feasible)** — If the failure can be reproduced locally without privileged credentials, run the failing command/test and record the result. A failure to reproduce is itself evidence (environment-specific, flake, cache-poisoned, runner-specific, auth-walled) — surface it; do not paper over it.

9. **Propose fixes — labeled by confidence and target repo** — For each proposed change:
   - `EVIDENCE-BASED` — fully supported by log + code reading at the pinned ref + (where applicable) reproduction.
   - `HYPOTHESIS` — plausible but unverified. Surface it, explain the gap, and ask before acting on it.

   For each fix, also label the **target repo**:
   - `[UPSTREAM]` — change must be made in `shubhodeep1/coding-workflows`. This is what the user will paste into a session opened against that repo.
   - `[CONSUMER]` — change must be made in this consumer repo (e.g. wrapper YAML, pinned ref, secret).
   - `[BOTH]` — coordinated change required.

   Do **NOT** apply fixes. This skill is read-only. Surface the proposed diff (as a fenced code block with `file:line` anchors) and let the user open a session in the appropriate repo to implement it.

10. **Final output structure** — Always produce, in this order:
    - **Summary** (1–3 lines, including the parsed leads from Step 1 and `UPSTREAM_TAG (UPSTREAM_SHA)`)
    - **Evidence Ledger** (numbered)
    - **Root Cause(s)** — confidence label, plus a target-repo label only when the root cause is a code defect (`[UPSTREAM]` / `[CONSUMER]` / `[BOTH]`). Environmental root causes are marked non-actionable instead (see Rules) and carry no target-repo label.
    - **Proposed Fix(es)** with `<repo>@<UPSTREAM_TAG> (<short-sha>):<file>:<line>`, target-repo label, confidence label, and rationale. Environmental issues do not get a Proposed Fix entry — they are surfaced under **Reproduction Result** / **Open Questions** instead.
    - **Inaccessible Resources** — exact URLs the user must open manually, with what's needed from each and what conclusion is blocked without it
    - **Reproduction Result**
    - **Open Questions** — surface remaining ambiguity rather than guessing
    - **Next Step for the User** — one-sentence instruction telling the user which repo to open a session against (`shubhodeep1/coding-workflows` for `[UPSTREAM]` fixes, this repo for `[CONSUMER]` fixes, both for `[BOTH]` fixes) and a copy-paste-ready prompt summarising the proposed change. If the only finding is environmental, instruct the user to re-run after the environment recovers and explain why no code change is recommended.

11. **Cleanup** — Remove temp log files from `/tmp/` when done.

## Rules

- **Read-only.** This skill never edits files in the consumer repo. It produces a report. The user takes `[UPSTREAM]` fixes to a session in `shubhodeep1/coding-workflows`; the user takes `[CONSUMER]` fixes to a session in this repo. If during investigation you would normally apply an `EVIDENCE-BASED` edit, do **not** — emit it as a proposed fix instead.
- **Always pin upstream reads to `UPSTREAM_SHA`.** Reading upstream files at `main` is a bug — the consumer is not running `main`. Every `mcp__github__get_file_contents` call against `shubhodeep1/coding-workflows` MUST pass `ref=<UPSTREAM_SHA>` (the resolved SHA from Step 2, never the bare tag name — moving tags like `stable` can shift mid-investigation).
- **Always download the complete log.** Never truncate or skip sections.
- **Retry Rule**: For transient HTTP/GitHub errors (5xx, 429, timeouts, connection resets, DNS failures), retry with exponential backoff (2s, 4s, 8s, 16s — up to 4 retries) before declaring failure. Applies to every fetch: the log download, GitHub MCP calls, raw HTTP follow-ups.
- **Inaccessible resources** — If a resource is still unreachable after retries, or returns a hard failure (401, 403, 404, 410), or is auth-walled / expired / private, record it under **Inaccessible Resources**:
  - The exact URL
  - What is needed from it
  - What conclusion is blocked without it

  Stop that specific line of inquiry, but continue the broader analysis if the primary log + `UPSTREAM_SHA` are accessible and the root cause / proposed fix is still supported by available evidence. Do not make claims that depend on the inaccessible content — surface those gaps under **Open Questions** instead. Abort the analysis only if (a) the primary log itself is inaccessible, (b) `UPSTREAM_SHA` cannot be resolved, or (c) the missing resource blocks the root-cause conclusion.
- **Use the GitHub MCP tools (`mcp__github__*`) for all GitHub interactions.** The `gh` CLI is not assumed available. The skill operates against whatever scope the host session permits; it does not attempt to widen scope.
- **Prioritise the root cause** — the first meaningful error in the log — over cascading failures.
- **Multiple independent failures** → address each separately, each with its own evidence and proposed fix.
- **Environmental failures** (service down, rate limit, runner outage) — say so explicitly rather than proposing a code fix. Mark them as **environmental / non-actionable** (no target-repo label, no Proposed Fix entry — target-repo labels are reserved for `[UPSTREAM]` / `[CONSUMER]` / `[BOTH]` code defects) and recommend re-running once the environment recovers. Only assign a target-repo label if the evidence shows an actual upstream or consumer code defect underlying the environmental symptom (e.g. a missing retry around a transiently-flaky service).
- **Forbidden silent moves** (when articulating the proposed fix): modifying tests to make them pass (unless the test is genuinely wrong, with evidence), broadening `except`/`catch` blocks, suppressing warnings, version-bumping without verified compatibility, adding retries to mask deterministic failures, switching the consumer's pinned upstream ref to `main` to dodge a stable-tag bug.
- **No guessing.** Every diagnosis cites evidence; every fix is tied to a specific log line and source location at a specific ref. If you cannot find evidence, ask or surface the gap — do not invent it.
- **No scope creep.** Stay within the failure(s) implied by the input. Do not propose unrelated cleanups, refactors, or "while we're in here" changes.
