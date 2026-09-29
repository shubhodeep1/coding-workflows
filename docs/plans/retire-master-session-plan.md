# Retire the master session: projects resolve their own escalations, resumes and guard syncs

## Summary

Every `/implement-plan-claude` and `/implement-issue-claude` project, in coding-workflows and in consumer repos, must run end to end with no interactive "master" session and no "master poller" session. This plan closes the gaps the existing fix issues leave:
- an escalation judge that settles §28.C failure escalations;
- a pickup sweep that resumes blocked projects once a human answers;
- a classifier that tells tightening guard changes from loosening ones for the #4785 sync;
- an alert policy, then retiring the poller and the master handbook.

## Context

On 2026-09-29 the operator set the goal of a completely unattended Claude coding workflow. They added that "the master session like this won't be available for future projects, so they should be able to resolve queries and problems on their own." Today two long-lived sessions fill that role:

- **The master session** (`docs/operations/master-session.md`) does a number of jobs by hand:
  - held merges (Q46);
  - `.claude/**` twin syncs (Q40) and hook or `settings.json` approval windows (Q62/Q64);
  - stacked-validation skips (Q17);
  - duplicate closes (Q58);
  - dead-checker restarts (Q63);
  - resuming stage sessions after an answer;
  - every §28.C failure escalation.
- **The master poller** (`session_012fZCZUmcWNdWwcdSsuDAGu`) polls every 15 minutes. It lists sessions that are blocked or waiting on a prompt, classifies them, restarts dead checkers, alerts the operator, and hands the rest to the master.

Most of these duties already have fix issues in flight. The "Retiring the master" table in `docs/operations/master-session.md` maps them:

| Issue | What it takes over |
|---|---|
| #4648 | GPT judge (held merges) |
| #4785 | Actions twin sync |
| #4734 | Stacked validation |
| #4867 | Duplicate closes |
| #4886, #4887 | Session titles and archiving |
| #4910 | Dead checkers, as a pickup step |
| #4911 | In-session questions |
| #4938 | Sessions without a repository checkout |
| #5018 | Classifier refusing answered resumes |
| #4990 | Pickup throughput |
| #5068 and related | Permission prompts |
| #5119 | Flaky CI |

This plan covers only what none of them does.

Facts this plan builds on (all from `origin/main` at `bb80990` unless noted):

- **§28.C escalation stops.** `.claude/commands/implement-plan-claude.md` stops at `Status: BLOCKED` for every failure escalation (CLAUDE.md §28.C, lines 2237-2294):
  - blocked-PR interventions: cap 3 per PR, line 55;
  - conformance: cap 3 runs, lines 66-72;
  - security: a non-success run, a cap of 5 cycles, a follow-up closed or blocked, lines 73-76;
  - validation: a pre-validation failure, a cap of 3 cycles, terminal classes, lines 78-82;
  - verify-activation: a cap of 3 cycles, lines 89-92.

  In issue mode each stop posts one `<!-- ai:claude-blocked:v1 -->` comment, adds the `ai:claude-blocked` label, sends one `PushNotification`, and ends (lines 98-118). A human answers and comments `/reclarify`.
- **`/reclarify` resume path.** `clarify.yml:21` accepts only a `User` comment from an owner, member or collaborator. The route (`scripts/claude_issue_route.py`) and the handoff (`scripts/claude_issue_handoff.sh`) lead to `claude-issue-intake.yml`, then a queue item, then the pickup. From there, `/implement-issue-claude` step 4 finds the in-flight project and resumes from its log, and step 2 removes `ai:claude-blocked`.
- **Pickup.** `.claude/commands/claude-issue-pickup.md` wakes hourly and never reads target issues (line 73). #4910 adds step 3b (the dead-checker restart), #4887 adds 3a and #4817 adds 3.4. #4990 adds catch-up wakes and a limit of 20.
- **#4785's sync** (plan `docs/plans/issue-4785-twin-first-claude-sync-plan.md` on its project branch) treats every `.claude/hooks/**`, `.claude/settings.json` or `settings.local.json` change as guard-only. Such a change gets `ai:claude-sync-approval` and never auto-merges. It has no loosening detection (plan lines 41-47).
- **Existing judge pattern.** `security_pass_exhaustion_judge` (`scripts/orchestrate_poll_process.sh:6256-6291`, `prompts/mode-judge-security-pass-exhaustion.txt`) already applies a bounded menu (`keep_fixing` / `accept_with_followup` / `fail`) with "do not repeat what failed" and fail-closed rules. That is the same shape the operator chose for Q4.
- **Session lineage.** Stage sessions are started by the project checker, which stays at a fixed depth. Having a stage start another session directly would grow the lineage toward the 8-link limit (CLAUDE.md §26.B step 1c; `agents.md` "Interactive slash-command model selection").

## Operator decisions (2026-09-29, recorded in `docs/operations/master-session.md`)

| ID | Decision |
|---|---|
| Q1: A | Long-term fixes are filed as `ai:claude` issues for the unattended pipeline. |
| Q3: A | Once #4785 lands, its sync PR merges hook and `settings.json` changes with no human, **except guard-loosening changes**, which wait for the operator. |
| Q4: A | §28.C failure escalations go to an **escalation judge**, a fresh Opus session. It picks from a fixed menu: one more budget round with a narrower fix; de-scope the failing part as an `AD` entry; or close as not planned with a report. It never skips the security pass, never merges past a failing required check, never repeats a choice for the same failure, and notifies the operator. |
| Q5: A | The poller's checks fold into the hourly pickup and an Actions job, which act instead of handing off. The operator is alerted only for §22.B / §23.C / §24.D operations. Then the poller is retired. |
| Q6: A | The blocked stage starts the judge straight away (through its project checker, see Approach). The pickup sweep is the backstop. |
| Q7: A | The judge covers every `/implement-plan-claude` project, in issue mode and operator-started, in this repo and in consumers. |
| Q8: A | Human-only stops, never judged: §22.B / §23.C / §24.D operations, a session with no claude-code-remote tools, and a depth-limit refusal with no pickup to route through. Every other §28.C stop goes to the judge. |
| Q9: A | `settings.json` is classified deterministically from its permission-rule diff. Any `.claude/hooks/**` script edit, deletion or wiring change counts as loosening. |
| Q10: A | The classifier is its own phase of this plan. It starts only once #4785's `scripts/claude_twin_sync.py` is on the default branch. |
| Q11: A | The pickup resumes a blocked project on its own when a trusted human comment newer than the blocker exists. It wakes the recorded live session, or requeues through `claude-issue-intake.yml` `workflow_dispatch`. |
| Q12: A | Alerts: the pickup sends a PushNotification and the Actions sweep sends Telegram. They fire only for ask-first operations, a judge closing a project as not planned, and a Q8 human-only stop. |
| Q13: A | The final phase deletes the poller's triggers, archives the poller session, and rewrites `docs/operations/master-session.md` as an operator runbook. |
| Q14: A | Four phases, implemented with `/implement-plan-claude`, twin-first for `.claude/**`. |

## Goals

- G1: No §28.C failure escalation (Q8 exclusions aside) waits for a human. A judge decides within one checker cycle, about 2 minutes, of the stop. Verified by the phase-1 tests and by one real escalation after merge that ends with an `ES-<n>` entry and a resumed or closed project.
- G2: The judge never repeats a choice for the same failure fingerprint, never records a security or validation pass that did not happen, and never merges past a failing required check. Verified by tests on `escalation_ledger.py` and by command-text tests.
- G3: An `ai:claude-blocked` issue with a trusted answer newer than its blocker resumes within one pickup wake (at most about 60 minutes, or about 30 with #4990's catch-up), with no `/reclarify`. Verified by tests on `claude_blocked_sweep.py`.
- G4: Every other blocked issue is still seen:
  - an escalation-type blocker with no judge record older than 30 minutes gets a judge;
  - a human-only blocker produces exactly one operator alert.

  Verified by tests on the sweep's `decide`.
- G5: With #4785 on the default branch, a sync PR whose guard changes only tighten (added `deny` or `ask`, removed `allow`) merges with no human. Any loosening change keeps `ai:claude-sync-approval`. Verified by classifier tests and by one real tightening sync.
- G6: After phase 4, the poller session is archived, none of its triggers is enabled, and `docs/operations/master-session.md` is an operator runbook with no master duties.

## Non-goals

- The duties already owned by #4648, #4785, #4734, #4867, #4886, #4887, #4910, #4911, #4938, #5018, #4990, #5068 and related issues, #5119. This plan neither re-implements them nor changes their scope.
- Automating §22.B / §23.C / §24.D operations. They stay human (Q8).
- Changing the Codex orchestrator's own escalation handling (`orchestrate_poll_process.sh` judges). CLAUDE.md §28.F keeps those separate.
- A new long-running session. The pickup stays the only one.
- Model-based classification of hook scripts (rejected in Q9).

## Constraints

- **§1 priority.** Security first. The judge's menu can never lower a security bar: no "skip the security pass", no "mark validation passed", and no merge past a failing required check (Q4). A de-scope removes the failing code from the project, so the remaining code still gets its security pass.
- **§6 naming immutability.** No existing identifier is renamed or removed. New identifiers, each checked for clashes:
  - `escalation-judge` (command);
  - `.claude/scripts/escalation_ledger.py`;
  - `.claude/scripts/claude_blocked_sweep.py`;
  - `scripts/claude_guard_change_classifier.py`;
  - markers `<!-- ai:claude-escalation:v1 … -->` and `<!-- ai:claude-operator-paged:v1 … -->`;
  - log section `## Escalations` with `ES-<n>` ids;
  - log prefixes `CLAUDE_BLOCKED_SWEEP` and `CLAUDE_GUARD_CLASSIFY`.

  The existing `<!-- ai:claude-blocked:v1 -->` marker, the `ai:claude-blocked` label, and `Status: BLOCKED` keep their meaning. Section numbers are covered by §6: CLAUDE.md gains a new **§28.G** and changes no existing letter.
- **§10.** No MongoDB impact.
- **§14 consumers.** `.claude/commands/**`, `.claude/scripts/**` and root `CLAUDE.md` reach the 13 repos in `.github/ai/consumer_repos.json` on the next `@stable` sync. The pickup and the Actions sweep run in coding-workflows only and already serve every registered repo.
- **§15 API hygiene.** Every new read is batched and budgeted (see each phase). The sweep reuses the pickup's existing wake, and the classifier makes no API calls.
- **§18.** No manual scripts. Every new script runs from the pickup (the existing supervisor, extended) or from an existing workflow. No new supervisor, so no `docs/scripts-pending-removal.md` entry is needed: the new scripts are permanent helpers, not single-use.
- **§19.** PR bodies use `Refs #N` for any `ai:orchestrator-tracking` issue.
- **§20.** One changelog fragment per phase PR.
- **§23.** The pickup's `claude-issue-intake.yml` dispatch is documented in the pickup command, so it is a command-invoked dispatch (§23.C carve-out). Closing a project as not planned (a judge choice) closes PRs the chain opened. Closing the source issue is added to §28.G as an explicit operator-approved act (Q4: A).
- **§25/§26.** No PR-activity subscription. The judge is started through the project checker, like every other stage (§26.B lineage depth).
- **§27.** No workflow file grows past 480,000 bytes. Phase 3 adds a small step to `claude-twin-sync.yml` only.
- **§28.** The judge is an §28.C carve-out: it answers failure escalations under the operator's standing Q4 decision, recorded in §28.G. The start-up checks and the Q8 human-only stops stay in §28.C unchanged.

## Approach

**Escalation judge (phase 1).** Every §28.C failure-escalation stop in `implement-plan-claude.md` changes as follows:

1. The stage still writes the blocker (the question, options and evidence) into the progress log and posts the `ai:claude-blocked:v1` comment. In issue mode that comment goes on the source issue; otherwise it goes on the final PR.
2. The comment gains a `kind=escalation stop=<stop-id>` attribute, taken from a fixed list: `intervention-cap`, `conformance-cap`, `fix-check-defective`, `security-run-failed`, `security-cap`, `security-followup-unmerged`, `validation-run-failed`, `validation-cap`, `validation-terminal`, `verify-activation-cap`.
3. Instead of ending the turn, it hands its project checker a one-shot `escalation` wait, two minutes out. The checker starts the judge as the next stage session (`implement-plan <slug> — escalation judge`, Opus 5.5 at high effort, two-step start) with a `— resume.` block naming the stop.

The judge runs `/escalation-judge`. It reads the log, the blocker and the evidence (PR, run logs, findings), then calls `.claude/scripts/escalation_ledger.py allowed --log <log> --stop <stop-id> --fingerprint <fp>`. The script works out the failure fingerprint (stop id plus the failing check names, finding ids, or validation class, normalised) and returns the choices not yet used for it:

- **`budget`**: one more round of the capped loop with a narrower fix, and the cap goes up by one for that fingerprint only. The judge must name the narrower fix.
- **`descope`**: remove the failing part from the project in a revert PR through the normal review, then record it as `AD-<n>` (status `pending review`). This is allowed only when the remaining project still meets its plan's core goal. For security and validation stops, the next stage re-runs the pass on the de-scoped branch; the pass is never waived.
- **`close`**: close the chain's own PRs and the source issue as not planned. Write a report in the log and on the issue, archive the project checker, and send a `PushNotification`.

The judge records `ES-<n> [<stop>, <date>] fingerprint=<fp> choice=<budget|descope|close> why=<one line>` under a new `## Escalations` section of the log. It posts one `<!-- ai:claude-escalation:v1 stop=<id> fp=<fp> choice=<c> -->` comment next to the blocker, removes `ai:claude-blocked`, and hands the checker the next stage (`budget` → the capped stage again; `descope` → a revert-PR stage; `close` → none).

`allowed` never returns a used choice. When `budget` and `descope` are both used for a fingerprint, only `close` remains. A new fingerprint (a different failure) starts with the full menu. The judge never posts a verdict marker, never merges, and never dispatches a convergence run. A notification goes out for `close` only (Q12). `budget` and `descope` are recorded but not pushed.

**Blocked-issue sweep and automatic resume (phase 2).** On each wake, after step 3b (#4910) and before its report, the pickup gains **step 3c** (the letter is picked to avoid #4887's 3a and #4910's 3b):

1. **Scan (one script call).** `claude_blocked_sweep.py scan --repos-file .github/ai/consumer_repos.json` lists open `ai:claude-blocked` issues per registered repo: one REST list call per repo (14 today), `issues?labels=ai:claude-blocked&state=open&per_page=100`, with pull requests dropped. For each issue it reads the newest comments (one paginated call, `since=` the blocker's time) and emits a JSON line per issue:
   - the blocker's `created_at`, `kind`, `stop` and `ai:claude-blocked-session:v1 id`;
   - whether a trusted comment is newer than the blocker. Trusted means a `User` owner, member or collaborator, not the automation account's own blocker or escalation comments. For the operator's own login (`CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`, which all automation posts as), the comment counts only if it carries no `<!-- ai:` marker;
   - whether an `ai:claude-escalation:v1` comment exists after the blocker.
2. **Decide.** `claude_blocked_sweep.py decide` is pure (no API calls) and returns one action per issue:
   - `wake`: an answer exists and the recorded blocked session is alive. The pickup checks with `get_session`, then sends a one-shot trigger into it with the answer's comment id.
   - `requeue`: an answer exists and there is no live session. The pickup dispatches `claude-issue-intake.yml` with `repo`, `issue_number` and `trigger=manual`. That is the same bound queue path as `/reclarify`, with the same authorization.
   - `judge`: `kind=escalation`, no escalation comment, and the blocker is older than 30 minutes. The pickup hands the project checker the `escalation` wait. If the checker is dead, #4910's step 3b restarts it first, and the judge waits one wake.
   - `page`: a Q8 human-only blocker (`kind` is `ask-first`, `no-tools` or `depth-limit`) created since the previous hourly wake. The pickup sends one `PushNotification` naming the issue and the operation. Because the window matches the wake interval, each blocker is paged once without any stored state.
   - `skip`: anything else, including an answered blocker whose session is already running.

At most 10 actions run per wake. `wake` and `requeue` come first, then `judge`, then `page`, and the rest waits for the next wake. Budget per wake: 14 list calls, plus one comments call per blocked issue, plus one `get_session` per `wake` candidate, plus at most 10 writes. The Actions side pages by Telegram: `claude-issue-queue-watchdog.yml` gains a step that runs the same `scan` and `decide` read-only and sends one `tg_send_msg ERROR` per `page` item. It dedupes with a `<!-- ai:claude-operator-paged:v1 blocker=<comment id> -->` comment it posts on the issue, so a paged blocker is never paged again.

**Guard-change classifier (phase 3; starts once `scripts/claude_twin_sync.py` is on the default branch).** `scripts/claude_guard_change_classifier.py classify --base <file> --head <file> [--path …]` returns `tighten` or `loosen` with one reason line. No API calls.

- **`settings.json`:** it parses both JSON documents and compares `permissions.allow`, `permissions.deny`, `permissions.ask`, `hooks`, and every other top-level key.
  - `tighten` needs **all** of these: every change is an added `deny`/`ask` entry or a removed `allow` entry; `hooks` is byte-identical; no other key changed.
  - Anything else is `loosen`, including a parse failure.
- **`.claude/hooks/**`** (edit, add, delete, rename) and **`settings.local.json`**: always `loosen` (Q9: A).

`claude_twin_sync.py` calls the classifier for each guard path in a sync. When every guard change is `tighten`, the PR follows the non-guard auto-merge rule (every check green, `lint` present, `--match-head-commit`). Otherwise it keeps `ai:claude-sync-approval` and the owner-approval status, as #4785 ships. Every decision is logged as `CLAUDE_GUARD_CLASSIFY path=<p> verdict=<tighten|loosen> reason=<r>`.

**Alerts, retiring the poller, runbook (phase 4).**
- Document the alert policy (Q12) in CLAUDE.md §28.G and §26, `agents.md` and the README.
- **Retirement.** The phase's stage session first confirms that phase 1 (`.claude/commands/escalation-judge.md`) and phase 2 (step 3c in `claude-issue-pickup.md`) are on the default branch. If not, it records `Poller retirement: waiting on phase <n>` and does docs only. Otherwise it:
  - lists triggers and deletes every enabled trigger bound to the poller session or named `Master poller…` / `Poll hand-off…`, checking each against its `persistent_session_id`;
  - archives the poller session after a `get_session` title check (`Master poller`);
  - records both in the log.
- **Runbook.** It rewrites `docs/operations/master-session.md` as `docs/operations/operator-runbook.md` (new) and leaves a one-paragraph stub at the old path that points to it. The old path is referenced in CLAUDE.md, `agents.md` and #5018's plan, so it stays under §6. The runbook covers what a human does when paged (ask-first operations, a Q8 stop, a `close` report, a guard-loosening sync PR) and the standing decisions that still apply. The master duties table is removed, and each duty links its fix issue as done.

## Phases & Merge Strategy

Each phase is its own PR into the project branch `claude/implement-plan-retire-master-session`. The final PR merges only after every phase, conformance, the security pass and validation. Phases have no ordering between them:

1. **Escalation judge.** Adds `.claude/commands/escalation-judge.md` (twin first), `.claude/scripts/escalation_ledger.py` (twin first), the §28.C stop rewiring in `implement-plan-claude.md` (twin first), CLAUDE.md §28.G, `agents.md`, tests, and a changelog fragment.
   - **Done:** every §28.C escalation stop in the command hands off to the judge; `escalation_ledger.py` passes its tests; the template-parity tests pass after the twin sync. The system is complete without phases 2–4 (the stage starts the judge itself).
   - **Rollback:** revert the PR. Stops go back to `BLOCKED` and wait for a human.
2. **Blocked-issue sweep and automatic resume.** Adds `.claude/scripts/claude_blocked_sweep.py` (twin first), pickup step 3c, the page step in `claude-issue-queue-watchdog.yml` plus `scripts/claude_issue_queue_watchdog.sh`, the new `kind=` attribute values listed for the Q8 stops, tests, and a fragment.
   - **Done:** a synthetic answered blocker produces `wake` or `requeue`; a human-only blocker produces one `page`; with phase 1 absent, `judge` actions degrade to `page`. That makes it independent: `decide` checks whether `escalation-judge.md` exists.
   - **Rollback:** revert. The pickup stops sweeping and `/reclarify` still works.
3. **Guard-change classifier.** Adds `scripts/claude_guard_change_classifier.py` and its call from `scripts/claude_twin_sync.py`, tests, and a fragment.
   - **Precondition:** `scripts/claude_twin_sync.py` is on the default branch (Q10). Until then the stage records `Phase 3: waiting on #4785` and the checker re-checks hourly. The project's other phases don't wait on it.
   - **Done:** a tightening-only sync auto-merges; any loosening change keeps the approval label.
   - **Rollback:** revert. Every guard change waits for the owner again, as #4785 ships.
4. **Alert policy, poller retirement, operator runbook.** Covers the docs in CLAUDE.md §26, §28.G, `agents.md` and the README; `docs/operations/operator-runbook.md` (new) plus the stub; the retirement steps; and a fragment.
   - **Done:** the docs are merged. The poller is archived once phases 1 and 2 are on the default branch, or `waiting` is recorded.
   - **Rollback:** revert the docs. Archiving can be undone with claude-code-remote `unarchive_session`, and the deleted poller triggers are re-created from `docs/operations/operator-runbook.md` "Restarting the poller", which the phase writes.

## Implementation Steps

**Phase 1: escalation judge**
1. Write `workflow-templates/.claude/scripts/escalation_ledger.py` [new]. Its subcommands:
   - `fingerprint --stop <id> --evidence <json>`: normalises and sorts failing check names, finding ids, and validation class and status, then returns 12 hex characters of SHA-1.
   - `allowed --log <path> --stop <id> --fingerprint <fp>`: parses `## Escalations` and returns the unused choices, in the order `budget`, `descope`, `close`. `close` is always available.
   - `record --log <path> …`: prints the `ES-<n>` line to append, with `n` = the highest existing plus one. It never writes files itself; the session edits the log with the Edit tool.

   It exits 2 on a malformed log line. Allowlist it in `workflow-templates/.claude/settings.json` in both plain and `PYTHONDONTWRITEBYTECODE=1` forms. Under Q9 any added `allow` counts as loosening, so the phase's `settings.json` twin sync goes through the owner: an approval window now, or `ai:claude-sync-approval` once #4785 is live. That is expected, and the phase's twin-sync blocker names it.
2. Write `workflow-templates/.claude/commands/escalation-judge.md` [new]:
   - **Inputs:** a `— resume.` block with `Stop:`, `Blocker comment:`, `Log:`, `Checker session:`.
   - **Steps:**
     1. read the evidence;
     2. compute the fingerprint;
     3. get `allowed`;
     4. choose, stating a narrower fix for `budget` or the de-scoped part plus a check that the core goal still holds for `descope`;
     5. record `ES-<n>` and, for `descope`, the `AD-<n>` too;
     6. post the escalation comment;
     7. remove `ai:claude-blocked` (issue mode);
     8. hand the checker the next stage, or for `close`, close the chain's PRs and the issue as not planned, write the report, archive the checker (§26.D title check), and send one `PushNotification`.
   - **Never:** skip or waive a security or validation pass; post verdict markers; merge; dispatch convergence; repeat a used choice; act on a Q8 stop.
3. `workflow-templates/.claude/commands/implement-plan-claude.md`:
   - At each §28.C failure-escalation stop (the ten listed in Approach, at lines 55, 61, 66, 68-69, 73-76, 78-82 and 89-92 on `bb80990`), keep the blocker write, add the `kind=escalation stop=<id>` attribute, and replace "end the turn and wait for a human" with the `escalation` wait handed to the checker.
   - Add an `escalation` wait type to the checker instructions (Check-in Loop): start the judge stage at once, with no PR state to poll.
   - Add `## Escalations` to the log template.
   - Update the "Never auto-decided" recap (line 283) and the Issue Mode bullet (line 117).
4. `CLAUDE.md` gets a new **§28.G Escalation judge** (after §28.F): the menu, the invariants, the fingerprint rule, Q8's human-only list, the operator approval of `close` (Q4: A), and notifications. In §28.C "Failure escalations", add one sentence pointing to §28.G. No existing text is deleted.
5. `agents.md`, in "Interactive slash-command model selection" and "Interactive post-push PR status check-in": one paragraph on the judge stage and the `escalation` wait.
6. Tests [new]: `tests/test_escalation_ledger.py` (fingerprint stability; `allowed` never returns a used choice; `close` is always available; a malformed log exits 2) and `tests/test_escalation_judge_command.py` (command text: invariants present in both copies; every stop id listed in `implement-plan-claude.md` maps to a stop the judge knows). Add each to `ci.yml` as its own step. Extend `tests/test_claude_md_section_numbers.py` for §28.G.
7. Add `changelog.d/<issue>-escalation-judge.md`.
8. The twin-sync stop follows the #4948 automatic twin-first default (or #4785's sync if it is on `main` by then).

**Phase 2: blocked-issue sweep**
1. Write `workflow-templates/.claude/scripts/claude_blocked_sweep.py` [new], with `scan` (REST reads, budget as above, fails open per repo with `errors[]`) and `decide` (pure). Its docstring states the §15 contract: the input shape, the output shape, the number of calls, and fail-open behaviour. Allowlist it in `workflow-templates/.claude/settings.json`.
2. `workflow-templates/.claude/commands/claude-issue-pickup.md` step 3c: run `scan`, then `decide`, then apply the actions in priority order (at most 10). `requeue` uses `.claude/scripts/dispatch_workflow.py --workflow claude-issue-intake.yml`; add `claude-issue-intake.yml` to `DISPATCHABLE_WORKFLOWS` and to the matching `gh workflow run` allow rule, both in the twin. Add the per-wake counts to the report line (`blocked=<n> woke=<n> requeued=<n> judged=<n> paged=<n>`).
3. `implement-plan-claude.md` and `claude-issue-dispatch.md` twins: the Q8 human-only stops write `kind=ask-first`, `kind=no-tools` or `kind=depth-limit` on their blocker marker. Every blocker writes `<!-- ai:claude-blocked-session:v1 id=<session id> -->` (seen already on #5018 and #4910; this makes it a rule).
4. `scripts/claude_issue_queue_watchdog.sh` and `.github/workflows/claude-issue-queue-watchdog.yml` gain a page step: run `claude_blocked_sweep.py scan|decide` read-only from the checked-out `.claude/scripts/`, then `tg_send_msg ERROR` and the `ai:claude-operator-paged:v1` dedupe comment per unpaged `page` item, using `GH_PAT` for cross-repo reads (§14). It fails open with `::warning::` and logs under `CLAUDE_BLOCKED_SWEEP`.
5. Tests [new] `tests/test_claude_blocked_sweep.py`, covering:
   - trusted-comment rules, including the automation account's marker comments not counting as answers;
   - each action and the priority order;
   - `judge` degrading to `page` without `escalation-judge.md`;
   - the page window;
   - fail-open behaviour.

   Extend `tests/test_dispatch_workflow.py` for the new dispatchable workflow and `tests/test_claude_issue_queue_watchdog*.py` (or a new test) for the page step. Add `ci.yml` steps.
6. `agents.md` item 15 (Claude issue implementer) and README "Claude issue implementer": document step 3c and the automatic resume.
7. Changelog fragment.

**Phase 3: guard-change classifier**
1. Precondition check. If `scripts/claude_twin_sync.py` is absent on the default branch, record the wait and stop the phase (the checker re-checks it).
2. Write `scripts/claude_guard_change_classifier.py` [new] as described in Approach, with no API calls.
3. `scripts/claude_twin_sync.py`: call the classifier for each guard path. Only when all guard paths classify `tighten` does it use the non-guard merge path. Log `CLAUDE_GUARD_CLASSIFY`.
4. Tests [new] `tests/test_claude_guard_change_classifier.py`, covering:
   - an added deny or ask → tighten;
   - a removed allow → tighten;
   - an added allow, a removed deny, a changed `hooks` block, another key changed, any hook script change, `settings.local.json`, or invalid JSON → loosen;
   - a mixed tighten plus loosen change → loosen.

   Extend #4785's sync tests for the merge path. Add a `ci.yml` step.
5. Update `agents.md` and the #4785 docs section: the Q3 rule.
6. Changelog fragment.

**Phase 4: alerts, poller retirement, runbook**
1. CLAUDE.md §28.G alert list (Q12) and a one-line §26 note that no master or poller hand-off exists.
2. `docs/operations/operator-runbook.md` [new] and a stub at `docs/operations/master-session.md`. Update references in `agents.md` and the README.
3. The retirement steps as in Approach, carried out by the phase's stage session with claude-code-remote tools, and recorded in the log. A `lineage depth` refusal or a classifier denial is recorded, and the phase finishes with docs only, plus one `PushNotification` asking the operator to archive the poller by hand.
4. Tests: extend `tests/test_implement_plan_claude_command.py` (or add a test) to pin the runbook's existence and the stub's pointer.
5. Changelog fragment.

## Files & Modules

- `workflow-templates/.claude/commands/escalation-judge.md` [new] and `.claude/commands/escalation-judge.md` [new, twin sync]
- `workflow-templates/.claude/scripts/escalation_ledger.py` [new] and `.claude/scripts/escalation_ledger.py` [new, twin sync]
- `workflow-templates/.claude/scripts/claude_blocked_sweep.py` [new] and `.claude/scripts/claude_blocked_sweep.py` [new, twin sync]
- `workflow-templates/.claude/commands/implement-plan-claude.md` and `.claude/commands/implement-plan-claude.md`
- `workflow-templates/.claude/commands/claude-issue-pickup.md`, if the twin exists; otherwise the `.claude/` file is listed as a no-twin diff in the twin-sync blocker, per the #4948 default
- `workflow-templates/.claude/commands/claude-issue-dispatch.md` and `.claude/commands/claude-issue-dispatch.md`
- `workflow-templates/.claude/settings.json` and `.claude/settings.json` (new allow rules; the twin sync goes through the owner, Q9)
- `.claude/scripts/dispatch_workflow.py` and its twin (`DISPATCHABLE_WORKFLOWS`)
- `scripts/claude_guard_change_classifier.py` [new]
- `scripts/claude_twin_sync.py` (after #4785)
- `scripts/claude_issue_queue_watchdog.sh`, `.github/workflows/claude-issue-queue-watchdog.yml`
- `.github/workflows/ci.yml` (new test steps)
- `CLAUDE.md` (§28.C pointer, §28.G new, §26 note)
- `agents.md`, `README.md`
- `docs/operations/operator-runbook.md` [new], `docs/operations/master-session.md` (stub)
- `tests/test_escalation_ledger.py` [new], `tests/test_escalation_judge_command.py` [new], `tests/test_claude_blocked_sweep.py` [new], `tests/test_claude_guard_change_classifier.py` [new], `tests/test_dispatch_workflow.py`, `tests/test_claude_md_section_numbers.py`, `tests/test_implement_plan_claude_command.py`
- `changelog.d/<issue>-*.md` (one per phase)

## Data Model / Index Changes

None.

## Tests

- **Unit:** the four new test files above, with every decision table fully enumerated. These are pure functions with no network access; the sweep's `scan` is tested with a fake `gh` on `PATH`, following the pattern in `tests/test_claude_pr_sweep.py`.
- **Contract:** command-text tests pin the judge's invariants and the stop-id list in both copies. The template-parity tests must pass after each twin sync. `tests/test_claude_md_section_numbers.py` checks §28.G.
- **CI:** each new test file gets its own `ci.yml` step.
- **End to end (after merge, observed rather than scripted):** one real escalation produces `ES-1` and a resumed or closed project. One answered blocker resumes without `/reclarify`. One tightening-only sync auto-merges once #4785 is live. The project's runtime validation (`validate.yml`) covers the scripts via their CLIs.

## Risks & Mitigations

- **The judge picks `close` too readily.** Mitigation: `close` needs the report to name why `budget` and `descope` can't work. Close reports notify the operator, whose `/reclarify` reopens the project under a new fingerprint.
- **A de-scope removes a security fix.** Mitigation: for security stops, `descope` may only remove the code that introduced the finding, never a fix for one. The next stage re-runs the security pass on the de-scoped branch.
- **The classifier refuses a resume after the judge answers (the #5018 pattern).** Mitigation: the judge's answer is a documented §28.G decision carried by the chain itself, not an outside "answer", and #5018 makes standing decisions visible. ACCEPTED — pending the first real escalation after merge. If it recurs, it is filed under #5018 with the transcript.
- **The automation account's own comments look like human answers** (every automation comment posts as `shubhodeep1`). Mitigation: comments carrying any `<!-- ai:` marker never count as answers, and tests cover it.
- **Pickup step collisions with #4887, #4910 and #4817.** Mitigation: step 3c is lettered to avoid them, and the phase's sync-with-main stage merges the default branch first.
- **Phase 3 depends on an outside issue (#4785).** ACCEPTED: its precondition wait keeps the other phases independent (Q10).
- **Retiring the poller loses coverage if phase 2 misbehaves.** Mitigation: retirement waits for phases 1 and 2 on the default branch, and the runbook documents how to restart the poller.
- **More API use.** About 14 list calls plus the blocked count per hourly wake, at 5,000 an hour shared. ACCEPTED: under 1% of the budget.

## Rollout

- There is no feature flag. Each phase is a normal `/implement-plan-claude` phase PR into the project branch, reviewed under Claude-fixer mode. The final PR merges into the default branch after conformance, the security pass and validation.
- **Consumer repos** get the command, script and CLAUDE.md changes on the next `@stable` release through `update_workflows.yml`. The pickup and the watchdog run in coding-workflows only and cover every registered repo from the moment they merge.
- **Rollback** is per phase, by reverting its PR (see each phase).
- **Order in practice:** phases 1 and 2 first. Phase 3 when #4785 lands. Phase 4 last in practice, though its retirement steps guard themselves.

## Decisions

### D1 — Escalation judge instead of a human for §28.C failures
- Chosen: a fresh Opus judge session with a fixed menu (`budget`, `descope`, `close`), never repeating a choice per fingerprint (Q4: A, Q7: A).
- Alternatives considered: keep stopping for a human; always de-scope; a Codex judge in Actions like `security_pass_exhaustion_judge`.
- Why: no master will exist. A Claude session has the project context and tools, and the fingerprint ledger bounds the loop.

### D2 — The judge starts through the project checker
- Chosen: the blocked stage hands its checker an `escalation` wait, and the checker starts the judge like any stage (Q6: A).
- Alternatives considered: the stage calls `create_session` itself; the pickup starts every judge.
- Why: stages already start through the checker, so lineage depth stays fixed (§26.B step 1c), and the judge still starts within one cycle.

### D3 — Automatic resume from the pickup
- Chosen: pickup step 3c, a script-decided sweep that wakes the recorded session or requeues through the intake (Q11: A).
- Alternatives considered: `/reclarify` only; a new Actions producer with its own queue payload.
- Why: it reuses the bound queue path and the one long-lived session, and a GITHUB_TOKEN comment cannot fire `/reclarify` (`clarify.yml:21`).

### D4 — Deterministic guard classification
- Chosen: `settings.json` rule-diff classification, with every hook script change counted as loosening (Q9: A).
- Alternatives considered: model classification of hooks; keep every guard change waiting for the owner.
- Why: a hook can emit `allow` decisions, so a diff cannot prove a hook edit safe. The deterministic rule fails safe.

### D5 — Retire the poller only after its replacements are on the default branch
- Chosen: phase 4 checks for phases 1 and 2 before deleting the poller's triggers and archiving it (Q13: A).
- Alternatives considered: the operator retires it by hand; retire it immediately.
- Why: it keeps phase 4 independently mergeable and never leaves blocked projects unwatched.

## References

- Operator decisions Q1–Q14 (this session, 2026-09-29); `docs/operations/master-session.md` ("Retiring the master", standing decisions Q1–Q5)
- CLAUDE.md §18, §23, §25, §26, §28
- `.claude/commands/implement-plan-claude.md`, `.claude/commands/claude-issue-pickup.md`, `.claude/commands/implement-issue-claude.md`
- #4785 and `docs/plans/issue-4785-twin-first-claude-sync-plan.md` (on its project branch)
- #4910 and `docs/plans/issue-4910-restart-dead-checkers-plan.md` (on its phase branch)
- #4648, #4734, #4867, #4886, #4887, #4911, #4938, #4990, #5018, #5068, #5119
- `scripts/orchestrate_poll_process.sh` `security_pass_exhaustion_judge`, `prompts/mode-judge-security-pass-exhaustion.txt`
