Given a reference to a **completed** project in `$ARGUMENTS` — a GitHub issue (number / URL), a PR, a plan doc, or a clearly-named feature — walk me through **deploying it and making it active** in this repo, **one step at a time**, starting from a **fresh MacBook with no GitHub repos synced**. A project here can live on one of two sides — **this consumer repo's own code/config**, or the **upstream workflow library** (`shubhodeep1/coding-workflows`) that this repo's wrappers call — and you decide which from the evidence. This is the action companion to `/verify-activation`: that command only diagnoses (LIVE / DORMANT / INCOMPLETE); this one drives a DORMANT-but-complete project all the way to LIVE. Interactive by contract: emit **exactly one step**, then **stop and wait** for me to paste the command output (or say `done` / `next`) before emitting the next step. The deploy commands run on **my** machine — you guide, I execute and paste back; never run the mutating deploy steps yourself. `$ARGUMENTS` should contain at least one concrete reference. **Resumable across sessions:** progress is persisted to a per-project activation log in this repo (`docs/deploy-activation/<ref-slug>.md`). Before emitting any step you **read that log first** and resume from the first not-yet-done step; after each completed step you update and push the log, so a fresh session — new machine, re-cloned container, or a later day — picks up exactly where the last one stopped instead of restarting the runbook. See [Activation Log](#activation-log).

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS`, then load the activation log.** Extract the issue number / URL, PR refs, plan-doc path, or feature name. If there is no concrete reference, stop and ask for one. Restate the parsed reference in the opening summary. Then **read the activation log first** (see [Activation Log](#activation-log)): compute the log path from the reference and read `docs/deploy-activation/<ref-slug>.md` in `THIS_REPO`. If a log exists, it is the source of truth for progress — resume from the first step not marked `[x]`, skip the steps already done (and say so), and if it shows `Status: LIVE` report LIVE and stop. If I instead paste a plain statement of which steps are completed / remaining, reconcile that into the log before resuming. If no log exists, you will create one when you emit Step 1.

2. **Build the full deploy plan internally, before emitting Step 1.** Do all the read-only analysis up front so the runbook is stable; only the *delivery* is one-at-a-time.

   a. **Understand the project and resolve `THIS_REPO`.** Fetch the issue and everything linked — linked PRs, tracking comments, sub-issues — via `mcp__github__issue_read` / `pull_request_read` (or `gh`). Pin down what it builds, its acceptance criteria, and **how it is meant to run** (cron, `pull_request` / `push`, `repository_dispatch`, `workflow_dispatch` manual, a supervisor, or on-demand). Determine **`THIS_REPO`** — the `owner/repo` the command runs in (the SessionStart hook prints the resolved slug; otherwise derive it from the git remote).

   b. **Classify the side that owns activation.**
      - **`[CONSUMER]`** — this repo's own workflows / config / code (a wrapper workflow, a repo-var this repo sets). Read at `THIS_REPO@main`.
      - **`[UPSTREAM]`** — behavior that lives in the upstream library and only runs through this repo's wrapper at the **ref this repo is pinned to**. Read upstream pinned to `UPSTREAM_SHA` (see below).
      - **`[BOTH]`** — a wrapper here plus the upstream reusable workflow it calls.

      **Resolving the upstream pin (for the `[UPSTREAM]` / `[BOTH]` side):** find every `uses:` / `repository:` / `ref:` in this repo's `.github/workflows/*.yml` that references `shubhodeep1/coding-workflows`. The exact `ref` is the pin: tag `@vX.Y.Z` → resolve `UPSTREAM_SHA` via `mcp__github__get_tag` / `list_tags`; direct SHA → use it; moving `@stable` → resolve via `list_tags`; branch `@main` → resolve to that branch's tip. Record `UPSTREAM_TAG` + `UPSTREAM_SHA`; pass `ref=<UPSTREAM_SHA>` on **every** upstream read.

   c. **Completeness gate — STOP if not complete.** This command is scoped to *completed* projects. Verify the code / config / workflows / contracts exist and the project's PR(s) are **merged or genuinely merge-ready**, **at the ref for its side** (`THIS_REPO@main` for `[CONSUMER]`; `shubhodeep1/coding-workflows@UPSTREAM_SHA` for `[UPSTREAM]`). If clearly **INCOMPLETE** (code missing, PR neither merged nor open, scaffold-only), **do not emit a deploy runbook** — report what is missing, point me to `/implement-plan-claude` or `/implement-plan-ai`, and stop.

   d. **Enumerate the activation gates** between "code present" and "runs automatically here," using the `/verify-activation` lens:
      - **Wrapper wiring (consumer side)** — does this repo have the `.github/workflows/*.yml` wrapper with the right trigger, calling the upstream reusable workflow at the expected ref? A feature that exists upstream but is **not wired into a consumer wrapper** will never run here — adding/adjusting the wrapper is an activation step.
      - **Default-branch reachability** — a **cron** schedule only fires from the **default branch**; a wrapper change must be merged to the default branch to take effect.
      - **Repo-vars / feature flags that default OFF** — `*_ENABLED=0`, an empty roster var. Consumers commonly must **opt in** by setting a repo-var. Name each variable, its read-site, and its default.
      - **Required secrets** — does the run need a model API key, `GH_PAT`, a Telegram token, etc. that I must add in this repo's settings? Unset → dormant.
      - **Upstream version gap** — is this repo pinned to an `UPSTREAM_TAG` that **predates** the feature? If so the feature is not available here until the pin is bumped — **bumping the pin** is the activation step.
      - **Supervisor / DB gates** — does it need a started supervisor or a code-gated migration rather than a manual step?

   e. **Compose the ordered runbook** and keep it as a stable numbered list you track across turns:
      - **Prerequisite bootstrap (fresh Mac, nothing synced):** install Homebrew → `brew install git gh` (plus any tool the deploy needs) → `gh auth login` (or export `GH_TOKEN`) with the scopes the deploy needs → clone `THIS_REPO` and check out the branch / PR that holds the project.
      - **Activation steps:** one gate per step — `gh variable set NAME --body 1 -R <THIS_REPO>` for a repo-var; `gh secret set NAME -R <THIS_REPO>` for a secret; edit the wrapper `.github/workflows/*.yml` (add it, fix the trigger, or bump the `ref:` pin) then commit / push / open a PR / merge to the default branch.
      - **Verification:** confirm the trigger will actually fire (a scheduled run appears, the workflow is enabled, the flag now reads ON).

3. **Deliver the runbook one step at a time.** Follow the [Interaction Protocol](#interaction-protocol) exactly. The opening message previews the plan (summary + side + gate list + total step count) and then gives **Step 1 only**.

4. **Finish on a verified LIVE state.** The final step verifies the project now runs automatically here; close with the `✅ LIVE` line from the [Output Format](#output-format).

## Interaction Protocol

This is the load-bearing behavior — honor it strictly:

- **One step per turn.** Emit a single step, then **end the turn**. Never include Step k+1 in the same message as Step k. Each step states: a short title; the **exact** command(s) to paste into a Mac terminal (or the precise YAML edit to make); what **success** looks like; and the literal ask — *"paste the output (or say `done`) and I'll give you the next step."*
- **Wait, then react to the pasted output.** On my reply, read what I pasted. If it shows success → advance. If it shows an **error or unexpected state** → do **not** advance; diagnose and emit a **corrective step** (still one at a time). If I say `done` with no output, trust me, but where a cheap read-only check settles it, verify before moving on.
- **Track progress visibly, and persist it.** Each turn, show a compact checklist (done ✓ / current ▶ / remaining), e.g. `Step 4 of ~9`. The count may grow if a step fails and needs a fix-up — say so. The same checklist is mirrored into the activation log: after each step is confirmed, mark it `[x]` in `docs/deploy-activation/<ref-slug>.md`, then **commit and push the log** so the next session can resume (see [Activation Log](#activation-log)).
- **Adapt to reality.** If pasted output proves a gate is already satisfied (var already set, wrapper already present, pin already current), **skip** that step and say why.
- **Never bundle, never auto-execute.** Do not merge steps, and do not run the mutating deploy commands or git pushes yourself — they run on my machine. Read-only verification (`mcp__github__*` / `gh ... --json` reads, `Read`/`Grep` on your checkout) is fine.
- **Stop conditions.** STOP and ask in Q/A format if a step needs a decision with material tradeoffs, would rename/remove a §6 identifier, or is ambiguous. STOP and report if the project turns out to be incomplete mid-run.

## Activation Log

This command is **resumable**. Progress for each project is persisted to a per-project log committed in `THIS_REPO`, so a fresh session — new machine, re-cloned container, or just a later day — picks up at the exact next step instead of restarting the runbook.

- **Path.** `docs/deploy-activation/<ref-slug>.md`, one file per project. Derive `<ref-slug>` deterministically from the parsed reference so the same project always maps to the same file:
  - issue → `issue-<N>`  ·  PR → `pr-<N>`  ·  plan doc → `plan-<basename-without-extension>`  ·  bare feature name → `feature-<kebab-case>`.
  - If `$ARGUMENTS` carries more than one reference, key the slug off the primary one in this priority: issue → PR → plan doc → feature name, and record the secondary references inside the file.
- **Read first (mandatory).** Before emitting any step, read this file in `THIS_REPO`. If it exists it is the source of truth for what is done: resume from the first step not marked `[x]`, skip the `[x]` steps (say that you are skipping them), and if `Status: LIVE` just report LIVE and stop. If it does not exist, create it when you emit Step 1.
- **Accept a pasted progress statement.** If I paste a list of steps already completed / still remaining (rather than raw command output), reconcile it into the log — mark the named steps `[x]`, leave the rest open — and resume from the first open step.
- **Update after every step.** When a step's pasted output (or a `done`) confirms success, mark it `[x]` with a one-line evidence note and the date, mark the next step current, refresh `Last updated` and `Last note`, then **commit and push the log** (below). On full completion set `Status: LIVE`; if a step is blocked, set `Status: BLOCKED` and record why in `Last note`.
- **Persistence (commit & push).** The log only helps a future session if it survives this container, so writing the file is not enough — commit it and `git push` to the working branch of `THIS_REPO`. This is the command's own bookkeeping, **not** a mutating deploy step, so it is fine to run yourself: it never touches repo settings, secrets, vars, merges, wrapper edits, or upstream pins. Use the date from the session context for timestamps.

**Log file format:**

```
# Deploy-Activation Log — <project in one phrase>

- Reference: <#1234 / URL / plan path / feature>   (+ any secondary refs)
- Side: [CONSUMER] | [UPSTREAM] | [BOTH]
- Deploy target: <THIS_REPO>   (+ upstream pin <UPSTREAM_TAG> (<short-sha>) if [UPSTREAM]/[BOTH])
- How it runs: <cron «expr» from default branch | push | pull_request | repository_dispatch | supervisor>
- Status: IN_PROGRESS | BLOCKED | LIVE
- Last updated: <YYYY-MM-DD>
- Last note: <one line — where the last session stopped / why blocked>

## Runbook
1. [x] Prereqs: Homebrew, git, gh, auth, clone THIS_REPO + checkout   — done <YYYY-MM-DD>: <evidence>
2. [x] Set repo-var X=1 (read at file:line, default 0)                — done <YYYY-MM-DD>: var now 1
3. [ ] Bump upstream pin @vA.B.C → @vA.B.D (if the pin predates the feature)
4. [ ] Merge wrapper change to the default branch (cron needs default branch)
5. [ ] Verify LIVE

## Notes
- <free-form: errors hit, steps skipped because already-satisfied and why, decisions made>
```

## Output Format

**Opening message (plan preview, then Step 1):**

When resuming from an existing log, the checklist below shows the already-done steps as `[x]` (carried over from the log), say "Resuming from Step k per `docs/deploy-activation/<ref-slug>.md`", and emit the first not-yet-done step instead of Step 1.

```
Summary: <parsed reference; project in one phrase; side [CONSUMER]/[UPSTREAM]/[BOTH]; deploy target: THIS_REPO>
Side: [CONSUMER] THIS_REPO@main  |  [UPSTREAM] shubhodeep1/coding-workflows@<UPSTREAM_TAG> (<short-sha>)  |  [BOTH]
How it runs: <cron «expr» from default branch | push | pull_request | repository_dispatch | supervisor>
Completeness: COMPLETE — <evidence: file:line, merged/merge-ready PR#>   (if INCOMPLETE: stop per Procedure 2c)

Activation gates (~N steps total):
- [ ] Prereqs: Homebrew, git, gh, auth, clone THIS_REPO + checkout
- [ ] <add/adjust wrapper .github/workflows/Z.yml — trigger / upstream ref>
- [ ] <bump upstream pin @vA.B.C → @vA.B.D>     (only if the pin predates the feature)
- [ ] <set repo-var X=1 — read at file:line, default 0>
- [ ] <add secret Y>
- [ ] <merge wrapper change to the default branch>     (cron needs default branch)
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
✅ LIVE — <project> now runs automatically in <THIS_REPO>.
Trigger: <cron «expr» from default branch | push | pull_request | repository_dispatch>
Verified by: <the scheduled run / enabled workflow / flag-now-ON evidence>
```

## Tool Access

The deploy is executed by **me** on my Mac; your tools are for building and adapting the runbook and for read-only verification:

- **`mcp__github__*` MCP tools** — `issue_read`, `pull_request_read`, `list_commits`, `list_tags`, `get_tag`, `get_file_contents`, `search_issues`, `search_pull_requests`. For the upstream side, pass `ref=<UPSTREAM_SHA>` on every read; for the consumer side, read `THIS_REPO@main`.
- **`gh` CLI (read-only)** — when `gh` is installed and `GH_TOKEN` / `GITHUB_TOKEN` is set (verify nounset-safe: `{ [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; } && gh auth status`). **Pass `-R <owner>/<repo>`** on every call — Claude Code Web's remote is a local proxy; bare `gh` fails with `failed to determine base repo`. Use `gh` here only to *inspect* state (`gh run list`, `gh variable list`, `gh pr view --json`), never to mutate.
- **`Read` / `Grep` / `Glob`** — verify completeness, the consumer wrapper, triggers, flag defaults, and the upstream pin on the local checkout. **`Bash`** for read-only inspection only.

## Rules

- **Read the log first, always.** Never emit Step 1 before reading `docs/deploy-activation/<ref-slug>.md` in `THIS_REPO` for the parsed reference. If it exists, resume from the first not-done step; if it shows `Status: LIVE`, report LIVE and stop. After every confirmed step, update the log and `git push` it so the next session resumes correctly (see [Activation Log](#activation-log)).
- **One step, then wait — always.** The whole value of this command is the paced, paste-driven loop. Emitting multiple steps at once, or racing ahead before I confirm, breaks it.
- **You guide; I execute.** Never run the mutating deploy commands (`gh variable set`, `gh secret set`, the wrapper edit's commit/push, merge) yourself — they run on my machine and I paste the result. Read-only inspection on your side keeps the next step accurate.
- **Pick the side from evidence, pin upstream reads to the consumer's ref.** A feature implemented upstream but not wired into a consumer wrapper, or gated behind a repo-var this repo never set, is **DORMANT for this repo** even though the upstream code is perfect. Analyzing upstream activation at `main` when this repo is pinned to a release is wrong — read at `UPSTREAM_SHA`.
- **Completed projects only.** If the project is not fully implemented / merge-ready on its side, stop and route me to `/implement-plan-claude` or `/implement-plan-ai` (Procedure 2c) — do not improvise a deploy for incomplete code.
- **Assume a clean machine.** Start from prerequisites (Homebrew, git, gh, auth, clone) because nothing is synced. Do not assume any tool, repo, or credential is present until a pasted output proves it.
- **"Activated" ≠ "implemented."** Name the exact gate: wrapper wiring, a `*_ENABLED` default, an unset secret, or an upstream pin that predates the feature — and the exact flip.
- **Distinguish trigger types precisely.** `workflow_dispatch` is manual, never automatic. `cron` requires the workflow on the **default branch**. `repository_dispatch` needs a dispatcher + token scope. Name which applies and what "LIVE" means for it.
- **Honor §6.** Never instruct a rename/removal of an existing identifier without the Q/A ask flow first.
