Investigate a PR, issue, workflow run, or other reference, identify the root cause, and ship a fix. `$ARGUMENTS` is **free-form prose** that may contain any combination of GitHub PR URLs, issue URLs, Actions run/job URLs, raw log URLs, `#1234` references, run IDs / job IDs, commit SHAs, branch / tag names, file paths, stack traces, and quoted error messages — plus an optional free-form description of what the user expects you to focus on.

**Which repo / which ref.** By default the reference lives in **this** repo (`shubhodeep1/coding-workflows`), which is investigated at **`main`** (the local working checkout) — never a `stable` tag, because this is the upstream library itself and the fix lands on `main`. If the evidence shows the reference actually concerns a **different** repo (e.g. a consumer repo) and the root cause is internal to that repo, investigate **that** repo at its **`main`** branch instead — decide which repo to investigate from the leads + description + evidence, and state the decision in the `Summary`. You can only commit / push fixes to **this** repo: when the fix must land in a different repo, do not edit — produce the [Output Format](#output-format) report with the proposed fix and tell the user which repo to open.

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS`.** Scan the prose greedily for every actionable lead: PR / issue URLs and `#1234` refs, Actions run/job/log URLs, run IDs, job IDs, artifact URLs, commit SHAs, branch / tag names, file paths, stack traces, quoted error messages. Capture any remaining free-form prose verbatim as the user description — it shapes prioritisation in steps 2–4 but never overrides evidence. Restate parsed leads + description in the eventual `Summary` so any miss is visible. If `$ARGUMENTS` contains zero actionable leads (no URLs, refs, SHAs, or paths — just vague prose), stop and ask the user for at least one concrete reference. If the only leads are log URLs and there's no PR/issue/run-ID context, point the user at `/analyze-log` instead.
2. **Fetch every referenced artifact.** PRs / issues → `mcp__github__pull_request_read` / `mcp__github__issue_read` (description, comments, linked issues, review threads, status checks). Actions runs/jobs → `mcp__github__get_workflow_run_logs` / `mcp__github__get_job_logs`, or `gh run view --log <run-id> -R <owner>/<repo>` / `gh run view --log --job <job-id> -R <owner>/<repo>` / `gh run view --log-failed <run-id> -R <owner>/<repo>` when `GH_TOKEN` is set (see [Tool Access](#tool-access) for why `-R` is mandatory in this environment). Raw log / artifact URLs → `curl --fail-with-body -sSL -o /tmp/<name>.log -w '%{http_code}\n' <url>` (use a distinct `<name>` per URL — e.g. `log-1.log`, `log-2.log` — so concurrent downloads don't overwrite each other). Retry transient errors (5xx, 429, timeouts, DNS, connection reset) with exponential backoff (up to 4 retries: 2s, 4s, 8s, 16s). Hard failures (401/403/404/410, auth-walled) → record the URL under `Artifacts needed`; stop only if every primary lead is inaccessible. Reference log lines via `nl -ba /tmp/<name>.log` so `L<n>` citations are stable; cite as `log-<n> L<line>` to disambiguate when multiple logs are in play.
3. **Read everything end to end.** No skimming. For each log: errors, stack traces, exit codes, timeouts, dep mismatches, and embedded references (PRs, issues, SHAs, run IDs, artifact URLs). For each PR/issue: linked issues, review comments, status checks, every embedded reference. Use the user's description to prioritise which signals to chase first, but don't let it cause you to skip parts of any artifact.
4. **Investigate — keep pulling artifacts until the root cause is nailed down.** Follow every relevant reference recursively until you bottom out: PRs/issues, workflow YAML the run executed, scripts/prompts that workflow invokes, `git blame` / `git log -p -- <file>` on files in stack traces, recent commits intersected with implicated files, related runs of the same workflow (regression vs. flake). When a log/PR/comment names another run / job / artifact / commit, fetch it — don't infer. Use either `mcp__github__*` or `gh` CLI (see [Tool Access](#tool-access)); both can reach run logs, job logs, PRs, issues, commits, and file contents with `GH_TOKEN`. If the diagnosis isn't yet evidence-based, the next step is to read more, not to guess. Read the actual source — never guess at code, function signatures, env vars, or workflow inputs.
5. **Decide and act.** Apply the [Decision Rule](#decision-rule) below.

## Decision Rule

After investigation, classify each finding as **EVIDENCE-BASED** (fully supported by logs + code + linked PR/issue context, plus reproduction when feasible) or **HYPOTHESIS** (plausible but unverified). Then:

- **All findings are EVIDENCE-BASED, the fix lands in THIS repo (`shubhodeep1/coding-workflows`), and no missing/inaccessible resource blocks root cause or fix verification** → design the fix, apply it against `main`, verify it (re-run the repro / failing test when feasible), commit, push, open a PR. Do not ask. Report using the [Output Format](#output-format) afterward. Non-blocking gaps (e.g. an inaccessible linked PR that doesn't affect the diagnosis) still get listed under `Artifacts needed` for transparency.
- **All findings are EVIDENCE-BASED but the fix must land in a DIFFERENT repo** (the root cause is internal to a consumer repo, investigated at its `main`, and you have no checkout to push to) → do **not** edit. Emit the proposed fix in the `Fix:` line with a `file:line` anchor and tell the user which repo to open a session against. This is the only read-only path.
- **Otherwise** (any HYPOTHESIS finding, or any missing/inaccessible resource that blocks root cause or fix verification) → stop before editing. Report using the [Output Format](#output-format) and ask the user how to proceed.
- **Environmental failure** (service down, rate limit, runner outage) → no fix; say so explicitly.

## Output Format

Keep it tight. No prose padding.

```
Summary: <1–2 lines: parsed leads, which repo + ref was investigated (this repo @ main, or <other-repo> @ main), what failed, root cause; mention the user's description in one phrase>

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

- **`mcp__github__*` MCP tools** — always available. Preferred when the schema fits cleanly (e.g. `get_workflow_run_logs`, `get_job_logs`, `pull_request_read`, `issue_read`, `list_commits`, `get_file_contents`, `search_issues`, `search_pull_requests`).
- **`gh` CLI** — the `GH_TOKEN` transport for GitHub reads. Shared rules live in **CLAUDE.md §23** and are not restated here: availability and the nounset-safe auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene, and the self-serve-read / ask-first-mutation split (§23.A–E). This command needs only §23.A reads. Use `gh` for one-off reads where the MCP surface is awkward, or for tools that don't have a clean MCP equivalent in this session — e.g. `gh run view --log <run-id> -R <owner>/<repo>`, `gh run view --log --job <job-id> -R <owner>/<repo>`, `gh run view --log-failed <run-id> -R <owner>/<repo>` (failed-step lines only), `gh api repos/<owner>/<repo>/actions/runs/<id>/logs`. The token reaches run logs, job logs, PRs, issues, commits, and file contents the same way the MCP tools do.

**Always pass `-R <owner>/<repo>` on `gh` calls that need repo context.** In Claude Code Web sessions the only git remote points at a local proxy (e.g. `http://...@127.0.0.1:PORT/git/<owner>/<repo>`), so `gh` cannot auto-detect the GitHub repo from `git remote -v`; bare `gh run view ...` fails with `failed to determine base repo`. The SessionStart hook prints the resolved slug — use that value (defaults to `shubhodeep1/coding-workflows` for this repo).

Repo scope: primarily `shubhodeep1/coding-workflows`, but reads may target a different repo (e.g. a consumer repo) when the evidence points there — pass `-R <owner>/<repo>` for the repo you're actually reading. If `gh auth status` succeeds, `gh` is usable; if a *specific* `gh` call returns 401/403/404 for one resource (GitHub returns 404 for many auth-walled / private resources, so treat it as a permission-or-visibility error rather than "resource missing"), or `gh` is missing entirely, fall back to the MCP tool for that call — don't conclude `gh` is broken globally and don't stop the investigation.

**Keep going until the diagnosis is evidence-based.** A single PR, issue, or run rarely contains the whole story. If the root cause isn't yet supported by log + source citations, pull the next layer: re-fetch the run with `--log-failed`, fetch sibling/previous runs of the same workflow, fetch the linked PR/issue, fetch the workflow YAML, fetch artifacts, follow `git blame` on the failing line. Only stop reading when (a) you have an evidence-based fix, (b) you've hit a [Decision Rule](#decision-rule) blocker that genuinely requires the user, or (c) every reasonable lead is exhausted and recorded under `Artifacts needed`.

## Rules

- **Always emit the full Output Format — even when the fix has been applied, committed, and pushed.** The PR/commit alone is not the user-facing report. The chat reply MUST include `Summary` (with the root cause), `Evidence-based` cites, and `Fix:` describing what changed and why. A bare "Done — see PR #X" is not acceptable; the user wants the diagnosis and a description of the generated fix without leaving the chat.
- Download / read every referenced artifact in full; never truncate. When multiple URLs are present, this rule applies to each.
- Prefer `mcp__github__*` for GitHub reads; fall back to the `gh` CLI + `GH_TOKEN` when the MCP surface is awkward or scope-gated (CLAUDE.md §23.D) (see [Tool Access](#tool-access)). Repo scope: primarily `shubhodeep1/coding-workflows`; read a different repo at its `main` when the evidence shows the root cause is internal to it (pass the right `-R <owner>/<repo>`).
- **`gh` calls that need repo context MUST pass `-R <owner>/<repo>` explicitly.** Claude Code Web's git remote is a local proxy that `gh` cannot auto-resolve — bare `gh run view --log <id>` fails with `failed to determine base repo`. The SessionStart hook prints the resolved slug; use that value (`shubhodeep1/coding-workflows` for this repo).
- No citation → no claim. No claim → no fix.
- Forbidden silent moves: editing tests to pass without evidence the test is wrong, broadening `except`/`catch`, suppressing warnings, version-bumping without verified compatibility, adding retries to mask deterministic failures.
- Address the root cause (first meaningful error), not cascading failures. Multiple independent failures → handle each separately.
- Cleanup: delete every temp log written to `/tmp/` during step 2 when done.
