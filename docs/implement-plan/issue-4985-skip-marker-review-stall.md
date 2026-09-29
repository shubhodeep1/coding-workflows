# Implement-Plan Log — Honour only an intentional skip-AI marker, log every gate skip, and hand stalled claude/* reviews to a fixer

- Plan: docs/plans/issue-4985-skip-marker-review-stall-plan.md
- Source issue: shubhodeep1/coding-workflows#4985 (https://github.com/shubhodeep1/coding-workflows/issues/4985)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4985-skip-marker-review-stall   Final PR: draft (opened right after this commit; number in the issue progress comment)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened; implementing phase 1 under the interim twin-first rule (Q40).

## Phases
1. [ ] Phase 1 — intentional-marker rule, gate skip log and comment, review-stall detection and fixer re-dispatch   — protected paths: `.claude/scripts/check_in_status.py`, `.claude/scripts/dispatch_workflow.py`, `.claude/settings.json`, `.claude/commands/fix-claude-pr.md` (all edited through their `workflow-templates/.claude/` twins)
   - gate: marker rule, `AUTOFIX_GATE_SKIP` line on every skip, one `claude/*` skip comment per head (`.github/workflows/review_autofix.yml`)
   - sweeps: same rule in `.github/workflows/review_autofix_sweep.yml` and `scripts/claude_pr_sweep.py`; `review-stalled` is due; `CLAUDE_REVIEW_STALL_HOURS` in the catch-all env
   - checker twin: `has_skip_ai_marker`, `review-stalled` → `hand_back_fixer`, `stall_redispatched`
   - fixer twin: `review-stalled` → claim `review`, re-dispatch `internal-review.yml` / `ai-review.yml`; dispatch helper and settings twins allow `internal-review.yml`
   - tests: `tests/test_skip_ai_marker_rule.py` [new], hand-back, sweep, dispatch parity
   - docs: CLAUDE.md (+ template), README.md, agents.md, `changelog.d/4985-skip-marker-review-stall.md`
   - Done: new and updated tests pass against the twins; ruff and yamllint clean; `review_autofix.yml` < 480,000 bytes; stage stops BLOCKED for `[claude-twin-sync]`

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How does phase 1 change `.claude/**`? — Picked: A — twin-first per Q40, as the issue says. Alternatives: B — edit `.claude/**` directly. Why: the operator's standing rule and the issue text. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What exactly counts as an intentional body marker? — Picked: A — outside ``` / ~~~ fences, a line matching `^ {0,3}\[skip ai\][ \t]*$`; the title keeps substring matching. Alternatives: B — also list items / headings; C — title only. Why: the issue's "a body line that consists of the marker alone". Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Do the two sweeps adopt the same rule? — Picked: A — yes, parity-tested. Alternatives: B — gate only. Why: the §26.H catch-all skipped #4807 too. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Add `ai:review-skipped` to a marker-skipped PR? — Picked: A — no; the comment marker is the signal. Alternatives: B — add it. Why: the label contract is the deterministic doc/size skip. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Comment on drafts? — Picked: A — no; drafts get the log line and are never stalled. Alternatives: B — comment on drafts too. Why: the draft final PR would collect one comment per phase merge. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] Where do the skip log and comment live? — Picked: A — inline at the end of the gate step. Alternatives: B — a new step script. Why: the gate job has no support checkout; the file stays < 460,000 bytes. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] What proves a head was reviewed or deliberately not reviewed? — Picked: A — hand-off for the head, `auto_merge`, gate-skip comment, marker, draft, `ai:merge-queued`, active run. Alternatives: B — also check-run names. Why: no new read types. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-29] What starts the stall clock? — Picked: A — the head commit's committer date, `CLAUDE_REVIEW_STALL_HOURS` default 2. Alternatives: B — PR `updated_at`. Why: `updated_at` moves on every comment. Applied in: phase 1. Status: pending review
- AD-9 [plan, 2026-09-29] Which claim kind does `review-stalled` use? — Picked: A — `review`. Alternatives: B — a new `stall` kind. Why: no claim or queue payload change. Applied in: phase 1. Status: pending review
- AD-10 [plan, 2026-09-29] How are repeated re-dispatches bounded? — Picked: A — `stall_redispatched`; the fixer holds instead of dispatching again. Alternatives: B — no guard. Why: a traceless skip would loop every lease period. Applied in: phase 1. Status: pending review
- AD-11 [plan, 2026-09-29] Which workflow does the fixer re-dispatch? — Picked: A — `internal-review.yml` here (allowlisted in the twins), `ai-review.yml` in consumers. Alternatives: B — `review_autofix.yml`. Why: only `internal-review.yml` runs are bound to the PR by title. Applied in: phase 1. Status: pending review
- AD-12 [plan, 2026-09-29] Does plain `--pr` mode also report stalls? — Picked: A — no, hand-back mode only. Alternatives: B — plain mode too. Why: §5; the catch-all covers implement-plan PRs. Applied in: phase 1. Status: pending review
- AD-13 [plan, 2026-09-29] Log every skip even with an earlier `AUTOFIX_GATE_SKIP` line? — Picked: A — yes, one uniform end-of-gate line. Alternatives: B — only when none was logged. Why: one searchable line per run. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Protected-path approval: phase 1 — twin-first per Q40 (issue #4985 body, owner-authored: "In `check_in_status.py` (edit the `workflow-templates/.claude/**` twin first, Q40)", 2026-09-29). The phase edits only the `workflow-templates/.claude/` twins, pushes, posts a `hold` claim, and stops BLOCKED listing the files to copy; the `settings.json` twin needs the operator's approval window (Q62/Q64).
- The session started before the repository was cloned, so the SessionStart hook had not run; `bash .claude/hooks/session-start.sh` installed `gh` (2.101.0).
- Issue progress comment: 5883835523.
