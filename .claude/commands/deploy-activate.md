Given a reference to a **completed** project in `$ARGUMENTS` — a GitHub issue (number / URL), a PR, a plan doc, or a clearly-named feature — walk me through **deploying it and making it active** in this repo (`shubhodeep1/coding-workflows`), **one step at a time**, starting from a **fresh MacBook with no GitHub repos synced**. This is the action companion to `/verify-activation`: that command only diagnoses (LIVE / DORMANT / INCOMPLETE); this one drives a DORMANT-but-complete project all the way to LIVE. Interactive by contract: emit **exactly one step**, then **stop and wait** for me to paste the command output (or say `done` / `next`) before emitting the next step. The deploy commands run on **my** machine — you guide, I execute and paste back; never run the mutating deploy steps yourself. `$ARGUMENTS` should contain at least one concrete reference (`#1234`, an issue/PR URL, a plan path, or a clearly-named feature). **Resumable across sessions:** progress is persisted to a per-project activation log in the repo (`docs/deploy-activation/<ref-slug>.md`). Before emitting any step you **read that log first** and resume from the first not-yet-done step; after each completed step you update and push the log, so a fresh session — new machine, re-cloned container, or a later day — picks up exactly where the last one stopped instead of restarting the runbook. See [Activation Log](#activation-log).

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS`, then load the activation log.** Extract the issue number / URL, PR refs, plan-doc path, or feature name. If there is no concrete reference — just vague prose — stop and ask for one. Restate the parsed reference in the opening summary. Then **read the activation log first** (see [Activation Log](#activation-log)): compute the log path from the reference and read `docs/deploy-activation/<ref-slug>.md`. If a log exists, it is the source of truth for progress — resume from the first step not marked `[x]`, skip the steps already done (and say so), and if it shows `Status: LIVE` report LIVE and stop. If I instead paste a plain statement of which steps are completed / remaining, reconcile that into the log before resuming. If no log exists, you will create one when you emit Step 1.

2. **Build the full deploy plan internally, before emitting Step 1.** Do all the read-only analysis up front so the runbook is stable; only the *delivery* is one-at-a-time.

   a. **Understand the project.** Fetch the issue and everything linked to it — linked PRs, tracking comments, sub-issues, the orchestrator state comment if present — via `mcp__github__issue_read` / `pull_request_read` (or `gh`, see [Tool Access](#tool-access)). Pin down what it builds, its acceptance criteria, and **how it is meant to run**: a cron schedule, a `pull_request` / `push` trigger, `repository_dispatch`, `workflow_dispatch` (manual), a long-running supervisor (§18.C), or on-demand. Read `README.md` / `agents.md` for the env-var / repo-var / secret contract.

   b. **Completeness gate — STOP if not complete.** This command is scoped to *completed* projects. Verify on the implicated branch (and on `main` where relevant) that the code / config / workflows / contracts actually exist and that the project's PR(s) are **merged or genuinely merge-ready**. Cheap checks: `Grep` / `Read` the files the project names; confirm linked-PR merge state. If it is clearly **INCOMPLETE** (code missing, PR neither merged nor open, scaffold-only), **do not emit a deploy runbook** — report exactly what is missing, point me to `/implement-plan-claude` (in-session) or `/implement-plan-ai` (AI orchestrator), and stop.

   c. **Enumerate the activation gates** that stand between "code merged" and "runs automatically," using the `/verify-activation` lens — for *this* repo specifically:
      - **Default-branch reachability** — a **cron** schedule only fires from the **default branch**. If the project lives on a branch / unmerged PR, merging to `main` is an activation step.
      - **Repo-vars / feature flags that default OFF** — e.g. `*_ENABLED=0`, an empty roster var. Name each variable, its **read-site (`file:line`)**, and its default.
      - **Required secrets** — does the run need `GH_PAT`, `TG_BOT_SECRET`, `TG_ADMIN_CHAT_ID`, a model API key, etc.? An unset required secret means dormant.
      - **Consumer propagation (§14)** — if it is a `workflow-templates/**` or `.claude/**` change consumers must pick up, activation includes **tagging a new `@stable` release** and confirming the `repository_dispatch` to every repo in `.github/ai/consumer_repos.json` (the `GH_PAT` needs `repo` scope on each). Read the actual release / dispatch workflow under `.github/workflows/` to get the **exact** tag/dispatch mechanism — do not hardcode it.
      - **Supervisor / long-running (§18.C)** — does it need a supervisor that must be started and wired into startup automation?
      - **DB gates (§18.D)** — does a backfill / migration run from code behind a gate, or is it (wrongly) waiting on a manual step?

   d. **Compose the ordered runbook** and keep it as a stable numbered list you track across turns:
      - **Prerequisite bootstrap (fresh Mac, nothing synced):** install Homebrew → `brew install git gh` (plus any tool the deploy needs) → `gh auth login` (or export `GH_TOKEN`) with the scopes the deploy needs (`repo`; for §14 dispatch, a PAT with `repo` scope on the consumer repos) → clone `shubhodeep1/coding-workflows` and check out the branch / PR that holds the project.
      - **Activation steps:** one gate per step — `gh variable set NAME --body 1 -R shubhodeep1/coding-workflows` for a repo-var flip; `gh secret set NAME -R shubhodeep1/coding-workflows` for a secret; merge the PR to `main`; tag / move `@stable` per the release workflow; verify the consumer dispatch fired.
      - **Verification:** confirm the trigger will actually fire (a scheduled run appears, the dispatch was delivered, the flag now reads ON).

3. **Deliver the runbook one step at a time.** Follow the [Interaction Protocol](#interaction-protocol) exactly. The opening message previews the plan (summary + gate list + total step count) and then gives **Step 1 only**.

4. **Finish on a verified LIVE state.** The final step verifies the project now runs automatically; close with the `✅ LIVE` line from the [Output Format](#output-format).

## Interaction Protocol

This is the load-bearing behavior — honor it strictly:

- **One step per turn.** Emit a single step, then **end the turn**. Never include Step k+1 in the same message as Step k. Each step states: a short title; the **exact** command(s) to paste into a Mac terminal; what **success** looks like; and the literal ask — *"paste the output (or say `done`) and I'll give you the next step."*
- **Wait, then react to the pasted output.** On my reply, read what I pasted. If it shows success → advance to the next step. If it shows an **error or unexpected state** → do **not** advance; diagnose and emit a **corrective step** (still one at a time). If I say `done` with no output, trust me, but where a cheap read-only check settles it, verify before moving on.
- **Track progress visibly, and persist it.** Each turn, show a compact checklist of the runbook (done ✓ / current ▶ / remaining) so I always see where we are, e.g. `Step 4 of ~9`. The count may grow if a step fails and needs a fix-up — say so. The same checklist is mirrored into the activation log: after each step is confirmed, mark it `[x]` in `docs/deploy-activation/<ref-slug>.md`, then **commit and push the log** so the next session can resume (see [Activation Log](#activation-log)).
- **Adapt to reality.** If pasted output proves a gate is already satisfied (var already `1`, PR already merged, secret already present), **skip** that step and say why.
- **Never bundle, never auto-execute.** Do not merge steps to "save time," and do not run the mutating deploy commands yourself — they run on my machine. Read-only verification commands against your own checkout (or `mcp__github__*` / `gh ... --json` reads) are fine.
- **Stop conditions.** STOP and ask in Q/A format if a step needs a decision with material tradeoffs, would rename/remove a §6 identifier, or is otherwise ambiguous. STOP and report if the project turns out to be incomplete mid-run.

## Activation Log

This command is **resumable**. Progress for each project is persisted to a per-project log committed in the repo, so a fresh session — new machine, re-cloned container, or just a later day — picks up at the exact next step instead of restarting the runbook.

- **Path.** `docs/deploy-activation/<ref-slug>.md`, one file per project. Derive `<ref-slug>` deterministically from the parsed reference so the same project always maps to the same file:
  - issue → `issue-<N>`  ·  PR → `pr-<N>`  ·  plan doc → `plan-<basename-without-extension>`  ·  bare feature name → `feature-<kebab-case>`.
  - If `$ARGUMENTS` carries more than one reference, key the slug off the primary one in this priority: issue → PR → plan doc → feature name, and record the secondary references inside the file.
- **Read first (mandatory).** Before emitting any step, read this file. If it exists it is the source of truth for what is done: resume from the first step not marked `[x]`, skip the `[x]` steps (say that you are skipping them), and if `Status: LIVE` just report LIVE and stop. If it does not exist, create it when you emit Step 1.
- **Accept a pasted progress statement.** If I paste a list of steps already completed / still remaining (rather than raw command output), reconcile it into the log — mark the named steps `[x]`, leave the rest open — and resume from the first open step.
- **Update after every step.** When a step's pasted output (or a `done`) confirms success, mark it `[x]` with a one-line evidence note and the date, mark the next step current, refresh `Last updated` and `Last note`, then **commit and push the log** (below). On full completion set `Status: LIVE`; if a step is blocked, set `Status: BLOCKED` and record why in `Last note`.
- **Persistence (commit & push).** The log only helps a future session if it survives this container, so writing the file is not enough — commit it and `git push` to the working branch. This is the command's own bookkeeping, **not** a mutating deploy step, so it is fine to run yourself: it never touches `shubhodeep1/coding-workflows` settings, secrets, vars, merges, or release tags. Use the date from the session context for timestamps.

**Log file format:**

```
# Deploy-Activation Log — <project in one phrase>

- Reference: <#1234 / URL / plan path / feature>   (+ any secondary refs)
- Deploy target: shubhodeep1/coding-workflows
- How it runs: <cron «expr» from default branch | push | pull_request | repository_dispatch | supervisor>
- Status: IN_PROGRESS | BLOCKED | LIVE
- Last updated: <YYYY-MM-DD>
- Last note: <one line — where the last session stopped / why blocked>

## Runbook
1. [x] Prereqs: Homebrew, git, gh, auth, clone + checkout   — done <YYYY-MM-DD>: <evidence>
2. [x] Set repo-var X=1 (read at file:line, default 0)      — done <YYYY-MM-DD>: var now 1
3. [ ] Merge PR #N to main (cron needs default branch)
4. [ ] Verify LIVE

## Notes
- <free-form: errors hit, steps skipped because already-satisfied and why, decisions made>
```

## Output Format

**Opening message (plan preview, then Step 1):**

When resuming from an existing log, the checklist below shows the already-done steps as `[x]` (carried over from the log), say "Resuming from Step k per `docs/deploy-activation/<ref-slug>.md`", and emit the first not-yet-done step instead of Step 1.

```
Summary: <parsed reference; project in one phrase; deploy target: shubhodeep1/coding-workflows>
How it runs: <cron «expr» from default branch | push | pull_request | repository_dispatch | supervisor>
Completeness: COMPLETE — <evidence: file:line, merged/merge-ready PR#>   (if INCOMPLETE: stop per Procedure 2b)

Activation gates (~N steps total):
- [ ] Prereqs: Homebrew, git, gh, auth, clone + checkout
- [ ] <merge PR #… to main>            (cron needs default branch)
- [ ] <set repo-var X=1 — read at file:line, default 0>
- [ ] <add secret Y>
- [ ] <tag @stable / confirm consumer dispatch §14>     (only if a template/.claude change)
- [ ] <verify LIVE>

—— Step 1 of ~N: <title> ——
Run:
  <exact command(s)>
Success looks like: <what to expect>
Paste the output (or say `done`) and I'll give you Step 2.
```

**Each subsequent turn:** the updated checklist, then exactly one `—— Step k of ~N ——` block in the same shape.

**Final message:**

```
✅ LIVE — <project> now runs automatically.
Trigger: <cron «expr» from default branch | push | pull_request | repository_dispatch>
Verified by: <the scheduled run / delivered dispatch / flag-now-ON evidence>
```

## Tool Access

The deploy is executed by **me** on my Mac; your tools are for building and adapting the runbook and for read-only verification:

- **`mcp__github__*` MCP tools** — `issue_read`, `pull_request_read`, `list_commits`, `get_file_contents`, `list_tags`, `get_tag`, `search_issues`, `search_pull_requests` for the project, its merge state, and release/tag state. Read at `main` (or the project's branch).
- **`gh` CLI (read-only)** — when `GH_TOKEN` / `GITHUB_TOKEN` is set (verify nounset-safe: `{ [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; } && gh auth status`). **Pass `-R shubhodeep1/coding-workflows` on every call** — Claude Code Web's remote is a local proxy and bare `gh` calls fail with `failed to determine base repo`; the SessionStart hook prints the resolved slug. Use `gh` here only to *inspect* state (`gh run list`, `gh variable list`, `gh pr view --json`), never to mutate.
- **`Read` / `Grep` / `Glob`** — verify completeness, workflow triggers, flag defaults, and the release/dispatch mechanism on the local checkout. **`Bash`** for read-only inspection only.

## Rules

- **Read the log first, always.** Never emit Step 1 before reading `docs/deploy-activation/<ref-slug>.md` for the parsed reference. If it exists, resume from the first not-done step; if it shows `Status: LIVE`, report LIVE and stop. After every confirmed step, update the log and `git push` it so the next session resumes correctly (see [Activation Log](#activation-log)).
- **One step, then wait — always.** The whole value of this command is the paced, paste-driven loop. Emitting multiple steps at once, or racing ahead before I confirm, breaks it.
- **You guide; I execute.** Never run the mutating deploy commands (`gh variable set`, `gh secret set`, merge, tag, dispatch) yourself — they run on my machine and I paste the result. Read-only inspection on your side is encouraged to keep the next step accurate.
- **Completed projects only.** If the project is not fully implemented / merge-ready, stop and route me to `/implement-plan-claude` or `/implement-plan-ai` (Procedure 2b) — do not improvise a deploy for incomplete code.
- **Assume a clean machine.** Start from prerequisites (Homebrew, git, gh, auth, clone) because nothing is synced. Do not assume any tool, repo, or credential is already present until a pasted output proves it.
- **"Activated" ≠ "implemented."** A merged feature behind `FOO_ENABLED=0`, an unset required secret, or a scheduled workflow not yet on the default branch is dormant. Name the exact gate and the exact flip.
- **Distinguish trigger types precisely.** `workflow_dispatch` is manual, never "automatic." `cron` requires the workflow on the **default branch**. `repository_dispatch` requires a dispatcher and a token with scope (§14). Name which one applies and what "LIVE" means for it.
- **Honor §14 for template / `.claude` changes.** Activation for consumers means tagging a new `@stable` release and confirming the `repository_dispatch` to every repo in `.github/ai/consumer_repos.json`; read the real release workflow for the exact commands.
- **Honor §6 / §10.** Never instruct a rename/removal of an existing identifier without the Q/A ask flow first; DB activation runs from code behind a gate (§18.D), never an ad-hoc mongo shell step.
