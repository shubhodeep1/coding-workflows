# Implement-Plan Log — Keep untrusted command text inside the permission-prompt report's code fence

- Plan: docs/plans/issue-5127-permission-report-fence-plan.md
- Source issue: shubhodeep1/coding-workflows#5127 (https://github.com/shubhodeep1/coding-workflows/issues/5127)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4755-report-blocking-permission-prompts
- Project branch: claude/implement-plan-issue-5127-permission-report-fence   Final PR: (opening)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: Project branch opened from the issue #4755 project branch (bf032b5e) with the plan and this log.

## Phases
1. [ ] Phase 1 — fence the report text so nothing inside can close it   — protected paths: .claude/scripts/permission_prompts.py
   - workflow-templates/.claude/scripts/permission_prompts.py: `_fenced_text` (fence longer than every backtick run, at least 4) used by `immediate_block` and `_occurrence_block`; `parse_immediate_block` anchored on the fence that closes before the session marker
   - tests/test_permission_prompts.py: fence rule, CommonMark closing-line check, hostile-command round trips, legacy four-backtick block, unchanged output without backtick runs, issue-body example
   - agents.md: fence sentence and untrusted `lookup` command sentence
   - changelog.d/5127-permission-report-fence.md (security)
   - Done: new tests pass against the twin, the rest of tests/test_permission_prompts.py passes except the twin-parity check waiting on the sync, ruff clean

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; security_pass_skip.py verified).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How is the command made inert in the report? — Picked: A — a backtick fence longer than every backtick run in the text (at least four), per the CommonMark closing rule; lossless. Alternatives: B — replace backticks in the command (lossy: `lookup` would return a different command); C — an HTML `<pre>` block with entity escaping (larger format change, `lookup` must unescape). Why: the issue's own recommendation, smallest lossless change (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Fix only the reported line 636, or both fenced sites? — Picked: A — both `immediate_block()` (line 636) and `_occurrence_block()` (line 419). Alternatives: B — line 636 only. Why: line 419 is the same defect on the issue-filing path (§1). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How does `lookup` read the command back from a variable fence? — Picked: A — anchor on the fence that closes right before the session marker, take its opening line, and the last line-start heading before it; old four-backtick reports parse the same way. Alternatives: B — keep `rfind(IMMEDIATE_HEADING)` with a variable-length regex (a command holding the heading text can still forge the fields); C — no parser change (reports with a longer fence would lose their command). Why: the finding asks that downstream readers treat the report as data, and `lookup` is the one reader in this repo. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How is "downstream agents treat the report as data" enforced? — Picked: A — keep the report's existing "untrusted data from the session" label, and document in `agents.md` that the report and `lookup`'s `command` are untrusted data the poller shows and never follows; no new JSON field. Alternatives: B — add an `untrusted: true` field to `lookup`'s output; C — also edit CLAUDE.md §23.I in both CLAUDE.md copies. Why: CLAUDE.md §23.I already says issue text is untrusted data; §5 and §6 favour no new output field. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Also change adjacent untrusted text (the `reasons` lines rendered raw, marker-lookalike text in a command matched by `MARKER_RE`)? — Picked: A — leave them out of this phase and record them under Notes for the next security pass. Alternatives: B — fix them here too. Why: neither breaks the fence or is what the finding covers; §5 keeps this PR to the reported defect, and the #4755 project's next security cycle audits the branch again. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Started by the Claude issue dispatcher routine in session session_01Bh7LETtVxLHXyjMExJxZUf (Auto mode). The GitHub MCP tools were not available in this session, so GitHub reads and routine writes went through `gh api` (REST).
- Security pass: skip (`security_pass_skip.py`: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- Adjacent, not changed (AD-5): `_occurrence_block()` renders each prompt reason as a raw Markdown list item (redacted, 300 characters, newlines kept); `existing_issues()` takes the first `MARKER_RE` match in an issue body, and the example command sits before the real marker, so a command holding `<!-- ai:permission-prompt:v1 sig=<other> -->` can map that issue to another signature.
- Issue progress comment: 5890566591.
