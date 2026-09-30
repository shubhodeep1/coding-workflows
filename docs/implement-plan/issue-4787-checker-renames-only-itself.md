# Implement-Plan Log — §26 checker: rename and archive only the right session

- Plan: docs/completed/issue-4787-checker-renames-only-itself-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4787
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4787-checker-renames-only-itself   Final PR: #4797 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation
- Waiting on: completion PR (claude/implement-plan-issue-4787-checker-renames-only-itself-complete → the project branch)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01MMNEf5XMfHgN5BQ8EwUveg (reused, AD-10)   safety net / hand-back: see the validation 1/3 read-result stage report
- Last updated: 2026-09-29
- Last note: validation cycle 1 (run 36521732268, target_ref = the project branch) passed 10/10; no validation-fix PR, so no conformance re-run. Plan moved to docs/completed/ in the completion PR; next is final-merge (mark final PR #4797 ready).

## Phases
1. [x] Phase 1 — session-targeting rules for §26 checkers and fixers   — PR #4828 merged 2026-09-28 (merge commit 39d53a4; merged by the supervising session under the operator's Q1: A on #4787 after round 3 left no valid finding); review rounds: 3; interventions: 0 — protected paths: `.claude/commands/implement-plan-claude.md`, `.claude/commands/fix-claude-pr.md`, `.claude/commands/claude-issue-pickup.md`, `workflow-templates/.claude/commands/implement-plan-claude.md`, `workflow-templates/.claude/commands/fix-claude-pr.md`
   - CLAUDE.md §26.B step 3, §26.C step 5, §26.D: own-id line, never target a subscriber, §26.D title check via `get_session`, archive reported only on success, delivered/gone rule, re-check before a fresh fixer
   - `/implement-plan-claude` (+ twin): checker prompt step 4b not-found rule, checker renames/archives no session, title check before archiving the project checker
   - `/fix-claude-pr` (+ twin): step 2, step 8, Rules bullet
   - `/claude-issue-pickup` step 5.3: archive only the id `create_session` returned
   - `agents.md` check-in section; `tests/test_check_in_session_targeting.py` [new] wired into `ci.yml`; `changelog.d/4787-checker-renames-only-itself.md` [new]
   - Done: every plan goal present in the text; new test, twin-parity tests, and `tests/test_claude_md_section_numbers.py` pass

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — no fixes (pre-security)

## Security pass
- Cycle 1 — run 36512923727 2026-09-29 (ref: claude/implement-plan-issue-4787-checker-renames-only-itself at aec06cc): clean — findings=0, followups_created=0. Run 36512928347, first recorded for this cycle, was issue-4813's audit, returned by the dispatch helper's run-matching race (#5016, AD-11).

## Validation
- Cycle 1 — run 36521732268 2026-09-29 (target_ref: claude/implement-plan-issue-4787-checker-renames-only-itself): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 291s); no fix PR

## Completion
- Completion PR (branch claude/implement-plan-issue-4787-checker-renames-only-itself-complete) open — doc moved to docs/completed/issue-4787-checker-renames-only-itself-plan.md
- Final PR #4797 draft

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Should the §26 checker archive itself after the terminal hand-back? — Picked: A — no; it renames only its own id and never calls `archive_session`; the fixer archives it in §26.D after the title check. Alternatives: B — the checker archives its own id as its last call. Why: self-archiving cuts the turn off mid-tool-call (rule pinned by `test_checker_does_not_archive_itself`). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How should §26.C step 5 treat a hand-back Routine that `get_trigger` cannot find? — Picked: A — `get_session` the subscriber; archived or not found → gone, otherwise delivered. Alternatives: B — keep fired hand-backs in `stale_routines.py` for 24 hours; C — keep "not found → gone". Why: owner comment asks for A; the sweep deletes fired hand-backs by design. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Should `/implement-plan-claude` checker step 4b get the same not-found rule? — Picked: A — yes. Alternatives: B — leave it. Why: same `get_trigger` logic; a false "gone" starts a duplicate block stage. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] What does the re-check before a fresh fixer require? — Picked: A — start it only when a fresh `check_in_status.py --hand-back` still returns `action: hand_back_fixer`. Alternatives: B — skip only on `state: claimed`. Why: also covers a hold, a moved head, and a terminal PR at the same cost. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] Which checker titles pass the §26.D title check? — Picked: A — exactly `PR #<n> status check-in`, or starting `PR #<n> merged — handed to ` / `PR #<n> closed — handed to `, not archived, not this session. Alternatives: B — only the exact initial title. Why: the issue allows the terminal title. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-28] Should `scripts/claude_issue_route.py`'s `ready_prompt` repeat the rule? — Picked: A — no; it points to §26.B steps 3–4, which will carry it. Alternatives: B — append the rule text. Why: §5 minimal change. Applied in: no code change. Status: pending review
- AD-7 [plan, 2026-09-28] Should `.claude/commands/claude-issue-dispatch.md` change too? — Picked: A — no. Alternatives: B — add the same wording. Why: it already archives only the session it just created; the issue names three other commands. Applied in: no code change. Status: pending review
- AD-8 [plan, 2026-09-28] Where does the new test run in CI? — Picked: A — a new file in the existing `ci.yml` step "Claude-fixer hand-back, claim and catch-all sweep tests (CLAUDE.md §26.C, §26.H)". Alternatives: B — extend `tests/test_implement_plan_claude_command.py` only; C — a new step. Why: the rules span CLAUDE.md and three commands, and that step covers §26.C. Applied in: phase 1 PR. Status: pending review
- AD-9 [phase 1/1, 2026-09-28] Where does `/implement-plan-claude` put the title check before it archives the project checker? — Picked: A — one new subsection, "Archiving the project checker", linked from Arming the wait step 1, step 12 LIVE, and step 13, with the existing sentences kept word for word. Alternatives: B — repeat the full check inline at all three points. Why: one rule in one place, and `tests/test_implement_plan_claude_command.py` pins the existing sentences. Applied in: PR #4828. Status: pending review
- AD-10 [conformance 1/3, 2026-09-29] Reuse the recorded project checker even though its title now reads `#4787 · PR #4797 — implement-plan issue-4787-checker-renames-only-itself — checker` instead of the exact `implement-plan <slug> — checker`? — Picked: A — yes, reuse session_01MMNEf5XMfHgN5BQ8EwUveg. Alternatives: B — create a new project checker and archive the old one. Why: the prefix is a display label added outside the chain; the session is the recorded checker, not archived, not failed, and still carries the slug and `— checker`, and a new checker would add a parent link. Applied in: no code change. Status: pending review
- AD-11 [security-pass 1/5, 2026-09-29] File the dispatch_workflow.py run-matching race as a separate issue? — Picked: A — yes, filed #5016. Alternatives: B — record in the log only. Why: a wrong run id can apply another project's clean verdict (§1); the command's Rules route separate fixes through an issue. Applied in: no code change. Status: pending review

## Lessons
- [source:intervention] When a paragraph says it restates another section's rules for a context-free session (a checker prompt), list every rule and pin each one in the contract test, because a partial restatement reads as the complete rule set. (files: CLAUDE.md, tests/test_check_in_session_targeting.py)
- [source:plan-deviation] When an instruction file's existing sentences are pinned by a contract test, add a new rule as a linked subsection or an appended sentence instead of rewording the pinned sentence. (files: .claude/commands/implement-plan-claude.md, tests/test_implement_plan_claude_command.py)
- [source:security] After dispatching a workflow, confirm the returned run's inputs (ref / target_ref) name this project before trusting its verdict, because a concurrent dispatch of the same workflow can be returned as this project's run. (files: .claude/scripts/dispatch_workflow.py)
- [source:intervention] When a prompt opens with a blanket "never call X", name the exceptions its own later steps need (a cleanup call), because a low-effort session follows the blanket rule and skips the cleanup. (files: .claude/commands/implement-plan-claude.md, workflow-templates/.claude/commands/implement-plan-claude.md)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher (trigger `dispatch shubhodeep1/coding-workflows#4787: start`) in session session_01SDx7S3Hw7kHnJLkiWzrJ9z; permission mode auto.
- Security pass: run (`security_pass_skip.py`: no skip label).
- Blocked before phase 1 (CLAUDE.md §28.C protected paths): no `Protected-path approval: phase 1` line. The question is posted on issue #4787 (`<!-- ai:claude-blocked:v1 -->`). The next session records the answer here as `Protected-path approval: phase 1 — <letter> (<date>)` and resumes.
- Protected-path approval: phase 1 — A (twin-first, 2026-09-28). Operator decision on issue #4787 (the operator's standing Q40: A), until #4785 lands: the phase edits only the `workflow-templates/.claude/commands/**` twins, never `.claude/**`; the `claude-issue-pickup.md` edit (no twin) is listed in the blocked comment; the phase PR gets a hold claim; the supervising session copies the twins into `.claude/commands/**` as `[claude-twin-sync]`, runs the tests, pushes (lifting the hold), and posts `/reclarify`.
- Resumed 2026-09-28 by session session_01C6qsveeGqNgUq9H16mCNUC (trigger `dispatch shubhodeep1/coding-workflows#4787: start`); permission mode auto. Project branch synced with `main` (706e1b7).
- Expected red until the twin sync: `tests/test_check_in_session_targeting.py` (the `.claude/commands` cases and twin parity), `tests/test_implement_plan_claude_command.py::test_template_parity`, and any other twin-parity check. With the twins copied and the pickup edit applied, the related suites pass locally (409 passed).
- Twin sync landed as 6704437 (`[claude-twin-sync]`), which lifted the hold; review round 1 handed off on that head.
- Review round 2 (2026-09-28): the fix to the checker prompt intro is in `workflow-templates/.claude/commands/implement-plan-claude.md` only (twin-first, #4785 still open). Hold claim posted on the round 2 head; `ai:claude-blocked` comment on #4787 lists the one `cp` for the supervising session. Before the sync, only the five `implement-plan-claude.md` twin-parity cases fail; with the twin copied, the related suites pass locally (453 passed).
- Review round 3 (2026-09-28) left no valid finding; with no `CLAUDE_FIXER_VERDICT_BOT_LOGIN` configured the chain could not post the verdict, so it stopped BLOCKED (#4787 comment 5875449511). The operator answered Q1: A and the supervising session merged #4828 into the project branch on 2026-09-28 (comment 5880813843). The same blocker can recur on final PR #4797.
- 2026-09-29: conformance 1/3 CONFORMANT (no fix PR); security cycle 1 clean (run 36512923727); validation cycle 1 pass (run 36521732268). Project branch synced with `main` at 0cecf02 before the completion PR.
