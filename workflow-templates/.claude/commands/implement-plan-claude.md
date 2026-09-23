Implement the plan documented at the path / reference in `$ARGUMENTS` **directly in this Claude Code session, one phase at a time, driving the whole project to completion the way the AI orchestrator does**: ship each phase of the plan's **Phases & Merge Strategy** as its own PR against the default branch, wait for the review-autofix workflow and auto-merge to land it (a 3-hourly check-in, never a CI or review-comment babysit), then implement the next phase from the freshly merged default branch. After the last phase merges, run the repo's **security audit** workflow and then the **runtime validation** workflow — the orchestrator's `ai:security-pass → ai:validating` order — looping each until it is clean, and only then open the completion PR that moves the plan doc into `docs/completed/`. Progress is persisted to `docs/implement-plan/<slug>.md` so any later session resumes where the last one stopped. This is the **in-session** variant — Claude writes the code here and now. To hand the plan to the unattended **AI orchestrator** instead (which decomposes the work into a dependency DAG of issues and ships each phase as its own PR), use `/implement-plan-ai`. `$ARGUMENTS` is **free-form prose** that must resolve to a single plan markdown doc — typically a path like `docs/plans/<slug>-plan.md`, but it may also be a slug, a topic that matches one plan filename, or a GitHub reference (issue / PR) that links to the plan. Optional trailing prose (focus areas, a phase to start with) is allowed but not required.

$ARGUMENTS

## Procedure

1. **Resolve the plan doc.** Parse `$ARGUMENTS` for a path under `docs/plans/` (or elsewhere), a slug, or a reference that links to a plan. Resolve it to exactly one markdown file and `Read` it in full. If `$ARGUMENTS` is empty, matches no plan, or matches more than one and the intent is ambiguous, **stop and ask** which plan to implement — never guess. If it names a GitHub issue / PR, fetch that first (see [Tool Access](#tool-access)) to find the linked plan file. Derive `<slug>` from the plan filename (`docs/plans/<slug>-plan.md` → `<slug>`; a plan elsewhere uses its basename without `-plan.md` / `.md`).

2. **Read the progress log first, then project context.** Read `docs/implement-plan/<slug>.md` (see [Progress Log](#progress-log)). If it exists it is the source of truth for what has already shipped: resume from its `Stage:` / `Waiting on:` lines (re-verifying against GitHub, since the copy on the default branch can lag the session by one step), skip the phases marked `[x]`, and say so. If it shows `Status: COMPLETE`, report COMPLETE and stop. If it does not exist, create it before opening the first PR. Then always read `README.md`, `agents.md`, and `CLAUDE.md` at the repo root before editing. If the plan touches a MongoDB collection, also read every relevant `/db/contracts/*.yml` (CLAUDE.md §10). Read the actual source for every file the plan names — never guess at code, env vars, or workflow inputs.

3. **Build the phase checklist.** Convert the plan's **Phases & Merge Strategy** section into a tracked checklist (CLAUDE.md §11): one top-level item per phase, in the plan's order, each carrying the plan's per-phase implementation steps, files, tests, and "done" condition as sub-items. A plan whose Phases section says it is a single phase is a one-item list — the same loop runs once. A plan with no Phases section is under-specified: **stop and ask** how to split it (§0/§2) rather than inventing phases. Keep the checklist visible in chat and mirrored in the progress log.

4. **Implement the current phase — and only that phase.** From a fresh checkout of the default branch (`git fetch origin <default> && git checkout -B claude/implement-plan-<slug>-phase-<n> origin/<default>`; append `-2`, `-3`, … on collision), implement exactly the steps the plan assigns to phase `<n>`. Honor §5 (minimal change set — implement what the plan specifies, no speculative extras and nothing from a later phase), §6 (naming immutability — renames are breaking; add aliases), §9 (style — tabs except where the format forbids them; YAML stays 2-space), §10 (MongoDB — index registry, contracts, unique-index safety), and §18 (automation bias — wire new operations into the scheduler; no standalone manual scripts; DB work runs from code with a gate). Write the code, update `/db/contracts/*` and indexes where touched, add/extend the tests the plan calls for, update `README.md` / `agents.md` when behavior changes (§7), and add a `changelog.d/` fragment when the phase changes observable behaviour (§20). If the phase introduces a single-use or long-running script/supervisor, add its entry to `docs/scripts-pending-removal.md` in this same PR (§18.F).

5. **Verify the phase — evidence-based, not vibes.** Re-read the phase's "done" condition and check **each** checklist sub-item against the *actual* repo state: run the test suite and linters the plan specifies (and the repo's standard checks); `Grep` / `Read` to confirm the code, config, workflow wiring, and contracts exist and are correct; confirm rollout wiring (§18). Classify every sub-item **complete** (with a `file:line` / test-name citation), **partial**, or **not-done**. A phase ships only on a clean sweep; if something is blocked or too large to finish safely in this PR, stop, leave the log at `Status: BLOCKED`, and report exactly what remains.

6. **Commit, push, open the phase PR, update the log.** Group commits by scope (§12.E): the implementation commit(s), then a separate commit for the progress-log update plus any `README.md` / `agents.md` changes. Push with `git push -u origin <branch>` (retry transient network errors with exponential backoff: 2s, 4s, 8s, 16s — up to 4 retries). Open a ready-for-review PR via `mcp__github__create_pull_request` against the default branch (resolve it dynamically — do **not** hardcode `main`), `draft: false`, titled `<plan title> — phase <n>/<total>: <phase scope>`, whose body names the plan path, the phase, the checklist evidence from step 5, and the progress log. **PR-body keyword discipline (§19):** if the plan links an `ai:orchestrator-tracking` issue, use `Refs #N` / `Related to #N`, never `Fixes/Closes/Resolves #N`. Record the PR number in the log (`Waiting on: PR #N`).

7. **Wait for the merge — arm the check-in and end the turn.** Do **not** subscribe to PR activity (§25), do not poll, and do not fix CI failures or answer review comments while the PR is healthy: `review_autofix.yml` reviews, autofixes, resolves conflicts, and enables auto-merge on PR-backed `claude/**` branches by itself. Arm the 3-hourly check-in per [Check-in Loop](#check-in-loop), then end the turn. On each wake:
   - **Merged** → disarm the check-in (delete the Routines), tick the phase in the log, and go to step 4 for the next phase from the freshly merged default branch. After the last phase, go to step 8.
   - **Blocked** (the definition in [Check-in Loop](#check-in-loop)) → this is the one case where Claude touches the PR: check out the branch, resolve the block under CLAUDE.md §12 (merge the default branch and resolve the conflict, address the review threads, fix the failing check, or re-open / re-create the PR if it was closed unmerged), verify, push, record the intervention in the log, and re-arm. Cap: **3 interventions per PR**; on the fourth blocked wake stop and ask in Q/A format instead of pushing again.
   - **Still open and healthy** → re-arm silently; do not message the user and do not comment on the PR.

8. **Security pass (orchestrator order: security before validation).** Dispatch the security audit workflow — `security-audit.yml` in this library, `ai-security-audit.yml` in consumer repos (pick the one that exists; if neither exists, record `Security: not wired` in the log and continue to step 9) — with `gh workflow run <file> -R <owner>/<repo>` (or `mcp__github__actions_run_trigger`), record the run in the log (`Waiting on: run <id>`), arm the check-in, and end the turn. Invoking this command *is* the §23.C approval for these dispatches — do not re-ask. On the wake that finds the run finished, read its result: the new dated section on the `AI Security Audit Tracker` issue (label `ai:security-audit`, marker `<!-- ai:security-audit-tracker:v1 -->`) and the `ai:security` follow-up issues it opened (marker `<!-- ai:security-finding:<id> -->`), or the log-only skip when `SECURITY_AUDIT_SKIP_IF_UNCHANGED` found the default branch already audited.
   - **No follow-up issues opened, or the run skipped as unchanged** → the pass is clean; go to step 9.
   - **Follow-up issues opened** → Claude writes **no** security fix. The follow-ups enter the normal clarify → plan → implement → review pipeline on their own (that is how the orchestrator's security pass fixes findings too). Record them in the log, arm the check-in to wait until every one of them is `ai:merged` or closed, then re-dispatch the audit. Each re-dispatch is one **security cycle**; cap **5 cycles** (the orchestrator's `MAX_SECURITY_PASS_CYCLES` default). On exhaustion, or if a follow-up is closed without a merged PR, stop with `Status: BLOCKED` and ask in Q/A format.
   - **Run failed before auditing** (workflow error, missing secret) → record it and ask in Q/A format; do not loop on an infrastructure failure.

9. **Runtime validation.** Dispatch the validate workflow — `internal-validate.yml` here, `ai-validate.yml` in consumers (if neither exists, record `Validation: not wired` and continue) — with `-f tracking_issue=0 -f pr_number=0` (standalone mode: there is no orchestrator tracking issue, so the workflow neither comments nor opens fix-up issues; its verdict is in the run), record the run, arm the check-in, end the turn. On the wake that finds it finished, read the verdict from the `VALIDATION_FAILURE_SUMMARY` line in the run log (`status=pass|fail|error`, `raw_status=…`, `summary=…`, `failure_summary=…`; `gh run view --log <run-id> -R <owner>/<repo>`) and, when present, the `validation_status.json` in the `ai-validation-<run-id>-<attempt>` artifact.
   - **`status=pass`** → go to step 10.
   - **`raw_status=needs_fixes`** → Claude fixes it: branch `claude/implement-plan-<slug>-validation-fix-<cycle>` from the default branch, implement the fixes the diagnosis describes (the `fix_issues` proposals), verify per step 5, push, open the PR, wait for its merge exactly as in step 7, then re-dispatch validation. Each re-dispatch is one **validation cycle**; cap **3 cycles** (`MAX_VALIDATE_CYCLES` default). On exhaustion stop with `Status: BLOCKED` and ask.
   - **`harness_error`, `infeasible`, `codex_failure`, or an unknown payload** → these are terminal in the orchestrator too; record the summary and ask in Q/A format rather than guessing at a fix.

10. **Completion PR — move the doc only now.** From the default branch, `git mv docs/plans/<slug>-plan.md docs/completed/<slug>-plan.md` (create `docs/completed/` if missing; use `git mv` so history is preserved), set the log to `Status: COMPLETE` with the merged PR list, security and validation evidence, and land any `README.md` / `agents.md` update the phases did not already ship. Branch `claude/implement-plan-<slug>-complete`, push, open the PR (§19 discipline applies; if the plan links an `ai:orchestrator-tracking` issue the `lint-plan-archival.yml` gate requires that issue's checkboxes to be ticked or a `## De-scoped phases` section — never de-scope silently), disarm every Routine this command created, and wait for the merge via step 7. When it merges, report.

11. **Report.** Emit the [Output Format](#output-format) in chat at every hand-off point: after each PR is opened, after each wake that changed state, and at completion.

## Check-in Loop

The wait between "PR opened" and "PR merged" (and between "workflow dispatched" and "run finished") is a **3-hourly check-in**, not a watch. It is built from the claude-code-remote MCP Routine tools (`create_trigger`, `fire_trigger`, `delete_trigger`, `list_triggers`, `send_later`) that CLAUDE.md §25.C explicitly allows; it never uses `subscribe_pr_activity`, which §25 forbids and a hook blocks.

**Two Routines, so the idle loop runs on a cheap model.** Waking this session re-sends its whole context on the session's model, and a 3-hour gap outlives the prompt cache, so a wake costs the full context at full price even when nothing changed. The checker therefore runs elsewhere:

1. **Poke Routine** — `create_trigger` with **no schedule** (neither `cron_expression` nor `run_once_at`), bound to this session (the default), `name: "implement-plan <slug>: resume"`, `initiation: own_followup`, prompt: `Check-in for /implement-plan-claude <plan path>: the checker reports a state change. Re-read docs/implement-plan/<slug>.md, verify the PR / run state on GitHub yourself, and continue the procedure from the recorded stage.` It fires only when the checker fires it. Record its `trig_…` id in the log.
2. **Checker Routine** — `create_trigger` with `cron_expression: "0 */3 * * *"`, `create_new_session_on_fire: true`, `model: claude-sonnet-5`, `notifications: {}`, `name: "implement-plan <slug>: 3h checker"`, `initiation: own_followup`, and a standalone prompt built from the [Checker prompt](#checker-prompt) template. Each firing is a fresh, throwaway Sonnet session that reads the log and one or two GitHub objects, fires the poke Routine when the wait is over, and exits. Record its id too.

On the poke, this session wakes on its own model, **re-verifies the state itself** (the checker is a hint, not a verdict), acts, and then either re-arms (create a fresh checker if it deleted the old one, or leave the existing pair in place when the same wait continues) or disarms both Routines with `delete_trigger`. Before creating Routines, `list_triggers` and delete stale `implement-plan <slug>` Routines from an earlier session so two checkers never race.

**What the checker reports as "done waiting":**
- *Waiting on a PR* — the PR is **merged**; or it is **blocked**: it (or its linked issue) carries `ai:review-blocked`, `ai:review-autofix-failed`, or `ai:needs-human`; it was **closed without merging**; or it is **stuck** — `mergeable_state` is `dirty` (conflict) or a required check is failing, **and** the head commit is older than 6 hours, **and** no `review_autofix` run for the PR is queued or in progress. Red checks or open review threads on a PR whose head is younger than 6 hours, or whose review run is still going, are the autofix workflow's business — the checker stays silent and this session never intervenes.
- *Waiting on a workflow run* — the run's `status` is `completed` (any conclusion).
- *Waiting on `ai:security` follow-up issues* — every listed issue is `ai:merged` or closed.

**Fallbacks.** If `create_trigger` is unavailable but `send_later` is, arm a plain `send_later` with `delay_minutes: 180` into this session and re-arm it on every wake (same loop, on the session's model). If neither exists (a local CLI session), say so once, leave the log at the current stage, and tell the user that re-running `/implement-plan-claude <plan>` resumes from the log — do not fall back to polling with `sleep`.

### Checker prompt

Fill every `<…>` before creating the checker Routine; the prompt must stand alone because the checker session starts with no context.

```
You are the 3-hourly checker for /implement-plan-claude in <owner>/<repo>. Read-only, no questions, no pushes, no PR comments, no code changes — a Bash `sleep` or a `subscribe_pr_activity` call is forbidden. Do this and then end the turn:

1. Read docs/implement-plan/<slug>.md from origin/<default> (`git fetch origin <default> && git show origin/<default>:docs/implement-plan/<slug>.md`) for the current `Stage:` and `Waiting on:` lines; the values below are what the main session recorded and win if the file lags.
   Stage: <stage>   Waiting on: <PR #N | run <id> | issues #a, #b>
2. Check that object with a handful of REST calls, never a sweep (`gh api repos/<owner>/<repo>/pulls/<N>` for merged / state / labels / mergeable_state / head sha; `gh api repos/<owner>/<repo>/commits/<head sha>/check-runs` for failing checks and the head commit date; `gh run view <id> -R <owner>/<repo> --json status,conclusion` for a run; `gh api repos/<owner>/<repo>/issues/<n>` per listed issue; `gh run list -R <owner>/<repo> --workflow=<review workflow file> --branch=<head branch> -L 3 --json status` for the review run). Never use GraphQL-backed `gh` commands.
3. Decide "done waiting" exactly as follows: a PR is done when `merged` is true, when `state` is closed, when it or its linked issue carries ai:review-blocked / ai:review-autofix-failed / ai:needs-human, or when (`mergeable_state` is dirty OR a required check is failing) AND the head commit is older than 6 hours AND no review run for the branch is queued or in progress. A run is done when `status` is completed. An issue list is done when every issue carries ai:merged or is closed. Anything else is not done.
4. Not done → say `still waiting` in one line and end the turn. Done → call fire_trigger with trigger_id <poke trig_… id> and a `text` of one line stating what you observed (e.g. `PR #N merged at <time>` / `PR #N blocked: ai:review-blocked` / `run <id> completed: <conclusion>`), then end the turn.
```

## Progress Log

`docs/implement-plan/<slug>.md`, one file per plan, committed in the repo so that a fresh session — new machine, re-cloned container, or a later day — resumes at the exact stage instead of restarting. It rides in each phase PR, each validation-fix PR, and the completion PR, so the copy on the default branch can lag the live session by one step; that is why step 2 re-verifies `Waiting on:` against GitHub before acting.

- **Read first (mandatory).** Never open a PR before reading this file. `Status: COMPLETE` → report and stop. `Status: BLOCKED` → restate the recorded blocker and ask before continuing.
- **Update at every hand-off**: PR opened, PR merged, intervention pushed, run dispatched, run read, cycle counter incremented, Routines created or deleted. Refresh `Last updated` and `Last note` each time; use the date from the session context.
- **Persist by committing it** on the branch of the PR that is in flight (its own `docs:`-scoped commit). A log change with no PR in flight (e.g. "validation dispatched, run 123") waits for the next branch — the Routine prompts carry the same facts in the meantime.

**Log file format:**

```
# Implement-Plan Log — <plan title>

- Plan: docs/plans/<slug>-plan.md
- Repo: <owner>/<repo>   Default branch: <branch>
- Status: IN_PROGRESS | BLOCKED | COMPLETE
- Stage: phase <n>/<total> | security-pass cycle <k>/5 | validation cycle <k>/3 | completion
- Waiting on: PR #N | run <id> | issues #a, #b | none
- Routines: poke <trig_…>   checker <trig_…>   (or: none)
- Last updated: <YYYY-MM-DD>
- Last note: <one line — what the last session did / why blocked>

## Phases
1. [x] Phase 1 — <scope>   — PR #N merged <YYYY-MM-DD>; interventions: 0
2. [ ] Phase 2 — <scope>   — PR #M open (waiting); interventions: 1 (<date>: merged main, resolved conflict in <file>)
3. [ ] Phase 3 — <scope>

## Security pass
- Cycle 1 — run <id> <date>: <clean | follow-ups #a, #b (waiting) | skipped: unchanged>

## Validation
- Cycle 1 — run <id> <date>: status=<pass|fail|error> raw_status=<…> — <fix PR #P merged | summary>

## Completion
- PR #C <open | merged <date>> — doc moved to docs/completed/<slug>-plan.md

## Notes
- <free-form: decisions, skipped steps and why, errors hit>
```

## Output Format

```
Plan: <title>  (docs/plans/<slug>-plan.md)
Status: IN_PROGRESS / BLOCKED / COMPLETE
Stage: phase <n>/<total> | security-pass cycle <k>/5 | validation cycle <k>/3 | completion

Phases:
- [x] Phase 1 — <scope> — PR #N merged <date>
- [ ] Phase 2 — <scope> — PR #M open, waiting (next check-in ~<time> UTC)
- [ ] Phase 3 — <scope>

This turn: <what was implemented / verified / dispatched / resolved, with file:line and test evidence>
Waiting on: PR #M | run <id> | issues #a, #b   Check-in: Sonnet checker <trig_…> every 3h → poke <trig_…>  (or: send_later fallback / none — resume by re-running the command)
Security: <not started | cycle k: clean | cycle k: follow-ups #a, #b pending | not wired>
Validation: <not started | cycle k: pass | cycle k: needs_fixes → fix PR #P | not wired>
Doc: <in docs/plans/ | moved to docs/completed/<slug>-plan.md in PR #C>
Log: docs/implement-plan/<slug>.md
Branch: <branch>   PR: <url>
```

No prose padding. A bare "phase 1 pushed — see PR" is not acceptable: the user wants the stage, the evidence, what is being waited on, how the check-in is armed, and where the log is.

## Tool Access

- **`mcp__github__*` MCP tools** — always available. `mcp__github__create_pull_request` to open each PR; `pull_request_read` / `issue_read` / `list_pull_requests` / `list_issues` / `get_file_contents` / `list_branches` for merge state, follow-up issues, the tracker issue, and branch-collision checks; `mcp__github__actions_run_trigger` / `actions_get` / `get_job_logs` as the MCP route for the security and validation dispatches and their results.
- **`gh` CLI** — the `GH_TOKEN` transport. Shared rules live in **CLAUDE.md §23** and are not restated here: availability and the nounset-safe auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene, and the self-serve-read / ask-first-mutation split (§23.A–E). Use for default-branch detection (`gh repo view --json defaultBranchRef -q .defaultBranchRef.name -R <owner>/<repo>`), `gh workflow run` for the two dispatches, and `gh run list` / `gh run view --log` for their results. Pushing branches and opening PRs are §23.B routine writes — self-serve. The security-audit and validate dispatches in steps 8–9 are the §23.C **command-invoked** carve-out: invoking this command is the approval, so do not re-ask; every other §23.C operation (merging a PR yourself, force-push, deletions, repo settings) still needs the §2 Q/A ask — in particular this command **never merges** a PR, auto-merge and the review workflow do.
- **claude-code-remote MCP tools** (`mcp__Claude_Code_Remote__*`) — `create_trigger`, `fire_trigger`, `delete_trigger`, `list_triggers`, `send_later` for the [Check-in Loop](#check-in-loop). Never `subscribe_pr_activity` (§25).

Local file reads use `Read`; code search uses `Grep` / `Glob`; git + test/lint runs use `Bash`.

## Rules

- **One phase per PR, in the plan's order, and never the next phase before the current one merged.** The plan's Phases & Merge Strategy is the unit of delivery, exactly as the orchestrator ships one PR per phase issue. Do not fold two phases into one PR to save a cycle, and do not start phase `n+1` on top of an unmerged phase `n` branch.
- **Wait, don't babysit.** Between PR-opened and PR-merged the only activity is the 3-hourly checker. No `subscribe_pr_activity` (§25 — a hook blocks it), no polling loop, no `sleep`, no CI fixing, no review replies while the PR is healthy. `review_autofix.yml` owns review, autofix, conflict resolution, and auto-merge for PR-backed `claude/**` branches.
- **Intervene only on a blocked PR, and only under §12.** "Blocked" is the definition in the [Check-in Loop](#check-in-loop): autofix has given up (`ai:review-blocked`, `ai:review-autofix-failed`, `ai:needs-human`), the PR was closed unmerged, or it has sat red / conflicted for 6+ hours with no review run active. Then fix it as PR Review Mode work (address threads, fix the check, resolve the conflict), push, and go back to waiting. At most 3 interventions per PR before asking.
- **Security before validation, both from the repo's own workflows, both bounded.** Security findings are fixed by the `ai:security` follow-up issues flowing through the AI pipeline — never by a Claude-authored security patch — and the audit re-runs after they merge (cap 5 cycles). Validation failures with `needs_fixes` are fixed by Claude in a validation-fix PR and validation re-runs (cap 3 cycles). Terminal validation classes (`harness_error`, `infeasible`, `codex_failure`) and any workflow that fails before doing its job are asked about, not looped on.
- **Never move the doc before the gates pass.** `docs/completed/` is the "project done" signal. It moves in the completion PR only after every phase merged, the security pass is clean, and validation passed — with `git mv` so the plan's history follows it. A partial project leaves the doc in `docs/plans/` and the log at `IN_PROGRESS` / `BLOCKED`.
- **Verification is evidence-based.** Run the tests and linters; read the code; confirm the wiring. Do not assume a step is done because you wrote it — confirm it against the repo, with `file:line` / test citations in the PR body and the report.
- **The log is the resume point.** Read it before acting, update it at every hand-off, commit it with the PR in flight. A session that cannot schedule check-ins leaves an accurate log so the next invocation continues instead of restarting.
- **Honor the project rules while implementing:** §5 (minimal change set), §6 (naming immutability — add aliases, never rename in place), §7 (update `README.md` / `agents.md` on behavior change), §9 (style), §10 (MongoDB contracts + index registry), §14 (consumer-repo registry if templates/`.claude` assets change), §15 (GitHub API hygiene — the checker makes one or two REST calls per firing, never a per-item sweep), §18 (automation bias + future-removal registry), §19 (PR-body auto-close discipline), §20 (changelog fragments, never `CHANGELOG.md` directly), §21 (never commit onto a merged PR's branch — every phase gets a fresh branch from the default branch), §25 (no PR watching).
- **Branch + push + PR; never push to the default branch.** Each PR is the deliverable for its phase; this command never merges anything itself.
- **If a plan step belongs in the upstream workflow library rather than this repo**, do not edit the upstream from this session — implement the parts that belong here and surface the upstream-bound step in the report instead of guessing.
- **If the plan is ambiguous or under-specified** such that implementing a phase correctly would require guessing intent, stop and ask (§0/§2) rather than shipping a guess; leave the log at `BLOCKED` with the question.
- **In-session only — for orchestrator hand-off use `/implement-plan-ai`.** This command implements the plan itself, phase by phase, with Claude as the implementer and the review-autofix workflow as the merger. `/implement-plan-ai` dispatches the unattended orchestrator, which decomposes the plan into issues and runs the same security → validation → completion lifecycle from the poller instead.
