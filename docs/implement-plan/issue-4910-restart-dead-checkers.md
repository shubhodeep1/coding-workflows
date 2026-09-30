# Implement-Plan Log — Restart dead /implement-plan-claude checkers from the hourly Claude issue pickup

- Plan: docs/plans/issue-4910-restart-dead-checkers-plan.md
- Source issue: shubhodeep1/coding-workflows#4910 (https://github.com/shubhodeep1/coding-workflows/issues/4910)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4910-restart-dead-checkers   Final PR: #4965 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #4984
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01PUk3sYkLpwZZR6nKcaXnx9   safety net and hand-back: see the review-round stage report (session_01NTgs5QnaUNJ2TYubSY4NBh)
- Last updated: 2026-09-30
- Last note: review round 3 (head 7f64d5a) went stale: the head moved to bf7d4fd (`[claude-merge-resolve]` from another session) before session_01CVRH1iMmUrTV87ECCnN7KV pushed, so nothing was pushed (judgement in PR comment 5903321307). Review round 4 (workflow round 1 on head bf7d4fd) fixed here: the open `ai:claude` issue list is paginated, and `_project_trigger` strips any trailing run of `…`, `.`, and spaces (AD-15: this stage took the head over the sweep's unconsumed reservation).

## Phases
1. [ ] Phase 1 — restart script, pickup step 3b, settings twin, docs   — PR #4984 open (twin sync 0e436d7 pushed, hold lifted); review rounds: 4 (round 3 stale, not pushed); interventions: 0   — protected paths: `.claude/settings.json` (edited through its `workflow-templates/.claude/` twin), `.claude/commands/claude-issue-pickup.md` (no twin; exact edit in the blocked comment)
   - `scripts/claude_checker_restart.py` [new] (`scan`, `decide`) + `tests/test_claude_checker_restart.py` [new] + `ci.yml` step
   - `settings.json` twin: allow the two subcommands and `set_session_tags`
   - pickup step 3b (restart + re-queue), step 0 tools, step 1 `limit: 100`, step 2 exits, step 4 report, Rules, Tool Access
   - `README.md`, `agents.md`, `changelog.d/4910-restart-dead-checkers.md` [new]
   - Done: new tests pass; ruff clean; existing pickup and settings contract tests pass on the synced copy

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where does the decision script live? — Picked: A — a new `scripts/claude_checker_restart.py`, coding-workflows-only. Alternatives: B — `.claude/scripts/` plus a twin; C — a `claude_issue_route.py` subcommand. Why: no protected path, and the tests run before the sync. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Which `list_sessions` page? — Picked: A — the newest page every wake. Alternatives: B — a cursor walk; C — several pages. Why: condition 4 needs every session created in the last 90 minutes. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How are checkers older than the page found? — Picked: A — the ids named by pending safety-net prompts and by the logs of open, non-blocked `ai:claude` issues, looked up with `get_session` (at most 8 per wake, rotated hourly). Alternatives: B — page titles only; C — a cursor. Why: covers the die-in-a-wait case and the missing-checker addition. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Condition 5 marker? — Picked: A — a session tag `ai-checker-restart:<YYYYMMDDTHHMMZ>` on the checker. Alternatives: B — the fired restart trigger (swept); C — an issue comment. Why: durable, free to read, scoped to the checker. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Which checker states can be restarted? — Picked: A — `SESSION_STATUS_IDLE` only, not `need_input`, bucket not `BLOCKED`. Alternatives: B — also `RUNNING`. Why: §1. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] Which sessions belong to the project? — Picked: A — the operator's lineage, branch and title rules plus the issue-start titles. Alternatives: B — the operator's rules only. Why: an extra match only prevents a restart. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] Sibling checker alive? — Picked: A — skip the dead one. Alternatives: B — restart both. Why: it is a zombie. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-29] `list_triggers` has more? — Picked: A — restart nothing that wake. Alternatives: B — proceed. Why: condition 1 cannot be proven. Applied in: phase 1. Status: pending review
- AD-9 [plan, 2026-09-29] How is an issue re-queued? — Picked: A — a `/reclarify` comment with a hidden `ai:claude-checker-requeue:v1` marker. Alternatives: B — dispatch `claude-issue-intake.yml`; C — write a queue item. Why: the operator's words; no binding or helper change. Applied in: phase 1. Status: pending review
- AD-10 [plan, 2026-09-29] What counts toward once per 24 hours? — Picked: A — any trusted `/reclarify` in the last 24 hours. Alternatives: B — only the pickup's marker comments. Why: a human `/reclarify` already resumed it. Applied in: phase 1. Status: pending review
- AD-11 [plan, 2026-09-29] Missing-checker scan scope? — Picked: A — this repository's open, non-blocked `ai:claude` issues with an unfinished log and no active project session. Alternatives: B — consumer repos too. Why: only readable projects. Applied in: phase 1. Status: pending review
- AD-12 [plan, 2026-09-29] How are logs read? — Picked: A — `git ls-remote` plus one shallow `git fetch`. Alternatives: B — REST contents per issue. Why: no API calls. Applied in: phase 1. Status: pending review
- AD-13 [plan, 2026-09-29] When does step 3b run? — Picked: A — every wake and `start`, even with an empty or unreadable queue. Alternatives: B — only after a good queue read. Why: a queue outage must not stop restarts. Applied in: phase 1. Status: pending review
- AD-14 [plan, 2026-09-29] How does phase 1 edit `.claude/**`? — Picked: A — twin-first per Q40, as the issue says. Alternatives: B — direct edits. Why: the operator's standing rule. Applied in: phase 1. Status: pending review
- AD-15 [phase 1/1 — review round, 2026-09-30] PR #4984's head bf7d4fd is held only by the catch-all sweep's reservation (`sweep-run-36661062838`, queue item #5388 not yet picked up); does this review-round stage fix it? — Picked: A — claim the head as this session and fix the round; the pickup's `/fix-claude-pr` session then sees a live claim by someone else and stands down. Alternatives: B — defer to the reservation, arm the wait, and end. Why: the chain is the PR's own fixer, the previous stage already deferred once, and the checker keeps starting review-round stages while the reservation sits unconsumed. Applied in: PR #4984. Status: pending review

## Lessons
- [source:intervention] A fail-safe loop that skips an item on a read failure must still list the failure in `errors[]` with the item it concerned, including the failure of any follow-up check used to classify it; a silent `continue` hides the miss from the report. (files: scripts/claude_checker_restart.py)
- [source:intervention] Routine names in `list_triggers` come both whole (over 60 characters) and cut with a trailing `…`; name matchers should accept a full-name prefix and a cut stem with or without a marker. (files: scripts/claude_checker_restart.py)
- [source:intervention] A stage session that runs past the 3-hour claim lease loses its head to the catch-all sweep's fixer; re-read the PR head before pushing, and never push a duplicate round over a newer head. (files: .claude/commands/implement-plan-claude.md)
- [source:intervention] A listing read that feeds a "which items to check" set must paginate; a single `per_page=100` page silently drops every item past the first page. (files: scripts/claude_checker_restart.py)

## Notes
- Protected-path approval: phase 1 — twin-first per Q40 (issue #4910 body: "Protected paths: `.claude/**`, so use the interim twin-first rule (Q40: A)", 2026-09-29). `.claude/settings.json` changes through its twin; the `claude-issue-pickup.md` diff goes in the blocked comment; the phase PR carries a `hold` claim and the stage stops BLOCKED for the supervising session's `[claude-twin-sync]`.
- #4887's pickup step 3a reads its own `list_sessions` cursor page; this step needs the newest page (plan Notes).
- Q1: A (2026-09-29, master session `session_0199TGCqNCt5BJNmyeqrYpSp`, OWNER comment on #4910): the `.claude/` part landed under the interim rule as `[claude-twin-sync]` 0e436d7 (settings sha256 `930f7292…`, pickup sha256 `2ccbd45f…`); its wake message stood in for `/reclarify`. `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1` is passed to every `check_in_status.py` call and checker prompt (#5057).
