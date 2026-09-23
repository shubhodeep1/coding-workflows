Implement the plan documented at the path / reference in `$ARGUMENTS` **directly in Claude Code, one phase at a time, driving the whole project to completion and activation the way the AI orchestrator does**: ship each phase of the plan's **Phases & Merge Strategy** as its own PR against the default branch, wait for the review-autofix workflow and auto-merge to land it (a cheap 3-hourly Haiku check-in, never a CI or review-comment babysit), then implement the next phase from the freshly merged default branch. After the last phase merges, run the repo's **security audit** workflow and then the **runtime validation** workflow — the orchestrator's `ai:security-pass → ai:validating` order — looping each until it is clean, then open the completion PR that moves the plan doc into `docs/completed/`, then run **`/verify-activation`** on the finished project and, when it reports DORMANT, start **`/deploy-activate`** for you. **Every stage runs in its own fresh session** (see [Stage Sessions](#stage-sessions)) so no step re-sends the whole history; progress is persisted to `docs/implement-plan/<slug>.md` and to the stage-session prompts so any later session resumes where the last one stopped. This is the **in-session** variant — Claude writes the code. To hand the plan to the unattended **AI orchestrator** instead (which decomposes the work into a dependency DAG of issues and ships each phase as its own PR), use `/implement-plan-ai`. `$ARGUMENTS` is **free-form prose** that must resolve to a single plan markdown doc — typically a path like `docs/plans/<slug>-plan.md`, but it may also be a slug, a topic that matches one plan filename, or a GitHub reference (issue / PR) that links to the plan. Optional trailing prose (focus areas, a phase to start with) is allowed, and a trailing `— source revision <ref>` makes every stage and checker session check out `<ref>` instead of the default branch (only for testing an unmerged change to this command; normal runs omit it); a stage session started by this command also receives a trailing `— resume.` block (see [Stage Sessions](#stage-sessions)).

$ARGUMENTS

## Procedure

0. **Session preflight.** Call `get_session` (claude-code-remote MCP) with no `session_id`; record your own session id, `session_context.model` (the **stage model**), and `permission_mode`.
   - **Permission mode.** Stage and checker sessions inherit at most this session's mode, and nobody is watching them to approve prompts. If the mode is not `auto` (or `bypassPermissions`), stop and ask before doing anything else:
     > **Q1: This session is in `<mode>` mode; the unattended stage sessions this command starts would stop at every permission prompt the `.claude/settings.json` allowlist does not cover. How should I continue?**
     > - **A** — Switch this session to Auto mode in the app, then reply `Q1: A` and I continue (RECOMMENDED)
     > - **B** — Continue in `<mode>` mode; a stage session that hits an unlisted prompt waits for you to approve it in that session
     A `— resume.` stage session skips this question — its mode was fixed by the session that created it.
   - **Resume hygiene** (only when `$ARGUMENTS` carries a `— resume.` block): for the `Previous stage session` and `Checker session` ids it names, call `get_session`; archive each with `archive_session` **unless** its `status_bucket` is `blocked` or its `post_turn_summary.status_category` is `need_input` (a session waiting on the user is never archived). Delete the `Safety-net trigger` it names with `delete_trigger` (ignore not-found).
   - **No claude-code-remote tools** (a local CLI / desktop / IDE session): say so once and follow [Check-in Loop → Fallbacks](#fallbacks).

1. **Resolve the plan doc.** Parse `$ARGUMENTS` for a path under `docs/plans/` (or elsewhere), a slug, or a reference that links to a plan. Resolve it to exactly one markdown file and `Read` it in full. If `$ARGUMENTS` is empty, matches no plan, or matches more than one and the intent is ambiguous, **stop and ask** which plan to implement — never guess. If it names a GitHub issue / PR, fetch that first (see [Tool Access](#tool-access)) to find the linked plan file. Derive `<slug>` from the plan filename (`docs/plans/<slug>-plan.md` → `<slug>`; a plan elsewhere uses its basename without `-plan.md` / `.md`). A plan already moved to `docs/completed/<slug>-plan.md` is valid input for the activation stages (steps 11–12).

2. **Read the progress log first, then project context.** Read `docs/implement-plan/<slug>.md` (see [Progress Log](#progress-log)). If it exists it is the source of truth for what has already shipped: resume from its `Stage:` / `Waiting on:` lines — and from the `— resume.` block, which wins where the log lags — re-verifying against GitHub, skip the phases marked `[x]`, and say so. If it shows `Status: COMPLETE` with `Activation: LIVE` or `Activation: deploy-activate started`, report and stop; `Status: COMPLETE` with any other `Activation:` value resumes at step 11. If it does not exist, create it before opening the first PR. Then always read `README.md`, `agents.md`, and `CLAUDE.md` at the repo root before editing. If the plan touches a MongoDB collection, also read every relevant `/db/contracts/*.yml` (CLAUDE.md §10). Read the actual source for every file the plan names — never guess at code, env vars, or workflow inputs. Before creating anything, `list_triggers` and delete stale `implement-plan <slug>` Routines from the previous design (a `resume` / `3h checker` / `safety-net` pair) so two check-ins never race.

3. **Build the phase checklist.** Convert the plan's **Phases & Merge Strategy** section into a tracked checklist (CLAUDE.md §11): one top-level item per phase, in the plan's order, each carrying the plan's per-phase implementation steps, files, tests, and "done" condition as sub-items. A plan whose Phases section says it is a single phase is a one-item list — the same loop runs once. A plan with no Phases section is under-specified: **stop and ask** how to split it (§0/§2) rather than inventing phases. Keep the checklist visible in chat and mirrored in the progress log.

4. **Implement the current phase — and only that phase.** From a fresh checkout of the default branch (`git fetch origin <default> && git checkout -B claude/implement-plan-<slug>-phase-<n> origin/<default>`; append `-2`, `-3`, … on collision), implement exactly the steps the plan assigns to phase `<n>`. Honor §5 (minimal change set — implement what the plan specifies, no speculative extras and nothing from a later phase), §6 (naming immutability — renames are breaking; add aliases), §9 (style — tabs except where the format forbids them; YAML stays 2-space), §10 (MongoDB — index registry, contracts, unique-index safety), and §18 (automation bias — wire new operations into the scheduler; no standalone manual scripts; DB work runs from code with a gate). Write the code, update `/db/contracts/*` and indexes where touched, add/extend the tests the plan calls for, update `README.md` / `agents.md` when behavior changes (§7), and add a `changelog.d/` fragment when the phase changes observable behaviour (§20). If the phase introduces a single-use or long-running script/supervisor, add its entry to `docs/scripts-pending-removal.md` in this same PR (§18.F).

5. **Verify the phase — evidence-based, not vibes.** Re-read the phase's "done" condition and check **each** checklist sub-item against the *actual* repo state: run the test suite and linters the plan specifies (and the repo's standard checks); `Grep` / `Read` to confirm the code, config, workflow wiring, and contracts exist and are correct; confirm rollout wiring (§18). Classify every sub-item **complete** (with a `file:line` / test-name citation), **partial**, or **not-done**. A phase ships only on a clean sweep; if something is blocked or too large to finish safely in this PR, stop, leave the log at `Status: BLOCKED`, and report exactly what remains.

6. **Commit, push, open the phase PR, update the log.** Group commits by scope (§12.E): the implementation commit(s), then a separate commit for the progress-log update plus any `README.md` / `agents.md` changes. Push with `git push -u origin <branch>` (retry transient network errors with exponential backoff: 2s, 4s, 8s, 16s — up to 4 retries). Open a ready-for-review PR via `mcp__github__create_pull_request` against the default branch (resolve it dynamically — do **not** hardcode `main`), `draft: false`, titled `<plan title> — phase <n>/<total>: <phase scope>`, whose body names the plan path, the phase, the checklist evidence from step 5, and the progress log. **PR-body keyword discipline (§19):** if the plan links an `ai:orchestrator-tracking` issue, use `Refs #N` / `Related to #N`, never `Fixes/Closes/Resolves #N`. Record the PR number in the log (`Waiting on: PR #N`). This command's checker is the PR's CLAUDE.md §26 check-in — do not arm a second one.

7. **Wait for the merge — hand off to a checker and end.** Do **not** subscribe to PR activity (§25), do not poll, and do not fix CI failures or answer review comments while the PR is healthy: `review_autofix.yml` reviews, autofixes, resolves conflicts, and enables auto-merge on PR-backed `claude/**` branches by itself. Arm the wait per [Check-in Loop](#check-in-loop) with next stages `phase <n+1>/<total>` (or `security-pass 1/5` after the last phase) on merge and `phase <n>/<total> — blocked PR` on a block, emit the report, and end the turn. The checker starts the next stage session; this session is archived by it.
   - **Merged** (next stage session) → tick the phase in the log and go to step 4 for the next phase from the freshly merged default branch. After the last phase, go to step 8.
   - **Blocked** (the definition in [Check-in Loop](#check-in-loop); next stage session) → this is the one case where Claude touches the PR: check out the branch, resolve the block under CLAUDE.md §12 (merge the default branch and resolve the conflict, address the review threads, fix the failing check, or re-open / re-create the PR if it was closed unmerged), verify, push, record the intervention in the log, and arm the wait again. Cap: **3 interventions per PR**; on the fourth blocked stage stop and ask in Q/A format instead of pushing again.

8. **Security pass (orchestrator order: security before validation).** Dispatch the security audit workflow — `security-audit.yml` in this library, `ai-security-audit.yml` in consumer repos (pick the one that exists; if neither exists, record `Security: not wired` in the log and continue to step 9) — with `gh workflow run <file> -R <owner>/<repo>` (or `mcp__github__actions_run_trigger`), find the run id (`gh run list -R <owner>/<repo> --workflow=<file> -L 1 --json databaseId`), record it (`Waiting on: run <id>`), arm the wait on the run (next stage `security-pass <k>/5 — read result`), and end the turn. Invoking this command *is* the §23.C approval for these dispatches — do not re-ask. In the next stage session, **first read the run's conclusion** (`gh run view <id> -R <owner>/<repo> --json status,conclusion`; the `— resume.` block's `Checker observed:` line carries it too) — only a `success` run has audited anything; then read the result: the new dated section on the `AI Security Audit Tracker` issue (label `ai:security-audit`, marker `<!-- ai:security-audit-tracker:v1 -->`) and the `ai:security` follow-up issues it opened (marker `<!-- ai:security-finding:<id> -->`), or the log-only skip when `SECURITY_AUDIT_SKIP_IF_UNCHANGED` found the default branch already audited.
   - **Conclusion is not `success`** (`failure`, `cancelled`, `timed_out`, `startup_failure`, …) → the audit did not run to completion, so an empty tracker section and zero follow-ups prove nothing: this is the *run failed* case below, never a clean pass.
   - **Conclusion `success` and no follow-up issues opened, or the run skipped as unchanged** → the pass is clean; go to step 9.
   - **Follow-up issues opened** → Claude writes **no** security fix. The follow-ups enter the normal clarify → plan → implement → review pipeline on their own (that is how the orchestrator's security pass fixes findings too). Record them in the log, arm the wait on the issue list (next stage `security-pass <k+1>/5`), and re-dispatch the audit in that stage. Each re-dispatch is one **security cycle**; cap **5 cycles** (the orchestrator's `MAX_SECURITY_PASS_CYCLES` default). On exhaustion, or if a follow-up is closed without a merged PR, stop with `Status: BLOCKED` and ask in Q/A format.
   - **Run failed** (any non-`success` conclusion: workflow error, missing secret, a crashed audit step) → record the run id, conclusion, and the failing step from `gh run view --log-failed <id> -R <owner>/<repo>` in the log, set `Status: BLOCKED`, and ask in Q/A format (re-dispatch after a fix lands, or skip the security pass); do not loop on an infrastructure failure and never continue to step 9 on your own.

9. **Runtime validation.** Dispatch the validate workflow — `internal-validate.yml` here, `ai-validate.yml` in consumers (if neither exists, record `Validation: not wired` and continue) — with `-f tracking_issue=0 -f pr_number=0` (standalone mode: there is no orchestrator tracking issue, so the workflow neither comments nor opens fix-up issues; its verdict is in the run), record the run, arm the wait on it (next stage `validation <k>/3 — read result`), end the turn. In the next stage session, first read the run's conclusion as in step 8; a non-`success` run with no `VALIDATION_FAILURE_SUMMARY` line failed before validating and is handled like the terminal classes below (record, `Status: BLOCKED`, ask). Otherwise read the verdict from the `VALIDATION_FAILURE_SUMMARY` line in the run log (`status=pass|fail|error`, `raw_status=…`, `summary=…`, `failure_summary=…`; `gh run view --log <run-id> -R <owner>/<repo>`) and, when present, the `validation_status.json` in the `ai-validation-<run-id>-<attempt>` artifact.
   - **`status=pass`** → go to step 10.
   - **`raw_status=needs_fixes`** → Claude fixes it: branch `claude/implement-plan-<slug>-validation-fix-<cycle>` from the default branch, implement the fixes the diagnosis describes (the `fix_issues` proposals), verify per step 5, push, open the PR, wait for its merge exactly as in step 7 (next stage `validation <k+1>/3`), then re-dispatch validation. Each re-dispatch is one **validation cycle**; cap **3 cycles** (`MAX_VALIDATE_CYCLES` default). On exhaustion stop with `Status: BLOCKED` and ask.
   - **`harness_error`, `infeasible`, `codex_failure`, or an unknown payload** → these are terminal in the orchestrator too; record the summary and ask in Q/A format rather than guessing at a fix.

10. **Completion PR — move the doc only now.** From the default branch, `git mv docs/plans/<slug>-plan.md docs/completed/<slug>-plan.md` (create `docs/completed/` if missing; use `git mv` so history is preserved), set the log to `Status: COMPLETE`, `Activation: pending verify-activation`, with the merged PR list and security and validation evidence, and land any `README.md` / `agents.md` update the phases did not already ship. Branch `claude/implement-plan-<slug>-complete`, push, open the PR (§19 discipline applies; if the plan links an `ai:orchestrator-tracking` issue the `lint-plan-archival.yml` gate requires that issue's checkboxes to be ticked or a `## De-scoped phases` section — never de-scope silently), and arm the wait as in step 7 (next stage `verify-activation 1/3`).

11. **Verify activation.** In the `verify-activation <k>/3` stage session, `Read` `.claude/commands/verify-activation.md` and follow its procedure with the completed plan (`docs/completed/<slug>-plan.md`, plus the merged PR list from the log) as its `$ARGUMENTS`. Then act on its result:
    - **Fix PR opened** (its Step 7 found fixable defects) → arm the wait on that PR as in step 7 (next stage `verify-activation <k+1>/3`); the next stage re-runs verification on the merged fixes. Cap **3 verify cycles**; if the verdict is still INCOMPLETE after the third, stop with `Status: BLOCKED` and ask.
    - **LIVE** → record `Activation: LIVE`, report, send one `PushNotification` (`<plan title>: complete and LIVE`), and stop. `/deploy-activate` is not needed. This final session stays open for the report.
    - **DORMANT** → go to step 12.
    - **INCOMPLETE with no fix PR** (every finding needs a decision) → stop with `Status: BLOCKED` and ask the Q/A questions `/verify-activation` produced.

12. **Start `/deploy-activate`.** `/deploy-activate` is interactive — it gives you one step at a time and waits for your pasted output — so it gets its own fresh session: `create_session` with `source_url` = the repo, `model` = the stage model, `permission_mode` = this session's mode, `title` = `implement-plan <slug> — deploy-activate`, and `prompt` = `/deploy-activate docs/completed/<slug>-plan.md`. Record `Activation: deploy-activate started (<session id>)`, send one `PushNotification` (`<plan title>: ready to activate — open "implement-plan <slug> — deploy-activate"`), report, and archive this session with `archive_session` as the last action.

13. **Report.** Emit the [Output Format](#output-format) in chat at every hand-off point: after each PR is opened, after each stage that changed state, and at completion.

## Stage Sessions

Waking one long-lived session re-sends its whole conversation on every wake, and a 3-hour gap outlives the prompt cache, so each wake pays for the full history at full price. This command therefore never wakes the session that did the previous stage. Each **stage** — one phase, one blocked-PR intervention, one security or validation read, the completion PR, one verify-activation cycle — runs in a **fresh session** started by the checker, whose only inputs are this command file, the progress log, and a short `— resume.` block. The expensive model only ever reads what the current stage needs.

- **Started by** the checker with `create_session`: `source_url` = the repo, no `source_revision` (the default branch, so the stage sees every merged phase; only a `— source revision <ref>` run passes `<ref>`), `model` = the stage model, `permission_mode` = the mode the checker was given, `title` = `implement-plan <slug> — <next stage>` (for example `implement-plan heal-autofix — phase 2/4`, `… — security-pass 1/5`, `… — validation 2/3 — read result`, `… — verify-activation 1/3`), and `prompt`:
  ```
  /implement-plan-claude <plan path> — resume.
  Stage: <next stage>   Checker observed: <verdict reason>
  Previous stage session: <id>   Checker session: <id>   Safety-net trigger: <trig_… id>
  Waiting on was: <PR #N | run <id> | issues #a, #b>   Stage model: <model>
  <— source revision <ref>, only when the run carries one>
  ```
- **Names say where the project is.** Titles always carry the plan slug and the stage, so the session list reads as the project's timeline.
- **Archiving** (step 0): each stage session archives the previous stage session and the checker, never one that is waiting on the user. The final LIVE report and the `/deploy-activate` session stay open. In steady state you see one open session per plan: the current stage (or its Haiku checker while waiting).

## Check-in Loop

The wait between "PR opened" and "PR merged" (and between "workflow dispatched" and "run finished", and "follow-ups opened" and "follow-ups merged") is a **3-hourly check-in by a Haiku checker session**, not a watch. It never uses `subscribe_pr_activity`, which §25 forbids and a hook blocks; it is the scheduled check-in §25.C allows and this plan's §26 check-in.

**Why a checker session and not a Routine.** A Routine created with `create_new_session_on_fire` starts a session with **no MCP tools and no repository** (connectors are not carried over), so it can neither read the repo nor start the next stage — a checker built that way can never report and the project stalls silently. A session started with `create_session` gets the repository, `gh`, and the claude-code-remote tools, inherits the creator's permission mode, and can re-arm itself with `send_later`.

**Arming the wait** (the stage session, at the end of its stage):
1. **Checker** — `create_session` with `source_url` = the repo (plus `source_revision` for a `— source revision <ref>` run), `model: claude-haiku-4-5-20251001`, `permission_mode` = this session's mode, `title` = `implement-plan <slug> — waiting: <PR #N | run <id> | issues #a, #b>`, and a standalone prompt built from the [Checker prompt](#checker-prompt) template.
2. **Safety net** — `send_later` into **this** session with `delay_minutes: 1440`, `initiation: own_followup`, `name: implement-plan <slug>: safety net`, and a message: `Safety net for /implement-plan-claude <plan path>: the next stage never started. get_session <checker id>; if it is blocked or idle with no pending check-in, run .claude/scripts/check_in_status.py yourself, then either start the next stage session per the Stage Sessions template or arm a fresh checker, and re-arm this safety net.` The next stage session deletes it (step 0), so it only fires when the chain has stalled — one wake of this session per stalled day, never in the normal path.
3. Record both ids (`Checker:`, `Safety net:`) in the log and the report, then end the turn.

**What counts as "done waiting"** is decided by `.claude/scripts/check_in_status.py`, never by the checker model:
- *PR* — merged; closed without merging; labelled `ai:review-blocked`, `ai:review-autofix-failed`, or `ai:needs-human`; or **stuck**: `mergeable_state` is `dirty` or a check run on the head commit failed, **and** the head commit is older than 6 hours, **and** no workflow run on the head branch is queued or in progress. Red checks or open threads on a younger head, or while a run is active, are the autofix workflow's business — the checker stays quiet.
- *Workflow run* — `status` is `completed` (any conclusion); a non-`success` conclusion reports `state: failed`, which routes to the block stage (for steps 8–9, `security-pass <k>/5 — run failed` / `validation <k>/3 — run failed`).
- *Issue list* — every issue is closed or labelled `ai:merged`.

The script issues one REST call in terminal-only mode. Non-terminal PR checks paginate check runs in 100-item pages and use at most three further calls once an old failure is found; it never uses GraphQL (§15), and prints one JSON line (`done`, `state`, `reason`); exit 2 means the read failed.

### Checker prompt

Fill every `<…>` before creating the checker; the prompt must stand alone because the checker starts with no context.

```
You are the Haiku check-in session for /implement-plan-claude in <owner>/<repo>, plan <plan path>. Read-only on GitHub: no pushes, no PR comments, no code edits, no Bash `sleep`, never subscribe_pr_activity. Load deferred tools with ToolSearch when needed. Each time you run (now, and each time a send_later reminder wakes you):

1. Run: PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/check_in_status.py --repo <owner>/<repo> <--pr N | --run ID | --issues a,b>
2. `"done": false` and no `error` → call send_later with delay_minutes 180, initiation own_followup, name "implement-plan <slug>: check-in", message "Check-in: repeat the steps in your first message." Reply `still waiting: <reason>` in one line and end the turn.
3. `error` (exit 2) → same as step 2, but count it; on the third consecutive error treat it as done with reason `checker could not read state: <error>`.
4. `"done": true` → pick the next stage: if `state` is merged / completed / resolved use `<next stage on success>`; if `state` is blocked / closed / stuck / failed use `<next stage on block>`. Call create_session with source_url https://github.com/<owner>/<repo><, source_revision <ref> — only for a source-revision run>, model <stage model>, permission_mode <mode>, title "implement-plan <slug> — <next stage>", and prompt:
   /implement-plan-claude <plan path> — resume.
   Stage: <next stage>   Checker observed: <reason>
   Previous stage session: <stage session id>   Checker session: <your id — Bash: echo "session_${CLAUDE_CODE_REMOTE_SESSION_ID#cse_}">   Safety-net trigger: <trig_… id>
   Waiting on was: <what you checked>   Stage model: <stage model>
   <— source revision <ref>, only for a source-revision run>
   Then call PushNotification (if available) with "<slug>: <reason> — started <next stage>", and finally archive_session on your own session id. Do nothing after that.
```

### Fallbacks

- **No `create_session`** (a local CLI, desktop, or IDE session without the claude-code-remote MCP server): arm `send_later` into this session with `delay_minutes: 180` and a message that repeats the checker steps, and on each wake delegate the check to a Haiku subagent (Agent tool, `model: "haiku"`) that runs `check_in_status.py` and returns its JSON; when done, continue in this session. If `send_later` is missing too, use `CronCreate` (every 3 hours, deleted with `CronDelete` when done) and say once that it lives only as long as the session. If neither exists, say so, leave the log at the current stage, and tell the user that re-running `/implement-plan-claude <plan>` resumes from the log — never poll with `sleep`.
- **A checker that is blocked** on a permission prompt (a stage started in a non-Auto mode) shows as `blocked` in the session list; approving its prompt lets it continue, and the safety net catches it after 24 hours either way.

## Progress Log

`docs/implement-plan/<slug>.md`, one file per plan, committed in the repo so that a fresh session — new machine, re-cloned container, or a later day — resumes at the exact stage instead of restarting. It rides in each phase PR, each validation-fix PR, each verify-activation fix PR, and the completion PR, so the copy on the default branch can lag the live session by one step; the `— resume.` block carries the current stage in the meantime, and step 2 re-verifies `Waiting on:` against GitHub before acting.

- **Read first (mandatory).** Never open a PR before reading this file. `Status: BLOCKED` → restate the recorded blocker and ask before continuing.
- **Update at every hand-off**: PR opened, PR merged, intervention pushed, run dispatched, run read, cycle counter incremented, checker / safety net armed, session handed off. Refresh `Last updated` and `Last note` each time; use the date from the session context.
- **Persist by committing it** on the branch of the PR that is in flight (its own `docs:`-scoped commit). A log change with no PR in flight (e.g. "validation dispatched, run 123") waits for the next branch — the checker and stage-session prompts carry the same facts in the meantime.

**Log file format:**

```
# Implement-Plan Log — <plan title>

- Plan: docs/plans/<slug>-plan.md
- Repo: <owner>/<repo>   Default branch: <branch>
- Status: IN_PROGRESS | BLOCKED | COMPLETE
- Stage: phase <n>/<total> | security-pass cycle <k>/5 | validation cycle <k>/3 | completion | verify-activation <k>/3 | deploy-activate
- Activation: not started | pending verify-activation | LIVE | deploy-activate started (<session id>)
- Waiting on: PR #N | run <id> | issues #a, #b | none
- Stage model: <model>   Permission mode: <mode>
- Check-in: checker <session id>   safety net <trig_…>   (or: none)
- Last updated: <YYYY-MM-DD>
- Last note: <one line — what the last stage did / why blocked>

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

## Activation
- Verify cycle 1 — <date>: <LIVE | DORMANT | INCOMPLETE> — <fix PR #F | no fixes>
- Deploy-activate: <not needed | session <id> started <date>>

## Notes
- <free-form: decisions, skipped steps and why, errors hit>
```

## Output Format

```
Plan: <title>  (docs/plans/<slug>-plan.md)
Status: IN_PROGRESS / BLOCKED / COMPLETE
Stage: phase <n>/<total> | security-pass cycle <k>/5 | validation cycle <k>/3 | completion | verify-activation <k>/3 | deploy-activate

Phases:
- [x] Phase 1 — <scope> — PR #N merged <date>
- [ ] Phase 2 — <scope> — PR #M open, waiting (next check-in ~<time> UTC)
- [ ] Phase 3 — <scope>

This stage: <what was implemented / verified / dispatched / resolved, with file:line and test evidence>
Waiting on: PR #M | run <id> | issues #a, #b   Check-in: Haiku checker <session id> every 3h → next stage "<title>"; safety net <trig_…> (or: fallback / none — resume by re-running the command)
Security: <not started | cycle k: clean | cycle k: follow-ups #a, #b pending | not wired>
Validation: <not started | cycle k: pass | cycle k: needs_fixes → fix PR #P | not wired>
Activation: <not started | verify k: LIVE | verify k: DORMANT → deploy-activate session <id> | verify k: fix PR #F>
Doc: <in docs/plans/ | moved to docs/completed/<slug>-plan.md in PR #C>
Log: docs/implement-plan/<slug>.md
Branch: <branch>   PR: <url>
```

No prose padding. A bare "phase 1 pushed — see PR" is not acceptable: the user wants the stage, the evidence, what is being waited on, how the check-in is armed, and where the log is.

## Tool Access

- **`mcp__github__*` MCP tools** — always available. `mcp__github__create_pull_request` to open each PR; `pull_request_read` / `issue_read` / `list_pull_requests` / `list_issues` / `get_file_contents` / `list_branches` for merge state, follow-up issues, the tracker issue, and branch-collision checks; `mcp__github__actions_run_trigger` / `actions_get` / `get_job_logs` as the MCP route for the security and validation dispatches and their results.
- **`gh` CLI** — the `GH_TOKEN` transport. Shared rules live in **CLAUDE.md §23** and are not restated here: availability and the nounset-safe auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene, and the self-serve-read / ask-first-mutation split (§23.A–E). Use for default-branch detection (`gh api repos/<owner>/<repo> --jq .default_branch` — REST, and note `gh repo view` takes the repo as a positional argument, not `-R`), `gh workflow run` for the two dispatches, and `gh run list` / `gh run view --log` for their results. Pushing branches and opening PRs are §23.B routine writes — self-serve. The security-audit and validate dispatches in steps 8–9 are the §23.C **command-invoked** carve-out: invoking this command is the approval, so do not re-ask; every other §23.C operation (merging a PR yourself, force-push, deletions, repo settings) still needs the §2 Q/A ask — in particular this command **never merges** a PR, auto-merge and the review workflow do.
- **`.claude/scripts/check_in_status.py`** — the checker's verdict (see [Check-in Loop](#check-in-loop)); stage sessions may run it too.
- **claude-code-remote MCP tools** (`mcp__Claude_Code_Remote__*`; in sessions started by `create_session` the same tools appear under a generated server name) — `get_session`, `create_session`, `archive_session`, `set_session_title`, `send_later`, `list_triggers`, `delete_trigger` for [Stage Sessions](#stage-sessions) and the [Check-in Loop](#check-in-loop); `PushNotification` for the hand-off and completion notices. Never `subscribe_pr_activity` (§25).
- **Permissions** — `.claude/settings.json` pre-approves the tools this command calls (file edits, `claude/*` pushes, `gh` REST reads, the four security / validate dispatches, the GitHub MCP tools, the claude-code-remote tools, and `check_in_status.py`). In a `create_session` child the claude-code-remote tools appear under a generated server name; the name observed in this account's cloud environment (`mcp__bf7c680d-5fdc-5ef4-b4a0-abadb619bf0a`) is allowlisted as a whole server, but allow rules cannot wildcard the server segment, so any other environment still relies on Auto mode (step 0). Auto mode alone is not enough either — its classifier has been seen to prompt on `get_session` in a checker — which is why the checker reads its own id from `CLAUDE_CODE_REMOTE_SESSION_ID` instead.

Local file reads use `Read`; code search uses `Grep` / `Glob`; git + test/lint runs use `Bash`.

## Rules

- **One phase per PR, in the plan's order, and never the next phase before the current one merged.** The plan's Phases & Merge Strategy is the unit of delivery, exactly as the orchestrator ships one PR per phase issue. Do not fold two phases into one PR to save a cycle, and do not start phase `n+1` on top of an unmerged phase `n` branch.
- **One stage per session.** Never wait inside the session that did the work: arm the checker and end the turn. Never wake a finished stage session to continue — the checker starts a fresh one.
- **Wait, don't babysit.** Between PR-opened and PR-merged the only activity is the Haiku checker. No `subscribe_pr_activity` (§25 — a hook blocks it), no polling loop, no `sleep`, no CI fixing, no review replies while the PR is healthy. `review_autofix.yml` owns review, autofix, conflict resolution, and auto-merge for PR-backed `claude/**` branches.
- **Intervene only on a blocked PR, and only under §12.** "Blocked" is the definition in the [Check-in Loop](#check-in-loop): autofix has given up (`ai:review-blocked`, `ai:review-autofix-failed`, `ai:needs-human`), the PR was closed unmerged, or it has sat red / conflicted for 6+ hours with no workflow run active. Then fix it as PR Review Mode work (address threads, fix the check, resolve the conflict), push, and go back to waiting. At most 3 interventions per PR before asking.
- **Security before validation, both from the repo's own workflows, both bounded.** Security findings are fixed by the `ai:security` follow-up issues flowing through the AI pipeline — never by a Claude-authored security patch — and the audit re-runs after they merge (cap 5 cycles). Validation failures with `needs_fixes` are fixed by Claude in a validation-fix PR and validation re-runs (cap 3 cycles). Terminal validation classes (`harness_error`, `infeasible`, `codex_failure`) and any workflow that fails before doing its job are asked about, not looped on.
- **Never move the doc before the gates pass.** `docs/completed/` is the "project done" signal. It moves in the completion PR only after every phase merged, the security pass is clean, and validation passed — with `git mv` so the plan's history follows it. A partial project leaves the doc in `docs/plans/` and the log at `IN_PROGRESS` / `BLOCKED`.
- **Activation follows completion.** `/verify-activation` runs only after the completion PR merged; `/deploy-activate` starts only on a DORMANT verdict, in its own session, and waits for you there — this command never runs a deploy step itself.
- **Never archive a session that waits on the user.** A session asking a Q/A question, blocked on a permission prompt, or holding the final report stays open.
- **Verification is evidence-based.** Run the tests and linters; read the code; confirm the wiring. Do not assume a step is done because you wrote it — confirm it against the repo, with `file:line` / test citations in the PR body and the report.
- **The log is the resume point.** Read it before acting, update it at every hand-off, commit it with the PR in flight. A session that cannot start stage sessions or schedule check-ins leaves an accurate log so the next invocation continues instead of restarting.
- **Honor the project rules while implementing:** §5 (minimal change set), §6 (naming immutability — add aliases, never rename in place), §7 (update `README.md` / `agents.md` on behavior change), §9 (style), §10 (MongoDB contracts + index registry), §14 (consumer-repo registry if templates/`.claude` assets change), §15 (GitHub API hygiene — terminal checks use one REST call; non-terminal checks paginate check runs and add at most three old-failure reads), §18 (automation bias + future-removal registry), §19 (PR-body auto-close discipline), §20 (changelog fragments, never `CHANGELOG.md` directly), §21 (never commit onto a merged PR's branch — every phase gets a fresh branch from the default branch), §25 (no PR watching), §26 (this command's checker is the PR's check-in).
- **Branch + push + PR; never push to the default branch.** Each PR is the deliverable for its phase; this command never merges anything itself.
- **If a plan step belongs in the upstream workflow library rather than this repo**, do not edit the upstream from this session — implement the parts that belong here and surface the upstream-bound step in the report instead of guessing.
- **If the plan is ambiguous or under-specified** such that implementing a phase correctly would require guessing intent, stop and ask (§0/§2) rather than shipping a guess; leave the log at `BLOCKED` with the question.
- **In-session only — for orchestrator hand-off use `/implement-plan-ai`.** This command implements the plan itself, phase by phase, with Claude as the implementer and the review-autofix workflow as the merger. `/implement-plan-ai` dispatches the unattended orchestrator, which decomposes the plan into issues and runs the same security → validation → completion lifecycle from the poller instead.
