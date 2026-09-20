Hand the plan documented at the path / reference in `$ARGUMENTS` to the **AI orchestrator** (the unattended pipeline) for implementation, by dispatching the orchestrator's `workflow_dispatch` trigger with the plan fed in as the `project_description`. The orchestrator clarifies scope, decomposes the work into a dependency DAG of issues, opens a tracking issue, and ships each phase as its own PR. This command does **not** implement the plan in this session and opens **no** PR of its own — it kicks off the orchestrator and reports the dispatched run + tracking issue. For in-session implementation by Claude, use `/implement-plan-claude` instead. `$ARGUMENTS` is **free-form prose** that must resolve to a single plan markdown doc — typically a path like `docs/plans/<slug>-plan.md`, but it may also be a slug, a topic that matches one plan filename, or a GitHub reference (issue / PR) that links to the plan. Optional trailing prose (an emphasis to pass through to the orchestrator) is allowed but not required.

$ARGUMENTS

## Procedure

1. **Resolve the plan doc.** Parse `$ARGUMENTS` for a path under `docs/plans/` (or elsewhere), a slug, or a reference that links to a plan. Resolve it to exactly one markdown file and `Read` it in full. If `$ARGUMENTS` is empty, matches no plan, or matches more than one and the intent is ambiguous, **stop and ask** which plan to hand off — never guess. If it names a GitHub issue / PR, fetch that first (see [Tool Access](#tool-access)) to find the linked plan file.

2. **Read enough context to compose an accurate hand-off.** Read the plan in full, plus `README.md`, `agents.md`, and `CLAUDE.md` at the repo root for naming/context. You specifically need the plan's title, its Summary, and its **Phases & Merge Strategy** section, so the dispatched description can tell the orchestrator to preserve the plan's phase split. This command makes no code changes, so `/db/contracts/*` reads are only needed if you must understand the plan's terminology.

3. **Resolve a remote-reachable reference to the plan (reference-only — do not inline the whole plan).** The orchestrator runs on the repo's default branch and reads `project_description` as prose; its codex agent has `gh`/`git` tool access and can fetch a branch or PR. Resolve, in order of preference:
   - **On the default branch already** → reference the plan by its default-branch path.
   - **Committed on a branch, not yet merged** → ensure that branch is pushed to the remote (`git push -u origin <branch>` if it is local-only — retry transient network errors with exponential backoff 2s/4s/8s/16s, up to 4 retries). Find the plan's open PR if one exists (`mcp__github__list_pull_requests` with head=`<branch>`, or `gh pr list --head <branch> -R <owner>/<repo>`). Record path + branch + PR URL.
   - **Uncommitted / not reachable on the remote** → **stop and ask** the user to commit + push the plan (or point you at its branch / PR). The orchestrator cannot read a plan that exists only in the local working tree.
   Pushing the plan's branch is **not** opening a PR — this command still opens no PR of its own (the orchestrator owns all implementation PRs).

4. **Locate the orchestrator dispatch workflow.** Look in `.github/workflows/` and pick the first that exists:
   - `ai-orchestrate.yml` — the consumer-repo wrapper (calls `orchestrate.yml@stable`).
   - `internal-orchestrate.yml` — this library's own wrapper (calls `orchestrate.yml@main`).
   Both expose `workflow_dispatch` with a single required `project_description` input. If neither exists, **stop and report** that this repo has no orchestrator trigger wired (it is not orchestrator-enabled) — do not invent a trigger.

5. **Compose `project_description` (reference-only).** Build a single string with three parts:
   - **First line = a concise project title** derived from the plan's title. The orchestrator uses the first line of `project_description` as the tracking-issue title (`orchestrate.yml` reads `head -n 1`), so keep it ≤ ~200 chars and descriptive — a title, not an instruction.
   - **An instruction preamble** stating this is a *pre-written, already-clarified* implementation plan that is the authoritative spec: the orchestrator must read the FULL plan before decomposing, and must preserve the plan's existing **Phases & Merge Strategy** split (one independently-mergeable, production-safe PR per phase) when building the issue DAG.
   - **A reference block**: the plan's repo-relative path, its branch, and its PR URL, with an explicit instruction to read the plan from that branch/PR if it is not yet on the default branch (e.g. `git fetch origin <branch> && git show origin/<branch>:<path>`, or `gh pr view <url>`).
   Do **not** paste the plan's full body into `project_description` — keep it reference-only.

6. **Dispatch the orchestrator.** Trigger the workflow chosen in step 4:
   - `gh workflow run <workflow-file> -R <owner>/<repo> -f project_description="$DESC"` (a multi-line value passed via a shell variable is fine), **or** `mcp__github__actions_run_trigger` with the workflow file as the `workflow_id` and `{ "project_description": "<DESC>" }` as the inputs.
   - `workflow_dispatch` runs from the default branch by definition — exactly where `orchestrate.yml@main` / `@stable` is meant to run.

7. **Capture the dispatched run + tracking issue.** `gh workflow run` does not return the run id, so resolve it: poll `gh run list --workflow=<workflow-file> --event=workflow_dispatch -R <owner>/<repo> -L 5 --json databaseId,url,status,createdAt` and pick the newest run created at/after your dispatch; record its URL. The orchestrator opens an `ai:orchestrator-tracking` issue early in the run — surface it if it has appeared (`mcp__github__search_issues`, or `gh issue list --label ai:orchestrator-tracking -R <owner>/<repo> -L 5 --json number,title,url,createdAt`), otherwise tell the user it will appear shortly and how to find it. Do **not** block for the whole orchestrator run — the dispatch is the deliverable.

8. **Report.** Emit the [Output Format](#output-format) in chat.

## Output Format

```
Plan: <title>  (<plan path>)
Mode: AI orchestrator (dispatched — NOT implemented in this session)
Reference: path=<path>  branch=<branch>  PR=<url | none>
Dispatched: <workflow-file>  →  run <run url>
Tracking issue: <url>   (or: "will be opened by the orchestrator — watch for the ai:orchestrator-tracking issue")
Note: This command opened no PR and moved no doc. The orchestrator owns implementation, the per-phase PRs, and project completion (including archiving the plan to docs/completed/ when the project is verified done).
```

No prose padding. A bare "dispatched, see Actions" is not acceptable — the user wants the plan, the reference, the dispatched run URL, and the tracking issue (or where it will appear).

## Tool Access

- **`mcp__github__*` MCP tools** — always available. `mcp__github__actions_run_trigger` to dispatch the orchestrator; `mcp__github__list_pull_requests` / `issue_read` / `search_issues` / `get_file_contents` / `list_branches` for the plan reference and tracking-issue lookup.
- **`gh` CLI** — the `GH_TOKEN` transport. Shared rules live in **CLAUDE.md §23** and are not restated here: availability and the nounset-safe auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene, and the self-serve-read / ask-first-mutation split (§23.A–E). Use `gh workflow run` to dispatch, `gh run list` to capture the run, and `gh repo view --json defaultBranchRef -q .defaultBranchRef.name -R <owner>/<repo>` for default-branch detection. The dispatch this command performs is the §23.C command-invoked carve-out — invoking this command *is* the approval, so don't re-ask before dispatching; every other §23.C operation (merging, force-push, deletions, repo/org administration) still needs the §2 Q/A ask.

Local file reads use `Read`; code search uses `Grep` / `Glob`; git + the dispatch use `Bash`.

## Rules

- **Hand off — do not implement.** This command's job is to dispatch the orchestrator, not to write code. In-session implementation is `/implement-plan-claude`. If you find yourself editing source files, you are in the wrong command.
- **No PR, no doc move from this command.** The orchestrator owns every implementation PR and the plan's lifecycle. Do **not** move the plan to `docs/completed/` here — that would trip the `lint-plan-archival.yml` gate (a PR adding to `docs/completed/` must reference a tracking issue with all checkboxes ticked, which is impossible before the orchestrator has shipped anything) and re-create the misleading "this plan is done" signal the gate exists to prevent (see `docs/postmortems/2026-05-18-project-2734-stall.md`). The orchestrator archives the plan itself when the project is verified complete.
- **Reference-only `project_description`.** Pass the plan's path + branch + PR URL and let the orchestrator read it; never inline the full plan body. Keeps the dispatch payload small and the plan single-sourced.
- **First line is the tracking-issue title.** The orchestrator derives the tracking-issue title from the first line of `project_description` — make it a concise, descriptive project title (≤ ~200 chars), not an instruction or a path.
- **The plan must be reachable on the remote before dispatch** — default branch, a pushed branch, or an open PR. A plan that exists only in the local working tree cannot be read by the orchestrator: push it (a branch push is not a PR) or stop and ask.
- **§19 — auto-close keyword discipline.** The orchestrator's tracking issue carries `ai:orchestrator-tracking`; never reference it with `Fixes/Closes/Resolves` anywhere. This command opens no PR, but if you ever comment, use `Refs #N` / `Related to #N`.
- **Pick the orchestrator wrapper that exists** (`ai-orchestrate.yml` in consumer repos, `internal-orchestrate.yml` in this library). If neither is present the repo is not orchestrator-enabled — stop and report.
- **Default branch.** Resolve dynamically via `gh repo view --json defaultBranchRef -q .defaultBranchRef.name -R <owner>/<repo>`; do not hardcode `main`. `workflow_dispatch` always runs from the default branch.
- **If the plan is ambiguous, under-specified, or unreachable** such that handing it off correctly would require guessing, stop and ask (§0/§2) rather than dispatching a guess.
