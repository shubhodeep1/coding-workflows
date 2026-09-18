Given an issue **and a proposed fix reported from a consumer repo** (in `$ARGUMENTS` — an issue URL / number, pasted issue text, a PR carrying the fix, or prose describing both), determine two things about **this** upstream library (`shubhodeep1/coding-workflows`) at its **[target ref](#target-ref)** (default `stable` — the ref consumers pin and actually run, not `main`): (1) is it a **valid issue** — real, reproducible, and correctly diagnosed as an upstream bug rather than consumer misconfiguration; and (2) is the **proposed fix** correct, complete, and safe to land here? Then **act on the verdict**: when the issue is a genuine upstream defect and the correct fix (the proposed one, or a corrected/completed version you derive from the evidence) is fully evidence-based, safe, and needs no clarification, **implement it here** — apply, verify, commit, push, open a PR — exactly as `/investigate-issue` would. Otherwise this stays a read-only verdict: report the two judgments and ask / route, never landing a flawed or ambiguous fix.

$ARGUMENTS

## Target ref

Everything this command reads, traces, reproduces, branches from, and opens its PR against is the **target ref**. Pick it once, up front, and state it in the report:

- **Default — `stable`.** Validate against `stable` and land the fix there. Consumers pin `@stable`, so that is the ref the failing run actually executed and the ref a fix must reach; a fix landed on `main` does not reach them until the next `main → stable` promotion. `stable` is **both a branch and a tag** — branch off the **branch**: `git fetch origin stable` then branch off `origin/stable`, and give the working branch a `-stable` suffix so the target is obvious. Open the PR with **base = `stable`**.
- **Explicit `main`.** Use `main` only when it is explicitly selected or specified — e.g. `$ARGUMENTS` says "implement on main" / "target main" / "land on main" / "@main", or the report explicitly states the consumer's upstream ref is `main`. Then branch off, validate against, and PR into `main`.
- **Explicit other ref.** If the report names some other pinned ref (a version tag or another branch), prefer **that** ref over the `stable` default — it is what the consumer runs.

Always state in the report which target ref was chosen and why: **default-stable**, **explicit-main**, or **explicit-other**. When the fix lands on `stable` only, add the regression caveat to the report and PR body: it should also be ported to `main`, or the next `main → stable` promotion will silently revert it.

## Procedure

1. **Parse `$ARGUMENTS`.** Separate the two halves: the **reported issue** (the symptom, where it surfaced, which consumer repo + which upstream ref it was running if stated) and the **proposed fix** (a diff, a `file:line` pointer, or a prose description). Fetch any referenced issue / PR via `mcp__github__issue_read` / `pull_request_read` (or `gh`). If neither a clear issue nor a fix is present, stop and ask for the missing half. Determine the **[target ref](#target-ref)** for this run (default `stable`; `main` or another ref only when explicitly selected / named).

2. **Read project context.** Read `README.md`, `agents.md`, and `CLAUDE.md`. If the issue touches a MongoDB collection, read the relevant `/db/contracts/*.yml` (§10). Read the actual upstream code the report implicates — the scripts, workflows, and prompts at the **[target ref](#target-ref)** (default `stable`) — never reason from the report's description alone.

3. **Validate the ISSUE.** Trace it in this library's code at the **[target ref](#target-ref)** (default `stable`) and, where feasible, reproduce it. Decide:
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

5. **Decide and act.** Apply the [Decision Rule](#decision-rule) below — implement the correct fix here when the gate is met, otherwise stay read-only and report + ask. The two judgments from Steps 3–4 always ship in the report regardless.

6. **Report.** Emit the [Output Format](#output-format) — always, whether the fix was applied or the command stayed a read-only verdict.

## Decision Rule

First settle on **the correct fix** — the change that actually resolves the validated issue, never a flawed proposal applied as-is:

- Proposed fix **CORRECT** → that is the fix; apply it as proposed.
- **CORRECT-BUT-INCOMPLETE** → the fix is the proposal plus the missing pieces (edge cases, tests) that make it complete.
- **INCORRECT** or **UNNECESSARY**, but the issue is **VALID-UPSTREAM** → derive the correct fix from the evidence. This is the `/investigate-issue` behaviour: find the real solution rather than landing the flawed proposal.

Then classify every finding — the issue diagnosis and the fix's correctness — as **EVIDENCE-BASED** (supported by code reading at the **[target ref](#target-ref)** (default `stable`) plus reproduction where feasible) or **HYPOTHESIS** (plausible but unverified). Then:

- **Issue is VALID-UPSTREAM, the correct fix is fully EVIDENCE-BASED, it lands in THIS repo (`shubhodeep1/coding-workflows`), it triggers no §6/§10 hard gate and no cross-consumer break, and no missing/inaccessible resource blocks root cause or fix verification** → design the fix, branch off the **[target ref](#target-ref)** (default `stable` — `git fetch origin stable` then branch off `origin/stable`, naming the working branch with a `-stable` suffix) and apply it there, verify it (re-run the repro / failing test when feasible), commit, push, open a PR **with base = the target ref** (default `stable`). This command lands its fixes on `stable` by default — never `main` unless `main` (or another ref) is explicitly selected per [Target ref](#target-ref). Do not ask. Then report using the [Output Format](#output-format) with `Fix:` = applied, the chosen target ref + why, and the branch/PR link; when the fix landed on `stable` only, include the port-to-`main` caveat. Non-blocking gaps still get listed for transparency.
- **Issue is CONSUMER-MISCONFIG** → do **not** edit this library; the remedy lives on the consumer's side. Report the exact configuration change the consumer must make. A code fix here would address the wrong layer — flag that.
- **Issue is NOT-REPRODUCIBLE / INVALID** → do not edit; report and ask for what's needed to reproduce.
- **Otherwise** — any HYPOTHESIS finding; a §6 hard gate (the only correct fix needs an unaliased rename/removal of a public identifier); a §10 hard gate (a collection/index change without its `/db/contracts/*` update); a fix that would break other consumers pinned to this library; multiple plausible fixes with material tradeoffs; or a missing/inaccessible resource that blocks root cause or fix verification → **stop before editing**. Report the two verdicts using the [Output Format](#output-format) and ask the user how to proceed (use the CLAUDE.md §2 Q/A format for a §6/§10/tradeoff decision).
- **Environmental failure** (service down, rate limit, runner outage) → no fix; say so explicitly.

## Output Format

```
Issue: <one-line restatement; consumer repo + upstream ref if known>
Target ref: <stable (default-stable) | main (explicit-main) | <ref> (explicit-other)> — <why>
Issue verdict: VALID-UPSTREAM / CONSUMER-MISCONFIG / NOT-REPRODUCIBLE
- Evidence: <file:line, code path, repro result>

Proposed fix: <one-line restatement>
Fix verdict: CORRECT / CORRECT-BUT-INCOMPLETE / INCORRECT / UNNECESSARY
- <why — file:line, §-citations>
- Gaps: <missing tests, §6 rename, §10 contract, breaks other consumers>

Fix: <applied | none — read-only verdict>
- <file>:<line> — <one-line rationale; what the correct fix does>
- Caveat (stable-only fix): also port to `main`, or the next `main → stable` promotion will silently revert it.

Recommendation: <implemented here (branch/PR) | reject + the correct fix to land | return to consumer (misconfig: <what they must change>) | blocked — ask (reason)>
Next step: <branch/PR to review when applied; else the consumer-side change, or what's needed to unblock>
```

Omit empty sections; every verdict carries a citation. When the fix was applied and pushed, the `Fix:` line and `Next step` include the branch/PR link.

## Tool Access

**Reads (at the [target ref](#target-ref), default `stable`):**

- **`mcp__github__*` MCP tools** — `issue_read`, `pull_request_read`, `get_file_contents`, `list_commits`, `search_issues`, `search_pull_requests` for the report, the proposed-fix PR, and the implicated code.
- **`gh` CLI** — the `GH_TOKEN` transport; shared rules live in **CLAUDE.md §23** (auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene) — see **CLAUDE.md §23**. Use `shubhodeep1/coding-workflows` for upstream reads and the consumer's slug when reading the consumer's wrapper to confirm a misconfig — both are §23.A reads.
- **`Read` / `Grep` / `Glob`** — trace and reproduce against the local **target-ref** checkout (default `stable`). **`Bash`** for read-only inspection / reproduction.

**Writes (only when the [Decision Rule](#decision-rule) implement gate is met):**

- **`Edit` / `Write`** — apply the correct fix against the local **target-ref** checkout (default `stable`; branch off `origin/stable` with a `-stable`-suffixed working branch), and add/extend the test that verifies it.
- **`Bash`** — `git` (for the default: `git fetch origin stable` then branch off `origin/stable`; otherwise the chosen target ref — commit, `push -u origin <branch>`) and re-running the repro / failing test to verify the fix before pushing.
- **`mcp__github__create_pull_request`** (or `gh pr create`) — open the PR ready for review against the **[target ref](#target-ref)** (default `stable`; `main` or another branch only when explicitly selected).

## Rules

- **Judge, then act.** The two judgments always ship in the report. When the [Decision Rule](#decision-rule) implement gate is met, additionally apply → verify → commit → push → open a PR for the correct fix (the `/investigate-issue` behaviour). When it isn't met, stay read-only and report / ask — never land a flawed or ambiguous fix just because it was proposed.
- **Target ref defaults to `stable`.** Read, trace, reproduce, branch, and PR against the [target ref](#target-ref) — `stable` by default (what consumers pin and run), `main` or another ref only when explicitly selected / named. State the chosen ref + why (default-stable / explicit-main / explicit-other) in the report, and when landing on `stable` only, flag that it must also be ported to `main` or the next `main → stable` promotion will silently revert it.
- **Implement the *correct* fix, never the flawed proposal.** A CORRECT proposal is applied as-is; a CORRECT-BUT-INCOMPLETE one is completed; an INCORRECT / UNNECESSARY proposal against a real bug is replaced by the fix you derive from the evidence. Never commit a proposed change you judged INCORRECT.
- **Two independent judgments.** A real issue can arrive with a wrong fix; an invalid issue can arrive with a tempting but pointless fix. Validate the issue and the fix separately and report both — even when you go on to implement.
- **Watch for misconfig dressed as an upstream bug.** A wrong `@ref` pin, an unset secret/var, or a stale wrapper is **CONSUMER-MISCONFIG**, not a library defect — never gets a code fix here; tell the consumer exactly what to change.
- **§6 and §10 are hard gates on the fix.** A correct fix that requires an unaliased rename/removal of a public identifier (§6), or a collection/index change without its `/db/contracts/*` update (§10), is **not** auto-applied even when functionally right — route to the §2 Q/A ask path; add the alias / ship the contract update only with the user's go-ahead.
- **Weigh cross-consumer blast radius.** As the upstream library, a fix here ships to every consumer — a change that helps one and breaks another must not be auto-applied; surface it and ask.
- **Evidence-based.** No verdict, and no fix, without a `file:line` / repro citation. If you cannot trace or reproduce the issue, say NOT-REPRODUCIBLE rather than guessing.
- **Add or extend a test** when landing a fix for a defect that lacked coverage; do not ship a behaviour fix without verification.
- **Forbidden silent moves** (when implementing): editing tests to pass without evidence the test is wrong, broadening `except`/`catch`, suppressing warnings, version-bumping without verified compatibility, adding retries to mask deterministic failures.
