# Master session handbook

The **master session** is the interactive Claude Code session that supervises the Claude automation in `shubhodeep1/coding-workflows`. This file is its handover: a fresh master session reads it in full and takes over from it. It records the operator's standing decisions, the duties and how to do them, the IDs the automation runs on, and the gotchas learned doing this job.

The master runs in **Auto mode**. The operator's goal is unattended running: a human is involved only when Claude is really stuck.

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
| **Q40: A** (interim twin-first) | A stage that must change `.claude/**` edits only the `workflow-templates/.claude/**` twins, posts a `hold` claim, and stops `BLOCKED`. The master reviews the twin diff, copies it into `.claude/` as a `[claude-twin-sync]` commit on the phase branch, runs the tests, pushes, and comments `/reclarify`. When a stage asks "how should phase N run?" for a protected-path phase, the master answers with Q40 on the issue: a `Protected-path approval: phase N — twin-first per Q40 (<date>)` line, then `/reclarify`. Since #4948, stages in this repo record `Protected-path approval: phase N — twin-first (automatic, interim until #4785) (<date>)` themselves and skip that question; it still comes for an edit denied in the twin tree or a phase whose plan needs a watched session. This lasts until #4785 (Actions sync PR) lands. |
| **Q46: A** (standing merge approval) | The master may merge a **held** `claude/*` PR, into `main` or into a project branch, when all of these hold: every finding was rejected or there were 0 findings; the master checked each rejection against the code; tests pass (CI, or local runs where CI was cancelled or slow); the head hasn't moved; `mergeable_state` is `clean`; and the diff has no `.claude/hooks/**` or `settings.json` change. Post the evidence as a PR comment first, then merge with `merge_method: merge` and `expectedHeadSha`. Lasts until the GPT judge is on `main`. |
| **Q58: A** | The master closes **pipeline-filed** `ai:permission-prompt` duplicates. Pipeline-filed means the body carries `<!-- ai:permission-prompt:v1 sig=… -->`. Close with `state_reason: duplicate` and `duplicate_of`, after a comment naming the target and why. Closing the issue also stops its queued pickup item, because `claude_issue_route.py` refuses closed issues (`issue_closed`). #4867 makes this a documented session rule. |
| **Q60: A** | Session titles start with the numbers: `#<issue> · PR #<pr> — <old title>`. Leave `PR #<n> status check-in` titles unchanged (§26.B step 1b matches them exactly). #4886 makes the automation do this. Until then, rename new sessions by hand when convenient. |
| **Q61: A** | Archive finished sessions: fixer and hold sessions whose PR is terminal, issue-start sessions superseded by a later stage or whose issue is closed, stage sessions a `/reclarify` replaced, and report sessions after 7 days. Never archive a session waiting on a permission prompt. #4887 automates this; archive by hand until then. |
| **Q62/Q64: A** | Syncing a change to a **hook** or `settings.json` into `.claude/` needs the operator: the Auto-mode classifier denies it as "Self-Modification". Ask the operator to switch the master out of Auto mode once, run the copy (one prompt), then return to Auto. |
| **Q63: A** (dead-checker restart) | The poller restarts a project checker when all five conditions hold (below). #4910 moves the rule into the hourly pickup; after that lands, remove it from the poller. |
| **Q66: A** | #4695 composes the #4687 and #4688 rejection gates, with the `RF-` id as the only vote token. |
| **Q67: A** | Batch hook and `settings.json` syncs into one approval window (#4817, #4858, and #4891 when ready). |

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

## IDs

| What | ID |
|---|---|
| Master poller session | `session_012fZCZUmcWNdWwcdSsuDAGu` |
| Claude issue pickup session (hourly, trigger `Claude issue pickup: hourly`) | `session_01ArxJDkmfCGHBaW5Ph9zoHN` |
| Deprecated Routine the **operator** must delete by hand | `trig_01GykbNLA7znHCJGWRetq4Ye` |
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
| Finished sessions pile up; titles hide numbers | #4887, #4886 |
| Duplicate permission-prompt issues | #4867 |
| Parallel security follow-ups change the same code | #4934 |
| Command behind a blocking prompt goes unreported | #4755 |
| Close sweep closes issues on non-default-branch merges | #4813 |
| Merge resolver ran on draft `claude/*` PRs | fixed by #4869 (merged) |

## Gotchas

- **Classifier outages.** When the Auto-mode classifier has an outage, tool calls get "no verdict" or denials. Retry once, then back off. Never probe with throwaway commands, because the operator sees the prompts.
- **Large results.** `list_sessions` (limit 100) and `list_triggers` results are saved to a file. Parse them with `python3` (`data["ccr"]["data"]` for sessions). Paginate with `after_id`.
- **`run_once_at` must be in the future.** Compute it with `date -u -d '+2 minutes' +%Y-%m-%dT%H:%M:00Z` right before the call.
- **Never use `fire_trigger`** for hand-backs: it starts a fresh session with no context.
- **GitHub access through the proxy.** Search endpoints and GraphQL are blocked. Use `mcp__github__search_issues` and repo-scoped REST.
- **§21 merged-PR guard.** After your PR merges, start new work on a fresh branch from `origin/main`: `claude/magical-edison-7pkk3t-<n+1>`.
- **Protected-path writes.** Writes to `.claude/**` via `cp` work in Auto mode for commands and scripts, but hooks and `settings.json` are denied (Q62/Q64).
- **Session-status fields.** A session in `REQUIRES_ACTION` with "Waiting on permission: Bash" is a permission prompt only the operator can answer in that session's view. List those sessions by name with issue and PR numbers when reporting.
- **Rate limits.** Watch the master's own `rate_limit_info`. A `seven_day` `allowed_warning` means restart the master, or trim its work, before hitting the cap.
