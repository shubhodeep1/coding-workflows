Download the full logs from every URL in `$ARGUMENTS` and analyze them for errors, failures, and issues. `$ARGUMENTS` may contain **one or more log URLs** (newline- or whitespace-separated; mix and match accepted) and an **optional free-form description** of what the user expects you to focus on (suspected cause, what changed recently, which step failed, etc.). The description may appear before, between, or after the URLs. This consumer-repo variant traces the failure back through the **upstream** workflow library (`shubhodeep1/coding-workflows`) at the ref the consumer is actually running, and produces an evidence-backed proposed fix biased toward the upstream repo. **Read-only — never edits files.** The user takes the proposed fix to a session in the appropriate target repo (`shubhodeep1/coding-workflows` for `[UPSTREAM]` fixes, this repo for `[CONSUMER]` fixes) to verify and implement.

$ARGUMENTS

## Steps

1. **Parse `$ARGUMENTS` and download every log** — Extract every `https?://...` token as a log URL; treat all remaining (non-URL) text as the user's free-form description. Record both up front in the **Evidence Ledger**: list every URL (numbered `log-1`, `log-2`, …) and quote the description verbatim under `User description:` (or note `none`). The description shapes prioritisation in Steps 3–4 (what to focus on, which leads to chase first); it does **not** override evidence — never invent facts to match it.

   If `$ARGUMENTS` contains zero URLs, stop and ask the user for at least one log URL — this command is for log-anchored analysis. (If the user has only refs/IDs/prose without a log, point them at `/investigate-issue`.)

   Then download EACH log. Choose the fetch tool by URL shape; `curl` against a rendered GitHub page returns HTML, not log content.
   - **GitHub Actions run / job URLs** (e.g. `https://github.com/<owner>/<repo>/actions/runs/<id>`, `.../runs/<id>/job/<id>`, `.../runs/<id>/attempts/<n>`): use the appropriate GitHub MCP tool (e.g. `mcp__github__get_workflow_run_logs`, `mcp__github__get_job_logs`, or whichever workflow-log tool is exposed in the current session — search `mcp__github__*` for `log`). These return the raw log payload. Do NOT `curl` these URLs.
   - **Raw log URLs / artifact URLs / external (non-GitHub) URLs**: use `curl --fail-with-body -sSL -o /tmp/<unique-name>.log -w '%{http_code}\n' <url>` so HTTP errors are detected reliably; plain `curl -sL` exits 0 on 4xx/5xx and silently downloads the server's error page.

   Use a **distinct `<unique-name>` per URL** (e.g. derived from run/job ID, or numbered `log-1.log`, `log-2.log`) so concurrent downloads don't overwrite each other. Track the local path for each log alongside its `log-<n>` index in the Evidence Ledger so later citations (`log-2 L42: "..."`) are unambiguous when multiple logs are in play.

   Verify the status / payload of each download before treating the result as a log:
   - `2xx` → proceed.
   - `5xx`, `429`, network/timeout/connection-reset/DNS errors → transient; retry per the **Retry Rule** in the Rules section.
   - `401`, `403`, `404`, `410` → hard failure; record the URL under **Inaccessible Resources** and follow the **Inaccessible resources** rule.

   If at least one log downloads successfully, continue with the accessible logs. If **all** provided URLs are inaccessible after retries, stop — there is nothing to analyse.

2. **Resolve the upstream ref the consumer is actually running** — All subsequent reads of `shubhodeep1/coding-workflows` MUST be pinned to the ref the consumer's failing run was executing. Pinning to the *current* `stable` tag without checking the consumer's pin is wrong: if the consumer is pinned to `@v1.2.3` and `stable` has since moved to `@v1.3.0`, reading at `stable` analyses code the consumer is not running. Resolve once, up front, into **two distinct fields**:

   - `UPSTREAM_TAG` — the human-readable label the consumer is pinned to (e.g. `v1.2.3`, `stable`, `main`, or the literal SHA if pinned by SHA). Used for citations and the report.
   - `UPSTREAM_SHA` — the resolved commit SHA the tag/branch/ref points at. **This is the single canonical value passed as `ref=` to every GitHub MCP call.** Pinning to the SHA (not the tag name) makes the analysis stable even if a moving pointer like `stable` is updated mid-investigation.

   Resolution procedure:

   - **First, identify which workflow run produced the log** (read run/job IDs from the log content or the input URL). Read the run metadata to find the consumer-repo workflow file that triggered it (`.github/workflows/<name>.yml` in this repo).
   - **Inspect that wrapper YAML** (and any composite actions it delegates to) for every `uses:` / `repository:` / `ref:` entry that references `shubhodeep1/coding-workflows`. The exact `ref` (tag, branch, or SHA) is the consumer's pin.
     - If the wrapper pins a specific tag (e.g. `@v1.2.3`) → `UPSTREAM_TAG = v1.2.3`; resolve to `UPSTREAM_SHA` via `mcp__github__get_tag` (or `list_tags`).
     - If the wrapper pins a SHA directly → `UPSTREAM_TAG = <short-sha>`; `UPSTREAM_SHA = <full-sha>`.
     - If the wrapper pins the moving `stable` pointer (e.g. `@stable`) → `UPSTREAM_TAG = stable`; resolve `UPSTREAM_SHA` via `mcp__github__list_tags` (this repo uses a moving `stable` pointer set by `scripts/mark-stable.sh`).
     - If the wrapper pins an upstream branch (e.g. `@main`) → `UPSTREAM_TAG = main`. Resolve `UPSTREAM_SHA` to the upstream branch's commit at the time the failing run executed — **NOT** the consumer run's `head_sha` (that's a SHA in the consumer repo and will 404 against `shubhodeep1/coding-workflows`):
       1. Read the failing run's start timestamp from the run metadata.
       2. Use `mcp__github__list_commits` with `sha=<upstream-branch>` (and an `until=<run-start-time>` filter if the tool exposes one; otherwise paginate and pick the first commit whose committer date is `≤` the run-start time). That SHA is `UPSTREAM_SHA`.
       3. If the historical lookup is not feasible (timestamp missing, API constraints), fall back to the upstream branch's current tip SHA. Note explicitly that the branch may have moved since the run.
       In all branch-pinned cases, record the precise vs. fallback path in the Evidence Ledger and surface the inherent imprecision under **Open Questions** — the consumer is not on a stable release.
   - **Fallback when the consumer pin cannot be determined** (e.g. the wrapper file is inaccessible after retries): try `mcp__github__list_tags` for `stable`, else the highest semver tag (`vX.Y.Z`), else `mcp__github__get_latest_release`. Set `UPSTREAM_TAG` and `UPSTREAM_SHA` from whichever succeeded. Record the fallback path and the assumption under **Open Questions**.
   - **Also resolve `PREVIOUS_UPSTREAM_TAG` and `PREVIOUS_UPSTREAM_SHA`** — the tag immediately preceding `UPSTREAM_TAG` in semver order, plus its resolved SHA. List tags with `mcp__github__list_tags`, sort by semver, pick the entry below `UPSTREAM_TAG`. Used in Step 3 to scope the regression search ("changes merged between `PREVIOUS_UPSTREAM_SHA` and `UPSTREAM_SHA`"). If `UPSTREAM_TAG` is the lowest tag (or is a branch / direct SHA), set both `PREVIOUS_*` fields to null and skip the regression-search step.
   - Record `UPSTREAM_TAG`, `UPSTREAM_SHA`, `PREVIOUS_UPSTREAM_TAG`, `PREVIOUS_UPSTREAM_SHA` in the **Evidence Ledger**. Every later upstream tool call MUST pass `ref=<UPSTREAM_SHA>`. Every citation should be written as `<owner>/<repo>@<UPSTREAM_TAG> (<short-sha>):<file>:<line>`.
   - If `UPSTREAM_SHA` cannot be resolved at all after retries, record this under **Inaccessible Resources** and stop — the analysis cannot be pinned without a SHA. Do not silently fall back to `main`.

3. **Read every log carefully** — Do not skim or summarize. Read each downloaded log from start to finish. When multiple logs are present, read them in the order the user provided them (the first URL is usually the primary failure; subsequent ones are corroborating runs, retries, or related jobs). Use the user's description (recorded in the Evidence Ledger in Step 1) to prioritise which signals to chase first, but do not let it cause you to skip parts of any log. Pay attention to:
   - Error messages, stack traces, exceptions
   - Failed assertions or test failures
   - Exit codes, signal kills, OOM errors
   - Timeout or connectivity failures
   - Deprecation warnings that may have escalated to errors
   - Mismatched dependency versions
   - Environment or configuration issues
   - References to PRs (`#1234`), issues, commit SHAs, branches, workflow run IDs, artifact URLs — these are leads to follow in Step 4.

4. **Expand the investigation** — Before proposing any fix, follow every relevant reference. Be exhaustive about the **upstream** side because that is where the user is most likely to need a fix.

   **In this consumer repo (current working directory):**
   - PRs / issues referenced → fetch via `mcp__github__pull_request_read` / `mcp__github__issue_read`. Read description, comments, linked issues, recent reviews.
   - Workflow YAML used by the failing run (already read in Step 2).
   - `git blame` and `git log -p -- <file>` on consumer-repo files in stack traces.
   - Recent consumer-side commits → intersect files changed in recent commits/PRs with files in the stack trace to see whether the regression is consumer-introduced.
   - Other recent runs of the same workflow on `main` and on related branches (regression vs. flake).

   **In the upstream `shubhodeep1/coding-workflows` repo, pinned to `UPSTREAM_SHA`:**
   - Use `mcp__github__get_file_contents` with `ref=<UPSTREAM_SHA>` for every read. Never read upstream files at `main` — the consumer is not running `main`.
   - The reusable workflow that the consumer wrapper calls (under `workflow-templates/` or `.github/workflows/`).
   - Scripts invoked by that workflow (under `scripts/`).
   - Prompt files invoked by those scripts (under `prompts/`).
   - Any contracts (`db/contracts/*.yml`), agents config (`agents.md`), or `codex_system_instructions.md` rules referenced by the failing path.
   - PRs / issues in `shubhodeep1/coding-workflows` that touched the relevant files between `PREVIOUS_UPSTREAM_SHA` and `UPSTREAM_SHA` — these are the most likely culprits if the regression is upstream-introduced. Use `mcp__github__list_commits` with `path=<file>` (and a `since` / `sha` window bounded by the two refs) and `mcp__github__search_issues` / `mcp__github__search_pull_requests` scoped to the repo. If `PREVIOUS_UPSTREAM_SHA` is null, skip this regression-window search and note it under **Open Questions**.
   - Cross-reference: intersect files changed in upstream PRs merged between `PREVIOUS_UPSTREAM_SHA` and `UPSTREAM_SHA` with the files implicated by the consumer-side stack trace / log.

   **Retry transient errors when fetching from GitHub or any HTTP source** according to the **Retry Rule** in the Rules section. A transient blip is not an excuse to give up on a lead.

5. **Identify every distinct issue** — List each unique issue. For each:
   - Exact error message or log line (with `log-<n> L<line>` reference into the saved log file when multiple logs are present, otherwise just `L<line>`)
   - Root cause, not symptom
   - Whether it is the primary failure or a cascading/secondary failure
   - Whether the root cause is **upstream** (in `shubhodeep1/coding-workflows@<UPSTREAM_TAG> (<short-sha>)`), **consumer-side**, or **environmental** (runner / network / external service)

6. **Correlate to source code** — Locate the exact files and lines responsible. Use grep, paths from stack traces, and the references collected in Step 4. **Read the actual files at `UPSTREAM_SHA`.** Never guess at code structure or function signatures. Cite upstream files as `<owner>/<repo>@<UPSTREAM_TAG> (<short-sha>):<path>:<line>`; cite consumer-repo files as `<path>:<line>`.

7. **Build the Evidence Ledger** — Every claim and every proposed fix must cite:
   - `UPSTREAM_TAG`, `UPSTREAM_SHA`, `PREVIOUS_UPSTREAM_TAG`, `PREVIOUS_UPSTREAM_SHA` (resolved in Step 2) once at the top
   - The list of input log URLs and their saved `/tmp/` paths (numbered `log-1`, `log-2`, …) plus the user's verbatim description (or `none`), recorded in Step 1
   - Log line number(s) and exact text — `log-<n> L<line>: "..."` when multiple logs are present, otherwise just `L<line>: "..."`
   - Source location: `<repo>@<UPSTREAM_TAG> (<short-sha>):<file>:<line>` for upstream; `<file>:<line>` for consumer
   - Commit SHA / PR # that introduced the relevant code (when applicable)
   - Test output or reproduction result (when applicable)

   No citation → no claim. No claim → no fix.

8. **Attempt reproduction** — Where feasible, run the failing test/command locally and record the result. A failure to reproduce is itself evidence (environment-specific, flake, cache-poisoned, runner-specific, auth-walled) — surface it; do not paper over it.

9. **Propose fixes — labeled by confidence and target repo** — For each proposed change:
   - `EVIDENCE-BASED` — fully supported by log + code reading at `UPSTREAM_SHA` + (where applicable) reproduction.
   - `HYPOTHESIS` — plausible but unverified. Surface it, explain the gap, and ask before acting on it.

   For each fix, also label the **target repo**. **This command defaults to `[UPSTREAM]`** — almost all failures investigated through this command originate in the workflow library, not the consumer repo. Only emit `[CONSUMER]` when the evidence unambiguously points at consumer-side code (e.g. a syntax error in the consumer's own wrapper YAML, a typo in a consumer-specific secret name, or a missing consumer-side config file). When in doubt, prefer `[UPSTREAM]` and surface the consumer-side angle under **Open Questions**.
   - `[UPSTREAM]` — change must be made in `shubhodeep1/coding-workflows`. Default for this command. This is what the user will paste into a session opened against that repo.
   - `[CONSUMER]` — change must be made in this consumer repo (e.g. wrapper YAML, pinned ref, secret). Use only when evidence is unambiguous.
   - `[BOTH]` — coordinated change required.

   Do **NOT** apply fixes. This command is read-only. Surface the proposed diff (as a fenced code block with `file:line` anchors) and let the user open a session in the appropriate repo to implement it. Environmental failures do not get a Proposed Fix entry — surface them under **Reproduction Result** / **Open Questions** / **Next Step** instead.

10. **Final output structure** — Always produce, in this order:
    - **Summary** (1–3 lines, including `UPSTREAM_TAG (UPSTREAM_SHA)`)
    - **Evidence Ledger** (numbered)
    - **Root Cause(s)** — confidence label, plus a target-repo label only when the root cause is a code defect (`[UPSTREAM]` / `[CONSUMER]` / `[BOTH]`). Environmental root causes are non-actionable and carry no target-repo label.
    - **Proposed Fix(es)** with `<repo>@<UPSTREAM_TAG> (<short-sha>):<file>:<line>`, target-repo label, confidence label, and rationale. Default to `[UPSTREAM]` per Step 9.
    - **Inaccessible Resources** — exact URLs the user must open manually, with what's needed from each and what conclusion is blocked without it
    - **Reproduction Result**
    - **Open Questions** — surface remaining ambiguity rather than guessing
    - **Next Step for the User** — one-sentence instruction telling the user which repo to open a session against (`shubhodeep1/coding-workflows` for `[UPSTREAM]` fixes, this repo for `[CONSUMER]` fixes, both for `[BOTH]` fixes) and a copy-paste-ready prompt summarising the proposed change. If the only finding is environmental, instruct the user to re-run after the environment recovers and explain why no code change is recommended.

11. **Cleanup** — Remove every temp log file written to `/tmp/` during Step 1 when done.

## Rules

- **Read-only.** This command never edits files. It produces a report. The user takes `[UPSTREAM]` fixes to a session in `shubhodeep1/coding-workflows`; the user takes `[CONSUMER]` fixes to a session in this repo. If during investigation you would normally apply an `EVIDENCE-BASED` edit, do **not** — emit it as a proposed fix instead.
- **Always pin upstream reads to `UPSTREAM_SHA`.** Reading upstream files at `main` is a bug — the consumer is not running `main`. Every `mcp__github__get_file_contents` call against `shubhodeep1/coding-workflows` MUST pass `ref=<UPSTREAM_SHA>` (the resolved SHA from Step 2, never the bare tag name — moving tags like `stable` can shift mid-investigation).
- **Always download the complete log.** Never truncate or skip sections. When `$ARGUMENTS` lists multiple URLs, this rule applies to each — download every accessible log in full before drawing conclusions.
- **Retry Rule**: For transient HTTP/GitHub errors (5xx, 429, timeouts, connection resets, DNS failures), retry with exponential backoff (2s, 4s, 8s, 16s — up to 4 retries) before declaring failure. This applies to the initial log download **and** to every follow-up fetch (PRs, issues, workflow files, artifacts, related runs).
- **Inaccessible resources** — If a resource is still unreachable after retries, or returns a hard failure (401, 403, 404, 410), or is auth-walled / expired / private, record it under **Inaccessible Resources**:
  - The exact URL
  - What is needed from it
  - What conclusion is blocked without it

  Stop that specific line of inquiry, but continue the broader analysis if the primary log + `UPSTREAM_SHA` are accessible and the root cause / proposed fix is still supported by available evidence. Do not make claims that depend on the inaccessible content — surface those gaps under **Open Questions** instead. Abort the analysis only if (a) the primary log itself is inaccessible, (b) `UPSTREAM_SHA` cannot be resolved, or (c) the missing resource blocks the root-cause conclusion.
- **Use the GitHub MCP tools (`mcp__github__*`) for all GitHub interactions.** The `gh` CLI is not assumed available.
- **Prioritize the root cause** — the first meaningful error in the log — over cascading failures.
- **Multiple independent failures** → address each separately, each with its own evidence and proposed fix.
- **Environmental failures** (service down, rate limit, runner outage) — say so explicitly rather than proposing a code fix. Mark them as **environmental / non-actionable** (no target-repo label, no Proposed Fix entry) and recommend re-running once the environment recovers. Only assign a target-repo label if the evidence shows an actual upstream or consumer code defect underlying the environmental symptom (e.g. a missing retry around a transiently-flaky service).
- **Forbidden silent moves**: modifying tests to make them pass (unless the test is genuinely wrong, with evidence), broadening `except`/`catch` blocks, suppressing warnings, version-bumping without verified compatibility, adding retries to mask deterministic failures, switching the consumer's pinned upstream ref to `main` to dodge a stable-tag bug.
- **No guessing.** Every diagnosis cites evidence; every fix is tied to a specific log line and source location at a specific ref. If you cannot find evidence, ask or surface the gap — do not invent it.
- **No scope creep.** Stay within the failure(s) implied by the input. Do not propose unrelated cleanups, refactors, or "while we're in here" changes.
