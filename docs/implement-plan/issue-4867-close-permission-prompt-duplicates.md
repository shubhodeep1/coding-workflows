# Implement-Plan Log — Let issue sessions close pipeline-filed ai:permission-prompt duplicates themselves

- Plan: docs/plans/issue-4867-close-permission-prompt-duplicates-plan.md
- Source issue: shubhodeep1/coding-workflows#4867 (https://github.com/shubhodeep1/coding-workflows/issues/4867)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4867-close-permission-prompt-duplicates   Final PR: #4883 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3 — review round
- Activation: not started
- Waiting on: PR #5073 (conformance fix 1/3): the twin sync of `workflow-templates/.claude/scripts/permission_prompts.py` into `.claude/scripts/` for the review round 1 fix (hold claim on the fix head; asked as Q5 on #4867), then review round 2 on the sync head or the merge
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01T6EkqYzfsrGhLYKQTx7GXm (reused)   safety net and hand-back: in the stage report of session_01EEwN2dfCQ52wBrZnBwahCC
- Last updated: 2026-09-29
- Last note: review round 1 on PR #5073 head `92d72d0` (session_01EEwN2dfCQ52wBrZnBwahCC): both findings valid, fixed in the `permission_prompts.py` twin (misaligned heredoc counts bind no body; `open(` mode found by a bracket- and string-aware scanner at any depth). Waiting on its twin sync.

## Phases
1. [x] Phase 1 — duplicate-close carve-out, duplicate-check, and class routing   — protected paths: `.claude/commands/implement-issue-claude.md`, `.claude/scripts/permission_prompts.py` (edited through their `workflow-templates/.claude/` twins)   — PR #4903 merged 2026-09-29 (merge `4531747`, by the master session under Q46: A); review rounds: 3 (heads `c7a25d1`, `b7dc823`, `e3ed34a`); twin syncs: 3 (`c7a25d1`, `b7dc823`, `e3ed34a`); interventions: 0
   - CLAUDE.md §23.C carve-out → §23.I "Closing pipeline-filed duplicates" (conditions 1–4) → §28.C exception
   - `/implement-issue-claude` twin: step 5a duplicate check, Rules bullet
   - `permission_prompts.py` twin: `inline-interpreter-write` class, class marker, class routing to an open issue, `duplicate-check` subcommand
   - `tests/test_permission_prompt_duplicates.py` [new] wired into `ci.yml`; `agents.md`, `README.md`; `changelog.d/4867-close-permission-prompt-duplicates.md` [new]
   - Done: every plan goal present; new tests pass; with the twins copied, `tests/test_permission_prompts.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_claude_md_section_numbers.py` pass; ruff clean

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — fix PR #5073 (pre-security; review rounds: 1). Implemented: COMPLETE (goals 1–5 map to #4903); Correctness: CONCERNS, 4 EVIDENCE-BASED CONCERN findings: nested-call `open(..., 'w')` writes not classed; `open(os.path.join(d, 'w'))` read classed as a write; a write in another command's heredoc classed the `python3 -` heredoc; the `duplicate-check` docstring promised JSON on an argparse usage error.

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where does the duplicate check run? — Picked: A — a new step 5a in `/implement-issue-claude`, after context is read and before the plan is written. Alternatives: B — a gate in step 2; C — also in `/implement-plan-claude` stages. Why: duplicates are found while reading context; the issue names this command only; `5a` keeps step numbers stable (§6). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Who decides conditions 1 and 2? — Picked: A — a `permission_prompts.py duplicate-check` subcommand with a JSON verdict, reusing `security_pass_skip`'s label-at-creation check. Alternatives: B — instruction text only; C — a new helper script. Why: §1 and "the script decides"; no new allow rule, whereas C needs a `settings.json` change. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] What is "the workflow/session account"? — Picked: A — the authenticated login of the session running the check; the issue author must equal it. Alternatives: B — any `OWNER` author; C — `github-actions[bot]` only. Why: the filer posts under the session identity; A is the literal condition. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How must the fix PR relate to the target? — Picked: A — head branch contains `issue-<target>-` or title/body contains `#<target>`; open or merged; for a closed target, merged into the default branch. Alternatives: B — any open or merged PR. Why: condition 2 needs the target's own fix. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] What does condition 4 do with a branch the session created? — Picked: A — close only PRs this session opened, unmerged, with a comment linking the target; leave branches. Alternatives: B — also delete branches. Why: branch deletion stays §23.C ask-first. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] Implement the optional `permission_prompts.py` class match? — Picked: A — yes, one class `inline-interpreter-write` (#4858 item 1); the class marker marks the fix issue; signature match first; only open class issues attract new shapes. Alternatives: B — skip it; C — also class reads. Why: the issue marks it preferred; C exceeds the #4858 definition. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] Which transport closes the issue? — Picked: A — `mcp__github__add_issue_comment`, then `mcp__github__issue_write` with `state_reason: duplicate` and `duplicate_of` (without `duplicate_of` when the tool lacks it). Alternatives: B — `gh api -X PATCH`. Why: §23.D, §23.H. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] Which module do the new tests load? — Picked: A — the `workflow-templates/.claude/` twins, in a new test file. Alternatives: B — extend `tests/test_permission_prompts.py` against `.claude/scripts`. Why: the twin leads under twin-first; parity tests keep `.claude` equal to it. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:intervention] Under the interim twin-first rule every review round that touches `.claude/**` needs another supervising-session twin sync, so a project that edits `.claude/` should expect one BLOCKED stop per such round. (files: workflow-templates/.claude/scripts/permission_prompts.py)
- [source:intervention] `check_in_status.py` fails closed and reports a Claude-fixer PR as plain `open` when `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` is unset, so a resuming session must pass it (the review workflow's comment account) or it misses the pending review round. (files: .claude/scripts/check_in_status.py)
- [source:intervention] A regex that detects a file-write mode in `open(` must anchor on the mode argument (after a comma, `mode=`, or as `Path.open`'s first argument), not on any quoted string inside the call, or a one-letter file name such as `open('a')` reads as a write. (files: workflow-templates/.claude/scripts/permission_prompts.py)
- [source:conformance] A regex that finds a call's later argument by scanning to the first `)` misses a nested call in an earlier argument (`open(os.path.join(d, f), "w")`) and misreads a comma inside it; match balanced parentheses to a fixed depth instead. (files: workflow-templates/.claude/scripts/permission_prompts.py)
- [source:conformance] When a Bash command is split into simple commands, heredoc bodies must be bound to the command whose `<<` owns them (by operator order), or one command's heredoc text classifies another. (files: workflow-templates/.claude/scripts/permission_prompts.py)
- [source:intervention] A heuristic that binds heredoc bodies to commands must bind none when its two parsers disagree, not all of them, or the misalignment brings back the false positive the binding was added to prevent. A regex cannot balance brackets to arbitrary depth, so scan call arguments with a bracket- and quote-aware loop. (files: workflow-templates/.claude/scripts/permission_prompts.py)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher (trigger `trig_01Q2f2oDN3xSbvfenGBa8jLC`, `dispatch shubhodeep1/coding-workflows#4867: deliver`) in session `session_01Kpbk3DLYNswrp2GkQ573Wa`; permission mode auto.
- Security pass: run (`security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`).
- Protected-path approval: phase 1 — interim twin-first rule (operator #4750 Q40: A, restated in the #4867 body: "Edit the `workflow-templates/.claude/commands/**` twins first (twin-first rule until #4785 lands; the supervising session syncs them)") (2026-09-29). The phase edits only the `workflow-templates/.claude/` twins, pushes, posts a `hold` claim, and stops BLOCKED listing the files to copy.
- The session container had no `gh`; it was installed from the Ubuntu archive (`gh` 2.45.0) so the helper scripts run.
- Phase 1 (2026-09-29): edits only the twins. Expected red until the twin sync: `tests/test_permission_prompts.py::test_template_parity` and `tests/test_implement_issue_claude_command.py::test_template_parity[implement-issue-claude.md]` (2 failed, 204 passed locally). With both twins copied into `.claude/**` in a scratch tree: 526 passed across the permission-prompt, issue-command, plan-command, section-number, security-skip, check-in, and stale-Routine suites.
- `duplicate-check` smoke test against live data: #4843 (already closed as a duplicate of #4678, fix PR #4684) passes every pipeline-filed check and is refused only as `#4843 is not open`.
- Resumed 2026-09-29 by `session_019stfdgFtBaLtGT93crWq6z` (dispatcher trigger `trig_01TiPwwXSzJYbrQmXRtLCDsm`) after the twin sync `c7a25d1` and `/reclarify`. No checker was armed at the BLOCKED stop, so the round-1 hand-off (02:24 UTC) waited for this session.
- Review round on `b7dc823` (2026-09-29, session_01AAhuN3CZLU38bKQjJ5mZ5C): the round-fix head edits only the twin again, so `tests/test_permission_prompts.py::test_template_parity` is red until the third sync. With the twin copied in a scratch tree, 360 passed across the permission-prompt, duplicate, issue-command, guardrail, check-in, section-number, and security-skip suites; ruff clean. The new cases fail 4/87 against `b7dc823`'s twin.
- PR #4903 was merged into the project branch by the master session at 2026-09-29T06:13:44Z (merge `4531747`) under standing decision Q46: A, after all 6 round-1 findings on head `e3ed34a` were rejected (Q1: A on #4867, comment 5884895754). No verdict bot is configured, so a review round with only invalid findings cannot converge on its own.
- Conformance 1/3 (2026-09-29, session_01Ao1Lifx4ribLRqjYzmT3Kx): synced the project branch with `main` (`16abd00`, clean). A whole-repo `pytest tests` run was not usable here: under the container's Python 3.11 `tests/test_workflow_retro.py` fails to collect (a 3.12-only f-string in `scripts/workflow_retro.py`, outside this project), so the audit ran the suites the footprint reaches. The fix PR edits only the twin; `tests/test_permission_prompts.py::test_template_parity` is red until the twin sync (sha256 `5b7daa0c…ccaf7`). With the twin copied in a scratch tree, 522 passed across the related suites.
- Conformance fix 1, review round 1 (2026-09-29, session_01EEwN2dfCQ52wBrZnBwahCC): 2 findings on `92d72d0` (ledger `e5e0ebfd…`), both valid and fixed in the twin only. 3 of the 9 new cases fail against `92d72d0`. With the twin copied in a scratch tree, 377 passed across the related suites (`test_template_claude_md_is_the_same_file` and `test_section_28_scope_covers_issue_mode` fail only in the copy, where `cp` dereferenced the `workflow-templates/CLAUDE.md` symlink; they pass in the checkout).
