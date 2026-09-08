Implement the plan documented at the path / reference in `$ARGUMENTS` **directly in this Claude Code session**, then **re-verify it is fully implemented**, and **only if it is complete** move the plan doc into `docs/completed/`. Commit, push, and open a PR. This is the **in-session** variant — Claude writes the code here and now. To hand the plan to the unattended **AI orchestrator** instead (which decomposes the work into a dependency DAG of issues and ships each phase as its own PR), use `/implement-plan-ai`. `$ARGUMENTS` is **free-form prose** that must resolve to a single plan markdown doc — typically a path like `docs/plans/<slug>-plan.md`, but it may also be a slug, a topic that matches one plan filename, or a GitHub reference (issue / PR) that links to the plan. Optional trailing prose (focus areas, a phase to start with) is allowed but not required.

$ARGUMENTS

## Procedure

1. **Resolve the plan doc.** Parse `$ARGUMENTS` for a path under `docs/plans/` (or elsewhere), a slug, or a reference that links to a plan. Resolve it to exactly one markdown file and `Read` it in full. If `$ARGUMENTS` is empty, matches no plan, or matches more than one and the intent is ambiguous, **stop and ask** which plan to implement — never guess. If it names a GitHub issue / PR, fetch that first (see [Tool Access](#tool-access)) to find the linked plan file.

2. **Read project context.** Always read `README.md`, `agents.md`, and `CLAUDE.md` at the repo root before editing. If the plan touches a MongoDB collection, also read every relevant `/db/contracts/*.yml` (CLAUDE.md §10). Read the actual source for every file the plan names — never guess at code, env vars, or workflow inputs.

3. **Build an implementation checklist.** Convert the plan into a tracked checklist (CLAUDE.md §11): one item per goal, phase, implementation step, file/module, data-model change, test, and rollout action the plan specifies. Keep it visible and update it as you go. This same checklist is what step 5 verifies against.

4. **Implement.** Work through the plan's phases / steps in order. Honor §5 (minimal change set — implement what the plan specifies, no speculative extras), §6 (naming immutability — renames are breaking; add aliases), §9 (style — tabs except where the format forbids them; YAML stays 2-space), §10 (MongoDB — index registry, contracts, unique-index safety), and §18 (automation bias — wire new operations into the scheduler; no standalone manual scripts; DB work runs from code with a gate). Write the code, update `/db/contracts/*` and indexes where touched, add/extend the tests the plan calls for, and update `README.md` / `agents.md` when behavior changes (§7). If the plan introduces a single-use or long-running script/supervisor, add its entry to `docs/scripts-pending-removal.md` in this same PR (§18.F).

5. **Re-verify completeness — evidence-based, not vibes.** Re-read the plan's goals / acceptance criteria and check **each** checklist item against the *actual* repo state:
   - Run the test suite and linters the plan specifies (and the repo's standard checks); record pass/fail.
   - `Grep` / `Read` to confirm the code, config, workflow wiring, and contracts the plan describes actually exist and are correct.
   - Confirm rollout wiring is in place (trigger / schedule / flag / dispatch per §18).
   Classify every item **complete** (present + verified, with a `file:line` / test-name citation), **partial**, or **not-done**.

6. **Decide — move the doc only on a clean sweep.**
   - **All items complete & verified** → `git mv docs/plans/<slug>-plan.md docs/completed/<slug>-plan.md` (create `docs/completed/` if missing). Use `git mv` so history is preserved.
   - **Any item incomplete** → do **NOT** move the doc. If the remaining work is in-scope and safe to finish in this PR, keep implementing and re-run step 5. If it's blocked or genuinely too large to complete + verify safely in one PR, stop, leave the doc in `docs/plans/`, and report exactly what remains (per the [Output Format](#output-format)).

7. **Branch, commit, push, open PR.** Create a branch `claude/implement-plan-<slug>` (append `-2`, `-3`, … on collision). Group commits by scope (§12.E): the implementation commit(s), then a separate commit for the doc move + any `README.md` / `agents.md` updates. Push with `git push -u origin <branch>` (retry transient network errors with exponential backoff: 2s, 4s, 8s, 16s — up to 4 retries). Open a PR via `mcp__github__create_pull_request` against the default branch (resolve it dynamically — do **not** hardcode `main`), `draft: false`. **PR-body keyword discipline (§19):** if the plan links an `ai:orchestrator-tracking` issue, use `Refs #N` / `Related to #N`, never `Fixes/Closes/Resolves #N`.

8. **Report.** Emit the [Output Format](#output-format) in chat.

## Output Format

```
Plan: <title>  (docs/plans/<slug>-plan.md)
Status: COMPLETE / PARTIAL

Implemented:
- <plan item> — <evidence: file:line, passing test name, merged wiring>

Remaining (only if PARTIAL):
- <plan item> — <what's missing / the blocker>

Verification: <tests run + result; linters; key greps>
Doc: moved to docs/completed/<slug>-plan.md   (or: left in docs/plans/ — not yet complete)
Branch: <branch>   PR: <url>
```

No prose padding. A bare "done — see PR" is not acceptable: the user wants the status, the evidence, the verification result, and where the doc ended up.

## Tool Access

- **`mcp__github__*` MCP tools** — always available. Use `mcp__github__create_pull_request` to open the PR; `mcp__github__get_file_contents` / `pull_request_read` / `issue_read` / `list_branches` for research and branch-collision checks.
- **`gh` CLI** — the `GH_TOKEN` transport. Shared rules live in **CLAUDE.md §23** and are not restated here: availability and the nounset-safe auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene, and the self-serve-read / ask-first-mutation split (§23.A–E). Use for default-branch detection: `gh repo view --json defaultBranchRef -q .defaultBranchRef.name -R <owner>/<repo>`. Pushing the working branch and opening the PR are §23.B routine writes — self-serve; §23.C operations still need the §2 Q/A ask.

Local file reads use `Read`; code search uses `Grep` / `Glob`; git + test/lint runs use `Bash`.

## Rules

- **Never move the doc unless every plan item is verified complete**, each with a concrete `file:line` / test citation. A partial implementation leaves the doc in `docs/plans/` — moving it to `docs/completed/` is the signal that the project is *done*, so it must be earned by evidence.
- **Verification is evidence-based.** Run the tests and linters; read the code; confirm the wiring. Do not assume a step is done because you wrote it — confirm it against the repo.
- **Honor the project rules while implementing:** §5 (minimal change set), §6 (naming immutability — add aliases, never rename in place), §7 (update `README.md` / `agents.md` on behavior change), §9 (style), §10 (MongoDB contracts + index registry), §14 (consumer-repo registry if templates/`.claude` assets change), §15 (GitHub API hygiene — batch/reuse calls), §18 (automation bias + future-removal registry), §19 (PR-body auto-close discipline).
- **Use `git mv` for the move** so the plan's history follows it into `docs/completed/`.
- **If a plan step belongs in the upstream workflow library rather than this repo**, do not edit the upstream from this session — implement the parts that belong here and surface the upstream-bound step in the report instead of guessing.
- **Branch + push + PR; never push to the default branch.** The PR is the deliverable, not a local commit.
- **If the plan is ambiguous or under-specified** such that implementing it correctly would require guessing intent, stop and ask (§0/§2) rather than shipping a guess.
- **In-session only — for orchestrator hand-off use `/implement-plan-ai`.** This command implements the plan itself. If the plan is better executed by the unattended AI orchestrator (it is already split into independently-mergeable phases and is meant to ship as a multi-PR project), use `/implement-plan-ai` instead, which dispatches the orchestrator rather than implementing here.
