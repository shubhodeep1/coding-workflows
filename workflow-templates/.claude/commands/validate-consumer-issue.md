Given an issue **and a proposed fix** raised against **this** repo (in `$ARGUMENTS` — an issue URL / number, pasted issue text, a PR carrying the fix, or prose describing both), determine two things: (1) is it a **valid issue** — real, reproducible, and correctly diagnosed; and (2) is the **proposed fix** correct, complete, and safe? The root cause can live on one of two sides — **this repo's own code/config**, or the **upstream workflow library** (`shubhodeep1/coding-workflows`) this repo's wrappers call — and you decide which from the evidence; the side determines where a valid fix would land. Read-only verdict: this command reports a judgment in chat and does **not** implement the fix.

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS`.** Separate the **reported issue** (symptom, where it surfaced) from the **proposed fix** (a diff, a `file:line` pointer, or prose). Fetch any referenced issue / PR via `mcp__github__issue_read` / `pull_request_read` (or `gh`). If either half is missing, stop and ask. Determine **`THIS_REPO`** — the `owner/repo` the command runs in (the SessionStart hook prints the resolved slug).

2. **Read project context.** Read this repo's `README.md`, `agents.md`, and `CLAUDE.md` (and `/db/contracts/*.yml` if a MongoDB collection is implicated). Read the actual code the report points at — never reason from the report's description alone.

3. **Classify the side that owns the root cause.**
   - **`[CONSUMER-INTERNAL]`** — the defect is in this repo's own code / config / wrapper workflows. Validate at `THIS_REPO@main`. A valid fix lands **here** (read-write in a separate task).
   - **`[UPSTREAM]`** — the defect is in the upstream library, reached through this repo's wrapper at the **ref this repo is pinned to**. Validate the upstream side pinned to `UPSTREAM_SHA` (procedure below). A valid fix lands **upstream** — the user takes it to a `shubhodeep1/coding-workflows` session (this repo's session cannot push there).
   - **`[BOTH]`** — a coordinated defect; judge each side by its matching rule.

   ### Resolving the upstream pin (for the `[UPSTREAM]` / `[BOTH]` side)
   Inspect this repo's `.github/workflows/*.yml` for every `uses:` / `ref:` that references `shubhodeep1/coding-workflows`; the exact `ref` is the pin. Resolve it to `UPSTREAM_TAG` (the label) and `UPSTREAM_SHA` (the commit) — tag `@vX.Y.Z` via `get_tag` / `list_tags`; a direct SHA as-is; moving `@stable` via `list_tags`; branch `@main` to its current tip (note it may move). Pass `ref=<UPSTREAM_SHA>` on **every** upstream read. Validating an upstream report at `main` when this repo is pinned to a release is a bug — that ref may not match what this repo runs.

4. **Validate the ISSUE** at the ref for its side. Trace it in the code and, where feasible, reproduce it. Decide:
   - **VALID** — a genuine defect; cite the code path (`THIS_REPO`: `file:line`; upstream: `shubhodeep1/coding-workflows@UPSTREAM_TAG (short-sha):file:line`) and the repro result. State the side.
   - **MISCONFIG** — not a code defect: a wrong `@ref` pin, an unset secret / repo-var, a malformed wrapper, a missing input. Identify exactly what must change in this repo's setup.
   - **NOT-REPRODUCIBLE / INVALID** — cannot be traced or reproduced against the actual code.

5. **Validate the FIX** against the actual code at the correct ref:
   - **Root cause, not symptom**; **correct** on re-read; **complete** (edge cases + tests); **safe** — §1 (security/correctness first), §5 (minimal), §6 (no unaliased rename/removal), §10 (collection/index change ships its `/db/contracts/*` update), backward compatibility.
   - **Lands on the right side?** A fix written against this repo's files cannot fix an `[UPSTREAM]` defect, and vice-versa — flag a fix aimed at the wrong layer.
   - Classify: **CORRECT**, **CORRECT-BUT-INCOMPLETE** (needs X), **INCORRECT** (won't fix / breaks Y), or **UNNECESSARY**.

6. **Verdict + recommendation.** State both judgments, the side, and what to do: accept as-is, accept with specific changes, reject + describe the correct fix, or return as a misconfiguration with the exact remediation. Do **not** implement — for `[CONSUMER-INTERNAL]` point at `/investigate-issue` in this repo; for `[UPSTREAM]` tell the user to open a `shubhodeep1/coding-workflows` session.

7. **Report.** Emit the [Output Format](#output-format).

## Output Format

```
Issue: <one-line restatement>
Side: [CONSUMER-INTERNAL] THIS_REPO@main | [UPSTREAM] shubhodeep1/coding-workflows@<UPSTREAM_TAG> (<short-sha>) | [BOTH]
Issue verdict: VALID / MISCONFIG / NOT-REPRODUCIBLE
- Evidence: <file:line or repo@ref (short-sha):file:line; repro result>

Proposed fix: <one-line restatement>
Fix verdict: CORRECT / CORRECT-BUT-INCOMPLETE / INCORRECT / UNNECESSARY
- <why — file:line, §-citations>
- Gaps: <missing tests, §6 rename, §10 contract, aimed at the wrong side>

Recommendation: <accept as-is | accept with changes (list) | reject + the correct fix | return as misconfig: <what to change>>
Next step: <[CONSUMER-INTERNAL]: /investigate-issue here | [UPSTREAM]: open a shubhodeep1/coding-workflows session with this proposed fix>
```

Omit empty sections; every verdict carries a citation.

## Tool Access

Read-only surface:

- **`mcp__github__*` MCP tools** — `issue_read`, `pull_request_read`, `get_file_contents`, `list_commits`, `list_tags`, `get_tag`, `search_issues`, `search_pull_requests`. Consumer side: read `THIS_REPO@main`. Upstream side: pass `ref=<UPSTREAM_SHA>` on every read.
- **`gh` CLI** — when `gh` is installed and `GH_TOKEN` / `GITHUB_TOKEN` is set (verify nounset-safe: `{ [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; } && gh auth status`). **Pass `-R <owner>/<repo>`** — Claude Code Web's remote is a local proxy; bare `gh` fails with `failed to determine base repo`. Use the SessionStart slug for `THIS_REPO`, `shubhodeep1/coding-workflows` for upstream.
- **`Read` / `Grep` / `Glob`** — trace and reproduce on the local checkout. **`Bash`** for read-only inspection / reproduction only.

## Rules

- **Read-only verdict.** This command *judges*; it does not implement. Keep judging and fixing separate so a flawed fix is never applied just because it was proposed.
- **Pick the side from evidence**, and pin upstream reads to this repo's `UPSTREAM_SHA`. A fix lands where the defect lives: `[CONSUMER-INTERNAL]` here, `[UPSTREAM]` in `shubhodeep1/coding-workflows` (a different session). Flag any fix aimed at the wrong side.
- **Two independent judgments.** A real issue can arrive with a wrong fix; an invalid issue can arrive with a pointless fix. Validate the issue and the fix separately.
- **Watch for misconfig dressed as a code bug.** A wrong `@ref` pin, an unset secret/var, or a stale wrapper is **MISCONFIG** — name the exact setup change rather than proposing a code edit.
- **§6 and §10 are hard gates on the fix** — an unaliased public rename, or a collection/index change without its `/db/contracts/*` update, is INCORRECT-as-proposed even when functionally right.
- **Evidence-based.** No verdict without a `file:line` / repro citation. If you cannot trace or reproduce it, say NOT-REPRODUCIBLE.
