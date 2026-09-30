# Master session handbook

The **master session** is the interactive Claude Code session that supervises the Claude automation in `shubhodeep1/coding-workflows`. This file is its handover: a fresh master session reads it in full and takes over from it. It records the operator's standing decisions, the duties and how to do them, the IDs the automation runs on, and the gotchas learned doing this job.

The master runs in **Auto mode**. The operator's goal is unattended running: a human is involved only when Claude is really stuck.

**The master is temporary (operator, 2026-09-29).** Future projects, in this repo and in consumer repos, will run without a master session, so every project must resolve its own questions and problems. Every master duty in this file is interim: each one has, or gets, a fix issue that moves it into the automation (see "Retiring the master" below). When you do something by hand that a future project could not do for itself, that is a gap: file its fix issue the same turn.

## Ownership

- **Ours:** every `claude/*` PR, every issue labelled `ai:claude`, and every session this account's automation starts.
- **The other account's (leave alone):**
  - PRs: #4607, #4604, #4590, #4546, #4543, #4142;
  - issues: #4139, #4605, #4545, #4542, #4664, #4487, #4091, #4090;
  - projects: #3965 and `heal-deterministic-autofix-failures`.
- **The GPT conflict resolver stays on** (Q57: A). The other account's non-Claude PRs need it. It never runs on `claude/*` heads (Claude-fixer mode).

## Standing operator decisions

These are in force until the operator changes them. Cite them by Q-number when acting.

| Decision | What it allows or requires |
|---|---|
| **Fix rule** (2026-09-29) | For every item that disturbed or could disturb unattended running, ship or link a **long-term fix issue**, not just a manual unblock, and list the fix issue number next to each item in every report. The poller repeats this rule at the end of each hand-off. |
| **Q38: A** | The poller alerts the operator directly (PushNotification), never pauses, and hands off to the master only while the master is alive. |
| **Q40: A** (interim twin-first) | A stage that must change `.claude/**` edits only the `workflow-templates/.claude/**` twins, posts a `hold` claim, and stops `BLOCKED`. The master reviews the twin diff, copies it into `.claude/` as a `[claude-twin-sync]` commit on the phase branch, runs the tests, pushes, and comments `/reclarify`. When a stage asks "how should phase N run?" for a protected-path phase, the master answers with Q40 on the issue: a `Protected-path approval: phase N — twin-first per Q40 (<date>)` line, then `/reclarify`. This lasts until #4785 (Actions sync PR) lands. |
| **Q46: A** (standing merge approval) | The master may merge a **held** `claude/*` PR, into `main` or into a project branch, when all of these hold: every finding was rejected or there were 0 findings; the master checked each rejection against the code; tests pass (CI, or local runs where CI was cancelled or slow); the head hasn't moved; `mergeable_state` is `clean`; and the diff has no `.claude/hooks/**` or `settings.json` change. Post the evidence as a PR comment first, then merge with `merge_method: merge` and `expectedHeadSha`. Lasts until the GPT judge is on `main`. |
| **Q58: A** | The master closes **pipeline-filed** `ai:permission-prompt` duplicates. Pipeline-filed means the body carries `<!-- ai:permission-prompt:v1 sig=… -->`. Close with `state_reason: duplicate` and `duplicate_of`, after a comment naming the target and why. Closing the issue also stops its queued pickup item, because `claude_issue_route.py` refuses closed issues (`issue_closed`). #4867 makes this a documented session rule. |
| **Q60: A** | Session titles start with the numbers: `#<issue> · PR #<pr> — <old title>`. Leave `PR #<n> status check-in` titles unchanged (§26.B step 1b matches them exactly). #4886 makes the automation do this. Until then, rename new sessions by hand when convenient. |
| **Q61: A** | Archive finished sessions: fixer and hold sessions whose PR is terminal, issue-start sessions superseded by a later stage or whose issue is closed, stage sessions a `/reclarify` replaced, and report sessions after 7 days. Never archive a session waiting on a permission prompt. #4887 automates this; archive by hand until then. |
| **Q62/Q64: A** | Syncing a change to a **hook** or `settings.json` into `.claude/` needs the operator: the Auto-mode classifier denies it as "Self-Modification". Ask the operator to switch the master out of Auto mode once, run the copy (one prompt), then return to Auto. |
| **Q63: A** (dead-checker restart) | The poller restarts a project checker when all five conditions hold (below). #4910 moves the rule into the hourly pickup; after that lands, remove it from the poller. |
| **Q66: A** | #4695 composes the #4687 and #4688 rejection gates, with the `RF-` id as the only vote token. |
| **Q67: A** | Batch hook and `settings.json` syncs into one approval window (#4817, #4858, and #4891 when ready). |
| **Fix route, Q1: A** (2026-09-29) | A long-term fix is filed as an `ai:claude` issue (root cause, evidence, proposed fix) for the unattended pipeline to build; the master supervises it to merge and does not write it itself. |
| **Q2: A** (2026-09-29) | Q46 holds for each new master session. The operator confirmed it for `session_01Qt5nTTqhWxcYA4NciTC6DL`. A new master should confirm it once with the operator: the Auto-mode classifier cannot see a standing approval written in this file and denied a follow-up to a Q46 merge as "Merge Without Review" until the operator confirmed it in the session. |
| **Q3: A** (hook and `settings.json` changes, 2026-09-29) | Once #4785 lands, its Actions sync PR merges `.claude/hooks/**` and `settings.json` changes after the full review, security and validation passes, with no human. A change that **loosens** a guard (removes a deny, widens an allow rule, deletes a hook) still waits for the operator. |
| **Q4: A** (failure escalations, 2026-09-29) | On a §28.C failure escalation (a cap reached, a failed security or validation run), an escalation judge (a fresh Opus session) reads the evidence and picks from a fixed menu: one more budget round with a narrower fix, de-scope the failing part as an auto-decision, or close as not planned with a report. It never skips the security pass, never merges past a failing required check, never repeats a choice for the same failure, and notifies the operator of its decision. To be built by the "Retire the master" plan. |
| **Q5: A** (the poller, 2026-09-29) | The poller's checks (blocked-issue sweep, dead-checker restarts, waking sessions after an answer) fold into the hourly Claude issue pickup and an Actions job, which act on what they find. The operator is alerted only for §22.B / §23.C / §24.D operations. Then the poller session is retired. To be built by the "Retire the master" plan. |
| **Q17: A** (stacked-project validation skip, 2026-09-29; **superseded 2026-09-30**) | #4734 reached `main` at 2026-09-30T02:46Z (PR #4746): `validate.yml` now authorizes a final PR into a project branch or `stable`, so stacked and `stable` projects dispatch validation normally and Q17 no longer applies. Former rule: When a project's runtime validation cannot be dispatched only because its final PR targets **another project's branch** (`validate.yml` answers `Explicit validation target is not authorized`), the master answers the §28.C blocker with A: record `Validation: skipped (covered by #<parent>'s project validation)` and comment `/reclarify`. The parent project re-runs its security audit and runtime validation on a branch that contains the fix before anything reaches `main`. It never applies to a project whose final PR targets `main` or `stable`, or to a validation run that was dispatched and failed. Lasts until #4734 (validate stacked and `stable` targets) is on `main`. |
| **Multi-level stack validation skip** (operator, 2026-09-30, Q1/Q2: A on #5093) | #4734 authorizes only one level of stacking. When a project's runtime validation can't be dispatched only because its final PR is stacked **two or more levels** below `main` (for example #5107 → #4923 → #4709 → `main`), the master answers the §28.C blocker with A without asking: `/reclarify` first, then record `Validation: skipped (target_ref not authorizable: final PR #<n> is stacked <k> levels below main; covered by the base chain's validation)`, then continue with the completion PR. The base chain's own project validation covers the code before it reaches `main`. It never applies to a final PR into `main` or `stable`, a one-level stack (that dispatches normally since #4734), or a validation run that was dispatched and failed. Lasts until #5483 (authorize multi-level stacks in `validate.yml`) is on `main`. |
| **Q15–Q18: A** (2026-09-29) | Q15: merge #4891's PR #5033 (a hook change that only adds a `deny`) after verification. Q16: merge PR #4810 despite a stale 403 gate check (done, `401e349`). Q17b: merge #4619's PR #5004 (a hook change that only adds asks) after review (done, `964bc01`). Q18: Q17 holds for every master session. |
| **Operator delegation** (2026-09-29, 16:1xZ, for 12 hours) | While the operator is away, the master takes the RECOMMENDED option on every pending question and makes every decision itself, including held merges of hook or `settings.json` changes that only tighten a guard after the master's own adversarial review (a loosening change still waits, per Q3: A). Every issue found gets a long-term fix issue. |
| **15-minute cadence** (operator, 2026-09-30) | The master sweeps every 15 minutes (a `send_later` chain named `Master: 15-min sweep`), and the Claude issue pickup wakes every 15 minutes. Routines refuse a cron more often than hourly, so the pickup session has three extra hourly Routines, `Claude issue pickup: quarter :01/:16/:46`, next to `Claude issue pickup: hourly` (`:31`). When the pickup moves to a new session, move those three Routines with it. #5423 replaces them with a self-wake. |
| **Q1: A** (`gh api --help`, 2026-09-30) | Loosen the `gh api` guard only for a bare `gh api --help` / `-h` (no request is sent); every other unreadable call still asks. Fix filed as #5405. Broader "allow every read" (option B) was declined. |
| **Q1–Q5: A** (one-off approvals, 2026-09-30) | Auto-off window: the #5144 and #5083 hook syncs (done). Merges of `settings.json`-changing held PRs #4937 (#4887) and #4984 (#4910) (done). Update PR #4546's branch so it re-reviews and auto-merges (done). #5018 in a watched session (started). |

## Current priorities (operator, 2026-09-29; state 2026-09-30 04:45Z)

The operator wants these landed first, so the automation can be synced to consumer repos and used for projects there (Q9: A):

| Priority | What it fixes | Where it stands (2026-09-30 04:45Z) |
|---|---|---|
| **GPT judge**: convergence project `claude-fixer-unattended-convergence`, integration PR #4648 | A `claude/*` PR whose findings were all rejected converges without a verdict bot or a human. It replaces the Q46 merges and the operator's yes for hook and `settings.json` PRs. | Phase 4 PR #5215 got its round-1 twin sync `0bee67e` (03:23Z); review round 2 runs in stage `session_01M8FkonHxzc7E2GnGBFgqdS`. Project checker `session_01CUoZtWt9aXXvwPvx9QwhAx`. After phase 4: conformance, security, validation, final PR #4648. |
| **#4785**: automatic `.claude/` sync via Actions | Removes every twin-sync stop and, under Q3: A, every approval window except guard-loosening changes. | Final PR #4804 is clean and waits on security follow-ups #5246 and #5247 (both resumed 03:07Z; told at 04:02Z to dispatch validation normally, since #4734 landed). |
| **Retire the master session**: plan `docs/plans/retire-master-session-plan.md`, final PR #5132 (no source issue) | Escalation judge (phase 1), blocked-issue sweep and automatic resume (phase 2), guard-change classifier (phase 3, waits on #4785), poller retirement (phase 4). | Phase 1 PR #5164 was held 12 h on an unseen twin-sync blocker (posted on #5132, which has no issue); synced as `ba70679` at 03:29Z; review round in stage `session_01WxpmE7bZjiqP6dwYFuqiUK`. Project checker `session_01PxYwa7Rwnpb7RYbmsURfQ9`. |
| **#4948**: automatic twin-first default | Removes the "how should phase N run?" stop until #4785 lands. | Done, on `main` (`ce1db50`). |

**When #4648 and #4785 are on `main`:** release once (Q10: B). Dispatch `promote-main-to-stable.yml` on `main`. It fast-forwards `stable` to `main`, then dispatches `test-and-mark-stable.yml` on `stable`, which runs the release gate, tags `@stable`, and notifies the 13 consumer repos in `.github/ai/consumer_repos.json`.
- Never dispatch `test-and-mark-stable.yml` on `main`. Outside `gate_only` mode it fails unless it runs on `stable` (its `Validate dispatch ref` step).
- `stable` must be an ancestor of `main`. If the fast-forward is refused, fix the divergence first.
- Never dispatch while another gate, promote run or release is active: the gate's E2E job cancels concurrent siblings.
- Check first whether the daily `cycle` job of `promote-main-to-stable.yml` (00:00 UTC) has already promoted the tip.

Confirm with the operator in one line before running it (a billed dispatch reaching 13 repos, §23.C). Then confirm that both the promote run and the `test-and-mark-stable.yml` run it dispatched concluded `success`, and tell the operator the consumers are synced.

**Also pending (2026-09-30 04:45Z):**
- **#5417** (recover a chain stuck behind a prompt-blocked session) answered Q1: A: when PR #5215 merges into the convergence project branch, add `Integration branch: claude/implement-plan-claude-fixer-unattended-convergence` to #5417's body and comment `/reclarify` (or `/reclarify` alone if #4648 is already on `main`).
- **#5018** runs in the operator-watched session `session_01C59sT24KSrN1NWq6v9G8tQ` (default permission mode, started 04:38Z).
- **#4545 / PR #4546 belong to the other account.** With the operator's Q3: A the master updated #4546's branch with `main` at 04:26Z so it re-reviews and auto-merges. Do not start any session for #4545 yourself.
- **#5126** waits on recovery PR #5433 (its phase PR #5165 merged before its twin sync, the #5316 race).
- **Resumed at 04:02–04:40Z, confirm on the next sweeps:** #4619, #4909, #4723, #4665, #5016, #5068, #5144, #5083, #4887, #4910, #5127, #5259, #5376, #5377, #5124, #4867, #5258, #4786 (restarted after a prompt-stuck stage swallowed its hand-back).
- **New fix issues filed 2026-09-30:** #5405 (`gh api --help`), #5417 (prompt-stall recovery), #5423 (pickup 15-minute self-wake).
- **Pickup cost:** the pickup session `session_01ArxJDkmfCGHBaW5Ph9zoHN` was at 708k context tokens (about $98) at 04:20Z and now wakes 4 times an hour. Its command file says to restart it from a fresh app session when wakes grow expensive (`/claude-issue-pickup start — restart`), and the three quarter Routines must move with it.
- **`@stable` release** after #4648 and #4785 (above).

## Routines

### Poller hand-offs

- **Poller:** `session_012fZCZUmcWNdWwcdSsuDAGu` ("Master poller — running (direct alerts)"). It polls every 15 minutes (send_later chain) and delivers "Master poller hand-off" messages into the master session.
- **Each hand-off lists:** new blocked sessions (tagged `new-pattern` or `repeat`), dead-checker restarts, new PRs, and anything unusual.
- **For every item:** unblock it and name the long-term fix issue (fix rule). A `new-pattern` item with no fix issue gets one filed the same turn.
- **Also check every `ai:claude-blocked` issue yourself.** The poller only sees sessions asking for input. A stage that stopped `BLOCKED` in a `COMPLETED` session (for example a twin-sync request) doesn't show up. #4817's sync waited 12 hours that way.
- **Changing the poller's rules:** a one-shot `create_trigger` into the poller, two minutes out, named `Master poller: rule update (<topic>)`, stating the rule change. It replies `rule added` / `rule updated`.

### Dead-checker restart rule (Q63, as tightened 2026-09-29)

Restart a project checker only when ALL of these hold. A project checker is a non-archived session whose title contains `implement-plan <slug> — checker`, optionally after a `#<issue> · PR #<pr> — ` prefix.
1. No enabled trigger has `persistent_session_id` equal to the checker.
2. For an `issue-<N>-…` slug, issue #<N> is open and not labelled `ai:claude-blocked`.
3. The checker isn't `need_input` or waiting on a permission prompt.
4. No session **belonging to the project** is active:
   - **Belongs to the project:** its `parent_session_id` is the checker, or its `current_branches` is `claude/implement-plan-<slug>` or starts with `claude/implement-plan-<slug>-`, or its title contains `implement-plan <slug>` or starts with `#<N> · `.
   - **Active:** `RUNNING`, `REQUIRES_ACTION`, `need_input`, or created or updated in the last 90 minutes.
5. The checker wasn't restarted in the last 3 hours.

**To restart:** a one-shot trigger into the checker, two minutes out, named `implement-plan <slug>: check-in`, with the prompt "Repeat the steps in your most recent checker-instructions message now, starting at step 1". When the chain's next step is known, name it in the prompt (for example "PR #N merged; start `conformance 1/3`"). A checker whose issue carries `ai:claude-blocked` waits for the operator instead. When the operator's answer lets the chain proceed, remove the label and wake the checker directly.

### Twin syncs (Q40)

1. `git fetch origin <phase branch>` and `git checkout -B <phase branch> origin/<phase branch>`. Confirm the head is the one the blocker comment names.
2. Confirm each twin's `sha256sum` matches the blocker comment. Confirm `git diff --stat origin/main...HEAD` touches only the listed twins under `.claude`.
3. Review each diff: `git diff --no-index .claude/<f> workflow-templates/.claude/<f>`. Check for GitHub writes, redaction, caps, and fail-open behaviour. Hook or `settings.json` changes need the Q62/Q64 approval window.
4. `cp` each twin, then run the suites the blocker names plus `tests/test_update_workflows_guardrails.py`, with `PYTHONDONTWRITEBYTECODE=1`. Run `ruff check` on changed Python. `ruff format --check` also flags files on `main`, and CI doesn't run it.
5. Commit as `[claude-twin-sync] Sync #<issue> phase <n> twins into .claude/`, with `Refs #<issue>` and the attribution lines. Push with `git push origin HEAD:<phase branch>`. Switch back to your own branch.
6. Post a review summary on the issue, then `/reclarify`. Archive the blocked stage session; the `/reclarify` replaces it.

### Held-PR merges (Q46)

1. Run `check_in_status.py --repo shubhodeep1/coding-workflows --pr <n> --hand-back`, and read the latest hand-off plus the ledger and rejection comments.
2. Verify each rejection against the code. A background verification agent works well for a long test suite.
3. Confirm that the head is unchanged, `mergeable_state` is `clean`, and the files exclude `.claude/hooks/**` and `settings.json`.
4. Post the evidence comment, then merge with `merge_method: merge` and `expectedHeadSha`.
5. A 405 or conflict means the base moved (another PR merged into the same project branch). Hand the conflict to a fixer (below) and don't merge.

### Unblocking a stuck project

- **Stage or issue session asking a question in-session** (nobody sees it): send it the §28 rule via a one-shot trigger. Intent questions take the RECOMMENDED option and are recorded as AD entries. §28.C items are posted on the issue with `ai:claude-blocked`. If it still won't act, archive it and move the project forward directly (wake the checker, or start a fixer). Fix issues: #4911 (Stop hook), plus its `AskUserQuestion` scope addition.
- **PR needs a real fix** (conflict, failing check, review round) with no live fixer: start a fresh fixer. Call `create_session` with `model: claude-opus-5-5`, `permission_mode: auto`, a numbered title, and the prompt `/effort high` alone. Then `create_trigger` into it two minutes out with `/fix-claude-pr <PR URL> — kind <kind> — head <sha>`, plus any operator context (for example that a hold was answered, so it posts a newer claim).
- **Your own PR:** arm the §26 check-in (stale Routine sweep, hand-back Routine, Sonnet checker at `/effort low`). On a hand-back, run `/fix-claude-pr` in the master session.

## Retiring the master

Each duty below is done by hand today and moves into the automation through the issue named. A duty with no issue is a gap: the "Retire the master session" plan (Q4: A, Q5: A) covers the gaps. Remove a row when its fix is on `main`.

| Master duty today | Automated by |
|---|---|
| Held merges with every finding rejected (Q46) | #4648 (GPT judge) |
| Twin syncs of commands and scripts (Q40) | #4785 (Actions sync PR); #4948 in the meantime |
| Hook and `settings.json` syncs (approval windows) | #4785 under Q3: A; guard-loosening changes stay with the operator |
| Closing duplicate prompt reports (Q58) | #4867 |
| Numbered titles and archiving finished sessions (Q60/Q61) | #4886, #4887 |
| Restarting dead checkers (Q63) | #4910 |
| Answering in-session questions | #4911 |
| Replacing sessions started without a repository checkout | #4938 |
| Resumes the classifier refuses after a master answer | #5018 |
| Operator-only permission prompts | #5068, #4786, #4909, #4891, #4858, #4678 |
| A chain stalls when its stage or fixer session sits on a permission prompt and swallows the hand-back | #5417 |
| Waking the pickup early / 15-minute pickup cadence (quarter Routines) | #5423 |
| Answering blockers posted on final PRs of projects with no source issue | retire-master plan phase 2 (G3b) |
| Flaky CI that holds an otherwise mergeable PR | #5119 (the stall-guard test); file one issue per flake |
| Answering validation blockers of projects stacked two or more levels below `main` | #5483 |
| Failure escalations: caps, failed security or validation runs (§28.C) | gap: escalation judge (Q4: A) |
| The poller session: blocked-issue sweeps, hand-offs, operator alerts | gap: fold into the hourly pickup and an Actions job (Q5: A) |

## IDs

| What | ID |
|---|---|
| Master session (current, since 2026-09-30 04:50Z; sweep is a `send_later` chain named `Master: 15-min sweep`) | `session_01RNCnXFXVviFXqJaopAD78D` |
| Master poller session | `session_012fZCZUmcWNdWwcdSsuDAGu` |
| Claude issue pickup session (hourly, trigger `Claude issue pickup: hourly`) | `session_01ArxJDkmfCGHBaW5Ph9zoHN` |
| Deprecated Routine the **operator** must delete by hand | `trig_01GykbNLA7znHCJGWRetq4Ye` |
| Pickup quarter Routines (bound to the pickup session) | `trig_013dcats82A3kkwS3NAgdtHJ` (:01), `trig_0129nL2exMLdVs1PpnqQM6sL` (:16), `trig_0128GDwzYzk3F4pMPopnxz7N` (:46); hourly `trig_01Nus8YLq8teoETKP2CzmjVN` (:31) |
| Previous master session (retired 2026-09-30) | `session_011d3MdW6DqBqM9SgLWwfRXQ` |
| `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` | `shubhodeep1` |
| `CLAUDE_FIXER_VERDICT_BOT_LOGIN` | unset (no verdict bot; this is why all-rejected rounds hold) |

## Long-term fix issues by failure pattern

| Pattern | Fix issue(s) |
|---|---|
| Clean review finishes before CI, so the PR holds with 0 findings | #4900 |
| A stalled reviewer slot turns a clean ledger into a hold | #4835, #4885 |
| No verdict bot, so all-rejected rounds hold | GPT judge (convergence project, PR #4648) |
| Inline `python3` heredoc file edits prompt | #4678, #4858 |
| Read-only `gh api` loops prompt; `$(gh api …)` reads prompt | #4786, #4909 |
| `gh api --jq` with jq CLI flags | #4891 |
| `.claude/` changes need a watched session | #4785 |
| Checkers die when a re-arm fails | #4910, #4750 |
| Unattended sessions ask in-session or via `AskUserQuestion` | #4911 |
| Blocked sessions replaced by `/reclarify` stay open | #4817 |
| An answer with `/reclarify` on a later line is never routed | #5243 |
| `/reclarify` on an issue its own final PR merge closed is skipped | #5222 |
| Finished sessions pile up; titles hide numbers | #4887, #4886 |
| Duplicate permission-prompt issues | #4867 |
| Parallel security follow-ups change the same code | #4934 |
| Command behind a blocking prompt goes unreported | #4755 |
| Close sweep closes issues on non-default-branch merges | #4813 |
| Merge resolver ran on draft `claude/*` PRs | fixed by #4869 (merged) |
| Session starts without `gh`, GitHub MCP tools, or a repository checkout (often after a container restart) | #4938 |
| Protected-path phases stop to ask a question Q40 already answers | #4948 |
| Long-running branches run outdated hooks (guard fixes on `main` not merged in) | #4952 |
| Review gate silently skips a PR whose body quotes the skip-AI marker; checker waits forever | #4985 |
| Pickup starts only 10 sessions per hourly wake, so resumes wait hours | fixed by #4990 (merged: limit 20, resumes first, one 30-minute catch-up wake) |
| Auto-mode classifier refuses a resume the master answered ("Auto-Mode Bypass") | #5018 |
| Stacked projects cannot run runtime validation | #4734 (merged: one level); #5483 for two or more levels (the multi-level stack rule covers it until then) |
| Checker given an empty `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` never sees review hand-offs and waits forever | #5057 |
| Hold claims always say "reached the cap of 3", even for twin-sync and held-merge holds | #5058 |
| Classifier refuses optional cleanup (`delete_trigger` "Interfere With Workloads") three times, forcing a human prompt | #5068 |
| A flaky required check (`test_codex_stall_guard_scripts.py` timing race) holds an all-rejected PR | #5119 |
| Pickup-started fixer has no repository checkout and cannot find `/fix-claude-pr` (PR #4810, 10:33Z) | #4938 |
| `gh api --help` is an unknown flag, so the guard asks | #5405 |
| A prompt-stuck session swallows its chain's hand-back; nothing restarts the stage | #5417 |
| Resumes wait up to an hour for the pickup | #5423 (interim: quarter Routines) |
| `timeout <n> python3 -m pytest` does not match the pytest allow rule; read-only `python3` heredocs still prompt after #4678/#4858 | #4678 (notes added 2026-09-30) |

## Gotchas

- **Classifier outages.** When the Auto-mode classifier has an outage, tool calls get "no verdict" or denials. Retry once, then back off. Never probe with throwaway commands, because the operator sees the prompts.
- **Large results.** `list_sessions` (limit 100) and `list_triggers` results are saved to a file. Parse them with `python3` (`data["ccr"]["data"]` for sessions). Paginate with `after_id`.
- **`run_once_at` must be in the future.** Compute it with `date -u -d '+2 minutes' +%Y-%m-%dT%H:%M:00Z` right before the call.
- **Never use `fire_trigger`** for hand-backs: it starts a fresh session with no context.
- **GitHub access through the proxy.** Search endpoints and GraphQL are blocked. Use `mcp__github__search_issues` and repo-scoped REST.
- **§21 merged-PR guard.** After your PR merges, start new work on a fresh branch from `origin/main`: `claude/magical-edison-7pkk3t-<n+1>`.
- **Protected-path writes.** Writes to `.claude/**` via `cp` usually work in Auto mode for commands and scripts. Hooks and `settings.json` are denied (Q62/Q64). A command sync that also patches a `.claude/` file with no twin can be denied as "Self-Modification" too (#5068, 2026-09-30). Once denied, don't retry it in Auto mode; ask the operator for a non-Auto window, the same as Q62/Q64.
- **Session-status fields.** A session in `REQUIRES_ACTION` with "Waiting on permission: Bash" is a permission prompt only the operator can answer in that session's view. **Always give the operator a direct link to every session waiting on a permission prompt** (`https://claude.ai/code/<session id>`), with its issue and PR numbers, whenever you report one (operator, 2026-09-29). Find them yourself instead of repeating an older list: `list_sessions` (`mine: true`, `limit: 100`, paging with `after_id` back two days), keeping non-archived sessions whose `session_status` is `SESSION_STATUS_REQUIRES_ACTION` or whose `post_turn_summary` says "Waiting on permission". Then check that each one's issue is still open. The poller follows the same rule.
- **Rate limits.** Watch the master's own `rate_limit_info`. A `seven_day` `allowed_warning` means restart the master, or trim its work, before hitting the cap.
- **Context size.** Every turn re-reads the whole conversation, poller hand-offs included. Past about 700k tokens (`get_session` → `context_usage`), hand over to a fresh master. Have the **operator** start it in the app, so it sits at depth 0; a master you create yourself lands one link deeper each time. Then send it a handover message listing the in-flight items, re-point the poller, and archive yourself.

Learned 2026-09-29:
- **Read the whole blocker before a twin sync.** Blockers can list a `.claude/` file that has **no twin** (for example `.claude/commands/claude-issue-pickup.md`), with an exact edit and the expected sha256. A truncated read misses it: #4886's `72273b8` did, and `d0520f7` fixed it. Check every listed sha256 after copying.
- **Consumer-variant commands differ on purpose.** `validate-consumer-issue`, `verify-activation`, `analyze-log`, `investigate-issue` and `deploy-activate` never match their twins, so never sync them.
- **Wake a stage session directly** when you know its id. A one-shot trigger that says "answered on the issue (comment …); continue" costs one message. `/reclarify` goes through the pickup queue, which waits for the next pickup wake (#5423) and can start a session with **no repository checkout** (#4938). Use `/reclarify` only when you don't know the session.
- **`/reclarify` must be the first text of the comment.** `clarify.yml` starts the job only when the comment body starts with `/reclarify`. An answer that puts `/reclarify` on its last line is skipped silently: no `ai:claude-issue-routed` comment, and the project stays stalled. On 2026-09-29, 13 answered projects sat unrouted for 3–14 h this way (#5243). Put `/reclarify` on the first line and the answer below it. Within 5 minutes, check that the `ai:claude-issue-routed` comment appeared. `/reclarify` on a **closed** issue is always skipped (#5222): after merging an issue-mode final PR, start the next stage yourself with the two-step `create_session` + `— resume.` trigger.
- **Pickup-started sessions sometimes have no `sources`** (`get_session` → `session_context` has no `sources`). They ask about push access or skill locations. Archive the session and start a replacement yourself with `create_session` and `source_url` (#4938).
- **A session's `post_turn_summary` is frozen at its last turn.** Before acting on a "blocked" item, read the issue's newest comment and the PR head. The poller was told the same on 2026-09-29 (rule update "verify on GitHub").
- **Q46 excludes hook changes.** A held PR whose diff touches `.claude/hooks/**` or `settings.json` needs the operator's explicit yes, even when every finding was rejected (#4870, Q16: A).
- **The classifier may refuse a resume you answered** as `[Auto-Mode Bypass]` (#4891). The operator unblocks it by typing a confirmation into that session; #5018 is the lasting fix.
- **Every wake or resume prompt you send names `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1`** (never empty) for the checker instructions it will write. A checker armed with an empty value reports `open, wait` on every review round (PR #4807 stalled 2 h that way; #5057). The same prompt should tell the session to skip, not retry, a refused `delete_trigger` or `archive_session` (#5068), and to edit files with Edit/Write, not `python3` heredocs (#4858).
- **A hold comment's text does not tell you why it holds** (#5058). Read the linked issue's newest `ai:claude-blocked` comment: most holds are Q40 twin syncs or Q46 held merges the master clears itself, not caps.
- **Verification agents for Q46 merges and twin-sync prep.** A Sonnet background agent per PR (or per 3–4 branches) gathers the six Q46 conditions or prepares twin-sync worktrees under the scratchpad (`win/<issue>`), copying command and script twins only. Review every hook and `settings.json` diff yourself. Hook twins on different branches each fork from `main`'s copy, so each copy lands on its own branch.
- **Usage limit stops (2026-09-29 ~08:00Z).** When the account hits its weekly limit, every Sonnet session (poller, pickup, checkers) and many stage sessions fail with `You've hit your weekly limit`, and their `send_later` chains die. Once the operator enables extra usage (or the limit resets), list all non-archived sessions (page with `after_id` back 8 days), select `post_turn_summary.status_detail` containing `limit`, and send each a one-shot "resume after usage limit" trigger: checkers repeat their latest checker-instructions from step 1; other sessions continue from their latest instructions. `create_trigger` is rate-limited to about 10 per minute, so send them in batches of 10, a minute apart.
- **Approval windows.** Stage every command and script copy in Auto mode first. Then ask the operator to switch the master out of Auto, run **one** command that copies only the hooks and `settings.json`, ask them to switch back, and only then test, commit and push. Checking the status script for a `claude/*` PR needs `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1`; without it, hand-offs are hidden.
- **Wake a live stage session directly after a twin sync.** A trigger bound to it replaces the `/reclarify` and keeps its context: before the sync, check with `get_session` that it isn't archived. A trigger into an archived session fails ("session not active"); for those, comment `/reclarify` on the issue instead. For a PR held by a fixer session (a `PR … — on hold` title), wake the fixer that posted the hold, not the original stage session.
- **GitHub MCP outages recover by themselves.** From 14:40Z to 15:25Z on 2026-09-29, every GitHub MCP call returned HTTP 502 "builtin injection failed (github)" after a session resume, then recovered with no action. Retry once, then work from `gh api` REST reads (they go through the git proxy's own credential) and retry the MCP tools on the next cycle; do not restart sessions over it.
- **Blockers posted on a PR are invisible to the poller.** Outside issue mode a stage posts its `ai:claude-blocked:v1` comment on the project's final PR (the convergence project's stops on PR #4648 sat unseen for hours). Read the final PRs of the priority projects on every cycle until the retire-master plan's phase 2 sweep covers them (plan amended 2026-09-29).
- **The master's own branch can't be reused after its PR squash-merges.** Resetting to `origin/main` clears the §21 guard. But pushing that reset to the old branch name is a force push, which the repository rules decline. Stacking on the old head is blocked by §21. Put the next handbook change on a new branch (ask the operator for the name) and open a new PR. The stop hook then reports `main`'s squash commit as one "unpushed" commit on the old branch; that is expected and carries no work.

Learned 2026-09-30:
- **Unanswered blockers hide by age.** Sweep every open `ai:claude-blocked` issue by comparing its newest `<!-- ai:claude-blocked:v1 -->` comment with its newest comment: any blocker with nothing after it is unhandled, whatever its age. On 2026-09-30 the sweep found 11 blockers unanswered, some since the day before: twin syncs (#5016, #5068, #5126), all-rejected holds (#5127, #4887, #4910), and dependencies that had already landed (#4909, #4723, #4665). The poller files twin-sync and verdict-bot blockers as "routine, not escalating"; routine still means the master acts.
- **Check final PRs of projects with no source issue.** The retire-master project posted its phase 1 twin-sync blocker on final PR #5132 at 15:39Z, and it sat 12 h. Scan every open `claude/implement-plan-*` PR into `main` for a blocker newer than its last reply.
- **A dependency landing does not wake anyone.** When a blocker says "wait for X", nothing posts `/reclarify` when X lands. Check waiting blockers on every sweep (#4734 landed at 02:46Z; #4723 and #4665 had waited since the day before for it).
- **Obsolete interim rules.** Before adding an interim rule, check whether its sunset condition has already happened: #5370 (interim Q17 for stage sessions) was closed unmerged because #4734 had landed an hour earlier.
- **A pasted blocker patch may need `git apply --recount`.** Markdown can drop a trailing blank context line; check the resulting sha256 against the blocker.
- **Session lists go stale fast.** Re-read each session's live state with `get_session` right before telling the operator it needs approval; the operator may have answered it already.
- **Routines slip under load.** On 2026-09-30 ~04:15Z scheduled fires ran about 6 minutes late; a quarter Routine's `next_run_at` can show a later time than its cron minute. By ~05:10Z a one-shot trigger set for 04:54Z showed `next_run_at` 05:27Z (33 minutes late). Don't assume a rule update or wake has landed until the target session replies, and don't send a second one because the first is late.
- **Blockers on closed issues are invisible to the sweep.** The close sweep closes an issue when its completion PR merges into a **project branch**, not `main` (#4813). The project goes on: its final PR is still open. A later twin-sync blocker posted on that closed issue never appears in the open `ai:claude-blocked` list, and `/reclarify` there is skipped (#5222). #5012's blocker for final PR #5028 sat 11 h that way. Also check the hold claims on every open `claude/*` PR (`check_in_status.py --hand-back`, state `held`) and read the linked issue even when it is closed.
- **Restarting a checker whose issue the close sweep closed early.** Q63 condition 2 (issue open) is there so finished projects aren't restarted. When the issue was closed only by the #4813 sweep and the project's final PR is still open, the project isn't finished. Answer on the issue, then wake the project checker directly with the `implement-plan <slug>: check-in` trigger, and tell it the closed issue isn't terminal (done for #5012 on 2026-09-30).
- **Poller reports can be wrong.** At 03:07Z it reported PR #5370 as fixed while the PR was open. Verify anything it claims before acting or relaying it.
