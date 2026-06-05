Process every recommendation doc under `analysis/` — validate each recommendation against the *current* repo, **apply** the ones that are correct and safe to land without breaking existing processes/flows, **delete** the source docs that have been processed, and write a **processing report** under `analysis/`. Then branch, push, and open a PR. `$ARGUMENTS` is optional — pass a filter (a specific doc, a date, or a glob) to scope the run; empty means process all recommendation docs under `analysis/`.

$ARGUMENTS

## Procedure

1. **Read project context.** Read `README.md`, `agents.md`, and `CLAUDE.md` first. If any recommendation touches a MongoDB collection, also read the relevant `/db/contracts/*.yml` (§10).

2. **Enumerate the source docs.** List the recommendation / analysis markdown docs under `analysis/` (e.g. `analysis/workflow-optimization-*.md`). Honor any `$ARGUMENTS` filter. **Exclude** non-recommendation files: state files (`last_collection_timestamp.txt`, `validation-selftest-status.json`) and prior reports (`recommendation-processing-report*.md`). Read each source doc in full and build a tracked checklist (§11) of every distinct recommendation across all docs.

3. **Classify each recommendation against the current repo.** For every recommendation, re-read the code it targets and decide:
   - **VALID & SAFE** — the diagnosis is correct, it matches current code, the change is low blast-radius and reversible, and it will not break existing processes/flows. No §6 (naming) or §10 (contract) violation. → **apply** (step 4).
   - **VALID BUT RISKY** — correct, but high blast-radius / touches a public contract or hot path / has a material tradeoff / requires an identifier rename (§6) / requires a DB contract or index change (§10) / is ambiguous. → do **not** auto-apply; record as **recommended — not applied (needs review)** with the rationale.
   - **STALE / ALREADY DONE** — the repo already does this, or the code it targets no longer exists. → mark obsolete, no action.
   - **INVALID** — wrong on re-read of the actual code. → reject with the reason.
   Weigh each per §12.C: reversibility, blast radius, confidence in the diagnosis, test coverage, and §6/§10 conflicts.

4. **Apply the VALID & SAFE set.** Make the edits with a minimal change set (§5), honoring §6 (add aliases, never rename in place), §9 (style), §10 (contracts + index registry), and §18 (wire new automation into the scheduler; DB work runs from code with a gate; register any new single-use/long-running script in `docs/scripts-pending-removal.md`). Add or extend tests for behavior changes, and update `README.md` / `agents.md` when behavior changes (§7). After applying, run the repo's linters and tests; if a recommendation breaks something, fix it or back that one out and reclassify it as **not applied**.

5. **Delete processed source docs.** For every source doc whose recommendations are fully triaged (each applied, deferred-with-rationale, obsolete, or rejected — i.e. nothing in it still needs to live under `analysis/`), `git rm` the file. **Retain the filename in the report for provenance** (this matches the existing `recommendation-processing-report.md` convention). Never delete the excluded state files or prior reports.

6. **Write the processing report.** Update `analysis/recommendation-processing-report.md` — the established canonical report name — by folding in prior provenance and appending this pass (do not silently clobber prior content; the existing report explicitly accumulates provenance). The report MUST contain:
   - A grounding note that "actioned" reflects current repo state on this ref.
   - **Processed source docs** — the filenames deleted in step 5, retained for provenance.
   - Every recommendation with its classification and, for applied ones, the `file:line` and a one-line rationale; for not-applied ones, why deferred; for obsolete/rejected ones, the evidence.

7. **Branch, commit, push, open PR.** Branch `claude/apply-analysis-<date-or-slug>` (append `-2`, `-3`, … on collision). Commit by scope (§12.E): one commit for the applied fixes (grouped by theme), a separate commit for the source-doc deletions + the report. Push with `git push -u origin <branch>` (retry transient errors: 2s, 4s, 8s, 16s). Open a PR via `mcp__github__create_pull_request` against the dynamically-resolved default branch, `draft: false`. The PR description MUST enumerate the applied changes and, separately, the **not-applied (needs review)** list so reviewers can find them (§12.E). Honor §19 keyword discipline for any linked tracking issue.

8. **Report.** Emit the [Output Format](#output-format) in chat.

## Output Format

```
Analysis docs processed: N

Recommendations: <total>  (applied: A, not-applied/needs-review: B, obsolete: C, rejected: D)

Applied:
- <recommendation> — <file:line> — <rationale>

Not applied (needs review):
- <recommendation> — <why deferred: blast radius / §6 rename / §10 contract / tradeoff>

Obsolete / already done:
- <recommendation> — <evidence>

Rejected:
- <recommendation> — <why invalid>

Deleted source docs: <list, or "none">
Report: analysis/recommendation-processing-report.md
Verification: <linters + tests run, result>
Branch: <branch>   PR: <url>
```

## Tool Access

- **`Read` / `Grep` / `Glob`** — read source docs and verify each recommendation against the actual code.
- **`Edit` / `Write`** — apply the safe recommendations and write the report.
- **`Bash`** — `git rm` / `git mv`, run linters and tests, git branch/commit/push.
- **`mcp__github__*` / `gh` CLI** — open the PR (`mcp__github__create_pull_request`); resolve the default branch (`gh repo view --json defaultBranchRef -q .defaultBranchRef.name -R <owner>/<repo>`). Pass `-R <owner>/<repo>` on `gh` calls; verify auth nounset-safe (`{ [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; } && gh auth status`).

## Rules

- **Apply only VALID & SAFE recommendations.** Anything risky — high blast-radius, public-contract or hot-path impact, a §6 rename, a §10 contract/index change, or a real tradeoff — is **listed, not applied**. The bar for auto-applying is "correct *and* safe to land without breaking existing flows," not just "correct."
- **§1 priority order binds.** Security and correctness outrank performance and speed — never apply a performance recommendation that risks correctness or safety.
- **§6 and §10 are hard gates.** A recommendation that renames/removes a public identifier without an alias, or changes a collection/index without the matching `/db/contracts/*` update, is **not applied** even when functionally correct — record it as needs-review.
- **Verify before deleting.** Only `git rm` a source doc once all its recommendations are triaged, and always retain its filename in the report for provenance. **Never** delete `last_collection_timestamp.txt`, `validation-selftest-status.json`, or any `recommendation-processing-report*.md`.
- **Run linters + tests after applying.** If a change breaks the build/tests, fix it or back it out before opening the PR — do not ship a red branch.
- **If a recommendation belongs in the upstream workflow library** rather than this repo, do not apply it here — list it as "not applied — belongs upstream."
- **§18 automation bias.** A recommendation that adds a recurring operation must be wired into an existing scheduler/workflow (no standalone manual scripts); DB operations run from code behind a gate. Register any new single-use/long-running script in `docs/scripts-pending-removal.md` in this PR.
- **Branch + push + PR; never push to the default branch.** The PR description separates applied fixes from the needs-review list.
