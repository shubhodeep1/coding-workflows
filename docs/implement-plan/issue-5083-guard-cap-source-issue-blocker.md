# Implement-Plan Log — Unattended question guard publishes a source-issue blocker at its cap

- Plan: docs/plans/issue-5083-guard-cap-source-issue-blocker-plan.md
- Source issue: shubhodeep1/coding-workflows#5083 (https://github.com/shubhodeep1/coding-workflows/issues/5083)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-4911-unattended-question-guard
- Project branch: claude/implement-plan-issue-5083-guard-cap-source-issue-blocker   Final PR: pending (opened right after this commit; see the progress comment)
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: none (protected-path approval for phase 1, asked on #5083)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 must edit `.claude/hooks/unattended_question_guard.py` and has no `Protected-path approval` line; stopped before the phase started and asked on #5083 (CLAUDE.md §28.C).

## Phases
1. [ ] Phase 1 — cap blocker publish in the unattended question guard — protected paths: `.claude/hooks/unattended_question_guard.py` (twin: `workflow-templates/.claude/hooks/unattended_question_guard.py`)
   - [ ] Hook (twin first): `CAP_BLOCKER_MARKER`, fixed comment template, `publish_cap_blocker`, injectable `runner` / `sleep`, pending retry at the top of the `Stop` path, state and log fields, docstring
   - [ ] `tests/test_unattended_question_guard.py`: posted, exists, retry, pending → retried, label failure, `gh` missing, invalid repo, already posted
   - [ ] CLAUDE.md §28.G (+ `workflow-templates/CLAUDE.md`), agents.md, README.md
   - [ ] `changelog.d/5083-guard-cap-source-issue-blocker.md`
   - Done: whole guard test file passes with the twin synced into `.claude/`; docs describe the cap publish

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header.

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where does the notifier live? — Picked: A — a function inside `unattended_question_guard.py`. Alternatives: B — a new `.claude/scripts/` helper the hook calls. Why: one file and one twin to sync, one marker format (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Which transport does the notifier use? — Picked: A — `gh api` as a subprocess. Alternatives: B — `urllib` with `GH_TOKEN`. Why: §23.E forbids committed code that reads `GH_TOKEN` from the session; `gh` works through the proxy. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How is the publish made idempotent? — Picked: A — a per-session hidden marker in the comment, checked by one paginated read before the POST, plus a local `cap_blocker` state flag. Alternatives: B — the local state flag only. Why: the state file can be lost with the container; the issue is the source of truth. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How is a failed publish retried? — Picked: A — 3 attempts in the hook (1 s, 2 s backoff), then `pending` state retried at every later `Stop` in the session. Alternatives: B — in-hook attempts only; C — an Actions sweep. Why: bounded inside the hook timeout, and a later retry needs no new question; Actions cannot see local state. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] What does the comment contain? — Picked: A — a fixed template (session id, kind, block count, how to resume). Alternatives: B — also quote the final assistant message. Why: model text is untrusted and could carry secrets (§1). Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] Is the stop still allowed after the publish? — Picked: A — yes, as today. Alternatives: B — keep blocking until a publish succeeds. Why: an unbounded block loops the session; the blocker on the issue is the signal. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher routine (`trig_01EaRfriPigj1QtV6UR5y1Wm`) in session `session_011pj7ZGZg7yssSWeVqHH6d8`; permission mode auto.
- Security pass: skip (`security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- Session tooling: the repo was attached mid-session, so the SessionStart hook had not run and `gh` was missing; the session ran `.claude/hooks/session-start.sh` to install it. No `mcp__github__*` tools in this session: GitHub writes use REST through the agent proxy.
- Stale Routine sweep: 11 listed, 8 deleted; the Auto-mode classifier denied deleting `trig_01L96Luucx4ov6s9hiGEq7Ws`, `trig_017T43aSdHGCBcZtt3cdHBoZ`, `trig_012KUCYgapyhs2iKBEnHhBjR` (left for a later sweep).
- Progress comment: #5083 comment 5885854190.
- Protected-path approval: not recorded yet. The #4948 automatic twin-first default has not landed, so phase 1 stops to ask (master-session Q40 is offered as option D).
