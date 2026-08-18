Hand the recommendation docs under `analysis/` to the **AI orchestrator** (the unattended pipeline) for validation and implementation, by dispatching the orchestrator's `workflow_dispatch` trigger with the analysis docs fed in **by reference** as the `project_description`. The orchestrator reads each doc, validates every recommendation against the *current* repo, decomposes the valid-and-safe subset into a dependency DAG of issues, opens a tracking issue, and ships each phase as its own PR. This command does **not** apply any recommendation in this session and opens **no** PR of its own — it kicks off the orchestrator and reports the dispatched run + tracking issue. It mirrors `/implement-plan-ai` (the plan hand-off), but over the recommendation docs in `analysis/` instead of a single plan doc. `$ARGUMENTS` is optional — pass a filter (a specific doc, a date, or a glob) to scope which analysis docs are handed off; empty means hand off all recommendation docs under `analysis/`. Optional trailing prose (an emphasis to pass through to the orchestrator) is allowed but not required.

$ARGUMENTS

## Procedure

1. **Read project context.** Read `README.md`, `agents.md`, and `CLAUDE.md` at the repo root for naming/context needed to compose an accurate hand-off. This command makes no code changes, so `/db/contracts/*` reads are only needed if you must understand a collection name referenced in the docs.

2. **Enumerate the analysis docs.** List the recommendation / analysis markdown docs under `analysis/` (e.g. `analysis/workflow-optimization-*.md`). Honor any `$ARGUMENTS` filter (a specific doc, a date, or a glob). **Exclude** non-recommendation files: state files (`last_collection_timestamp.txt`, `validation-selftest-status.json`) and prior reports (`recommendation-processing-report*.md`). Read each selected doc in full so the hand-off describes the work accurately and you can derive a good tracking-issue title. If the filter matches nothing — or `analysis/` holds no recommendation docs — **stop and report** that there is nothing to hand off; do **not** dispatch an empty run.

3. **Resolve a remote-reachable reference to the docs (reference-only — do not inline the docs).** The orchestrator runs on the repo's default branch and reads `project_description` as prose; its codex agent has `gh`/`git` tool access and can fetch a branch or PR. For the selected docs, resolve in order of preference:
   - **On the default branch already** → reference them by their default-branch paths.
   - **Committed on a branch, not yet merged** → ensure that branch is pushed to the remote (`git push -u origin <branch>` if it is local-only — retry transient network errors with exponential backoff 2s/4s/8s/16s, up to 4 retries). Find the open PR if one exists (`mcp__github__list_pull_requests` with head=`<branch>`, or `gh pr list --head <branch> -R <owner>/<repo>`). Record paths + branch + PR URL.
   - **Uncommitted / not reachable on the remote** → **stop and ask** the user to commit + push the docs (or point you at their branch / PR). The orchestrator cannot read docs that exist only in the local working tree.
   Pushing the docs' branch is **not** opening a PR — this command still opens no PR of its own (the orchestrator owns all implementation PRs).

4. **Locate the orchestrator dispatch workflow.** Look in `.github/workflows/` and pick the first that exists:
   - `ai-orchestrate.yml` — the consumer-repo wrapper (calls `orchestrate.yml@stable`).
   - `internal-orchestrate.yml` — this library's own wrapper (calls `orchestrate.yml@main`).
   Both expose `workflow_dispatch` with a single required `project_description` input. If neither exists, **stop and report** that this repo has no orchestrator trigger wired (it is not orchestrator-enabled) — do not invent a trigger.

5. **Compose `project_description` (reference-only).** Build a single string with three parts:
   - **First line = a concise project title** derived from the docs being handed off. The orchestrator uses the first line of `project_description` as the tracking-issue title (`orchestrate.yml` reads `head -n 1`), so keep it ≤ ~200 chars and descriptive — a title, not an instruction. e.g. `Apply analysis recommendations from analysis/ — validate against current repo and implement the safe subset`. When `$ARGUMENTS` scopes to a single doc, derive the title from that doc.
   - **An instruction preamble** stating these are **analysis recommendation docs, not a pre-approved spec**: the orchestrator MUST read each referenced doc in full, then **validate every recommendation against the *current* repo before acting** — classify each VALID&SAFE / VALID-BUT-RISKY / STALE-or-already-done / INVALID, **implement only the VALID&SAFE subset**, and **defer the risky / contract-touching / ambiguous ones with a recorded rationale** rather than forcing them through. The bar is "correct *and* safe to land without breaking existing flows," not just "written in a doc." Pass through the project rules as hard constraints: §1 priority order (security and correctness outrank performance and speed — never apply a perf recommendation that risks correctness), §6 naming immutability (add aliases, never rename in place), §10 MongoDB contracts + index registry (a collection/index change requires the matching `/db/contracts/*` update), and §18 automation bias (wire recurring operations into the scheduler — no standalone manual scripts; DB work runs from code behind a gate; register any new single-use/long-running script in `docs/scripts-pending-removal.md`). Tell the orchestrator it owns the source-doc lifecycle: on verified completion it removes the fully-triaged source docs and folds a processing report into `analysis/recommendation-processing-report.md` (retaining filenames for provenance, matching the existing report convention), and it must **never** delete `last_collection_timestamp.txt`, `validation-selftest-status.json`, or any `recommendation-processing-report*.md`. Append any trailing emphasis from `$ARGUMENTS` here.
   - **A reference block**: each selected doc's repo-relative path, plus the branch and the PR URL, with an explicit instruction to read the docs from that branch/PR if they are not yet on the default branch (e.g. `git fetch origin <branch> && git show origin/<branch>:<path>`, or `gh pr view <url>`).
   Do **not** paste the docs' bodies into `project_description` — keep it reference-only.

6. **Dispatch the orchestrator.** Trigger the workflow chosen in step 4:
   - `gh workflow run <workflow-file> -R <owner>/<repo> -f project_description="$DESC"` (a multi-line value passed via a shell variable is fine), **or** `mcp__github__actions_run_trigger` with the workflow file as the `workflow_id` and `{ "project_description": "<DESC>" }` as the inputs.
   - `workflow_dispatch` runs from the default branch by definition — exactly where `orchestrate.yml@main` / `@stable` is meant to run.

7. **Capture the dispatched run + tracking issue.** `gh workflow run` does not return the run id, so resolve it: poll `gh run list --workflow=<workflow-file> --event=workflow_dispatch -R <owner>/<repo> -L 5 --json databaseId,url,status,createdAt` and pick the newest run created at/after your dispatch; record its URL. The orchestrator opens an `ai:orchestrator-tracking` issue early in the run — surface it if it has appeared (`mcp__github__search_issues`, or `gh issue list --label ai:orchestrator-tracking -R <owner>/<repo> -L 5 --json number,title,url,createdAt`), otherwise tell the user it will appear shortly and how to find it. Do **not** block for the whole orchestrator run — the dispatch is the deliverable.

8. **Report.** Emit the [Output Format](#output-format) in chat.

## Output Format

```
Analysis docs handed off: N
Docs: <list of repo-relative paths>
Mode: AI orchestrator (dispatched — NOT applied in this session)
Reference: branch=<branch>  PR=<url | none>
Dispatched: <workflow-file>  →  run <run url>
Tracking issue: <url>   (or: "will be opened by the orchestrator — watch for the ai:orchestrator-tracking issue")
Note: This command applied no recommendation, opened no PR, and deleted no doc. The orchestrator owns validation, implementation, the per-phase PRs, and the source-doc cleanup + processing report on completion.
```

No prose padding. A bare "dispatched, see Actions" is not acceptable — the user wants the docs handed off, the reference, the dispatched run URL, and the tracking issue (or where it will appear).

## Tool Access

- **`Read` / `Grep` / `Glob`** — enumerate the source docs under `analysis/` and read them to compose an accurate, well-titled hand-off. No source edits.
- **`mcp__github__*` MCP tools** — always available. `mcp__github__actions_run_trigger` to dispatch the orchestrator; `mcp__github__list_pull_requests` / `issue_read` / `search_issues` / `get_file_contents` / `list_branches` for the docs' reference and tracking-issue lookup.
- **`gh` CLI** — the `GH_TOKEN` transport. Shared rules live in **CLAUDE.md §23** and are not restated here: availability and the nounset-safe auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene, and the self-serve-read / ask-first-mutation split (§23.A–E). Use `gh workflow run` to dispatch, `gh run list` to capture the run, and `gh repo view --json defaultBranchRef -q .defaultBranchRef.name -R <owner>/<repo>` for default-branch detection. The dispatch this command performs is the §23.C command-invoked carve-out — invoking this command *is* the approval, so don't re-ask before dispatching; every other §23.C operation (merging, force-push, deletions, repo/org administration) still needs the §2 Q/A ask.

Local file reads use `Read`; code search uses `Grep` / `Glob`; git + the dispatch use `Bash`.

## Rules

- **Hand off — do not apply.** This command's job is to dispatch the orchestrator over the `analysis/` docs, not to validate-and-apply recommendations in this session. In-session implementation is not what this command does. If you find yourself editing source files, deleting analysis docs, or writing a processing report, you are in the wrong command.
- **No PR, no doc deletion, no report from this command.** The orchestrator owns every implementation PR, the source-doc cleanup, and the `analysis/recommendation-processing-report.md` report — exactly as `/implement-plan-ai` leaves the plan lifecycle to the orchestrator. Do **not** `git rm` any analysis doc here; that would re-create the in-session apply behavior this command deliberately removed.
- **Validation is the orchestrator's job, but it must actually happen.** The hand-off instruction MUST tell the orchestrator to validate every recommendation against the current repo and implement only the valid-and-safe subset — not blindly apply everything in the docs. Anything risky (high blast-radius, public-contract or hot-path impact, a §6 rename, a §10 contract/index change, or a real tradeoff) is deferred with rationale, not forced through.
- **Reference-only `project_description`.** Pass the docs' paths + branch + PR URL and let the orchestrator read them; never inline the docs' bodies. Keeps the dispatch payload small and the docs single-sourced.
- **First line is the tracking-issue title.** The orchestrator derives the tracking-issue title from the first line of `project_description` — make it a concise, descriptive project title (≤ ~200 chars), not an instruction or a path.
- **The docs must be reachable on the remote before dispatch** — default branch, a pushed branch, or an open PR. Docs that exist only in the local working tree cannot be read by the orchestrator: push them (a branch push is not a PR) or stop and ask.
- **§1 / §6 / §10 are hard gates passed through to the orchestrator.** Security and correctness outrank performance and speed; renames need aliases; collection/index changes need the matching `/db/contracts/*` update.
- **§18 automation bias passed through.** A recommendation that adds a recurring operation must be wired into an existing scheduler/workflow (no standalone manual scripts); DB operations run from code behind a gate; any new single-use/long-running script gets a `docs/scripts-pending-removal.md` entry — all owned by the orchestrator's implementation PRs.
- **§19 — auto-close keyword discipline.** The orchestrator's tracking issue carries `ai:orchestrator-tracking`; never reference it with `Fixes/Closes/Resolves` anywhere. This command opens no PR, but if you ever comment, use `Refs #N` / `Related to #N`.
- **Exclude state files and prior reports from the hand-off**, and never instruct the orchestrator to delete `last_collection_timestamp.txt`, `validation-selftest-status.json`, or any `recommendation-processing-report*.md`.
- **Pick the orchestrator wrapper that exists** (`ai-orchestrate.yml` in consumer repos, `internal-orchestrate.yml` in this library). If neither is present the repo is not orchestrator-enabled — stop and report.
- **Default branch.** Resolve dynamically via `gh repo view --json defaultBranchRef -q .defaultBranchRef.name -R <owner>/<repo>`; do not hardcode `main`. `workflow_dispatch` always runs from the default branch.
- **If nothing matches the filter, or the docs are unreachable on the remote** such that handing them off correctly would require guessing, stop and report/ask (§0/§2) rather than dispatching a guess or an empty run.
