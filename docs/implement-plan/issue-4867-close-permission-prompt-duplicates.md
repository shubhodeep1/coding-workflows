# Implement-Plan Log — Let issue sessions close pipeline-filed ai:permission-prompt duplicates themselves

- Plan: docs/plans/issue-4867-close-permission-prompt-duplicates-plan.md
- Source issue: shubhodeep1/coding-workflows#4867 (https://github.com/shubhodeep1/coding-workflows/issues/4867)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4867-close-permission-prompt-duplicates   Final PR: #4883 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: twin sync of `workflow-templates/.claude/{commands/implement-issue-claude.md,scripts/permission_prompts.py}` into `.claude/**` on the phase 1 PR (hold claim on its head), then `/reclarify`
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented and verified (71 new tests; with the twins copied, the related suites pass); phase PR opened against the project branch with a hold claim; stopped BLOCKED for the twin sync.

## Phases
1. [ ] Phase 1 — duplicate-close carve-out, duplicate-check, and class routing   — protected paths: `.claude/commands/implement-issue-claude.md`, `.claude/scripts/permission_prompts.py` (edited through their `workflow-templates/.claude/` twins)
   - CLAUDE.md §23.C carve-out → §23.I "Closing pipeline-filed duplicates" (conditions 1–4) → §28.C exception
   - `/implement-issue-claude` twin: step 5a duplicate check, Rules bullet
   - `permission_prompts.py` twin: `inline-interpreter-write` class, class marker, class routing to an open issue, `duplicate-check` subcommand
   - `tests/test_permission_prompt_duplicates.py` [new] wired into `ci.yml`; `agents.md`, `README.md`; `changelog.d/4867-close-permission-prompt-duplicates.md` [new]
   - Done: every plan goal present; new tests pass; with the twins copied, `tests/test_permission_prompts.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_claude_md_section_numbers.py` pass; ruff clean

## Conformance

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

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher (trigger `trig_01Q2f2oDN3xSbvfenGBa8jLC`, `dispatch shubhodeep1/coding-workflows#4867: deliver`) in session `session_01Kpbk3DLYNswrp2GkQ573Wa`; permission mode auto.
- Security pass: run (`security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`).
- Protected-path approval: phase 1 — interim twin-first rule (operator #4750 Q40: A, restated in the #4867 body: "Edit the `workflow-templates/.claude/commands/**` twins first (twin-first rule until #4785 lands; the supervising session syncs them)") (2026-09-29). The phase edits only the `workflow-templates/.claude/` twins, pushes, posts a `hold` claim, and stops BLOCKED listing the files to copy.
- The session container had no `gh`; it was installed from the Ubuntu archive (`gh` 2.45.0) so the helper scripts run.
- Phase 1 (2026-09-29): edits only the twins. Expected red until the twin sync: `tests/test_permission_prompts.py::test_template_parity` and `tests/test_implement_issue_claude_command.py::test_template_parity[implement-issue-claude.md]` (2 failed, 204 passed locally). With both twins copied into `.claude/**` in a scratch tree: 526 passed across the permission-prompt, issue-command, plan-command, section-number, security-skip, check-in, and stale-Routine suites.
- `duplicate-check` smoke test against live data: #4843 (already closed as a duplicate of #4678, fix PR #4684) passes every pipeline-filed check and is refused only as `#4843 is not open`.
