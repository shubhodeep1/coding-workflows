Answer **one** `/implement-plan-claude` failure escalation (CLAUDE.md §28.C) without a human, under the operator's standing decision in CLAUDE.md §28.G. A stage that hits one of the ten escalation stops posts its blocker and hands its project checker an escalation wait; the checker starts this command as the next stage session (`implement-plan <slug> — escalation judge — <stop id>`, Opus 5.5 at high effort, the two-step start). You pick one choice from a fixed menu, record it, and hand the checker the next stage. You never merge, never skip or waive a security or validation pass, and never pick a choice already used for the same failure.

`$ARGUMENTS` is the plan path followed by a `— resume.` block (see "Stage Sessions" in `.claude/commands/implement-plan-claude.md`) with a `Stop:` line: `Stop: <stop id>   Blocker comment: <comment URL | report>   Log: docs/implement-plan/<slug>.md`, and the usual `Previous stage session:`, `Checker session:`, `Safety-net trigger:`, and `Project branch:` fields.

**The blocker's thread** is the issue or PR the `Blocker comment:` URL points at: the source issue in issue mode, the final PR in project mode. A legacy-mode project has neither, so its line reads `Blocker comment: report` and there is no thread: step 1 reads no comments (the log and the `— resume.` block are the whole record), and every step below that posts on the thread (steps 3, 6, and 8) makes no GitHub call and puts the same text, marker included, in its report and under the log's `## Notes` instead.

$ARGUMENTS

## The menu

| Choice | What happens | Allowed when |
|---|---|---|
| `budget` | The stage that stopped runs once more past its cap, with a **narrower fix** you name | not yet used for this failure |
| `descope` | The failing part leaves the project: a revert PR through the normal review, recorded as an `AD-<n>` entry | not yet used for this failure, and the rest of the project still meets the plan's core goal |
| `close` | The chain's open PRs and the source issue are closed as not planned, with a report | always |

A **failure** is a stop id plus its fingerprint (`.claude/scripts/escalation_ledger.py fingerprint`): the failing check names, finding ids, follow-up issue numbers, and validation class and status, normalised. A different failure starts with the full menu. The same failure never gets the same choice twice, so once `budget` and `descope` are used for it, only `close` remains.

## Procedure

0. **Preflight.** Your session id: Bash `echo "session_${CLAUDE_CODE_REMOTE_SESSION_ID#cse_}"`. Do the resume hygiene of `/implement-plan-claude` step 0: archive the `Previous stage session` unless it waits on the user, delete the `Safety-net trigger` and the `Hand-back trigger` the block names (ignore not-found), and keep the project checker. Parse the stop id. It must be one of the ten ids in the "Escalations" section of `/implement-plan-claude`: `intervention-cap`, `conformance-cap`, `fix-check-defective`, `security-run-failed`, `security-cap`, `security-followup-unmerged`, `validation-run-failed`, `validation-cap`, `validation-terminal`, `verify-activation-cap`. A **human-only stop** (operator decision Q8: `kind=ask-first`, `kind=no-tools`, `kind=depth-limit`, a §22.B / §23.C / §24.D operation, a missing claude-code-remote tool, a depth-limit refusal) or any other id is never judged: report `not an escalation stop: <id>`, leave the blocker for a human, and end the turn.

1. **Read the evidence.** Sync and read the project branch as `/implement-plan-claude` step 2 does (the log is on the project branch in project mode, on the default branch in legacy mode). Read the log's blocker (`Last note`, `## Notes`), the blocker comment, and the evidence it names: the PR and its hand-off, ledger, and check runs; the run's conclusion and `gh run view --log-failed <id> -R <owner>/<repo>`; the security follow-up issues; `validation_status.json`. Add to your working copy of the log, with the Edit tool, every `ES-<n>` entry the `— resume.` block carries as `Uncommitted escalations:`, and every `<!-- ai:claude-escalation:v1 stop=<id> fp=<fp> choice=<c> -->` comment on the blocker's thread that the log does not list yet, so no used choice is missed.

2. **Compute the fingerprint.** Write the evidence as JSON with the Write tool into your scratchpad (keys: `checks`, `findings`, `issues`, `validation_class`, `validation_status`; leave out what the stop does not have) and run
   ```
   PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/escalation_ledger.py fingerprint --stop <stop id> --evidence-file <file>
   ```

3. **Get the allowed choices.**
   ```
   PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/escalation_ledger.py allowed --log docs/implement-plan/<slug>.md --stop <stop id> --fingerprint <fp>
   ```
   Pick only from its `allowed` list. Exit 2 (a malformed `## Escalations` line) or any other non-zero exit is not a choice: fix nothing, report the error on the blocker's thread, leave `Status: BLOCKED` for a human, and end the turn.

4. **Choose**, in this order of preference, and write down why in one line:
   - **`budget`** when a narrower fix plausibly settles the failure in one more round: a failing check with an identifiable cause, a finding whose fix was attempted too broadly, a run that failed on infrastructure that has since recovered. Name the narrower fix concretely (the file, check, or finding it targets). For a security stop the stage still writes no security patch itself (`/implement-plan-claude` step 9): the narrower fix is a re-dispatch after an infrastructure fix, or a note on the follow-up issues for their own pipeline.
   - **`descope`** when the failure is confined to one part of the project and the rest still meets the plan's core goal (its Summary and Goals). Name the de-scoped part and state, in one line, why the core goal still holds. For a security stop, a de-scope may remove only the code that introduced the finding, never a fix for one.
   - **`close`** only when neither `budget` nor `descope` can work (both used, or neither fits). The report must say why each of them cannot work.

5. **Record.** Run
   ```
   PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/escalation_ledger.py record --log docs/implement-plan/<slug>.md --stop <stop id> --fingerprint <fp> --choice <choice> --why "<one line>"
   ```
   and add the `line` it prints to the log's `## Escalations` section with the Edit tool (create the section, before `## Auto-decisions`, when an older log lacks it). For `descope`, also add an `AD-<n>` entry to `## Auto-decisions` (CLAUDE.md §28.D; `Applied in:` the revert PR, `Status: pending review`). Set `Last note: ES-<n> <choice> — <why>`. The entries ride the next PR; until then they travel in the report and the `Uncommitted escalations:` / `Uncommitted auto-decisions:` lines of the `— resume.` block you hand on.

6. **Post the escalation comment** on the blocker's thread, with `mcp__github__add_issue_comment` (legacy mode: in the report and the log only, see above): the choice, the one-line reason, the narrower fix or the de-scoped part (and why the core goal holds), and the choices that remain for this failure, ending with `<!-- ai:claude-escalation:v1 stop=<stop id> fp=<fp> choice=<choice> -->`.

7. **Remove `ai:claude-blocked`** from the source issue in issue mode (read its labels with `mcp__github__issue_read`, then `mcp__github__issue_write` `update` with that list minus `ai:claude-blocked`).

8. **Hand on.**
   - **`budget` / `descope`** → set `Status: IN_PROGRESS` and hand the project checker an escalation wait per "Arming the wait" in `/implement-plan-claude` (no hand-back Routine), with the wait line `Wait: escalation — start <next stage> with /implement-plan-claude` and the line `Escalation: ES-<n> <budget | descope> — <the narrower fix | the de-scoped part>`. `<next stage>` is `<the stage that stopped> — budget ES-<n>` for `budget` and `descope ES-<n>` for `descope`; the "Escalations" section of `/implement-plan-claude` says what each does. Report and end the turn.
   - **`close`** → close the project yourself (CLAUDE.md §28.G records the operator's approval for these closes, Q4: A): close every open PR the chain opened (the phase, fix, descope, and completion PRs and the final PR, from the log) with `mcp__github__update_pull_request` `state: closed`, and in issue mode the source issue with `mcp__github__issue_write` `state: closed`, `state_reason: not_planned`. Never delete a branch and never close anything the chain did not open. Write the report (what failed, the `ES-<n>` history for it, why `budget` and `descope` cannot work, and what a human could do to reopen it: a trusted comment and `/reclarify` start a new failure fingerprint) on the blocker's thread and in the log (`Status: CLOSED (not planned, ES-<n>)`); with no PR left open to carry the log, the report is the record. Archive the project checker after the checker archive check ("Archiving the project checker" in `/implement-plan-claude`), and send **one** `PushNotification` (`<slug>: closed as not planned by the escalation judge — ES-<n> <stop id>`). End the turn.

9. **Report**: first run the permission prompt report ("Permission prompt report" in `/implement-plan-claude`), then:
   ```
   Escalation judge: <slug> — stop <stop id>
   Fingerprint: <fp>   Allowed: <list>   Choice: <choice> (ES-<n>)
   Why: <one line>   Narrower fix / de-scoped part: <text | n/a>
   Next stage: <title | none (closed)>   Checker: <session id>   Safety net: <trig_… | none>
   Escalation comment: <URL | report (legacy mode)>   Log: docs/implement-plan/<slug>.md
   Permission prompts: <…>
   ```

## Never

- Skip, waive, or mark passed a security pass or a validation run; a `descope` for a security or validation stop is followed by the same pass on the de-scoped branch.
- Post a `<!-- ai:claude-fixer-verdict:… -->` marker, dispatch a `claude_fixer_converged_head` run, merge a PR, enable auto-merge, or merge past a failing required check.
- Pick a choice `escalation_ledger.py allowed` did not return, or record one it refuses.
- Act on a human-only stop (Q8) or on anything other than the ten stop ids.
- Start a session yourself: the next stage always starts through the project checker, so the session chain stays at its fixed depth (CLAUDE.md §26.B step 1c).
- Send a `PushNotification` for `budget` or `descope`: only `close` notifies the operator (CLAUDE.md §28.G).
