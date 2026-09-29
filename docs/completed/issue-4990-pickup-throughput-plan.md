# Claude issue pickup throughput: catch-up wake, higher limit, resumes first

Source issue: shubhodeep1/coding-workflows#4990 (https://github.com/shubhodeep1/coding-workflows/issues/4990)
Base branch: main
Security pass: run

## Summary

The Claude issue pickup starts at most 10 sessions per hourly wake, so a burst of 24 queued items took up to 3 hours to drain, and `/reclarify` resumes waited behind newly filed issues. This plan adds one self-bound catch-up wake 30 minutes after an hourly wake that left work behind, raises the per-wake limit to 20 (overridable), starts `reclarify` resumes first, and reports the age of the oldest waiting item.

## Automation (CLAUDE.md §18.E)

- **Scripts:** extends the existing `scripts/claude_issue_route.py` (`queue-pending`); no new script.
- **Scheduler entry point:** the pickup's existing self-bound Routine `Claude issue pickup: hourly` (cron `0 * * * *`, `.claude/commands/claude-issue-pickup.md` step 1). The new catch-up wake is one `send_later` into the pickup session itself, scheduled by the hourly wake (`.claude/commands/claude-issue-pickup.md` step 4). No workflow file changes.
- **Supervisor:** extends the existing Claude issue pickup supervisor. Its lifecycle is unchanged: same entry point (`/claude-issue-pickup start`), same single session and depth, `stop` / `start — restart` also remove its pending catch-up. A catch-up trigger bound to the pickup session adds no parent link and auto-disables when that session is archived.
- **DB work:** none.
- **Future-removal registry:** the existing `.claude/commands/claude-issue-pickup.md` entry in `docs/scripts-pending-removal.md` gains the catch-up trigger in its removal preflight check.

## Context

- On 2026-09-29 at 03:25Z the `ai:claude-issue-queue` held 24 open items. `QUEUE_PICKUP_LIMIT = 10` (`scripts/claude_issue_route.py:104`) and one wake per hour meant the last item could wait about 3 hours. Eight `/reclarify` resumes (#4750, #4817, #4858, #4755, #4723, #4926, #4867, #4786) queued behind new `opened` issues.
- Routines cannot run more often than hourly, so a second wake must come from a one-shot `send_later` bound to the pickup session (CLAUDE.md §26.B uses the same mechanism for checkers).
- `queue_pending` currently orders targets by the oldest queue issue number (`scripts/claude_issue_route.py:723`), and the pickup's binding reads cover the first `QUEUE_BINDING_SCAN_FACTOR` (3) × limit targets (`_cmd_queue_pending`).
- Related: #4910 (checker liveness), #4985, #4911, #4817 (in flight, also edits `claude-issue-pickup.md`).

## Goals

- With 24 queued items and the default limit, every item has started within about 30 minutes of its first possible wake (20 at the hourly wake, the remaining 4 at the catch-up wake).
- A `reclarify` resume never waits behind new `opened` issues: `queue-pending` lists every issue entry whose trigger is `reclarify` before every other entry.
- The pickup schedules at most one catch-up wake per hourly wake, a catch-up wake never schedules another, and the pickup never creates a session for its own next wake.
- `QUEUE_PICKUP_LIMIT` is 20 and `CLAUDE_ISSUE_PICKUP_LIMIT` overrides it (default 20, clamped to 1..30).
- The one-line pickup report carries `oldest_waiting=<minutes>`.

## Non-goals

- Changing the watchdog (`claude-issue-queue-watchdog.yml`) or its `CLAUDE_ISSUE_QUEUE_STALE_HOURS` threshold (default 3).
- Changing the queue binding checks (#4621), the dispatch step (`claude-issue-dispatch.md` step 2), or how `/implement-issue-claude` resumes.
- Prioritising `pr_fix` or `manual` entries (see AD-6).

## Constraints

- §6: `QUEUE_PICKUP_LIMIT` keeps its name; the `queue-pending` output keeps every existing field (`pending`, `ignored`, `remaining`, `deferred`) and only gains new ones; `--limit` keeps working. New identifiers (`QUEUE_PICKUP_LIMIT_ENV`, `QUEUE_PICKUP_LIMIT_MIN`, `QUEUE_PICKUP_LIMIT_MAX`, `QUEUE_WAKE_KINDS`, `resolve_pickup_limit`, `catch_up_due`, `--wake`, `--now`) were checked against the module and do not collide.
- §15: the queue read stays one call. More sessions per wake add only the per-item `create_session`, `create_trigger`, and `issue_write` calls. The binding window follows the limit (3 × 20 = 60 targets), so a backlog of more than 30 bound targets adds one compare read and one artifact download per extra completed producer run (AD-8). The catch-up wake costs one `send_later`, plus the same reads as an hourly wake.
- §25: no PR watching, no polling. The catch-up is a scheduled self check-in, not a watch.
- §28.C / Q40: `.claude/**` is never edited unattended. `claude-issue-pickup.md` has no `workflow-templates/.claude/**` twin, so its diff goes in the sync blocker for the supervising session (AD-9).
- §20: one `changelog.d/` fragment.

## Approach

1. **Script (`scripts/claude_issue_route.py`).**
   - `QUEUE_PICKUP_LIMIT = 20`; `resolve_pickup_limit(value)` reads `CLAUDE_ISSUE_PICKUP_LIMIT`: unset, empty, or non-integer → 20; an integer outside 1..30 is clamped. `queue-pending --limit` still wins when given; otherwise the CLI uses the resolved limit.
   - `queue_pending` orders its targets in two tiers with a stable sort: issue entries whose `trigger` is `reclarify` first, then everything else in queue order. The binding run window (`queue_binding_run_ids`) uses the same order, so resumes are always inside it.
   - `queue_pending` gains `now` and reports `oldest_waiting_minutes`: whole minutes since the oldest `created_at` among the queue issues it grouped or deferred this read (ignored items excluded, since they stay open for the watchdog), or `null` when none.
   - `queue-pending --wake <hourly | catch-up>` (default `hourly`) adds `catch_up_due`: true only for an hourly wake with `remaining > 0`. The output also carries the `limit` used.
2. **Pickup command (`.claude/commands/claude-issue-pickup.md`, via the sync blocker).** A new `— wake. — catch-up` mode runs the `— wake.` procedure with `--wake catch-up`. At the end of a start or hourly wake whose `catch_up_due` is true, and when step 1's `list_triggers` shows no enabled `Claude issue pickup: catch-up` trigger bound to the pickup, the pickup calls `send_later` into itself (30 minutes, name `Claude issue pickup: catch-up`). `stop` also deletes a pending catch-up. The report line gains `oldest_waiting=<minutes | none>` and `catch_up=<scheduled | pending | none | failed>`.
3. **Docs.** `README.md` and `agents.md` describe the catch-up wake, the new limit and its override, resume ordering, the report field, and the binding window; `docs/scripts-pending-removal.md` names the catch-up trigger.

Alternatives considered: two hourly Routines 30 minutes apart (rejected: two triggers break the "exactly one pickup trigger" rule and step 1's convergence logic); a catch-up that repeats until the queue is empty (rejected by the issue: at most one per hour).

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — pickup throughput.** The script changes, tests, docs, changelog fragment, and the `claude-issue-pickup.md` change (delivered through the Q40 sync blocker).
   - Files: see [Files & Modules](#files--modules).
   - Protected paths: `.claude/commands/claude-issue-pickup.md` (no twin; its diff goes in the sync blocker per Q40).
   - Done: `resolve_pickup_limit`, the resume ordering, `oldest_waiting_minutes`, and `catch_up_due` are covered by tests; the pickup command text describes the catch-up wake and its single-wake rule; `tests/test_claude_issue_route.py`, `tests/test_implement_issue_claude_command.py`, and `tests/test_claude_pr_sweep.py` pass after the sync.
   - Production-safe at merge: the script only adds output fields and reorders `pending`; an unsynced pickup ignores the new fields and keeps working with a higher limit. Rollback: revert the phase PR.

## Implementation Steps

Phase 1:
1. `scripts/claude_issue_route.py`: raise `QUEUE_PICKUP_LIMIT`, add `resolve_pickup_limit` and its constants, the two-tier ordering and `oldest_waiting_minutes` in `queue_pending`, `catch_up_due`, and the `--wake` / `--now` / resolved `--limit` handling in `_cmd_queue_pending`.
2. `tests/test_claude_issue_route.py`: tests for the limit (default, override, clamping, invalid values, CLI), the ordering (reclarify first, queue order within tiers, `pr_fix` unmoved, binding window order), `oldest_waiting_minutes` (ignored excluded, deferred included, `null` when empty), and `catch_up_due` (hourly with and without remaining, catch-up wake never, CLI flag).
3. `tests/test_implement_issue_claude_command.py`: assertions that the pickup command schedules one self-bound catch-up, that a catch-up wake never schedules another, and that the report carries `oldest_waiting=`.
4. `.claude/commands/claude-issue-pickup.md`: the text changes above, delivered as a diff in the sync blocker.
5. `README.md`, `agents.md`, `docs/scripts-pending-removal.md`: document the behaviour (§7).
6. `changelog.d/4990-pickup-throughput.md`: the fragment (§20).

## Files & Modules

- `scripts/claude_issue_route.py`
- `tests/test_claude_issue_route.py`
- `tests/test_implement_issue_claude_command.py`
- `.claude/commands/claude-issue-pickup.md` (protected; via the sync blocker)
- `README.md`
- `agents.md`
- `docs/scripts-pending-removal.md`
- `changelog.d/4990-pickup-throughput.md` [new]

## Tests

- Unit: the new `tests/test_claude_issue_route.py` cases above.
- Command text: `tests/test_implement_issue_claude_command.py` pickup assertions. They fail until the supervising session applies the pickup diff (Q40), which is also what keeps the phase PR from merging without it.
- Regression: the full `tests/test_claude_issue_route.py`, `tests/test_implement_issue_claude_command.py`, and `tests/test_claude_pr_sweep.py` suites (`claude_pr_sweep.py` calls `queue_pending` with a huge limit for its dedupe and must not change).

## Risks & Mitigations

- The catch-up `send_later` fails → the report says `catch_up=failed`; the next hourly wake drains the queue as today. ACCEPTED.
- A second catch-up piles up (a delayed catch-up still pending at the next hourly wake) → the hourly wake schedules only when no enabled `Claude issue pickup: catch-up` trigger is bound to the pickup (AD-3).
- The pickup doc change is edited concurrently by #4817 and #4910 → the step-2 sync merges `main` into the project branch at every stage, and the supervising session applies the diff against the current file.
- More binding reads with a backlog over 30 targets → bounded by the 100-item queue page and documented (AD-8).

## Rollout

Ships with the final PR into `main`. The pickup reads `.claude/commands/claude-issue-pickup.md` and the script fresh on every wake (step 0 "Fresh code"), so no restart is needed. `CLAUDE_ISSUE_PICKUP_LIMIT` is optional; setting it in the pickup session's environment lowers or raises the limit. Rollback: revert the final PR; a pending catch-up then runs one old-style wake and schedules nothing further.

## Auto-decisions

- AD-1 **Q1: How does a catch-up wake know it is one?** Picked: A — it carries `— wake. — catch-up` (the `— wake.` procedure plus a marker). Alternatives: B — the identical `— wake.` prompt, told apart by a `list_triggers` history read. Why: the issue requires that a catch-up never schedule another, and an identical prompt cannot tell the two apart without extra reads.
- AD-2 **Q2: Where is the catch-up decided?** Picked: A — the script (`queue-pending --wake`, output `catch_up_due`). Alternatives: B — the pickup model reads `remaining` itself. Why: the pickup's rule is "the script decides", and a script decision is testable.
- AD-3 **Q3: How is "one catch-up per hour" enforced?** Picked: A — schedule only when `catch_up_due` is true and no enabled `Claude issue pickup: catch-up` trigger is bound to the pickup (step 1's `list_triggers`); `stop` deletes it. Alternatives: B — schedule on every hourly wake with work left. Why: a late catch-up could otherwise overlap the next one.
- AD-4 **Q4: Does the `start` drain count as an hourly wake?** Picked: A — yes (`--wake hourly`). Alternatives: B — never schedule from `start`. Why: a restart during a backlog is exactly when the catch-up helps.
- AD-5 **Q5: How are bad `CLAUDE_ISSUE_PICKUP_LIMIT` values handled?** Picked: A — unset, empty, or non-integer → 20; out-of-range integers clamped to 1..30; an explicit `--limit` still wins; the output reports the `limit` used. Alternatives: B — fail the read on a bad value. Why: fail-open keeps the queue draining (§1 safety, the issue's "default 20, clamped").
- AD-6 **Q6: Which entries count as resumes?** Picked: A — issue entries whose `trigger` is `reclarify`; `opened`, `manual`, and `pr_fix` keep queue order after them. Alternatives: B — `pr_fix` items also go first. Why: the issue names only `reclarify` before `opened` (§5 smallest change).
- AD-7 **Q7: What does `oldest_waiting` measure?** Picked: A — minutes since the oldest `created_at` among the items grouped or deferred at read time (items started this wake included, ignored items excluded), `none` when nothing waits. Alternatives: B — only items left after this wake. Why: it shows the dead time the issue wants visible; ignored items stay open for the watchdog and would pin the value.
- AD-8 **Q8: Does the binding read window grow with the limit?** Picked: A — yes, it stays `QUEUE_BINDING_SCAN_FACTOR` × limit (60 targets at 20), with the extra reads documented. Alternatives: B — cap it at 30 targets. Why: the factor is what stops stuck items from starving bound ones; the extra reads only happen with a backlog over 30 targets.
- AD-9 **Q9: How does the phase change `.claude/commands/claude-issue-pickup.md`?** Picked: A — twin-first per Q40, as the issue body prescribes: no `workflow-templates/.claude/**` twin needs a change, the pickup diff goes in the sync blocker, and the phase stops `BLOCKED` with a `hold` claim after its PR opens. Alternatives: B — edit `.claude/**` in this unattended session; C — drop the pickup change. Why: §28.C forbids B, and C would ship the script without the catch-up wake.

## Notes

- `security_pass_skip.py` result: `{"skip": false, "label": null, "reason": "no skip label"}`.

## References

- Issue #4990; related #4910, #4985, #4911, #4817, #4785 (Q40 sunset), #4621 (queue binding), #4525 (why a pickup session).
- `docs/operations/master-session.md` (Q40 twin syncs).
