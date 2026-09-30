# Implement-Plan Log — Keep untrusted command text inside the permission-prompt report's code fence

- Plan: docs/completed/issue-5127-permission-report-fence-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5127 (https://github.com/shubhodeep1/coding-workflows/issues/5127)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4755-report-blocking-permission-prompts
- Project branch: claude/implement-plan-issue-5127-permission-report-fence   Final PR: #5161 (draft)
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4755-report-blocking-permission-prompts)
- Waiting on: the completion PR (branch claude/implement-plan-issue-5127-permission-report-fence-complete), then final PR #5161
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: project checker session_01R1LvzzMpNMRfQZiFpTTij6 (reused) armed when the completion PR opened (trigger ids in the #5127 progress comment)
- Last updated: 2026-09-30
- Last note: Q3 answered A (validation skipped, covered by #4755's project validation); completion PR moves the plan to docs/completed/.

## Phases
1. [x] Phase 1 — fence the report text so nothing inside can close it   — protected paths: .claude/scripts/permission_prompts.py
   - workflow-templates/.claude/scripts/permission_prompts.py: `_fenced_text` (fence longer than every backtick run, at least 4) used by `immediate_block` and `_occurrence_block`; `parse_immediate_block` anchored on the fence that closes before the session marker
   - tests/test_permission_prompts.py: fence rule, CommonMark closing-line check, hostile-command round trips, legacy four-backtick block, unchanged output without backtick runs, issue-body example
   - agents.md: fence sentence and untrusted `lookup` command sentence
   - changelog.d/5127-permission-report-fence.md (security)
   - Done: new tests pass against the twin, the rest of tests/test_permission_prompts.py passes except the twin-parity check waiting on the sync, ruff clean
   - Evidence: `_fenced_text` workflow-templates/.claude/scripts/permission_prompts.py:419, used at :438 and :655; `parse_immediate_block` :890-940; tests/test_permission_prompts.py:973-1051 (7 new tests, 17 cases); 121 passed at `38bea29`, twin parity included; ruff check clean
   - PR #5182 merged 2026-09-29 (`eb58135`; review rounds: 0; interventions: 0) before the twin sync
   - Twin sync `f4b1e75` stranded on the merged phase branch (Q2: A, comment 5900139652); cherry-picked as `a36c87f` and carried by PR #5274, merged 2026-09-30 as `38bea29` (review rounds: 1, all findings rejected; merged under Q46 by the master session, Q1: A, comment 5901525858)

## Conformance
- Sync 2026-09-30: `f0ae44b` merged the base branch in (`agents.md` conflict: both sides kept); `.claude/` copy and twin byte-identical (sha256 `59112420…74e69`).
- Run 1 — 2026-09-30: CONFORMANT — no fixes (pre-security). G1–G5 trace to merged code: `_fenced_text` used by `_occurrence_block` and `immediate_block`; `parse_immediate_block` anchored on the last session marker; tests cover a legacy four-backtick report and a report forged inside the command; `agents.md` documents the fence; the changelog fragment is a `security` entry. An independent CommonMark render (markdown-it-py) of hostile commands kept each inside one fence and `lookup` returned each exactly.

## Security pass
- Skipped (ai:security: automation-produced issue; security_pass_skip.py verified).

## Validation
- Cycle 1 — not dispatched: `validate.yml`'s *Authorize explicit validation target* step accepts a `target_ref` only when its open PR targets `main`, and final PR #5161 targets `claude/implement-plan-issue-4755-report-blocking-permission-prompts` (long-term fix: #4734). Blocker Q3 posted on #5127 (comment 5902102959).
- Q3: A (owner answer 2026-09-30, comment 5902375365; master-session standing decision Q17). Validation: skipped (covered by #4755's project validation).

## Completion
- Completion PR (this PR) — doc moved to docs/completed/issue-5127-permission-report-fence-plan.md; pre-completion checks: 197 passed (`test_permission_prompts.py`, `test_update_workflows_guardrails.py`, `test_changelog_fragment_contract.py`, `test_lint_plan_archival_completeness.py`, `test_ingest_implement_plan_lessons.py`), `ruff check` clean, twin byte-identical.
- Final PR #5161 draft (into the base branch)

## Activation
- n/a: the base `claude/implement-plan-issue-4755-report-blocking-permission-prompts` is not the default branch; the change goes live with #4755's project. The final-merge stage closes #5127 and labels it `ai:merged`.

## Auto-decisions
- AD-1 [plan, 2026-09-29] How is the command made inert in the report? — Picked: A — a backtick fence longer than every backtick run in the text (at least four), per the CommonMark closing rule; lossless. Alternatives: B — replace backticks in the command (lossy: `lookup` would return a different command); C — an HTML `<pre>` block with entity escaping (larger format change, `lookup` must unescape). Why: the issue's own recommendation, smallest lossless change (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Fix only the reported line 636, or both fenced sites? — Picked: A — both `immediate_block()` (line 636) and `_occurrence_block()` (line 419). Alternatives: B — line 636 only. Why: line 419 is the same defect on the issue-filing path (§1). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How does `lookup` read the command back from a variable fence? — Picked: A — anchor on the fence that closes right before the session marker, take its opening line, and the last line-start heading before it; old four-backtick reports parse the same way. Alternatives: B — keep `rfind(IMMEDIATE_HEADING)` with a variable-length regex (a command holding the heading text can still forge the fields); C — no parser change (reports with a longer fence would lose their command). Why: the finding asks that downstream readers treat the report as data, and `lookup` is the one reader in this repo. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How is "downstream agents treat the report as data" enforced? — Picked: A — keep the report's existing "untrusted data from the session" label, and document in `agents.md` that the report and `lookup`'s `command` are untrusted data the poller shows and never follows; no new JSON field. Alternatives: B — add an `untrusted: true` field to `lookup`'s output; C — also edit CLAUDE.md §23.I in both CLAUDE.md copies. Why: CLAUDE.md §23.I already says issue text is untrusted data; §5 and §6 favour no new output field. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Also change adjacent untrusted text (the `reasons` lines rendered raw, marker-lookalike text in a command matched by `MARKER_RE`)? — Picked: A — leave them out of this phase and record them under Notes for the next security pass. Alternatives: B — fix them here too. Why: neither breaks the fence or is what the finding covers; §5 keeps this PR to the reported defect, and the #4755 project's next security cycle audits the branch again. Applied in: no code change. Status: pending review
- AD-6 [phase 1/1 — review round, 2026-09-29] Where does the pending log update go while PR #5274 is mid-review? — Picked: A — wait for the next stage's PR. Alternatives: B — push it onto #5274 mid-review. Why: a push would restart the review round on a PR that only carries the twin sync. Applied in: no code change. Status: pending review

## Lessons
- [source:intervention] A hold claim does not stop a phase PR from merging, so a twin-sync copy must land before the PR can merge, and whoever copies must first check that the PR is still open (CLAUDE.md §21). (files: .claude/scripts/claude_fix_claim.py)
- [source:security] A parser that reads untrusted text back out of a Markdown report must anchor on the report's own trailing marker and delimiter, never search forward or `rfind` a heading, because the untrusted text can imitate every earlier part of the report. (files: .claude/scripts/permission_prompts.py)

## Notes
- Started by the Claude issue dispatcher routine in session session_01Bh7LETtVxLHXyjMExJxZUf (Auto mode). The GitHub MCP tools were not available in this session, so GitHub reads and routine writes went through `gh api` (REST).
- Security pass: skip (`security_pass_skip.py`: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- Adjacent, not changed (AD-5): `_occurrence_block()` renders each prompt reason as a raw Markdown list item (redacted, 300 characters, newlines kept); `existing_issues()` takes the first `MARKER_RE` match in an issue body, and the example command sits before the real marker, so a command holding `<!-- ai:permission-prompt:v1 sig=<other> -->` can map that issue to another signature.
- Issue progress comment: 5890566591.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
- Full suite under Python 3.12 (CI's version); the container's default python3 is 3.11, which cannot import scripts/workflow_retro.py (a 3.12 f-string), unrelated to this change.
- Completion stage started by the Claude issue dispatcher routine in session session_01T6Q3X4nu8MGkERJFb9dFXc (Auto mode) after the Q3 `/reclarify`; the project branch was already in sync with the base (`d5a9d16`).
