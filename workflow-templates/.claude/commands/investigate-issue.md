Investigate a problem reported against this repo, identify the root cause, and either **ship a fix** or produce an **evidence-backed proposed fix** — the choice depends on *where* the root cause lives. `$ARGUMENTS` is **free-form prose** that may contain any combination of GitHub PR URLs, issue URLs, Actions run/job URLs, raw log URLs, `#1234` / `owner/repo#1234` references, workflow run IDs / job IDs, commit SHAs, branch / tag names, file paths, stack traces, and quoted error messages. Trace back through every relevant reference (PRs, issues, Actions logs, comments, commits) and bottom out on the root cause.

**Where does the root cause live? You decide, from the evidence.** A problem surfaced in this repo can originate in one of two places, and the classification drives which ref you read and whether you edit:

- **`[CONSUMER-INTERNAL]`** — the bug is in **this** repo's own code / config / workflows, unrelated to the upstream workflow library. → Investigate **this** repo at **`main`** (the local working checkout). **Read-write: ship the fix** here (apply, verify, commit, push, open a PR).
- **`[UPSTREAM]`** — the bug is in the upstream workflow library `shubhodeep1/coding-workflows`.
  - If **this repo *is* `shubhodeep1/coding-workflows`** (it consumes its own templates), investigate it at **`main`** and **ship the fix** here (read-write) — there is no separate downstream to hand off to.
  - Otherwise (this is a *different* consumer repo), investigate `shubhodeep1/coding-workflows` pinned to the **upstream ref this consumer is actually running** (NOT `main`), and produce a **read-only** proposed fix the user takes to a `shubhodeep1/coding-workflows` session — a different repo's session cannot commit / push to the upstream library.
- **`[BOTH]`** — coordinated change spanning both; handle each side by the matching rule above.

Decide the classification from the parsed leads + the user's description + the evidence you gather. If it is genuinely unclear which side owns the root cause, investigate **both** sides and surface the ambiguity under **Open Questions** rather than guessing.

$ARGUMENTS

## Steps

1. **Parse `$ARGUMENTS`** — Scan the prose for every actionable lead. Be greedy; missing a lead silently is worse than restating an irrelevant one.
   - GitHub PR URLs / `#1234` references / `owner/repo#1234` references
   - GitHub issue URLs / `#1234` references
   - Actions log URLs (e.g. `https://github.com/<owner>/<repo>/actions/runs/<id>`, raw log URLs, job log URLs)
   - Workflow run IDs, job IDs, artifact URLs
   - Commit SHAs (full or short)
   - Branch names, tag names
   - File paths and stack traces
   - Error messages quoted in prose
   - Any free-form description of expected behaviour, suspected cause, or what the user has already tried — capture this verbatim; it shapes prioritisation in Steps 3–4 but never overrides evidence.

   Restate the parsed leads (and the verbatim user description, or `none`) back to the user in the **Summary** so any miss is visible. If `$ARGUMENTS` contains zero actionable leads (no URLs, no refs, no SHAs, no file paths — just vague prose), stop and ask the user for at least one concrete reference.

2. **Classify the issue and resolve the target repo + ref + mode.** This is the decision the rest of the command hangs on. Do it explicitly and record it in the **Evidence Ledger**.

   First, determine **`THIS_REPO`** — the `owner/repo` slug of the repo the command is running in. The SessionStart hook prints the resolved slug; otherwise derive it from the git remote. (In Claude Code Web the remote is a local proxy, so prefer the SessionStart slug.)

   Then classify the root cause as `[CONSUMER-INTERNAL]`, `[UPSTREAM]`, or `[BOTH]` using the parsed leads + description + any early evidence (a stack trace pointing at this repo's own files is `[CONSUMER-INTERNAL]`; a failure inside a reusable workflow / script / prompt pulled from `shubhodeep1/coding-workflows` is `[UPSTREAM]`). When unsure, lean toward investigating both and decide once the evidence is in.

   From the classification, set the target repo, the ref to read, and the mode:

   - **`[CONSUMER-INTERNAL]`** → `TARGET_REPO = THIS_REPO`, `TARGET_REF = main` (the local working checkout). **Mode = read-write** (apply the fix per the Decision Rule in Step 9).
   - **`[UPSTREAM]` and `THIS_REPO == shubhodeep1/coding-workflows`** → `TARGET_REPO = shubhodeep1/coding-workflows`, `TARGET_REF = main`. **Mode = read-write**. Resolve `TARGET_SHA` to the current `main` tip via `mcp__github__list_commits` with `sha=main` (or read files directly from the local `main` checkout). Do **not** run the stable-pinning procedure below — this repo is the upstream, and the fix lands on `main`.
   - **`[UPSTREAM]` and `THIS_REPO != shubhodeep1/coding-workflows`** → `TARGET_REPO = shubhodeep1/coding-workflows`, pinned to the **upstream ref this consumer is actually running** (the stable-pinning procedure below). **Mode = read-only** (propose the fix; the user takes it to a `shubhodeep1/coding-workflows` session).
   - **`[BOTH]`** → run both the consumer-internal path and the upstream path; apply only the edits the current session is allowed to push (the `[CONSUMER-INTERNAL]` side, and the upstream side only when `THIS_REPO == shubhodeep1/coding-workflows`), and propose the rest read-only.

   ### Stable-pinning procedure (only for `[UPSTREAM]` when `THIS_REPO != shubhodeep1/coding-workflows`)

   All upstream reads in this case MUST be pinned to the ref the consumer's failing run was executing. Pinning to the *current* `stable` tag without checking the consumer's pin is wrong: if the consumer is pinned to `@v1.2.3` and `stable` has since moved to `@v1.3.0`, reading at `stable` analyses code the consumer is not running. Resolve once, up front, into **two distinct fields**:

   - `UPSTREAM_TAG` — the human-readable label the consumer is pinned to (e.g. `v1.2.3`, `stable`, `main`, or the literal SHA if pinned by SHA). Used for citations and the report.
   - `UPSTREAM_SHA` — the resolved commit SHA the tag/branch/ref points at. **This is the single canonical value passed as `ref=` to every GitHub MCP call** (alias of `TARGET_SHA` for this path). Pinning to the SHA (not the tag name) makes the analysis stable even if a moving pointer like `stable` is updated mid-investigation.

   Resolution procedure:

   - **First, inspect the consumer's wrapper YAML** (the workflows under `.github/workflows/<name>.yml` in this repo, plus any composite actions they delegate to) to find every `uses:` / `repository:` / `ref:` entry that references `shubhodeep1/coding-workflows`. The exact `ref` (tag, branch, or SHA) is the consumer's pin and is the authoritative input.
     - If the wrapper pins a specific tag (e.g. `@v1.2.3`) → `UPSTREAM_TAG = v1.2.3`; resolve to `UPSTREAM_SHA` via `mcp__github__get_tag` (or `list_tags`).
     - If the wrapper pins a SHA directly → `UPSTREAM_TAG = <short-sha>`; `UPSTREAM_SHA = <full-sha>`.
     - If the wrapper pins the moving `stable` pointer (e.g. `@stable`) → `UPSTREAM_TAG = stable`; resolve `UPSTREAM_SHA` via `mcp__github__list_tags` (this repo uses a moving `stable` pointer set by `scripts/mark-stable.sh`).
     - If the wrapper pins an upstream branch (e.g. `@main`) → `UPSTREAM_TAG = main`. Resolve `UPSTREAM_SHA` to the upstream branch's commit at the time the failing run executed — **NOT** the consumer run's `head_sha` (that's a SHA in the consumer repo and will 404 against `shubhodeep1/coding-workflows`):
       1. Read the failing run's start timestamp from the run metadata.
       2. Use `mcp__github__list_commits` with `sha=<upstream-branch>` (and an `until=<run-start-time>` filter if the tool exposes one; otherwise paginate and pick the first commit whose committer date is `≤` the run-start time). That SHA is `UPSTREAM_SHA`.
       3. If the historical lookup is not feasible (timestamp missing, API constraints), fall back to the upstream branch's current tip SHA. Note explicitly that the branch may have moved since the run.
       In all branch-pinned cases, record the precise vs. fallback path in the Evidence Ledger and surface the inherent imprecision under **Open Questions** — the consumer is not on a stable release.
   - **Fallback when the consumer pin cannot be determined** (e.g. the wrapper file is inaccessible after retries): try `mcp__github__list_tags` for `stable`, else the highest semver tag (`vX.Y.Z`), else `mcp__github__get_latest_release`. Set `UPSTREAM_TAG` and `UPSTREAM_SHA` from whichever succeeded. Record the fallback path and the assumption under **Open Questions**.
   - **Also resolve `PREVIOUS_UPSTREAM_TAG` and `PREVIOUS_UPSTREAM_SHA`** — the tag immediately preceding `UPSTREAM_TAG` in semver order, plus its resolved SHA. List tags with `mcp__github__list_tags`, sort by semver, pick the entry below `UPSTREAM_TAG`. This is needed in Step 4 to scope the regression search ("changes merged between `PREVIOUS_UPSTREAM_SHA` and `UPSTREAM_SHA`"). If `UPSTREAM_TAG` is the lowest tag (or is a branch / direct SHA), set both `PREVIOUS_*` fields to null and skip the regression-search step.
   - Record `UPSTREAM_TAG`, `UPSTREAM_SHA`, `PREVIOUS_UPSTREAM_TAG`, `PREVIOUS_UPSTREAM_SHA` in the **Evidence Ledger**. Every later upstream tool call MUST pass `ref=<UPSTREAM_SHA>`. Every citation should be written as `<owner>/<repo>@<UPSTREAM_TAG> (<short-sha>):<file>:<line>` so the reader sees both the human label and the canonical SHA.
   - If `UPSTREAM_SHA` cannot be resolved at all after retries (see **Retry Rule**), record this under **Inaccessible Resources** and stop — this path cannot produce a pinned analysis without a SHA. Do not silently fall back to `main`.

   Record the chosen classification, `THIS_REPO`, `TARGET_REPO`, `TARGET_REF`/`TARGET_SHA`, and the mode (read-write vs read-only) in the **Evidence Ledger** before proceeding.

3. **Download any logs** referenced in the input — Choose the fetch tool by URL shape; `curl` against a rendered GitHub page returns HTML, not log content.
   - **GitHub Actions run / job URLs** (e.g. `https://github.com/<owner>/<repo>/actions/runs/<id>`, `.../runs/<id>/job/<id>`, `.../runs/<id>/attempts/<n>`): use the appropriate GitHub MCP tool (e.g. `mcp__github__get_workflow_run_logs`, `mcp__github__get_job_logs`, or whichever workflow-log tool is exposed in the current session — search `mcp__github__*` for `log`). When `GH_TOKEN` is set in the session environment (the SessionStart hook installs `gh` and authenticates with it), `gh run view --log <run-id> -R <owner>/<repo>`, `gh run view --log --job <job-id> -R <owner>/<repo>`, `gh run view --log-failed <run-id> -R <owner>/<repo>` (failed-step lines only), and `gh api repos/<owner>/<repo>/actions/runs/<id>/logs` are also available — see [Tool Access](#tool-access) for why `-R` is mandatory in this environment. These return the raw log payload. Do NOT `curl` these rendered URLs.
   - **Raw log URLs / artifact URLs / external (non-GitHub) URLs**: use `curl --fail-with-body -sSL -o /tmp/<unique-name>.log -w '%{http_code}\n' <url>` so HTTP errors are detected reliably; plain `curl -sL` exits 0 on 4xx/5xx and silently downloads the server's error page.

   Use a distinct `<unique-name>` per URL (e.g. derived from run/job ID, or numbered `log-1.log`, `log-2.log`) so concurrent downloads don't overwrite each other. Track the local path for each log alongside its `log-<n>` index in the Evidence Ledger so later citations (`log-2 L42: "..."`) are unambiguous when multiple logs are in play.

   Verify the status / payload before treating the result as a log:
   - `2xx` → proceed.
   - `5xx`, `429`, network/timeout/connection-reset/DNS errors → transient; retry per the **Retry Rule**.
   - `401`, `403`, `404`, `410` → hard failure; record under **Inaccessible Resources** and follow the **Inaccessible resources** rule.

   Read each log **in full**. Do not skim. Note: error messages, stack traces, exit codes, OOM/signal kills, timeouts, deprecation warnings, dependency mismatches, environment/config issues, and every embedded reference to PRs / issues / commits / run IDs / artifact URLs — these are leads to follow in Step 4.

4. **Expand the investigation — keep pulling artifacts until the root cause is nailed down.** Before proposing or applying any fix, follow every lead. When a log/PR/comment names another run / job / artifact / commit / PR, fetch it; don't infer from the name alone. Either `mcp__github__*` or `gh` CLI works (see [Tool Access](#tool-access)) — when `GH_TOKEN` is set, both can reach run logs, job logs, PRs, issues, commits, and file contents. If the diagnosis isn't yet evidence-based, the next step is to read more, not to guess.

   **In `THIS_REPO` (current working directory):**
   - PRs / issues referenced → fetch via `mcp__github__pull_request_read` / `mcp__github__issue_read`. Read description, comments, linked issues, review threads, status checks.
   - Workflow YAML used by the failing run → read the wrapper at `.github/workflows/<name>.yml` to identify which upstream reusable workflow / action / script it calls and at what ref (this also feeds the Step 2 classification: if the failure is inside this repo's own logic it is `[CONSUMER-INTERNAL]`; if it is inside the delegated upstream workflow it is `[UPSTREAM]`).
   - `.github/ai/consumer_repos.json` and any consumer-side config that affects which upstream behaviour is active.
   - `git blame` / `git log -p -- <file>` on `THIS_REPO` files in stack traces. For a `[CONSUMER-INTERNAL]` issue, this is the primary evidence trail.
   - Other recent runs of the same workflow (regression vs. flake).

   **For an `[UPSTREAM]` issue, in `shubhodeep1/coding-workflows`:**
   - When `THIS_REPO == shubhodeep1/coding-workflows`, read at `main` (`ref=main` or the local checkout). When `THIS_REPO` is a *different* consumer, use `mcp__github__get_file_contents` with `ref=<UPSTREAM_SHA>` for **every** read — never read upstream files at `main` in that case, because a fix proposed against `main` may not apply against the consumer's pinned wrapper.
   - Reusable workflow that the wrapper calls (under `workflow-templates/` or `.github/workflows/`).
   - Scripts invoked by that workflow (under `scripts/`).
   - Prompt files invoked by those scripts (under `prompts/`).
   - Any contracts (`db/contracts/*.yml`), agents config (`agents.md`), or system rules referenced by the failing path (unattended workflow paths use `unattended_system_instructions.md`; interactive Claude Code sessions use `CLAUDE.md`).
   - Regression window — the commits most likely to have introduced the bug:
     - Pinned case (`THIS_REPO != shubhodeep1/coding-workflows`): PRs / commits that touched the relevant files between `PREVIOUS_UPSTREAM_SHA` and `UPSTREAM_SHA` (resolved in Step 2). Use `mcp__github__list_commits` with `path=<file>` and a `since` / `sha` window bounded by the two refs, plus `mcp__github__search_issues` / `mcp__github__search_pull_requests` scoped to the repo. If `PREVIOUS_UPSTREAM_SHA` is null, skip this and note it under **Open Questions**.
     - `main` case (`THIS_REPO == shubhodeep1/coding-workflows`): recent commits to the implicated files on `main` (`mcp__github__list_commits` with `path=<file>`, or local `git log -p -- <file>`).
   - Cross-reference: intersect files changed in the regression window with the files implicated by the stack trace / log.

   **Retry transient errors** (5xx, 429, timeouts, connection resets, DNS failures) on every fetch — log download, GitHub MCP, raw HTTP — per the **Retry Rule**. A transient blip is not an excuse to give up on a lead.

5. **Identify every distinct issue** — List each unique issue. For each:
   - Exact error message or log line (with `log-<n> L<line>` reference into the saved log file when multiple logs are present, otherwise just `L<line>`)
   - Root cause, not symptom
   - Whether it is the primary failure or a cascading/secondary failure
   - Whether the root cause is **`[CONSUMER-INTERNAL]`** (in `THIS_REPO`), **`[UPSTREAM]`** (in `shubhodeep1/coding-workflows` — cite as `@main` or `@<UPSTREAM_TAG> (<short-sha>)` per the Step 2 path), or **environmental** (runner / network / external service)

6. **Correlate to source code** — Locate the exact files and lines responsible. **Read the actual files at `TARGET_REF` / `TARGET_SHA`.** Never guess at code structure, function signatures, env vars, or workflow inputs. Cite `THIS_REPO` files as `<path>:<line>`; cite pinned upstream files as `shubhodeep1/coding-workflows@<UPSTREAM_TAG> (<short-sha>):<path>:<line>`; cite upstream-at-main files as `shubhodeep1/coding-workflows@main:<path>:<line>`.

7. **Build the Evidence Ledger** — Every claim and every fix (applied or proposed) must cite:
   - The Step 2 decision once at the top: classification, `THIS_REPO`, `TARGET_REPO`, `TARGET_REF`/`TARGET_SHA`, mode. For the pinned upstream path, also `UPSTREAM_TAG`, `UPSTREAM_SHA`, `PREVIOUS_UPSTREAM_TAG`, `PREVIOUS_UPSTREAM_SHA`.
   - The list of parsed leads from Step 1 (URLs, refs, SHAs, paths) and the user's verbatim description (or `none`)
   - Log line number(s) and exact text — `log-<n> L<line>: "..."` when multiple logs are present, otherwise just `L<line>: "..."` (with the `/tmp/` path of each saved log)
   - Source location: `<path>:<line>` for `THIS_REPO`; `<repo>@<ref> (<short-sha>):<file>:<line>` for upstream
   - Commit SHA / PR # that introduced the relevant code (when applicable)
   - Test output or reproduction result (when applicable)

   No citation → no claim. No claim → no fix.

8. **Attempt reproduction (where feasible)** — If the failure can be reproduced locally without privileged credentials, run the failing command/test and record the result. A failure to reproduce is itself evidence (environment-specific, flake, cache-poisoned, runner-specific, auth-walled) — surface it; do not paper over it.

9. **Decide and act — driven by the mode set in Step 2.** Classify each finding as `EVIDENCE-BASED` (fully supported by log + code reading at the target ref + reproduction where feasible) or `HYPOTHESIS` (plausible but unverified).

   - **Read-write modes** — `[CONSUMER-INTERNAL]`, and `[UPSTREAM]` when `THIS_REPO == shubhodeep1/coding-workflows`:
     - All findings `EVIDENCE-BASED` and no missing/inaccessible resource blocks root cause or fix verification → design the fix, apply it, verify it (re-run the repro / failing test when feasible), commit, push, and open a PR. Do not ask. Report using the **Final output structure** afterward, with the applied fix and the branch / PR link.
     - Any `HYPOTHESIS` finding, or any missing/inaccessible resource that blocks root cause or fix verification → stop before editing. Report and ask the user how to proceed.
     - Add or extend a test when fixing a defect that lacked coverage; do not ship a behaviour fix without verification.
   - **Read-only mode** — `[UPSTREAM]` when `THIS_REPO != shubhodeep1/coding-workflows`:
     - Do **NOT** apply fixes. A different consumer's session cannot push to the upstream library. Surface the proposed diff (as a fenced code block with `file:line` anchors at `UPSTREAM_SHA`) labeled `[UPSTREAM]` and let the user open a `shubhodeep1/coding-workflows` session to implement it.
   - **`[BOTH]`** — apply the side this session is allowed to push (the `[CONSUMER-INTERNAL]` side, and the upstream side only when `THIS_REPO == shubhodeep1/coding-workflows`); propose the rest read-only with the matching target-repo label.
   - **Environmental** (service down, rate limit, runner outage) → no fix; mark as **environmental / non-actionable** and recommend re-running once the environment recovers.

   Label every fix with its **target repo**: `[CONSUMER]` (lands in `THIS_REPO`), `[UPSTREAM]` (lands in `shubhodeep1/coding-workflows`), or `[BOTH]`.

10. **Final output structure** — Always produce, in this order:
    - **Summary** (1–3 lines, including the parsed leads from Step 1, the Step 2 classification + mode, and the target ref — `@main` or `UPSTREAM_TAG (UPSTREAM_SHA)`)
    - **Evidence Ledger** (numbered)
    - **Root Cause(s)** — confidence label, plus a target-repo label only when the root cause is a code defect (`[CONSUMER]` / `[UPSTREAM]` / `[BOTH]`). Environmental root causes are marked non-actionable instead and carry no target-repo label.
    - **Fix(es)** — for read-write modes, what was **applied** (with `<file>:<line>` anchors and the branch / PR link); for read-only mode, the **proposed** diff with `<repo>@<UPSTREAM_TAG> (<short-sha>):<file>:<line>`, target-repo label, confidence label, and rationale. Environmental issues do not get a Fix entry.
    - **Inaccessible Resources** — exact URLs the user must open manually, with what's needed from each and what conclusion is blocked without it
    - **Reproduction Result**
    - **Open Questions** — surface remaining ambiguity rather than guessing
    - **Next Step for the User** — for read-write modes, the branch / PR to review; for read-only mode, a one-sentence instruction to open a `shubhodeep1/coding-workflows` session plus a copy-paste-ready prompt summarising the proposed change. If the only finding is environmental, instruct the user to re-run after the environment recovers and explain why no code change is recommended.

11. **Cleanup** — Remove every temp log file written to `/tmp/` during Step 3 when done.

## Tool Access

GitHub reads can go through either of two equivalent paths — pick whichever is exposed in the current session:

- **`mcp__github__*` MCP tools** — assumed always available. Preferred when the schema fits cleanly (e.g. `get_workflow_run_logs`, `get_job_logs`, `pull_request_read`, `issue_read`, `list_commits`, `get_file_contents`, `list_tags`, `search_issues`, `search_pull_requests`). For the pinned upstream path, all upstream reads MUST pass `ref=<UPSTREAM_SHA>`; for the `main` path, pass `ref=main`.
- **`gh` CLI** — available when `gh` is installed in the session and `GH_TOKEN` or `GITHUB_TOKEN` is set in the environment (consumer repos that ship a SessionStart hook to install `gh` will have both). **Verify auth state directly with `{ [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; } && gh auth status` (nounset-safe) before deciding `gh` is unavailable — don't infer it from the SessionStart log.** A consumer hook's secondary probe may only check `actions:read` and emit a `NOTE` / `WARNING` even when `gh` works fine for PRs, issues, commits, and file contents; treat those messages as scope hints, not as "gh is dead." Commands like `gh run view --log <run-id> -R <owner>/<repo>`, `gh run view --log --job <job-id> -R <owner>/<repo>`, `gh run view --log-failed <run-id> -R <owner>/<repo>`, and `gh api repos/<owner>/<repo>/actions/runs/<id>/logs` work for any repo the token has read access to. Useful as a fallback when an MCP call is awkward or returns truncated output. If a *specific* `gh` call returns 401/403/404 for one resource (GitHub returns 404 for many auth-walled / private resources, so treat it as a permission-or-visibility error rather than "resource missing"), or `gh` is missing entirely, fall back to the MCP tool for that call — don't conclude `gh` is broken globally and don't stop the investigation.

**Always pass `-R <owner>/<repo>` on `gh` calls that need repo context.** In Claude Code Web sessions the only git remote points at a local proxy (e.g. `http://...@127.0.0.1:PORT/git/<owner>/<repo>`), so `gh` cannot auto-detect the GitHub repo from `git remote -v`; bare `gh run view ...` fails with `failed to determine base repo`. The SessionStart hook prints the resolved slug — use that for `THIS_REPO`, and use `shubhodeep1/coding-workflows` for upstream calls.

**Keep going until the diagnosis is evidence-based.** A single PR, issue, or run rarely contains the whole story. If the root cause isn't yet supported by log + source citations at `TARGET_REF`/`TARGET_SHA`, pull the next layer: re-fetch the run with `--log-failed`, fetch sibling/previous runs of the same workflow, fetch the linked PR/issue, fetch the workflow YAML at the target ref, fetch artifacts, follow `git blame` on the failing line. Only stop reading when (a) you have an evidence-based fix (applied for read-write modes, proposed for read-only mode), (b) the missing piece is blocked by an **Inaccessible Resource** that is recorded transparently, or (c) every reasonable lead is exhausted.

## Rules

- **Mode is set by the Step 2 classification, not assumed.** Read-write (apply, verify, commit, push, PR) for `[CONSUMER-INTERNAL]` issues and for `[UPSTREAM]` issues when `THIS_REPO == shubhodeep1/coding-workflows`. Read-only (propose a fix the user takes elsewhere) for `[UPSTREAM]` issues when `THIS_REPO` is a *different* consumer — that session cannot push to the upstream library. Never edit files in a repo this session cannot push to.
- **Ref selection by case.** `[CONSUMER-INTERNAL]` → `THIS_REPO@main`. `[UPSTREAM]` + `THIS_REPO == shubhodeep1/coding-workflows` → `shubhodeep1/coding-workflows@main`. `[UPSTREAM]` + different consumer → pin every upstream read to `UPSTREAM_SHA` (the resolved SHA from Step 2, never the bare tag name — moving tags like `stable` can shift mid-investigation). Reading upstream files at `main` in the *pinned* case is a bug — that consumer is not running `main`.
- **Always download the complete log.** Never truncate or skip sections. When multiple log URLs are present, this rule applies to each — download every accessible log in full before drawing conclusions.
- **Retry Rule**: For transient HTTP/GitHub errors (5xx, 429, timeouts, connection resets, DNS failures), retry with exponential backoff (2s, 4s, 8s, 16s — up to 4 retries) before declaring failure. Applies to every fetch: the log download, GitHub MCP calls, raw HTTP follow-ups.
- **Inaccessible resources** — If a resource is still unreachable after retries, or returns a hard failure (401, 403, 404, 410), or is auth-walled / expired / private, record it under **Inaccessible Resources**:
  - The exact URL
  - What is needed from it
  - What conclusion is blocked without it

  Stop that specific line of inquiry, but continue the broader analysis if the primary log + target ref are accessible and the root cause / fix is still supported by available evidence. Do not make claims that depend on the inaccessible content — surface those gaps under **Open Questions** instead. Abort only if (a) the primary log itself is inaccessible, (b) the target ref cannot be resolved, or (c) the missing resource blocks the root-cause conclusion.
- **Prefer `mcp__github__*` for GitHub reads; fall back to `gh` CLI when `GH_TOKEN` is set and the MCP surface is awkward** (see [Tool Access](#tool-access)). The command operates against whatever scope the host session permits; it does not attempt to widen scope.
- **`gh` calls that need repo context MUST pass `-R <owner>/<repo>` explicitly.** Claude Code Web's git remote is a local proxy that `gh` cannot auto-resolve — bare `gh run view --log <id>` fails with `failed to determine base repo`. The SessionStart hook prints the resolved `THIS_REPO` slug; use that for `THIS_REPO` reads, and `shubhodeep1/coding-workflows` for upstream reads.
- **Prioritise the root cause** — the first meaningful error in the log — over cascading failures.
- **Multiple independent failures** → address each separately, each with its own evidence and fix, classified independently (one may be `[CONSUMER-INTERNAL]` and another `[UPSTREAM]`).
- **Environmental failures** (service down, rate limit, runner outage) — say so explicitly rather than proposing a code fix. Mark them as **environmental / non-actionable** (no target-repo label, no Fix entry) and recommend re-running once the environment recovers. Only assign a target-repo label if the evidence shows an actual code defect underlying the environmental symptom (e.g. a missing retry around a transiently-flaky service).
- **Forbidden silent moves** (whether applying or proposing the fix): modifying tests to make them pass (unless the test is genuinely wrong, with evidence), broadening `except`/`catch` blocks, suppressing warnings, version-bumping without verified compatibility, adding retries to mask deterministic failures, and — in the *pinned* upstream case — switching the consumer's pinned upstream ref to `main` to dodge a stable-tag bug. (Reading `shubhodeep1/coding-workflows` at `main` is correct and expected when `THIS_REPO == shubhodeep1/coding-workflows`; it is forbidden only as a workaround for a different consumer's stable-tag bug.)
- **No guessing.** Every diagnosis cites evidence; every fix is tied to a specific log line and source location at a specific ref. If you cannot find evidence, ask or surface the gap — do not invent it.
- **No scope creep.** Stay within the failure(s) implied by the input. Do not propose unrelated cleanups, refactors, or "while we're in here" changes.
