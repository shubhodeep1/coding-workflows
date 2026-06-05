Given an issue **and a proposed fix reported from a consumer repo** (in `$ARGUMENTS` — an issue URL / number, pasted issue text, a PR carrying the fix, or prose describing both), determine two things about **this** upstream library (`shubhodeep1/coding-workflows`) at `main`: (1) is it a **valid issue** — real, reproducible, and correctly diagnosed as an upstream bug rather than consumer misconfiguration; and (2) is the **proposed fix** correct, complete, and safe to land here? Read-only verdict: this command reports a judgment in chat and does **not** implement the fix.

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS`.** Separate the two halves: the **reported issue** (the symptom, where it surfaced, which consumer repo + which upstream ref it was running if stated) and the **proposed fix** (a diff, a `file:line` pointer, or a prose description). Fetch any referenced issue / PR via `mcp__github__issue_read` / `pull_request_read` (or `gh`). If neither a clear issue nor a fix is present, stop and ask for the missing half.

2. **Read project context.** Read `README.md`, `agents.md`, and `CLAUDE.md`. If the issue touches a MongoDB collection, read the relevant `/db/contracts/*.yml` (§10). Read the actual upstream code the report implicates — the scripts, workflows, and prompts on `main` — never reason from the report's description alone.

3. **Validate the ISSUE.** Trace it in this library's code at `main` and, where feasible, reproduce it. Decide:
   - **VALID-UPSTREAM** — a genuine defect in the library's own code / workflow / prompt. Cite the code path (`file:line`) and the repro result.
   - **CONSUMER-MISCONFIG** — not our bug. The symptom comes from the consumer's setup: a stale or wrong `@ref` pin, an unset secret / repo-var, a malformed wrapper workflow, a missing input. Identify exactly what the consumer must fix on their side.
   - **NOT-REPRODUCIBLE / INVALID** — cannot be traced or reproduced; the diagnosis does not hold against the actual code.
   A consumer-reported symptom is often a misconfiguration dressed as an upstream bug — distinguish them deliberately, because the remedy differs (fix the library vs. tell the consumer what to change).

4. **Validate the FIX.** Evaluate the proposed change against the actual code (do this even if the issue is CONSUMER-MISCONFIG — the fix may still be informative, but flag that it addresses the wrong layer):
   - **Root cause, not symptom** — does it fix the underlying defect or just paper over the symptom?
   - **Correct** — on re-reading the real code, does it do what it claims without introducing a new defect?
   - **Complete** — does it cover the edge cases and add/extend the tests the change warrants?
   - **Safe** — does it honor §1 (security/correctness first), §5 (minimal), §6 (no unaliased rename/removal of a public identifier — consumers depend on these), §10 (any collection/index change ships its `/db/contracts/*` update), and backward compatibility?
   - **Cross-consumer blast radius** — would it fix this consumer but break others pinned to the same library? This is the upstream maintainer's special duty.
   Classify the fix: **CORRECT**, **CORRECT-BUT-INCOMPLETE** (needs X), **INCORRECT** (won't fix / breaks Y), or **UNNECESSARY**.

5. **Verdict + recommendation.** State both judgments and what to do: accept the fix as-is, accept with specific changes, reject and describe the correct fix, or return it to the consumer as a misconfiguration with the exact remediation. Do **not** implement — point the user at `/investigate-issue` (to land a corrected upstream fix) or back to the consumer.

6. **Report.** Emit the [Output Format](#output-format).

## Output Format

```
Issue: <one-line restatement; consumer repo + upstream ref if known>
Issue verdict: VALID-UPSTREAM / CONSUMER-MISCONFIG / NOT-REPRODUCIBLE
- Evidence: <file:line, code path, repro result>

Proposed fix: <one-line restatement>
Fix verdict: CORRECT / CORRECT-BUT-INCOMPLETE / INCORRECT / UNNECESSARY
- <why — file:line, §-citations>
- Gaps: <missing tests, §6 rename, §10 contract, breaks other consumers>

Recommendation: <accept as-is | accept with changes (list) | reject + the correct fix | return to consumer (misconfig: <what they must change>)>
Next step: <e.g. /investigate-issue to land the corrected fix, or the consumer-side change to make>
```

Omit empty sections; every verdict carries a citation.

## Tool Access

Read-only surface (read at `main`):

- **`mcp__github__*` MCP tools** — `issue_read`, `pull_request_read`, `get_file_contents`, `list_commits`, `search_issues`, `search_pull_requests` for the report, the proposed-fix PR, and the implicated code.
- **`gh` CLI** — when `GH_TOKEN` / `GITHUB_TOKEN` is set (verify nounset-safe: `{ [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; } && gh auth status`). **Pass `-R <owner>/<repo>`** — Claude Code Web's remote is a local proxy; bare `gh` fails with `failed to determine base repo`. Use `shubhodeep1/coding-workflows` for upstream reads and the consumer's slug when reading the consumer's wrapper to confirm a misconfig.
- **`Read` / `Grep` / `Glob`** — trace and reproduce against the local `main` checkout. **`Bash`** for read-only inspection / reproduction only.

## Rules

- **Read-only verdict.** This command *judges*; it does not implement. Once a fix is accepted, the user lands it via `/investigate-issue` or a direct change — keep these responsibilities separate so a flawed fix is never applied just because it was proposed.
- **Two independent judgments.** A real issue can arrive with a wrong fix; an invalid issue can arrive with a tempting but pointless fix. Validate the issue and the fix separately and report both.
- **Watch for misconfig dressed as an upstream bug.** A wrong `@ref` pin, an unset secret/var, or a stale wrapper is **CONSUMER-MISCONFIG**, not a library defect — say so and tell the consumer exactly what to change rather than "fixing" the library.
- **§6 and §10 are hard gates on the fix.** A proposed change that renames/removes a public identifier without an alias, or alters a collection/index without the `/db/contracts/*` update, is **INCORRECT-as-proposed** even when functionally right — consumers and the contract depend on those.
- **Weigh cross-consumer blast radius.** As the upstream library, a fix here ships to every consumer — a change that helps one and breaks another is not acceptable; call that out.
- **Evidence-based.** No verdict without a `file:line` / repro citation. If you cannot trace or reproduce the issue, say NOT-REPRODUCIBLE rather than guessing.
