# Implement-Plan Log — Skip only permission-prompt occurrences already delivered to the filing repository

- Plan: docs/completed/issue-5125-key-reported-state-by-repo-and-session-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5125 (https://github.com/shubhodeep1/coding-workflows/issues/5125)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4755-report-blocking-permission-prompts
- Project branch: claude/implement-plan-issue-5125-key-reported-state-by-repo-and-session   Final PR: #5159 (draft)
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4755-report-blocking-permission-prompts)
- Waiting on: the completion PR (branch claude/implement-plan-issue-5125-key-reported-state-by-repo-and-session-complete), then final PR #5159
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: project checker armed when the completion PR opened (session and trigger ids in the #5125 progress comment)
- Last updated: 2026-09-29
- Last note: Q1 answered A (validation skipped, covered by #4755's project validation); completion PR moves the plan to docs/completed/.

## Phases
1. [x] Phase 1 — key reported state by repository and log session   — protected paths: .claude/scripts/permission_prompts.py
   - workflow-templates/.claude/scripts/permission_prompts.py: `report-now` entry records `repo` and `log_session`; `group_patterns` keeps per-session counts; `file` skips only occurrences delivered to the repo it files into
   - tests/test_permission_prompts.py: load the twin; tests for consumer-repo reports, other sessions, own-session skip, legacy entries, new entry fields
   - CLAUDE.md §23.I (both copies), agents.md, changelog.d/5125-permission-prompt-report-scope.md
   - Done: new tests pass against the twin; only test_template_parity red until the twin sync
   - PR #5184 merged 2026-09-29 (`9bda66a`; twin sync `ec18047`, review round 1 `ef823c7`); review rounds: 1; interventions: 0

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — no fixes (pre-security). G1–G4 map to `test_file_is_not_suppressed_by_a_report_to_another_repository`, `…_another_sessions_report`, `…_another_sessions_earlier_occurrence`, `test_file_skips_signatures_already_reported`, and `test_file_ignores_reports_without_a_repository_or_logged_session`; one correctness concern auto-decided as AD-3. Checks: 143 passed, `ruff check` clean, `.claude/` copy byte-identical to its twin.

## Security pass
- Skipped (ai:security: automation-produced issue; plan header, verified by `security_pass_skip.py`).

## Validation
- Cycle 1 — not dispatched: `validate.yml`'s *Authorize explicit validation target* step accepts a `target_ref` only when its open PR targets `main`, and final PR #5159 targets `claude/implement-plan-issue-4755-report-blocking-permission-prompts` (long-term fix: #4734). Blocker Q1 posted on #5125 (comment 5899540386).
- Q1: A (owner answer 2026-09-29, comment 5900140409; master-session standing decision Q17). Validation: skipped (covered by #4755's project validation).

## Completion
- Completion PR (this PR) — doc moved to docs/completed/issue-5125-key-reported-state-by-repo-and-session-plan.md; pre-completion checks: 180 passed (`test_permission_prompts.py`, `test_update_workflows_guardrails.py`, `test_changelog_fragment_contract.py`, `test_lint_plan_archival_completeness.py`, `test_ingest_implement_plan_lessons.py`), `ruff check` clean, twin byte-identical.
- Final PR #5159 draft (into the base branch)

## Activation
- n/a: the base `claude/implement-plan-issue-4755-report-blocking-permission-prompts` is not the default branch; the change goes live with #4755's project. The final-merge stage closes #5125 and labels it `ai:merged`.

## Auto-decisions
- AD-1 [plan, 2026-09-29] How should `file` decide an occurrence was already reported? — Picked: A — record the delivery repository and the hook log session on each `report-now` entry, and skip only occurrences from log sessions whose report reached the repository `file` files into; entries without those fields do not skip. Alternatives: B — key by repository only (another session's report still suppresses); C — drop the `already_reported` skip (a session's own prompt is reported twice). Why: A is the audit's recommendation and keeps the once-per-session rule. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How do the tests exercise the change before the twin sync? — Picked: A — `tests/test_permission_prompts.py` loads `permission_prompts` from the `workflow-templates/.claude` twin; `test_template_parity` keeps both copies identical after the sync. Alternatives: B — a second module object for the new tests only (the fixtures patch one module); C — tests against `.claude/` only (red until the sync). Why: the twin-first rule says tests read the twin, and the parity test keeps coverage of `.claude/`. Applied in: phase 1 PR. Status: pending review
- AD-3 [conformance 1/3, 2026-09-29] Should `report-now`'s occurrence count be changed to match what it marks filed? `_report_to_filing_repo` counts every session's unfiled occurrences, but since review round 1 the report marks only its own session's as filed. — Picked: A — leave it as is and treat it as the plan's accepted over-reporting risk. Alternatives: B — count only the reporting session's occurrences and mark that many filed (a single global counter can then attribute another session's occurrence to the reporter and hide it); C — keep per-session filed counts (changes the `filed-state.json` format, a plan non-goal). Why: the finding is about suppression, and A never suppresses (§1), while B and C either risk suppression or widen scope (§5). Applied in: no code change. Status: pending review

## Lessons
- [source:conformance] A single global filed-count per signature can only approximate per-session coverage; any partial-coverage rule on it must err toward re-reporting, never toward marking another session's occurrences filed. (files: .claude/scripts/permission_prompts.py)

## Notes
- Security pass: skip (security_pass_skip.py: {"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}).
- Base PR: the issue base is the head of draft final PR #4773 (open, into main); checked at stage start.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
- Validation skipped under Q17 (stacked project): #4755's chain re-runs its security audit and runtime validation on a branch that contains this fix before anything reaches `main`.
