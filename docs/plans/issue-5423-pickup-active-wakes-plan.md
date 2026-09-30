# Claude issue pickup: 15-minute active wakes while projects are active

Source issue: shubhodeep1/coding-workflows#5423 (https://github.com/shubhodeep1/coding-workflows/issues/5423)
Base branch: main
Security pass: run

## Summary

A queued item (a `/reclarify` resume, a new routed issue, a catch-all fix) waits up to 60 minutes, or about 30 with the #4990 catch-up, before the Claude issue pickup starts its session. Every unattended project goes through that hop at every blocker. This plan adds a **hot mode**: at the end of a `— wake.`, the pickup schedules its own next wake with `send_later`, `CLAUDE_PICKUP_ACTIVE_WAKE_MINUTES` (default 15) out, whenever it sees active work. With no active work it schedules nothing extra, and the hourly Routine stays the floor. The script decides hot or idle from the existing queue read plus at most two cheap repository-scoped reads. The stale Routine sweep learns to delete the fired pickup one-shots.

## Automation (CLAUDE.md §18.E)

- **Scripts:** extends the existing `scripts/claude_issue_route.py` (`queue-pending`) and `.claude/scripts/stale_routines.py` (twin first). No new script.
- **Scheduler entry point:** the pickup's existing self-bound Routine `Claude issue pickup: hourly` (cron `0 * * * *`, `.claude/commands/claude-issue-pickup.md` step 1). The new active wake is one `send_later` into the pickup session itself, scheduled at the end of a wake (`.claude/commands/claude-issue-pickup.md` step 4), the same mechanism as the #4990 catch-up. No workflow file changes.
- **Supervisor:** extends the existing Claude issue pickup supervisor. Its lifecycle is unchanged: same entry point (`/claude-issue-pickup start`), the same single session and depth, and `stop` / `start — restart` also remove a pending active wake. A `send_later` bound to the pickup session adds no parent link and auto-disables when that session is archived.
- **DB work:** none.
- **Future-removal registry:** the existing `.claude/commands/claude-issue-pickup.md` entry in `docs/scripts-pending-removal.md` gains the `Claude issue pickup: active wake` trigger in its removal preflight check. No new entry.

## Context

- Today the pickup wakes hourly from its Routine (the Routine cron minimum), plus one `send_later` catch-up 30 minutes later when a wake left queue items behind (#4990, `catch_up_due`). `send_later` has 1-minute granularity.
- On 2026-09-30 the operator asked for 15-minute sweeps. The master session currently wakes the pickup early with a one-shot `Claude issue pickup: master wake` trigger when a queue item is more than 5 minutes old. The retire-master-session plan removes the master session, and this latency comes back with it.
- The pickup runs in a claude.ai cloud session. Verified 2026-09-30 from such a session: the agent proxy refuses the search API (`search/issues` → HTTP 403 "sessions are bound to their configured repositories"), and only repositories attached to the session are readable. Repository-scoped list reads work: `issues?labels=ai:claude-blocked&state=open` returned 17 issues and `pulls?state=open&per_page=100` returned 94 PRs, 89 of them with a `claude/implement-plan-` head.
- `fetch_open_queue` reads `repos/<queue repo>/issues?labels=ai:claude-issue-queue&state=open&per_page=100` (`scripts/claude_issue_route.py:1176`). The REST list endpoint ANDs its `labels`, so the blocked read cannot be folded into that call.
- `stale_routines.py` deletes only Routines named `PR #<n> status check-in…`, `PR #<n> hand-back`, `implement-plan <slug>: …`, and `dispatch <owner>/<repo>#<n>: …`, so fired pickup one-shots (`Claude issue pickup: catch-up`, `… master wake`) pile up.
- Related, in flight: #4817, #4887, #4910 (pickup steps 3.4, 3a, 3b) and the retire-master-session project (phase 2 adds pickup step 3c, the blocked-issue sweep). All of them run inside the same `— wake.` procedure, so this plan extends that wake and adds no second loop.

## Goals

- G1: While active work exists, the pickup wakes about every `CLAUDE_PICKUP_ACTIVE_WAKE_MINUTES` (default 15) minutes, so a queued item starts within about 15 minutes instead of up to 60.
- G2: With no active work, the pickup schedules nothing beyond the hourly Routine.
- G3: At most one self-wake (active wake or catch-up) is pending at any time, and the active wake replaces the catch-up when both would be scheduled.
- G4: One cheap read decides hot or idle: no extra call when a queue item is open, otherwise at most two REST reads, documented in the docstring (§15).
- G5: A wake from an external one-shot trigger (such as `Claude issue pickup: master wake`) runs exactly like the hourly one.
- G6: The stale Routine sweep deletes fired `Claude issue pickup: master wake`, `Claude issue pickup: catch-up`, and `Claude issue pickup: active wake` one-shots, and never the `Claude issue pickup: hourly` Routine.

## Non-goals

- Changing the hourly Routine, the watchdog (`claude-issue-queue-watchdog.yml`), the queue binding (#4621), or the per-wake start limit (#4990).
- Reading consumer repositories for active work (see AD-1).
- Retiring the master session's `master wake` trigger. It keeps working and is simply treated as an hourly wake.
- The retire-master-session blocked-issue sweep itself (its phase 2).

## Constraints

- §6: every existing `queue-pending` output field (`pending`, `ignored`, `remaining`, `deferred`, `limit`, `oldest_waiting_minutes`, `catch_up_due`) keeps its name and meaning, and `--wake hourly | catch-up` keeps working. The report keys `catch_up=` and `oldest_waiting=` stay. New identifiers were checked against `scripts/claude_issue_route.py` and `stale_routines.py` and do not collide: `ACTIVE_WAKE_MINUTES_ENV`, `ACTIVE_WAKE_MINUTES_DEFAULT`, `ACTIVE_WAKE_MINUTES_MIN`, `ACTIVE_WAKE_MINUTES_MAX`, `CATCH_UP_WAKE_MINUTES`, `ACTIVE_PR_HEAD_PREFIX`, `resolve_active_wake_minutes`, `fetch_active_work`, `decide_self_wake`, output fields `active_work`, `active_work_errors`, `active_wake_minutes`, `self_wake`, `self_wake_minutes`, the wake kind `active`, and `PICKUP_ONE_SHOT_NAME_PATTERN`.
- §4: `CLAUDE_PICKUP_ACTIVE_WAKE_MINUTES` has a default (15), and a bad value falls back to it.
- §15: no extra call when the queue read already shows an open item. Otherwise one blocked-issue read, then one open-PR read only when that found nothing. No search API (refused by the proxy), no GraphQL, no per-item reads.
- §25: no PR watching, no polling. The active wake is a scheduled self check-in bound to the pickup, not a watch.
- §26.G: the sweep still deletes only Routines these flows create, only once they have ended.
- §28.C / Q40: `.claude/**` is never edited unattended. `stale_routines.py` is changed in its `workflow-templates/.claude/scripts/` twin. `claude-issue-pickup.md` has no twin, so its diff goes in the twin-sync blocker (AD-11).
- §20: one `changed` fragment in `changelog.d/`.
- §27: no workflow file grows.

## Approach

1. **Script (`scripts/claude_issue_route.py`).**
   - `resolve_active_wake_minutes(value)` reads `CLAUDE_PICKUP_ACTIVE_WAKE_MINUTES`. Unset, empty, or non-integer gives 15. An integer of 0 or less turns hot mode off. Other integers are clamped to 5..30 (AD-5).
   - `fetch_active_work(repo, issues, gh_read=None)` decides the activity from the queue read it is given plus at most two reads, short-circuiting on the first hit (AD-1, AD-2, AD-3, AD-4):
     1. any open queue issue in `issues` → `queue` (no call);
     2. `repos/<repo>/issues?labels=ai:claude-blocked&state=open&per_page=1` non-empty → `blocked`;
     3. `repos/<repo>/pulls?state=open&per_page=100` holds a PR whose head ref starts with `claude/implement-plan-` → `plan_pr`;
     4. otherwise `idle`, or `unknown` when a read failed and nothing was found (errors in `active_work_errors`).
   - `decide_self_wake(wake, remaining, active_work, active_minutes)` returns the one self-wake to schedule: `active` (`active_minutes` out) when hot mode is on and the activity is `queue`, `blocked` or `plan_pr`; else `catch-up` (30 minutes out) when `catch_up_due`; else `none`. The active wake therefore replaces the catch-up (AD-8).
   - `QUEUE_WAKE_KINDS` gains `active`. `catch_up_due` is unchanged: only an `hourly` wake can ask for a catch-up.
   - `queue-pending` output gains `active_work`, `active_work_errors`, `active_wake_minutes`, `self_wake`, and `self_wake_minutes`. With `--fetch-repo` it reads the activity. With `--issues-json` (offline) it makes no call, and a queue without open items reports `unknown`.
2. **Pickup command (`.claude/commands/claude-issue-pickup.md`, via the sync blocker).**
   - `$ARGUMENTS`: `— wake.` is any wake without a suffix, whatever trigger sent it: the hourly Routine or an external one-shot such as `Claude issue pickup: master wake` (AD-9). New mode `— wake. — active`: the active wake, sent only by the pickup's own `send_later`. It follows every `— wake.` rule, with `--wake active`.
   - Step 1: a self-wake counts as pending when an enabled `Claude issue pickup: catch-up` or `Claude issue pickup: active wake` trigger bound to the pickup has a `next_run_at` in the future (AD-7). `stop` and `start — restart` also delete `Claude issue pickup: active wake` triggers.
   - Step 2: pass `--wake <hourly | catch-up | active>`; describe the new output fields and the extra reads.
   - Step 4: schedule on `self_wake`: `active` → `send_later` `delay_minutes: <self_wake_minutes>`, `name: Claude issue pickup: active wake`, message `— wake. — active`; `catch-up` → the existing catch-up; `none` → nothing. Either way, nothing when a self-wake is already pending. The report line adds `activity=<active_work>` and `active_wake=<scheduled | pending | none | failed>`, and `catch_up=` gains `replaced` (AD-12).
   - Rules: "one wake per hour … plus at most one catch-up" becomes "the hourly wake, plus at most one pending self-wake: the active wake while there is active work, else the catch-up".
3. **Stale Routine sweep (`workflow-templates/.claude/scripts/stale_routines.py`, twin first).** `is_ours` also matches `Claude issue pickup: master wake`, `Claude issue pickup: catch-up`, and `Claude issue pickup: active wake`, so an ended one is deleted by the existing ended rule. `Claude issue pickup: hourly` never matches (AD-10).
4. **Docs.** `README.md`, `agents.md`, CLAUDE.md §26.G (names the sweep deletes) and §26.H (the pickup cadence), in both `CLAUDE.md` and `workflow-templates/CLAUDE.md`, and `docs/scripts-pending-removal.md`.

Alternatives considered:
- A second, 15-minute Routine. Rejected: the Routine minimum is hourly, and a second trigger breaks "exactly one pickup trigger".
- A search API query across every registered repo. Rejected: the proxy refuses it (verified).
- Always waking every 15 minutes. Rejected: the issue requires idle wakes to stay hourly.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — pickup active wakes.** The script change, the stale-sweep twin change, tests, docs, the changelog fragment, and the `claude-issue-pickup.md` change (delivered through the Q40 sync blocker).
   - Files: see [Files & Modules](#files--modules).
   - Protected paths: `.claude/commands/claude-issue-pickup.md` (no twin: its diff goes in the sync blocker), `.claude/scripts/stale_routines.py` (edited through its `workflow-templates/.claude/scripts/` twin).
   - Done: `resolve_active_wake_minutes`, `fetch_active_work` (every short-circuit, call counts and failures), `decide_self_wake` (hot vs idle, replacing the catch-up, hot mode off), the `active` wake kind, and the CLI fields are covered by tests. The sweep twin deletes ended pickup one-shots and keeps `Claude issue pickup: hourly`. The pickup text describes the active wake and the one-pending rule. `tests/test_claude_issue_route.py`, `tests/test_stale_routines.py`, `tests/test_implement_issue_claude_command.py`, and `tests/test_claude_pr_sweep.py` pass after the sync.
   - Production-safe at merge: the script only adds output fields and one wake kind. An unsynced pickup ignores the new fields and keeps the #4990 behaviour. Setting `CLAUDE_PICKUP_ACTIVE_WAKE_MINUTES=0` turns hot mode off without a revert. Rollback: revert the phase PR.

## Implementation Steps

Phase 1:
1. `scripts/claude_issue_route.py`: add the constants, `resolve_active_wake_minutes`, `fetch_active_work` (with a §15 docstring stating input, output, call count and fail-open behaviour, and why the blocked read cannot join the queue read), `decide_self_wake`, the `active` wake kind, and the new `queue-pending` fields. Update the module docstring's list of impure functions.
2. `tests/test_claude_issue_route.py`: tests for the minutes parser (default, off, clamping, invalid), `fetch_active_work` (queue hit with no call, blocked hit with one call, `claude/implement-plan-` PR hit with two calls, other PR heads ignored, idle, a failed read yielding `unknown` or still finding the next source), `decide_self_wake` (hot schedules active on every wake kind, idle schedules nothing, active replaces a due catch-up, hot mode off falls back to the catch-up, `unknown` schedules no active wake), and the CLI (`--wake active`, the new fields, the exact fake-`gh` call log for an empty queue, and the unchanged log when an item is open).
3. `workflow-templates/.claude/scripts/stale_routines.py`: the pickup one-shot names. `tests/test_stale_routines.py`: new cases that load the twin (ended pickup one-shots deleted, `Claude issue pickup: hourly` never deleted, a pending pickup one-shot kept).
4. `tests/test_implement_issue_claude_command.py`: assertions that the pickup schedules the active wake from `self_wake`, keeps one pending self-wake (`next_run_at` in the future), treats external `— wake.` triggers as hourly, and reports `activity=` and `active_wake=`. Update the catch-up assertions whose wording changes.
5. `.claude/commands/claude-issue-pickup.md`: the text changes above, delivered as a diff plus the resulting sha256 in the sync blocker.
6. `README.md`, `agents.md`, `CLAUDE.md` + `workflow-templates/CLAUDE.md` (§26.G, §26.H), `docs/scripts-pending-removal.md`: document the behaviour (§7).
7. `changelog.d/5423-pickup-active-wakes.md`: the fragment (§20).

## Files & Modules

- `scripts/claude_issue_route.py`
- `tests/test_claude_issue_route.py`
- `workflow-templates/.claude/scripts/stale_routines.py` (twin; `.claude/scripts/stale_routines.py` via the sync)
- `tests/test_stale_routines.py`
- `tests/test_implement_issue_claude_command.py`
- `.claude/commands/claude-issue-pickup.md` (protected, no twin; via the sync blocker)
- `README.md`
- `agents.md`
- `CLAUDE.md`, `workflow-templates/CLAUDE.md`
- `docs/scripts-pending-removal.md`
- `changelog.d/5423-pickup-active-wakes.md` [new]

## Data Model / Index Changes

None.

## Tests

- Unit: the new `tests/test_claude_issue_route.py` and `tests/test_stale_routines.py` cases above. The sweep cases load the twin, so they pass before the sync.
- Command text: the `tests/test_implement_issue_claude_command.py` pickup assertions. They fail until the supervising session applies the pickup diff (Q40), and `tests/test_stale_routines.py::test_template_parity` fails until it copies the sweep twin. Both keep the phase PR from merging without the sync.
- Regression: the full `tests/test_claude_issue_route.py`, `tests/test_stale_routines.py`, `tests/test_implement_issue_claude_command.py`, and `tests/test_claude_pr_sweep.py` suites (`claude_pr_sweep.py` calls `queue_pending` and must not change).

## Risks & Mitigations

- **The pickup stays hot almost all the time.** On 2026-09-30 there were 17 open `ai:claude-blocked` issues and 89 open `claude/implement-plan-*` PRs, so hot mode is the norm while projects exist. That is the behaviour the issue asks for. Each extra wake costs one pickup turn, one `list_triggers`, the queue read and at most two reads. ACCEPTED. `CLAUDE_PICKUP_ACTIVE_WAKE_MINUTES=0` turns it off.
- **A fired trigger still listed as enabled during its own wake** would make the pickup think a self-wake is pending and break the 15-minute chain. Mitigation: only a trigger with a `next_run_at` in the future counts as pending (AD-7). If the chain breaks anyway, the next hourly wake restarts it.
- **The active wake `send_later` fails.** The report says `active_wake=failed`, and the next hourly wake tries again. ACCEPTED.
- **Consumer-only activity does not make the pickup hot** (AD-1). A consumer's `/reclarify` still lands in this repo's queue, which is hot at the next wake. ACCEPTED.
- **More than 100 open PRs.** The PR read covers the newest 100. When no `claude/implement-plan-` PR is among them and no queue item or blocked issue exists, the pickup stays idle (hourly floor). ACCEPTED.
- **Retire-master-session phase 2 `page` action.** It pages blockers "created since the previous hourly wake", which assumes one wake per hour. With active wakes, that step must key off the `hourly` wake kind (the pickup passes `--wake`) or it pages up to four times. Recorded in this plan's Notes and the PR body for that project.
- **Concurrent pickup edits** (#4817, #4887, #4910, retire phase 2). The step-2 sync merges `main` at every stage, and the supervising session applies the diff against the current file.

## Rollout

Ships with the final PR into `main`. The pickup reads `.claude/commands/claude-issue-pickup.md` and the script fresh on every wake (step 0 "Fresh code"), so no restart is needed. The first wake after the merge schedules the first active wake when there is active work. `CLAUDE_PICKUP_ACTIVE_WAKE_MINUTES` is optional. Setting it in the pickup session's environment changes the delay, and `0` turns hot mode off. Rollback: revert the final PR. A pending active wake then runs one ordinary wake and schedules nothing further.

## Auto-decisions

- AD-1 **Q1: Where does the pickup read active work, given that the proxy refuses the search API and reaches only attached repositories?** Picked: A — repository-scoped reads in the queue repository only (coding-workflows), which holds the queue for every registered repo. Alternatives: B — the search API across every registered repo (HTTP 403 from the proxy, verified 2026-09-30); C — per-repository reads of all 14 registered repos (14+ calls per wake, and consumer repos are not attached to the pickup session). Why: A is the only option that works from the pickup's session within the §15 budget.
- AD-2 **Q2: Which pull requests count as an "open `claude/implement-plan-*` final PR"?** Picked: A — any open PR whose head ref starts with `claude/implement-plan-`. Alternatives: B — only PRs whose base does not start with `claude/implement-plan-`. Why: phase and fix PRs exist only while their project is open, so A is a safe superset, and B would miss issue-mode projects built on another project's branch.
- AD-3 **Q3: Which queue items count as active work?** Picked: A — any open `ai:claude-issue-queue` issue the queue read returned, including ignored ones. Alternatives: B — only bound or deferred items. Why: the issue says "an open queue item", `binding_pending` items become work within minutes, and stuck items are the watchdog's to flag.
- AD-4 **Q4: What does a failed activity read do?** Picked: A — the remaining sources are still read. The pickup is hot if any source found work, else `unknown`, which schedules no active wake (the hourly floor, today's behaviour). Alternatives: B — treat a failure as hot. Why: fail open to the pre-change behaviour and never loop on a broken read (§15).
- AD-5 **Q5: How is `CLAUDE_PICKUP_ACTIVE_WAKE_MINUTES` parsed?** Picked: A — unset, empty, or non-integer → 15; 0 or less → hot mode off; other integers clamped to 5..30. Alternatives: B — no off switch; C — any positive value. Why: 30 keeps the active wake no later than the catch-up it replaces, 5 keeps one wake from overlapping the next, and `0` is a rollback without a revert (§4 default).
- AD-6 **Q6: How does the active wake identify itself?** Picked: A — it carries `— wake. — active` and runs `queue-pending --wake active`, following every `— wake.` rule, and may schedule the next active wake but never a catch-up. Alternatives: B — reuse the plain `— wake.` prompt. Why: the report and future per-hour steps (retire phase 2) can tell the kinds apart, and `catch_up_due` stays hourly-only.
- AD-7 **Q7: How is "at most one pending self-wake" checked?** Picked: A — step 1's `list_triggers` shows an enabled `Claude issue pickup: catch-up` or `Claude issue pickup: active wake` trigger bound to the pickup whose `next_run_at` is in the future. External triggers such as `master wake` do not count. Alternatives: B — any enabled trigger with those names. Why: a one-shot that is firing right now could still be listed, and counting it would stop the 15-minute chain.
- AD-8 **Q8: Where is the hot/idle and catch-up choice made?** Picked: A — in the script (`self_wake`, `self_wake_minutes`), with `catch_up_due` kept unchanged for §6. Alternatives: B — the pickup model combines `catch_up_due` and the activity itself. Why: the pickup's rule is "the script decides", and a script decision is testable.
- AD-9 **Q9: How are external early wakes handled?** Picked: A — any `— wake.` without a suffix is an hourly wake, whatever trigger sent it. The pickup text says so, and nothing else changes. Alternatives: B — a separate `— wake. — master` mode. Why: the issue asks for the same behaviour, and the master's trigger already sends `— wake.`.
- AD-10 **Q10: Which Routine names does the stale sweep add?** Picked: A — exactly `Claude issue pickup: master wake`, `Claude issue pickup: catch-up` and `Claude issue pickup: active wake`, deleted only once ended. Alternatives: B — every `Claude issue pickup: …` name. Why: B would also match the recurring `Claude issue pickup: hourly` Routine, whose ended state `restart` handles, and the `arm request` / `checker ready` triggers, which other flows own.
- AD-11 **Q11: How does the phase change the protected files?** Picked: A — twin-first per Q40, as the issue body prescribes: edit `workflow-templates/.claude/scripts/stale_routines.py`, put the `claude-issue-pickup.md` diff (no twin) and every sha256 in the sync blocker, and stop `BLOCKED` with a `hold` claim after the phase PR opens. Alternatives: B — edit `.claude/**` in this unattended session; C — drop the `.claude/` changes. Why: §28.C forbids B, and C would ship the script without the wake.
- AD-12 **Q12: What does the report line show?** Picked: A — add `activity=<queue | blocked | plan_pr | idle | unknown>` and `active_wake=<scheduled | pending | none | failed>`, and `catch_up=replaced` when the active wake took the catch-up's place. Alternatives: B — replace `catch_up=` with one `self_wake=` key. Why: §6 keeps the existing report key, and the operator can see why the pickup is hot.

## Notes

- `security_pass_skip.py` result: `{"skip": false, "label": null, "reason": "no skip label"}`.
- Coordination with retire-master-session phase 2: its `page` window ("created since the previous hourly wake") must key off `--wake hourly` once active wakes exist.

## References

- Issue #5423; #4990 (catch-up wake), #4621 (queue binding), #4525 (why a pickup session), #4785 (Q40 sunset), #4817, #4887, #4910.
- `docs/plans/retire-master-session-plan.md` (phase 2, pickup step 3c).
- `docs/operations/master-session.md` (the interim `master wake`).
