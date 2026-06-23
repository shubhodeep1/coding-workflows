Given an issue **and a proposed fix** raised against **this** repo (in `$ARGUMENTS` — an issue URL / number, pasted issue text, a PR carrying the fix, or prose describing both), determine two things: (1) is it a **valid issue** — real, reproducible, and correctly diagnosed; and (2) is the **proposed fix** correct, complete, and safe? The root cause can live on one of two sides — **this repo's own code/config**, or the **upstream workflow library** (`shubhodeep1/coding-workflows`) this repo's wrappers call — and you decide which from the evidence; the side determines where a valid fix would land. Then **act on the verdict**: when the issue is a genuine defect and the correct fix (the proposal, or a corrected/completed version you derive from the evidence) is fully evidence-based, safe, and needs no clarification, **implement it on whichever side this session can push to** — apply, verify, commit, push, open a PR — exactly as `/investigate-issue` would. For a fix that must land upstream from a *different* consumer's session (which cannot push there), stay read-only and hand the user a proposed diff. Anything ambiguous or unsafe stays a read-only verdict + ask.

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS`.** Separate the **reported issue** (symptom, where it surfaced) from the **proposed fix** (a diff, a `file:line` pointer, or prose). Fetch any referenced issue / PR via `mcp__github__issue_read` / `pull_request_read` (or `gh`). If either half is missing, stop and ask. Determine **`THIS_REPO`** — the `owner/repo` the command runs in (the SessionStart hook prints the resolved slug).

2. **Read project context.** Read this repo's `README.md`, `agents.md`, and `CLAUDE.md` (and `/db/contracts/*.yml` if a MongoDB collection is implicated). Read the actual code the report points at — never reason from the report's description alone.

3. **Classify the side that owns the root cause.**
   - **`[CONSUMER-INTERNAL]`** — the defect is in this repo's own code / config / wrapper workflows. Validate at `THIS_REPO@main`. A valid fix lands **here** (read-write: this session can push).
   - **`[UPSTREAM]`** — the defect is in the upstream library, reached through this repo's wrapper at the **ref this repo is pinned to**. Validate the upstream side pinned to `UPSTREAM_SHA` (procedure below). A valid fix lands **upstream**: read-write **only when `THIS_REPO == shubhodeep1/coding-workflows`** (this repo consumes its own templates, so there is no separate downstream); from any other consumer this session **cannot push upstream**, so the fix is read-only — the user takes it to a `shubhodeep1/coding-workflows` session.
   - **`[BOTH]`** — a coordinated defect; judge each side by its matching rule.

   ### Resolving the upstream pin (for the `[UPSTREAM]` / `[BOTH]` side)
   Inspect this repo's `.github/workflows/*.yml` for every `uses:` / `ref:` that references `shubhodeep1/coding-workflows`; the exact `ref` is the pin. Resolve it to `UPSTREAM_TAG` (the label) and `UPSTREAM_SHA` (the commit) — tag `@vX.Y.Z` via `get_tag` / `list_tags`; a direct SHA as-is; moving `@stable` via `list_tags`; branch `@main` to its current tip (note it may move). Pass `ref=<UPSTREAM_SHA>` on **every** upstream read. Validating an upstream report at `main` when this repo is pinned to a release is a bug — that ref may not match what this repo runs. (Exception: when `THIS_REPO == shubhodeep1/coding-workflows`, the upstream side *is* this repo and is validated at `main`; its fix lands on the `stable` branch — see the Decision Rule.)

4. **Validate the ISSUE** at the ref for its side. Trace it in the code and, where feasible, reproduce it. Decide:
   - **VALID** — a genuine defect; cite the code path (`THIS_REPO`: `file:line`; upstream: `shubhodeep1/coding-workflows@UPSTREAM_TAG (short-sha):file:line`) and the repro result. State the side.
   - **MISCONFIG** — not a code defect: a wrong `@ref` pin, an unset secret / repo-var, a malformed wrapper, a missing input. Identify exactly what must change in this repo's setup.
   - **NOT-REPRODUCIBLE / INVALID** — cannot be traced or reproduced against the actual code.

5. **Validate the FIX** against the actual code at the correct ref:
   - **Root cause, not symptom**; **correct** on re-read; **complete** (edge cases + tests); **safe** — §1 (security/correctness first), §5 (minimal), §6 (no unaliased rename/removal), §10 (collection/index change ships its `/db/contracts/*` update), backward compatibility.
   - **Lands on the right side?** A fix written against this repo's files cannot fix an `[UPSTREAM]` defect, and vice-versa — flag a fix aimed at the wrong layer.
   - Classify: **CORRECT**, **CORRECT-BUT-INCOMPLETE** (needs X), **INCORRECT** (won't fix / breaks Y), or **UNNECESSARY**.

6. **Decide and act — driven by the side from Step 3.** First settle on **the correct fix** (the proposal if CORRECT; the proposal plus the missing pieces if CORRECT-BUT-INCOMPLETE; a fix you derive from the evidence if INCORRECT / UNNECESSARY against a VALID issue). Classify every finding as **EVIDENCE-BASED** or **HYPOTHESIS**, then apply the [Decision Rule](#decision-rule): implement the correct fix on whichever side this session can push to, propose it read-only on the side it cannot, and stop + ask on anything ambiguous or unsafe. The two judgments always ship in the report.

7. **Report.** Emit the [Output Format](#output-format) — always, whether the fix was applied or the command stayed a read-only verdict.

## Decision Rule

Settle on the correct fix first (never apply a proposal you judged INCORRECT as-is — derive the real fix from the evidence, the `/investigate-issue` behaviour), classify every finding **EVIDENCE-BASED** vs **HYPOTHESIS**, then:

- **Read-write side** — `[CONSUMER-INTERNAL]`, and `[UPSTREAM]` when `THIS_REPO == shubhodeep1/coding-workflows`:
  - Issue **VALID**, the correct fix fully **EVIDENCE-BASED**, no §6/§10 hard gate, no cross-consumer break, and no missing/inaccessible resource blocking root cause or verification → apply the fix at `THIS_REPO@main` (or, for the self-consuming case where `THIS_REPO == shubhodeep1/coding-workflows`, at `shubhodeep1/coding-workflows@stable` — branch off `stable`), verify (re-run the repro / failing test when feasible), commit, push, open a PR. Set the PR base to `THIS_REPO`'s default branch for the consumer-internal side, and to `stable` for the self-consuming `shubhodeep1/coding-workflows` side — the latter always lands its fixes on `stable`, never `main`, unless the request explicitly names a different target branch. Do not ask. Report with `Fix:` = applied + branch/PR.
  - Any HYPOTHESIS finding, a §6 hard gate (the only correct fix needs an unaliased rename/removal of a public identifier), a §10 hard gate (a collection/index change without its `/db/contracts/*` update), a cross-consumer break, multiple plausible fixes with material tradeoffs, or a blocking inaccessible resource → stop before editing; report the verdicts and ask (CLAUDE.md §2 Q/A format for a §6/§10/tradeoff decision).
- **Read-only side** — `[UPSTREAM]` when `THIS_REPO != shubhodeep1/coding-workflows`:
  - Do **NOT** edit — this session cannot push to the upstream library. Surface the correct fix as a proposed diff with `shubhodeep1/coding-workflows@<UPSTREAM_TAG> (<short-sha>):file:line` anchors and tell the user to open a `shubhodeep1/coding-workflows` session (where `/validate-consumer-issue` or `/investigate-issue` can land it).
- **`[BOTH]`** → implement the side this session can push (the `[CONSUMER-INTERNAL]` side, and the upstream side only when `THIS_REPO == shubhodeep1/coding-workflows`); propose the rest read-only with the matching target-repo label.
- **Issue MISCONFIG** → no code fix on either side; report the exact setup change (wrong `@ref` pin, unset secret/var, malformed wrapper, missing input) the user must make in this repo.
- **Issue NOT-REPRODUCIBLE / INVALID** → no edit; report and ask for what's needed to reproduce.
- **Environmental failure** (service down, rate limit, runner outage) → no fix; say so explicitly.
- Always add/extend a test when landing a fix for a defect that lacked coverage; never edit files on the read-only `[UPSTREAM]` side.

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

Fix: <applied (branch/PR) | proposed (read-only, upstream) | none>
- <file:line — what the correct fix does>

Recommendation: <implemented here (branch/PR) | reject + the correct fix to land | return as misconfig: <what to change> | blocked — ask (reason)>
Next step: <branch/PR to review when applied; [UPSTREAM] from a different consumer: open a shubhodeep1/coding-workflows session with the proposed diff; misconfig: the setup change>
```

Omit empty sections; every verdict carries a citation. When the fix was applied and pushed, the `Fix:` line and `Next step` include the branch/PR link.

## Tool Access

**Reads:**

- **`mcp__github__*` MCP tools** — `issue_read`, `pull_request_read`, `get_file_contents`, `list_commits`, `list_tags`, `get_tag`, `search_issues`, `search_pull_requests`. Consumer side: read `THIS_REPO@main`. Upstream side: pass `ref=<UPSTREAM_SHA>` on every read.
- **`gh` CLI** — when `gh` is installed and `GH_TOKEN` / `GITHUB_TOKEN` is set (verify nounset-safe: `{ [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; } && gh auth status`). **Pass `-R <owner>/<repo>`** — Claude Code Web's remote is a local proxy; bare `gh` fails with `failed to determine base repo`. Use the SessionStart slug for `THIS_REPO`, `shubhodeep1/coding-workflows` for upstream.
- **`Read` / `Grep` / `Glob`** — trace and reproduce on the local checkout. **`Bash`** for read-only inspection / reproduction.

**Writes (only on the read-write side per the [Decision Rule](#decision-rule)):**

- **`Edit` / `Write`** — apply the correct fix against the local `THIS_REPO@main` checkout (or the local `shubhodeep1/coding-workflows@stable` checkout when this repo *is* the library — branch off `stable`), and add/extend the verifying test. **Never edit files on the read-only `[UPSTREAM]` side** — a different consumer's session cannot push there.
- **`Bash`** — `git` (branch off the target ref — `main` for the consumer-internal side, `stable` for the self-consuming `shubhodeep1/coding-workflows` side — commit, `push -u origin <branch>`) and re-running the repro / failing test to verify before pushing.
- **`mcp__github__create_pull_request`** (or `gh pr create`) — open the PR ready for review against the default branch (consumer-internal side), or against the `stable` branch for the self-consuming `shubhodeep1/coding-workflows` side (override only when the request explicitly names another branch).

## Rules

- **Judge, then act on the pushable side.** The two judgments always ship in the report. When the [Decision Rule](#decision-rule) implement gate is met on a side this session can push to, additionally apply → verify → commit → push → open a PR for the correct fix (the `/investigate-issue` behaviour). On the read-only `[UPSTREAM]` side (a different consumer's session), surface a proposed diff instead. When the gate isn't met, stay read-only and report / ask.
- **Implement the *correct* fix, never the flawed proposal.** A CORRECT proposal is applied as-is; a CORRECT-BUT-INCOMPLETE one is completed; an INCORRECT / UNNECESSARY proposal against a real bug is replaced by the fix you derive from the evidence. Never commit a proposed change you judged INCORRECT.
- **Pick the side from evidence**, and pin upstream reads to this repo's `UPSTREAM_SHA`. A fix lands where the defect lives: `[CONSUMER-INTERNAL]` here, `[UPSTREAM]` in `shubhodeep1/coding-workflows` (read-write only when this repo *is* that library; otherwise a different session). Flag any fix aimed at the wrong side, and **never edit a repo this session cannot push to**.
- **Two independent judgments.** A real issue can arrive with a wrong fix; an invalid issue can arrive with a pointless fix. Validate the issue and the fix separately — even when you go on to implement.
- **Watch for misconfig dressed as a code bug.** A wrong `@ref` pin, an unset secret/var, or a stale wrapper is **MISCONFIG** — name the exact setup change rather than editing code.
- **§6 and §10 are hard gates on the fix** — a correct fix that needs an unaliased public rename/removal, or a collection/index change without its `/db/contracts/*` update, is **not** auto-applied even when functionally right; route to the §2 Q/A ask path.
- **Evidence-based.** No verdict, and no fix, without a `file:line` / repro citation. If you cannot trace or reproduce it, say NOT-REPRODUCIBLE.
- **Add or extend a test** when landing a fix for a defect that lacked coverage; do not ship a behaviour fix without verification.
- **Forbidden silent moves** (when implementing): editing tests to pass without evidence the test is wrong, broadening `except`/`catch`, suppressing warnings, version-bumping without verified compatibility, adding retries to mask deterministic failures.
