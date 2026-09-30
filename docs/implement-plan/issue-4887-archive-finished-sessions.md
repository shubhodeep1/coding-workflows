# Implement-Plan Log — Archive finished fixer, issue-start and report sessions automatically

- Plan: docs/plans/issue-4887-archive-finished-sessions-plan.md
- Source issue: shubhodeep1/coding-workflows#4887 (https://github.com/shubhodeep1/coding-workflows/issues/4887)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4887-archive-finished-sessions   Final PR: #4924 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3
- Activation: not started
- Waiting on: conformance fix PR (branch claude/implement-plan-issue-4887-archive-finished-sessions-conformance-fix-1; the number is in the issue progress comment and the resume block)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_019gAFuNuVjyEnPRhyvMWZzG   safety net and hand-back: re-armed by the conformance 1/3 stage (ids in the issue progress comment)
- Last updated: 2026-09-30
- Last note: conformance 1/3 (session_01WegsEYLPeefw1d3bEFB4uB, the /reclarify dispatch after the operator merged PR #4937 by hand): project branch synced with main (1c09c53, [claude-merge-resolve], both sides kept); verdict CONFORMANT (Implemented COMPLETE, Correctness CONCERNS); 2 EVIDENCE-BASED CONCERNs fixed in the conformance fix PR (test coverage of the #4886 titles, agents.md archiving sentence); next: conformance 2/3 re-audits the merged fixes.

## Phases
1. [x] Phase 1 — session sweep, fixer self-archive, docs   — PR #4937 merged 2026-09-30 by the operator's master session (947de10, bound to 656fd7f; issue #4887 Q1: B); review rounds: 3 (b7443b5: all rejected; e0d90bb: 2 nits fixed in 529ead3, 4 rejected; 656fd7f: 4 rejected, verdict unposted); twin syncs e0d90bb, 656fd7f; interventions: 0 — protected paths: `.claude/commands/fix-claude-pr.md`, `.claude/settings.json` (edited through their `workflow-templates/.claude/` twins), `.claude/commands/claude-issue-pickup.md` (no twin; exact edit in the blocked comment)
   - `scripts/claude_session_janitor.py` [new] + `tests/test_claude_session_janitor.py` [new] + `ci.yml` step
   - `fix-claude-pr.md` twin: terminal hand-back archives the fixer; step 8 wording
   - `settings.json` twin: allow `scripts/claude_session_janitor.py`
   - pickup step 3a (sweep), step 0 tools, step 4 report, Rules, Tool Access
   - CLAUDE.md §26.D + new §26.I; `README.md`, `agents.md`; `changelog.d/4887-archive-finished-sessions.md` [new]
   - Done: janitor tests pass; instruction-text tests pass with the twins and the pickup edit applied; ruff clean; section-number test passes

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (Implemented COMPLETE, Correctness CONCERNS) — conformance fix PR from claude/implement-plan-issue-4887-archive-finished-sessions-conformance-fix-1 (pre-security): 2 EVIDENCE-BASED CONCERNs fixed (untested #4886 session titles in tests/test_claude_session_janitor.py; stale "Only the chain archives its own sessions" in agents.md:876); 1 not fixed (AD-12); 1 HYPOTHESIS kept (see Notes)

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where does the sweep's decision script live? — Picked: A — a new `scripts/claude_session_janitor.py`, coding-workflows-only like `scripts/claude_issue_route.py`. Alternatives: B — `.claude/scripts/stale_sessions.py` plus a twin; C — a subcommand of `scripts/claude_issue_route.py`. Why: no protected path, no consumer copy, tests run before the sync. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] How does one page per wake reach older sessions? — Picked: A — a `next_after_id` cursor the pickup passes as `after_id` next wake, reset at the end or a 30-day horizon. Alternatives: B — newest page only; C — every page every wake. Why: keeps the §15 budget and reaches 7-day-old sessions. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Which waiting sessions are left alone? — Picked: A — `RUNNING` / `REQUIRES_ACTION` always; `need_input` keeps only report sessions. Alternatives: B — protect only `REQUIRES_ACTION`; C — protect every `need_input`. Why: reconciles the decision with the acceptance line. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] What is "superseded"? — Picked: A — a later non-checker `implement-plan issue-<N>-…` stage session for the same repo and issue on the page. Alternatives: B — any newer session for the issue. Why: a stood-down duplicate dispatch must not archive the active session; #4817 owns replacement. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] How does the sweep avoid racing the checker's terminal hand-back? — Picked: A — a 2-hour grace. Alternatives: B — a `list_triggers` Routine guard; C — 24 hours. Why: the fixer archives itself on the hand-back; the sweep catches the rest. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] How is the report's 7 days measured? — Picked: A — session `updated_at`. Alternatives: B — PR merged/closed time over REST. Why: no API call, and use of the session restarts the week. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] What does a fixer do on a terminal hand-back? — Picked: A — bookkeeping, a one-line reply, self-archive; no report. Alternatives: B — keep the report, sweep after 7 days. Why: the issue's shape and acceptance. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-29] Archive straight from the list? — Picked: A — no, `get_session` first, archive only if still IDLE with the same title. Alternatives: B — archive directly. Why: §1. Applied in: phase 1. Status: pending review
- AD-9 [phase 1/1 — review round, 2026-09-29] The step 2 sync made PR #4937 conflict with its base. Should this stage resolve it? — Picked: A — resolve it in this stage, keep both sides, and leave the root `.claude/` file for the twin sync. Alternatives: B — skip the sync this stage; C — push the autofix only and leave the conflict to a conflict round. Why: one twin sync instead of two cycles; B only defers the same protected-path conflict. Applied in: PR #4937 (b2b98b6). Status: pending review
- AD-10 [phase 1/1 — review round, 2026-09-29] `/implement-issue-claude` step 4 says to stop as "already in progress" when the recorded checker exists and the log says `IN_PROGRESS`; the checker had no pending check-in, the log lagged the resolved blocker, and a review round had been due since 12:53 UTC with no claim. Should this session resume? — Picked: A — resume as the `phase 1/1 — review round` stage, reusing the checker. Alternatives: B — stop as "already in progress", leaving the PR to the §26.H sweep, whose fixer hits the same verdict-bot wall. Why: the blocker said the `/reclarify` stage takes over, and nothing else was waiting. Applied in: no code change. Status: pending review
- AD-11 [conformance 1/3, 2026-09-30] The same step 4 rule again: the recorded checker session_019gAFuNuVjyEnPRhyvMWZzG was idle with no enabled trigger, the log still read `IN_PROGRESS` / `phase 1/1`, and the operator's `/reclarify` comment (5904025818) said PR #4937 was merged by hand and to continue with `conformance 1/3`. Should this session resume? — Picked: A — resume as the `conformance 1/3` stage in this session, reusing the checker. Alternatives: B — stop as "already in progress" (no session would ever run the stage). Why: the operator's instruction, and no live wait existed. Applied in: no code change. Status: pending review
- AD-12 [conformance 1/3, 2026-09-30] The audit found `.claude/commands/implement-plan-claude.md:161` ("Only the chain archives its own sessions") and `:420` ("Never archive a session that waits on the user") stale in the same way as `agents.md:876`, now that the §26.I sweep archives a superseded issue-start session. Fix them now? — Picked: B — fix `agents.md` (the operator-facing sentence) in the conformance fix PR and leave the two stage-facing lines, listing them in the final PR body. Alternatives: A — also edit the `workflow-templates/.claude/` twin, which under the interim twin-first rule stops the project for one more `[claude-twin-sync]`; C — leave all three. Why: the two lines change no stage behaviour, CLAUDE.md §26.I states the rule, and a twin-sync stop for a doc clause costs an operator cycle. Applied in: conformance fix PR (agents.md only). Status: pending review

## Lessons
- [source:conformance] A sweep that matches session titles must pin the title forms the creating commands produce, with a test that reads those command files, so a title change there cannot silently take sessions out of the sweep. (files: tests/test_claude_session_janitor.py, .claude/commands/claude-issue-dispatch.md, .claude/commands/fix-claude-pr.md)
- [source:conformance] When a new mechanism archives or deletes sessions from outside the chain, update every doc that says only the chain does (agents.md, the implement-plan command) in the same change. (files: agents.md, .claude/commands/implement-plan-claude.md)

## Notes
- 2026-09-30 conformance 1/3: step 2 sync `1c09c53` merged main into the project branch. It conflicted in `.claude/commands/claude-issue-pickup.md` (#4990's catch-up wake and `send_later` vs this project's step 3a sweep and `list_sessions`) and in `fix-claude-pr.md` and its twin (#4886's `#<I> · ` title prefix vs this project's step 8 archive wording). Both sides kept: step 3a sits before main's `Catch-up, then report` step 4, the report line keeps `oldest_waiting`/`catch_up` and adds `archived <a> (next …)`, the twins stay byte-identical, and the #4990 test that pins the report line now includes the archived part. Auto mode allowed the root `.claude/` merge edits, so no twin-sync stop was needed. Checks on the merged tree: 514 + 855 related tests passed, CI ruff selection clean, inventory parity OK.
- 2026-09-30 conformance 1/3 audit: HYPOTHESIS, not fixed: `_REPO` in `scripts/claude_session_janitor.py` accepts `.`/`..` path segments from a session title, so a crafted title could point the read-only `gh api` GET at another api.github.com path. Only sessions in the operator's own account set titles, and the read is a GET; unverified whether `gh` or GitHub normalises the path.
- 2026-09-30 container note: `tests/test_implement_post_codex_recovery.py` fails here only because `gawk` is not installed (`scripts/review_issue_ledger.sh:572`); unrelated to this project.
- 2026-09-29 twin sync: the supervising session copied `fix-claude-pr.md` and `settings.json` byte-for-byte from their twins and wrote its own `claude-issue-pickup.md` step 3a (the blocker comment was never posted: this session went idle at 02:26 UTC before posting it). Its step 3a matches the plan and additionally carries `next_after_id` in the one-line report so the cursor survives summarisation; 924 related tests pass on e0d90bb.
- 2026-09-29 phase 1 evidence: tests/test_claude_session_janitor.py 83 passed (pickup-text test awaits sync); 22 related suites on the unsynced tree fail only the expected parity/pickup tests; with the twins copied and the pickup diff applied, 1346 passed, 0 failed; inventory parity OK; ruff and yamllint clean; live dry run on the newest 100 sessions names 3 closed-issue issue-start sessions (#4881, #4688, #4857), no errors.
- Interim twin-first rule (#4750 Q40: A, restated in #4887): `.claude/**` changes land through `workflow-templates/.claude/**` twins; the phase PR carries a `hold` claim and the stage stops BLOCKED for the supervising session's `[claude-twin-sync]`.
- Overlap: `docs/plans/claude-fixer-unattended-convergence-plan.md` phase 4 (not started) plans `.claude/scripts/stale_sessions.py`; it should extend `scripts/claude_session_janitor.py` instead of adding a second janitor.
