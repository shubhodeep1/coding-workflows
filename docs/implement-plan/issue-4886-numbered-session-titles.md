# Implement-Plan Log — Name every automation session with its issue and PR number first

- Plan: docs/plans/issue-4886-numbered-session-titles-plan.md
- Source issue: shubhodeep1/coding-workflows#4886 (https://github.com/shubhodeep1/coding-workflows/issues/4886)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4886-numbered-session-titles   Final PR: #4943 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5009
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01Jerzz1RypxcPKdHChsnJMZ   safety net trig_01CpH8dvnvHoXAkn952XrZDh   hand-back trig_01QNPAeL7Fe34pSyAy7C68w1
- Last updated: 2026-09-29
- Last note: review round 1 (head d0520f7): one consensus finding (§26.D merged-report title lacked the issue-prefix note) fixed in CLAUDE.md and pinned in tests/test_session_titles.py; waiting on the next review round.

## Phases
1. [ ] Phase 1 — numbered session titles and dual-form matchers   — PR #5009 open (waiting); review rounds: 1; interventions: 0 — protected paths: `.claude/commands/implement-plan-claude.md`, `.claude/commands/implement-issue-claude.md`, `.claude/commands/claude-issue-dispatch.md`, `.claude/commands/fix-claude-pr.md` (edited through their `workflow-templates/.claude/commands/` twins), `.claude/commands/claude-issue-pickup.md` (no twin; the edit is listed for the supervising session)
   - `/implement-plan-claude` twin: `### Session titles`, stage, checker, deploy-activate, and hand-back titles, rename on PR open (steps 3a, 6), dual-form reuse check and zombie cleanup, three titles in the checker prompt
   - `/claude-issue-dispatch` twin and the pickup: `#<N> · issue <repo>#<N> — implement`
   - `/fix-claude-pr` twin: issue from the head ref, fresh-fixer rename after the claim, step 8 prefix
   - `/implement-issue-claude` twin: step 8 title note
   - CLAUDE.md §26.B steps 1b and 3, §26.C step 5, §26.D; `agents.md`
   - `tests/test_session_titles.py` [new] wired into `ci.yml`; `tests/test_implement_plan_claude_command.py` pinned strings; `changelog.d/4886-numbered-session-titles.md` [new]
   - Done: every plan goal present; new tests pass against the twins; with the twins copied and the pickup edit applied, the related suites pass; ruff clean

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which PR does a session that has no PR of its own show? — Picked: A — the project's final (integration) PR in project mode; omitted in legacy mode. Alternatives: B — omit it until the session opens its own PR. Why: every project session then shows a PR from the start, and the issue already puts the integration PR on the checker. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How does the checker title the stage it starts? — Picked: A — the arming stage fills three complete titles (success, review round, block) into the checker prompt, and the checker copies one. Alternatives: B — the checker composes the title from rules. Why: the low-effort checker never interprets (PR #4596). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] What does a rename do with an existing prefix? — Picked: A — strip a leading `#<n> · ` and `PR #<m> — ` before adding the new parts. Alternatives: B — prepend the new parts. Why: B stacks prefixes on every rename. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How does `/fix-claude-pr` know the issue? — Picked: A — from a `claude/implement-plan-issue-<I>-…` head ref only. Alternatives: B — also parse `Refs`/`Fixes #` in the PR body. Why: the ref is deterministic, while PR text is untrusted and can name several issues. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Does the pickup's fixer title gain the issue? — Picked: A — no; `PR <repo>#<N> — fix <kind>` stays, and the fixer adds `#<I> · ` after its claim. Alternatives: B — add the issue to the `claude_pr_fix.v1` payload. Why: the pickup never reads PRs, and a payload change widens the scope. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] Do the §26 checker's titles gain the issue? — Picked: A — no; `PR #<n> status check-in` and its `… — handed to …` renames stay, and only fixer and §26.D report titles gain `#<issue> · `. Alternatives: B — prefix the checker's terminal renames too. Why: §26.B step 1b matches the checker exactly (Q60), and the issue scopes the prefix to fixer and report titles. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] How do matchers accept both forms? — Picked: A — the title contains `implement-plan <slug> — checker` (or `— waiting:`), for the zombie cleanup and the reuse check alike. Alternatives: B — a strict optional-prefix pattern. Why: this is the issue's rule, and it also matches hand-prefixed titles. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] Where do the new tests live? — Picked: A — a new `tests/test_session_titles.py` that loads the twins, CLAUDE.md, and the pickup, wired into the existing `/implement-plan-claude` contract CI step. Alternatives: B — extend the existing `.claude`-reading suites. Why: the twin leads under twin-first, and one file keeps every title rule together. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-29] How does the twinless `claude-issue-pickup.md` change? — Picked: A — the exact edit is listed in the blocker comment for the supervising session. Alternatives: B — leave the pickup unchanged. Why: #4787 precedent, and the issue lists the pickup. Applied in: phase 1 PR (listed edit). Status: pending review
- AD-10 [plan, 2026-09-29] Does `checker_title` in `scripts/claude_issue_route.py` change? — Picked: A — no. It is the §26 checker title, looked up exactly only by §26.B step 1b, and that title is unchanged. Alternatives: B — accept both forms in a new lookup. Why: nothing looks it up by a changed title. Applied in: no code change. Status: pending review
- AD-11 [phase 1/1, 2026-09-29] Push the progress-log update (Status IN_PROGRESS, Waiting on PR #5009, twin syncs 72273b8 + d0520f7 verified: 363 passed) to the phase branch before the review runs? — Picked: A — defer it to the next stage's commit. Alternatives: B — push now and restart the review. Why: a docs-only push would restart the reviewer panel; this block carries the state meanwhile. Applied in: no code change (committed with the review round 1 fix). Status: pending review

## Lessons
- [source:intervention] When one qualifier applies to every item of an `A or B` list in a spec, state it once for all of them ("either title with …") rather than as a parenthetical after the last item, and pin every item in the test; reviewers and readers attach a trailing parenthetical to the last item only. (files: CLAUDE.md, tests/test_session_titles.py)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher (trigger `trig_01MF2bMLpiNc2DoCKNLpmpWo`, `dispatch shubhodeep1/coding-workflows#4886: start`) in session `session_01BGZEXrd6FYZpCoQvq27kaY`; permission mode auto. An earlier session (`session_01PKMcfjuzNCPE2CckfGEkBg`) had stopped before start, wrongly concluding that the `mcp__github__*` tools and `gh` were missing (they are deferred / installed by the SessionStart hook); the operator restarted the issue here.
- Security pass: run (`security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`).
- Protected-path approval: phase 1 — twin-first per Q40 (operator decision restated in the #4886 body: "follow the interim twin-first rule (operator Q40: A): edit the `workflow-templates/.claude/**` twins, post a hold, and stop BLOCKED") (2026-09-29). The phase edits only the `workflow-templates/.claude/` twins, lists the twinless pickup edit, pushes, posts a `hold` claim, and stops BLOCKED listing the files to copy.
- Phase 1 (2026-09-29): edits only the twins and CLAUDE.md, agents.md, tests, ci.yml, and the changelog fragment. Expected red until the twin sync (7): `test_implement_plan_claude_command.py::test_template_parity` and `::test_one_checker_per_project_keeps_the_chain_shallow`, `test_implement_issue_claude_command.py::test_template_parity[implement-issue-claude.md | claude-issue-dispatch.md | implement-plan-claude.md]`, `test_check_in_status_hand_back.py::test_fix_claude_pr_routes_on_action` (fix-claude-pr twin parity), and `test_session_titles.py::test_pickup_starts_issue_sessions_with_the_issue_number` (the pickup edit). With the four twins copied and the pickup edit applied in a scratch tree: 560 passed across the session-titles, plan-command, issue-command, issue-route, section-numbers, check-in hand-back, stale-Routine, check-in-reminder, update-workflows guardrails, and security-skip suites. A before/after run of the 86 test files that read a changed file shows the same 65 failures on `main` and with the change (Python 3.11 environment failures, e.g. `tests/test_workflow_retro.py` does not import); no regressions.
- The session container had no `pytest`; it was installed with pip so the suites run.
- Twin sync (2026-09-29): the master session applied the four twins to `.claude/commands/` in 72273b8 and the pickup edit (sha256 4e471ca3…) in d0520f7, then lifted the holds; the previous stage verified the synced tree (363 passed). Issue progress comment id 5882354685.
- Review round 1 (2026-09-29, head d0520f7, run 36521683144, ledger 22f8e2b9…): 14 ledger entries, all the same finding from six reviewers — the §26.D merged-report title had no `#<issue> · ` note while the closed-report title did. Valid: the parenthetical read as closed-only. Fixed by stating it once for both titles and asserting both in `test_claude_md_keeps_the_check_in_title_exact_and_prefixes_fixer_and_report`. Project branch synced with main (ce1b5e3, clean merge of 4 commits) at the start of the round.
