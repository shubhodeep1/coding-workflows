# Claude issue pickup: resume sessions stopped by the usage limit

Source issue: shubhodeep1/coding-workflows#5660 (https://github.com/shubhodeep1/coding-workflows/issues/5660)
Base branch: main
Security pass: run

## Summary

When the account hits its Claude usage limit, every running session fails its turn and nothing wakes it after the limit resets. Project checkers lose their `send_later` chain, and stage, fixer, and implementation sessions stop mid-task. Today the master session resumes them by hand. This plan makes the hourly Claude issue pickup do it: a deterministic selector script names the stopped sessions, and a new pickup step sends each one a one-shot `Resume after usage limit (#<N>)` trigger, capped per wake and paced under the trigger-creation rate limit.

## Automation (CLAUDE.md §18.E)

- **Scripts:** one new helper, `.claude/scripts/usage_limit_resumes.py`, run by the existing pickup on every wake. It is not a standalone manual script: it is part of the pickup's wake procedure, like `.claude/scripts/stale_routines.py` is part of the §26 flows. It extends `.claude/scripts/stale_routines.py` with one Routine name.
- **Scheduler entry point:** the pickup's existing self-bound cron Routine `Claude issue pickup: hourly` (`0 * * * *`), plus its catch-up wake (`.claude/commands/claude-issue-pickup.md` steps 1 and 4). The new step 1a runs on every `start`, `— wake.`, and `— wake. — catch-up` wake. No workflow file changes: GitHub Actions cannot reach claude.ai sessions (CLAUDE.md §23.E, §24.G).
- **Supervisor:** extends the existing Claude issue pickup supervisor (entry point `/claude-issue-pickup start`, cron trigger, restart with `— restart`). Its lifecycle, depth, and single-session rule are unchanged. The resume triggers are bound to the stopped sessions, so they add no parent link.
- **DB work:** none.
- **Future-removal registry:** no new entry. The helper is part of the pickup supervisor, whose existing entry in `docs/scripts-pending-removal.md` stays as it is. The helper is removed together with the pickup.

## Context

- Issue #5660: on 2026-09-30 between 10:29Z and 10:59Z, 47 sessions failed with `You've hit your session limit · resets 11am (UTC)` (28 stage and implementer sessions, 19 project checkers). At 12:10Z, 46 were still stopped. The master resumed them by hand from 12:14Z to 12:30Z, in batches of 10, because `create_trigger` answered `Trigger creation rate limit reached. Try again in 55s`. The same happened on 2026-09-29 with the weekly limit (`docs/operations/master-session.md`, "Usage limit stops").
- The pickup came back by itself because its wakes are cron Routines, and a cron Routine keeps firing after a failed turn. It is the first thing that runs after a reset.
- Owner comment (2026-09-30 12:43Z): the convergence checker `session_01CUoZtWt9aXXvwPvx9QwhAx` stopped at the 5-hour limit, but its `status_detail` still read "awaiting next cycle trigger". The limit showed only in `external_metadata.rate_limit_info` (`status: rejected`, `rateLimitType: five_hour`, `isUsingOverage: true`), and no trigger was bound to it. The selector must also catch that case.
- Observed data (a `list_sessions` read of 200 sessions, 2026-09-30 12:55Z):
  - The saved result file wraps the JSON in an `<other-session …>` envelope. Sessions are under `ccr.data`, newest `created_at` first.
  - `post_turn_summary` (`status_category`, `status_detail`, `needs_action`, `recent_action`) is a top-level session field. `rate_limit_info` (`status`, `resetsAt` in epoch seconds, `rateLimitType`, `isUsingOverage`) is under `external_metadata`.
  - `allowed_warning` is the normal state: 95 of 200 sessions show it under a `seven_day` or `five_hour` warning.
  - 13 sessions show `rejected` with `isUsingOverage: true` and healthy summaries. They were archived or waiting normally, because their turns completed on overage. A `rejected` snapshot alone does not mean a session stopped.
- `list_triggers` entries carry `persistent_session_id`, `enabled`, `next_run_at`, `cron_expression`, `run_once_at`, `ended_reason`, and `derived_state.prompt`.
- Related: #4910 (dead-checker restarts, separate), #5068 and #4858 (the skip and edit rules every resume prompt restates), #4948 / Q40 (twin-first), #4785 (its sunset).

## Goals

- After a usage-limit stop, every stopped session with no pending wake gets exactly one `Resume after usage limit (#<N>)` trigger. The trigger comes within one pickup wake after the reset, capped per wake at `CLAUDE_USAGE_LIMIT_RESUME_LIMIT` (default 20, clamped 1..40).
- The selector never picks a session that is:
  - running, pending, or `REQUIRES_ACTION`;
  - archived;
  - waiting on a permission prompt;
  - the pickup itself;
  - bound to a wake that is scheduled soon.
- A pending resume trigger counts as a scheduled wake, so no session is resumed twice for the same stop.
- While the account is still limited, the selector resumes nothing and reports `not_reset`.
- The pickup paces `create_trigger` at 10 per minute across the wake and waits out `Trigger creation rate limit reached` instead of failing.
- The pickup's one-line report carries `limit_resumed=<n>` and `limit_pending=<n>`.
- `.claude/scripts/stale_routines.py` deletes ended `Resume after usage limit (…)` Routines.
- The selector has unit tests for each skip reason, the cap, the ordering, the hold-off, and malformed input. The pickup wiring has command-text tests. `ruff check` is clean on the new script.

## Non-goals

- Limiting how many sessions run at once (issue Q6: A).
- Moving checkers from `send_later` chains to cron Routines (issue Q5 option B, not chosen).
- Restarting checkers that died for other reasons (#4910).
- Changing the queue logic, the watchdog, or `scripts/claude_issue_route.py`.

## Constraints

- **§6:** these new identifiers were checked against the repository and do not collide:
  - `usage_limit_resumes.py`, `CLAUDE_USAGE_LIMIT_RESUME_LIMIT`, and `Resume after usage limit (…)`;
  - `limit_resumed` and `limit_pending`;
  - the selector's output keys (`resume`, `pending`, `skipped`, `errors`, `not_reset`, `limit`, `considered`).

  `stale_routines.py` keeps every existing name and output field.
- **§15:** the selector makes no API calls. It reads only saved `list_sessions` and `list_triggers` files. Each wake adds:
  - at most 10 `list_sessions` pages (100 sessions each, 3 days back);
  - at most 5 `list_triggers` pages;
  - one `create_trigger` per resumed session.

  Nothing goes to GitHub.
- **§25:** no PR watching and no polling. A resume is a scheduled one-shot wake, the same mechanism the master uses today.
- **§26/§28:** the resume prompts keep checkers on their own instructions and restate `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` (never empty) and the #5068 / #4858 rules.
- **§28.C / Q40:** `.claude/**` is never edited unattended. This phase edits only the `workflow-templates/.claude/**` twins, which are the new script, `stale_routines.py`, and `settings.json`. `.claude/commands/claude-issue-pickup.md` has no twin, so its diff goes into the twin-sync blocker.
- **§20:** one `changelog.d/` fragment.
- **§4:** the new env var has a default of 20.

## Approach

1. **Selector `.claude/scripts/usage_limit_resumes.py`** (twin under `workflow-templates/.claude/scripts/`). Its style follows `stale_routines.py`: stdlib only, tabs, one JSON line on stdout.
   - **Inputs:**
     - `--sessions FILE` (repeatable): saved `list_sessions` results, in any of these forms: `{"ccr": {"data": […]}}`, `{"data": […]}`, a bare array, or the harness's `<other-session …>` envelope around any of them. The JSON is found by decoding from the first `{` or `[`.
     - `--triggers FILE` (repeatable): saved `list_triggers` results (`enabled: true`).
     - `--pickup-session ID` (required).
     - `--handoff-author-login LOGIN` (required, non-empty).
     - `--limit N` (overrides `CLAUDE_USAGE_LIMIT_RESUME_LIMIT`).
     - `--now` (tests).
   - **Signals:**
     - *Text:* `post_turn_summary.status_detail` matches the usage-limit or account rate-limit error text.
     - *Snapshot* (checkers only, AD-3): `rate_limit_info.status` is `rejected`, its `resetsAt` has passed, `status_category` is not `need_input`, and no enabled trigger is bound to the session.

     A session with neither signal is not a candidate.
   - **Skip reasons** for a candidate:
     - `pickup`: it is the pickup session.
     - `archived`.
     - `not_idle:<status>`.
     - `permission_prompt`: `needs_action` or `status_detail` asks to approve or deny, or says it is waiting on permission.
     - `needs_input`: snapshot path only.
     - `not_reset`: its own `rate_limit_info` is limited and `resetsAt` is still in the future.
     - `wake_pending`: an enabled trigger bound to it fires within 30 minutes or is overdue (text path), or any enabled trigger is bound to it (snapshot path).
   - **Kind:** `checker` when the title contains `— checker` or `status check-in`, otherwise `other`.
   - **Ordering:** checkers first, then the oldest `updated_at`.
   - **Cap:** the first `limit` entries go to `resume`. The rest go to `pending` (ids only), for the next wake.
   - **Account hold-off:** the pickup's own entry decides. When its status is not `allowed` or `allowed_warning` and its `resetsAt` is still in the future, `not_reset` is `true`, `resume` is empty, and every eligible session goes to `pending`.
   - **Each resume entry** carries `session_id`, `kind`, `issue` (from a leading `#<N> · ` in the title, or `null`), `signal` (`text` / `rate_limit_info`), `trigger_name`, and `prompt`.
     - `trigger_name` is `Resume after usage limit (#<N>)`. Without an issue it is `(PR #<n>)` when the title names a PR, otherwise the session id's last 8 characters (AD-7).
     - `prompt` is rendered by the script from fixed text and the login (AD-8). No session title or summary reaches a prompt.
   - **Errors:** malformed session or trigger entries go to `errors` and are skipped. An unreadable or malformed file, or a bad argument, exits 2 with `{"resume": [], "error": …}`.
2. **Pickup step 1a** (`.claude/commands/claude-issue-pickup.md`, delivered through the sync blocker). It runs after step 1 in `start` and both wake modes:
   - List sessions (`mine: true`, `limit: 100`, paged with `after_id` while `has_more` and the page's oldest `created_at` is within 72 hours, at most 10 pages).
   - List triggers (`enabled: true`, `limit: 100`, paged with `cursor`, at most 5 pages).
   - Read the login with `gh api user --jq .login`.
   - Run the script.
   - For each `resume` entry, call `create_trigger` with `persistent_session_id` = its id, `run_once_at` two minutes out, and the entry's `name` and `prompt`, with `initiation: own_followup`.
   - Pacing (AD-5):
     - At most 10 `create_trigger` calls per minute across the whole wake, so step 3 counts too.
     - After each 10th call, and on a `Trigger creation rate limit reached` refusal, run `sleep 60` as its own Bash call with `run_in_background: true`. End the turn, and continue from where you stopped when its completion wakes you.
     - A refused call is retried up to 3 times, then left for the next wake.
   - The report adds `limit_resumed=<n>; limit_pending=<n>`.
3. **Stale Routine sweep:** `stale_routines.py` (twin) also owns `Resume after usage limit (…)` Routines, deleting them once ended (CLAUDE.md §26.G).
4. **Allowlist:** `workflow-templates/.claude/settings.json` gains `Bash(python3 .claude/scripts/usage_limit_resumes.py *)` and `Bash(PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/usage_limit_resumes.py *)`, next to the `stale_routines.py` rules.
5. **Docs:**
   - `README.md` and `agents.md`: the new step, the env var and its default, the selection rules, and the failure modes.
   - `docs/operations/master-session.md`: replace the manual procedure, and add the "Retiring the master" row.
   - CLAUDE.md §26.G: the new deletable name.
   - `changelog.d/5660-resume-usage-limit-stops.md`.

**Alternatives considered:**
- A script heuristic for checkers that already handed off (a child session created near the checker's last update). It misfires for quick stages, so a prompt guard is used instead (AD-4).
- Treating any `rejected` snapshot as a stop, for every session. Observed data shows it would resume finished stage sessions (AD-3).
- An Actions job. It cannot reach claude.ai sessions.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1: usage-limit resumes.**
   - **Scope:** the selector twin, the sweep and settings twins, tests, docs, the changelog fragment, and the pickup change (delivered through the Q40 sync blocker).
   - **Files:** see [Files & Modules](#files--modules).
   - **Protected paths:**
     - `.claude/scripts/usage_limit_resumes.py` [new], `.claude/scripts/stale_routines.py`, and `.claude/settings.json`: via their `workflow-templates/.claude/**` twins;
     - `.claude/commands/claude-issue-pickup.md`: no twin, so its diff goes in the sync blocker.
   - **Done:** the selector twin passes its unit tests and `ruff check`. The sweep twin deletes ended resume Routines. The pickup diff describes step 1a, the pacing, and the report fields. After the sync, `tests/test_usage_limit_resumes.py`, `tests/test_stale_routines.py`, and `tests/test_implement_issue_claude_command.py` pass.
   - **Production-safe at merge:** the script is only called by the synced pickup, and an unsynced pickup ignores it. The sweep change only adds a deletable name for ended one-shots. Rollback: revert the phase PR, and a pending resume trigger then fires once and ends.

## Implementation Steps

Phase 1:
1. `workflow-templates/.claude/scripts/usage_limit_resumes.py` [new]: the selector above.
2. `workflow-templates/.claude/scripts/stale_routines.py`: add the `Resume after usage limit (` name pattern to `is_ours`, and update the docstring.
3. `workflow-templates/.claude/settings.json`: the two allow rules.
4. `tests/test_usage_limit_resumes.py` [new]: the unit tests (they load the twin), the parity and settings checks, and the docs checks.
5. `tests/test_stale_routines.py`: load the twin, and add the new name to the owned-name cases.
6. `tests/test_implement_issue_claude_command.py`: pickup step 1a, pacing, and report assertions. Update the report-line assertion.
7. `.github/workflows/ci.yml`: add `tests/test_usage_limit_resumes.py` to the `Stale Routine sweep tests` step.
8. `.claude/commands/claude-issue-pickup.md`: the step 1a text, the `start` hand-over to step 1a, and the report fields, delivered as an exact diff with the resulting sha256 in the sync blocker.
9. Docs:
   - `README.md` (Claude issue implementer section and the env var table);
   - `agents.md` (the pickup paragraph and the stale sweep bullet);
   - `docs/operations/master-session.md`;
   - CLAUDE.md §26.G.
10. `changelog.d/5660-resume-usage-limit-stops.md` [new].

## Files & Modules

- `workflow-templates/.claude/scripts/usage_limit_resumes.py` [new] (copied to `.claude/scripts/` by the sync)
- `workflow-templates/.claude/scripts/stale_routines.py` (copied by the sync)
- `workflow-templates/.claude/settings.json` (copied by the sync; needs the operator approval window, Q62/Q64)
- `.claude/commands/claude-issue-pickup.md` (protected; via the sync blocker)
- `tests/test_usage_limit_resumes.py` [new]
- `tests/test_stale_routines.py`
- `tests/test_implement_issue_claude_command.py`
- `.github/workflows/ci.yml`
- `README.md`
- `agents.md`
- `docs/operations/master-session.md`
- `CLAUDE.md` (`workflow-templates/CLAUDE.md` is a symlink to it)
- `changelog.d/5660-resume-usage-limit-stops.md` [new]

## Tests

- **Unit** (`tests/test_usage_limit_resumes.py`):
  - both signals;
  - every skip reason;
  - the 30-minute window, including overdue and unparseable `next_run_at` values;
  - the snapshot path's any-trigger rule and its checker-only scope;
  - the per-session and account hold-off (`allowed_warning` counts as allowed; a past `resetsAt` releases);
  - the cap, env default, clamping, and invalid values;
  - the ordering;
  - the trigger names;
  - the rendered prompts (login present; no title or summary text);
  - the envelope parsing, multiple pages, and duplicate ids;
  - malformed input (exit 2), and a missing or empty login.
- **Sweep** (`tests/test_stale_routines.py`): the new name is owned and deleted once ended; an enabled one is kept.
- **Command text** (`tests/test_implement_issue_claude_command.py`): the pickup's step 1a, pacing, and report fields. These fail until the supervising session applies the pickup diff (Q40), which also keeps the phase PR from merging without it.
- **Parity:** the `.claude/` copies equal their twins, and `.claude/settings.json` allowlists the new script. These are red until the sync, as Q40 expects.
- **Regression:** the full `tests/test_stale_routines.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_session_titles.py`, and `tests/test_claude_issue_route.py` suites, plus `ruff check` on the new script.

## Risks & Mitigations

- **A checker resumed after it already started the next stage starts a duplicate.** The checker resume prompt tells it to look for an existing session with the exact next-stage title, created after its latest instructions, before it creates one (AD-4).
- **A healthy idle checker with a stale `rejected` overage snapshot is resumed.** The snapshot path needs a passed `resetsAt`, no bound trigger, and the checker kind. The resumed turn re-runs its instructions, and the duplicate guard stops a second stage, so the cost is one Sonnet turn per stop. The account hold-off prevents repeats while the account is still on overage. ACCEPTED.
- **Summary text that mentions a limit for another reason.** The text patterns are narrow: "You've hit your … limit", "API Error: 429", `rate_limit_error`, "usage limit reached", or an API error naming a rate limit. The trigger-creation message is deliberately not matched.
- **A long-lived checker created more than 3 days ago is missed.** Paging follows the issue (3 days, AD-9); the master handbook keeps its manual fallback note. ACCEPTED.
- **Rate-limit contention with the queue's own `create_trigger` calls.** Pacing counts every `create_trigger` in the wake (AD-5).
- **The pickup file is edited concurrently by other projects.** Step 2 merges `main` into the project branch at every stage, and the supervising session applies the diff to the current file.

## Rollout

Ships with the final PR into `main`. The pickup reads its command file and scripts fresh on every wake (step 0 "Fresh code"), so no restart is needed. `CLAUDE_USAGE_LIMIT_RESUME_LIMIT` is optional (default 20, clamped 1..40), set in the pickup session's environment. Consumer repos receive the new script and the settings rules through the `.claude/` sync. It is unused there, since the pickup runs only in coding-workflows. Rollback: revert the final PR. Pending resume triggers fire once and end, and the sweep deletes them.

## Auto-decisions

- AD-1 **Q1: Which `rate_limit_info` decides the hold-off, and what counts as allowed?**
  - Picked: A. The pickup's own entry decides account-wide (resume nothing, `not_reset`), and each candidate's own entry decides for that session. `allowed` and `allowed_warning` both count as allowed. A limited status holds only while its `resetsAt` is in the future.
  - Alternatives: B, the pickup's own entry only; C, the literal reading: any status other than `allowed` holds.
  - Why: `allowed_warning` is the normal state under a weekly warning (95 of 200 sessions on 2026-09-30), so C would never resume anything. Snapshots are frozen at each session's last turn, so a passed `resetsAt` means that limit reset.
- AD-2 **Q2: Does a `rejected` status with `isUsingOverage: true` count as limited for the hold-off?**
  - Picked: A. Yes: it holds while `resetsAt` is in the future.
  - Alternatives: B, overage counts as allowed.
  - Why: the issue says to hold when the status is not allowed. Resuming dozens of sessions on paid overage is the operator's cost decision (§5: follow the issue).
- AD-3 **Q3: Which sessions does the snapshot-only rule from the owner's comment apply to?**
  - Picked: A. Checker sessions only, with the comment's three conditions plus `status_category` not `need_input`.
  - Alternatives: B, every session.
  - Why: on 2026-09-30, 13 sessions showed `rejected` overage snapshots with healthy summaries after normal turns. Under B, finished stage sessions would be resumed. The comment's case and reasoning are about checkers, whose summaries look healthy when their chain dies.
- AD-4 **Q4: How is a duplicate stage prevented when a resumed checker had already started it?**
  - Picked: A. The checker's resume prompt tells it to check `list_sessions` for a session with the exact title it would create, created after its latest instructions, and to stop if one exists.
  - Alternatives: B, a script heuristic on child sessions created near the checker's last update; C, no guard.
  - Why: B misreads quick stages that hand a new wait within minutes, and C risks a second copy of a stage.
- AD-5 **Q5: How does the pickup pace `create_trigger` and wait out the rate limit?**
  - Picked: A. At most 10 calls per minute across the wake (steps 1a and 3). After each 10th call and on a refusal, run a background `sleep 60` (`run_in_background: true`), end the turn, and continue when it completes. Retry a refused call up to 3 times, then leave it for the next wake.
  - Alternatives: B, stop at the first refusal; C, a foreground `sleep`.
  - Why: the issue says to wait out the limit instead of failing, and the harness blocks foreground `sleep`.
- AD-6 **Q6: Which bound triggers block a resume?**
  - Picked: A. Text path: an enabled trigger whose `next_run_at` is within 30 minutes, overdue, or unreadable. Snapshot path: any enabled trigger.
  - Alternatives: B, any enabled trigger on both paths.
  - Why: the issue body names 30 minutes. A stage session keeps a 7-day hand-back Routine that would block its resume under B. The owner's comment names "no enabled trigger" for the snapshot case.
- AD-7 **Q7: How is a resume trigger named when the session has no issue number?**
  - Picked: A. `Resume after usage limit (#<N>)` from a leading `#<N> · ` in the title; else `(PR #<n>)` from the title; else `(<last 8 chars of the session id>)`.
  - Alternatives: B, always the session id.
  - Why: A keeps the issue's format and stays under the 60-character cap. The sweep matches the prefix.
- AD-8 **Q8: Who writes the resume prompts?**
  - Picked: A. The script renders them from fixed text and a required, non-empty `--handoff-author-login`, and the pickup copies them as given.
  - Alternatives: B, the pickup model writes them.
  - Why: this follows the pickup's "the script decides" rule and is testable. No session title or summary (untrusted text) reaches a prompt.
- AD-9 **Q9: How far back does the pickup list sessions?**
  - Picked: A. 3 days by `created_at`, at most 10 pages of 100, as the issue says.
  - Alternatives: B, 8 days, as the handbook's manual procedure did.
  - Why: the issue specifies 3 days, and every hourly wake pays for the paging.
- AD-10 **Q10: Which pickup modes run the resume step?**
  - Picked: A. `start`, `— wake.`, and `— wake. — catch-up`, before the queue. Not `stop` or `— arm-check-in`.
  - Alternatives: B, hourly wakes only.
  - Why: the issue says "every wake", and a catch-up wake finishes a capped backlog sooner.
- AD-11 **Q11: How does this phase change `.claude/**`?**
  - Picked: A. Twin-first per Q40, as the issue body prescribes. Edit the `workflow-templates/.claude/**` twins (new script, `stale_routines.py`, `settings.json`), and put the `claude-issue-pickup.md` diff and sha256 in the sync blocker. After the phase PR opens, post a `hold` claim and stop `BLOCKED`.
  - Alternatives: B, edit `.claude/**` in this unattended session; C, drop the `.claude/` part.
  - Why: §28.C forbids B, and C would ship nothing the pickup runs.
- AD-12 **Q12: Where does the selector live?**
  - Picked: A. `.claude/scripts/usage_limit_resumes.py`, as the issue proposes, with its twin, which also reaches consumer repos through the sync (unused there).
  - Alternatives: B, `scripts/usage_limit_resumes.py`, next to `claude_issue_route.py`.
  - Why: the issue names the path and the `stale_routines.py` style. A shipped but unused helper is harmless.
- AD-13 **Q13: What happens to the handbook's manual procedure and the "Retiring the master" table?**
  - Picked: A. Replace the manual "Usage limit stops" procedure with a pointer to pickup step 1a and its report fields, keep a short manual fallback for sessions older than the 3-day window, and add the row `Resuming sessions stopped by the usage limit | #5660`, as the issue asks.
  - Alternatives: B, delete the note entirely and add no row.
  - Why: the issue asks for both. The fallback covers the AD-9 window.

## Notes

- `security_pass_skip.py` result: `{"skip": false, "label": null, "reason": "no skip label"}`.

## References

- Issue #5660, and the owner's comment of 2026-09-30 12:43Z.
- Related: #4910, #5068, #4858, #4948 / Q40, #4785, #4990 (the pickup catch-up wake), #4525 (why a pickup session).
- `docs/operations/master-session.md`: "Usage limit stops" and "Retiring the master".
