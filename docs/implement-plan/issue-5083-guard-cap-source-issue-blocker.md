# Implement-Plan Log — Unattended question guard publishes a source-issue blocker at its cap

- Plan: docs/plans/issue-5083-guard-cap-source-issue-blocker-plan.md
- Source issue: shubhodeep1/coding-workflows#5083 (https://github.com/shubhodeep1/coding-workflows/issues/5083)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-4911-unattended-question-guard
- Project branch: claude/implement-plan-issue-5083-guard-cap-source-issue-blocker   Final PR: #5087 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: operator twin sync of `.claude/hooks/unattended_question_guard.py` on the phase 1 PR (Q40), then `/reclarify`
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented twin-first (Q40): twin hook, tests, docs, changelog pushed as the phase PR with a `hold` claim; stopped BLOCKED on #5083 with the twin-sync request.

## Phases
1. [ ] Phase 1 — cap blocker publish in the unattended question guard — protected paths: `.claude/hooks/unattended_question_guard.py` (twin: `workflow-templates/.claude/hooks/unattended_question_guard.py`) — phase PR open, waiting on the twin sync; review rounds: 0; interventions: 0
   - [x] Hook (twin first): `CAP_BLOCKER_MARKER_PREFIX` / `cap_blocker_marker`, fixed template `cap_blocker_body`, `publish_cap_blocker`, `run_gh_api`, injectable `runner` / `sleep` / `clock` on `evaluate`, pending retry at the top of the `Stop` path, `read_state` / `_write_state` (merging), `cap_blocker_*` log lines, docstring
   - [x] `tests/test_unattended_question_guard.py`: 14 new cases (posted, exists, other session's marker, already posted, in-hook retry, label-only retry, pending → retried, no double retry, time budget, `gh` missing, invalid repo, state merge, runner timeout, no calls below the cap); `test_hook_makes_no_network_calls` replaced by `test_hook_network_access_is_only_the_gh_api_runner`
   - [x] CLAUDE.md §28.G (`workflow-templates/CLAUDE.md` is a symlink to it), agents.md, README.md
   - [x] `changelog.d/5083-guard-cap-source-issue-blocker.md`
   - [ ] `.claude/hooks/unattended_question_guard.py` twin sync (operator, Q40)
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
- Protected-path approval: phase 1 — twin-first per Q40 (2026-09-29) (#5083 comment 5886503268, OWNER, answered by the master session under standing decision Q40: A; option D of the #5083 blocked comment 5885866605). The phase edits only the `workflow-templates/.claude/hooks/` twin, pushes the phase PR, posts a `hold` claim, and stops BLOCKED with the twin-sync request.
- 2026-09-29 08:36Z: resumed by the master's routine `trig_01H6YF1vdFH3cuiPuJXnhjga` (replaces `/reclarify`); `ai:claude-blocked` removed; project branch synced with its base (`7a2fb7f`, clean merge; hook unchanged, sha256 `db5f32ba…`).
- Plan deviation (naming only): the plan's `CAP_BLOCKER_MARKER` shipped as `CAP_BLOCKER_MARKER_PREFIX` plus `cap_blocker_marker(session)`, because the marker carries the session id.
