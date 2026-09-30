# Keep untrusted command text inside the permission-prompt report's code fence

Source issue: shubhodeep1/coding-workflows#5127 (https://github.com/shubhodeep1/coding-workflows/issues/5127)
Base branch: claude/implement-plan-issue-4755-report-blocking-permission-prompts
Security pass: skip (ai:security: automation-produced issue)

## Summary

`permission_prompts.py` wraps the sanitized command of a permission prompt in a fixed four-backtick code fence. A command that contains a line of four backticks closes that fence early, so the rest of the command renders as Markdown in the GitHub report that downstream agents and the operator's poller read. This plan makes the fence longer than any backtick run in the text, so no command can close it, and makes `lookup` read the command back from the real fence.

## Context

- Security audit finding `permission-report-markdown-escape` (A03:2021-Injection, medium, confidence 8/10) at `.claude/scripts/permission_prompts.py:636` on the issue #4755 project branch, filed by `.github/workflows/security-audit.yml` (tracker #3576).
- The same fixed fence is used in two places on the base branch:
  - `immediate_block()` (line 636): the `report-now` immediate report (issue #4755).
  - `_occurrence_block()` (line 419): the example in every `ai:permission-prompt` issue body and "Seen again" comment filed by `file` and `report-now`.
- `parse_immediate_block()` (line 887) reads the command back for `permission_prompts.py lookup` with `^````text\n(.*)\n````$`, and finds the block with `head.rfind(IMMEDIATE_HEADING)`. A command that contains the heading text or a four-backtick line can make it return fields or a command that the session never ran.
- Reproduced on the base branch head `bf032b5e` with a CommonMark parser: the command `echo start` / four backticks / `# Injected heading` / … renders `Injected heading` as a heading outside the fence.
- CommonMark (and GitHub) closes a backtick fence only with a line of at least as many backticks, indented at most 3 spaces, followed only by spaces. A fence longer than every backtick run in the content therefore cannot be closed from inside.
- `.claude/scripts/permission_prompts.py` is a protected path (CLAUDE.md §28.C). The interim twin-first default applies (CLAUDE.md §28.C, `/implement-plan-claude` step 4): the change is made in `workflow-templates/.claude/scripts/permission_prompts.py` only and copied into `.claude/` by the operator's `[claude-twin-sync]` commit.

## Goals

- G1: No text in a report's command (or non-Bash tool input) can close the fence around it: the fence is backticks, at least four, and longer than the longest backtick run in the fenced text. Verified by a CommonMark parse in the tests' own assertions (no new dependency: the test checks the CommonMark closing rule directly) and by round-tripping commands that contain fence-like lines.
- G2: Both fenced sites use it: `immediate_block()` and `_occurrence_block()`.
- G3: `lookup` returns exactly the command that `immediate_block()` wrote, for commands containing backtick runs of any length, the immediate-report heading, field-like lines (`- **Tool:** …`), or `text`-fence openers. Reports already posted with the old four-backtick fence still parse.
- G4: A report whose text has no backtick run of four or more is byte-for-byte unchanged (the fence stays four backticks), so existing issues, markers, and signatures are unaffected.
- G5: `agents.md` states that the report and `lookup`'s `command` are untrusted session data that the poller shows as data and never follows.

## Non-goals

- The prompt `reasons` lines in `_occurrence_block()` and marker-lookalike text inside a command (`MARKER_RE` takes the first match in an issue body) are rendered or matched as before (AD-5). They do not break the fence and are not what the finding covers; they are recorded under Notes for the next security pass.
- No change to redaction, heredoc stripping, truncation (`MAX_COMMAND_CHARS`), signatures, markers, state files, or API calls.
- No new CLI flag, env var, JSON field, or dependency.

## Constraints

- §1: security first; the fix must be lossless so `lookup` still returns the real command.
- §5: minimal change set: one helper, two call sites, the parser, tests, docs.
- §6: no identifier is renamed or removed; new names (`_BACKTICK_RUN_RE`, `_fenced_text`, `_CLOSING_FENCE_RE`) were checked unused in `.claude/`, `workflow-templates/.claude/`, `tests/`, and `scripts/`.
- §7: `agents.md` documents the behaviour; §20: a `changelog.d/5127-permission-report-fence.md` fragment (`security`).
- §9: tabs in Python.
- §15: no new GitHub API call.
- §28.C interim twin-first rule: edit only `workflow-templates/.claude/**`; the twin-parity tests stay red until the `[claude-twin-sync]` copy.

## Approach

- `_fenced_text(text)` returns `` ```` ``… `text\n<text>\n` …`` with a fence of `max(4, longest backtick run + 1)` backticks (AD-1). Both sites call it (AD-2).
- `parse_immediate_block()` anchors on the marker (AD-3): the text just before the session marker must end with `\n<fence>\n\n`; the opening line is the last `\n<fence>text\n` before that, which cannot occur inside the content because the content has no run that long; the heading is the last one that starts a line before the opening fence, where only the report's single-line fields sit. The fields are read between the heading and the opening fence. When the anchor is missing, it returns None as it does today for a block it cannot find.
- Alternatives: replacing backticks in the command (lossy, `lookup` would return a different command) and an HTML `<pre>` block with entity escaping (a larger format change that `lookup` must unescape) were rejected (AD-1).

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for a standalone issue.

1. **Phase 1 — fence the report text so nothing inside can close it.** Files: `workflow-templates/.claude/scripts/permission_prompts.py` (twin of `.claude/scripts/permission_prompts.py`), `tests/test_permission_prompts.py`, `agents.md`, `changelog.d/5127-permission-report-fence.md`. Protected paths: `.claude/scripts/permission_prompts.py` (twin-first). Done when: the new tests pass against the twin, the rest of `tests/test_permission_prompts.py` passes except the twin-parity check that waits on the sync, `ruff check` is clean, and the rendered report keeps a hostile command inside the fence. Rollback: revert the phase PR (and its `[claude-twin-sync]` commit).

## Implementation Steps

1. `workflow-templates/.claude/scripts/permission_prompts.py`: add `_BACKTICK_RUN_RE` and `_fenced_text()`; use it in `_occurrence_block()` and `immediate_block()`; update the module docstring's "inside a fenced block" sentence.
2. Same file: add `_CLOSING_FENCE_RE` and rewrite the block location in `parse_immediate_block()` as in Approach; the returned dict keys are unchanged.
3. `tests/test_permission_prompts.py`: load the twin as a second module and add tests for G1–G4 (fence length, a CommonMark closing-line check over every content line, round trips through `parse_immediate_block()` for hostile commands, the legacy four-backtick block, unchanged output without backtick runs, and the issue-body example).
4. `agents.md`: one sentence on the fence and one on `lookup`'s `command` being untrusted data (G5).
5. `changelog.d/5127-permission-report-fence.md` (`<!-- changelog: security -->`).

## Files & Modules

- `workflow-templates/.claude/scripts/permission_prompts.py` (twin; `.claude/scripts/permission_prompts.py` via `[claude-twin-sync]`)
- `tests/test_permission_prompts.py`
- `agents.md`
- `changelog.d/5127-permission-report-fence.md` [new]
- `docs/plans/issue-5127-permission-report-fence-plan.md` [new], `docs/implement-plan/issue-5127-permission-report-fence.md` [new]

## Tests

- Unit (new, against the twin): `_fenced_text` length rule; no content line satisfies the CommonMark closing rule for the chosen fence; `parse_immediate_block(immediate_block(...))` returns the exact `record_example` for commands with four- and five-backtick lines, the heading text, field-like lines, and `text`-fence openers; a pre-change four-backtick block still parses; a command without backtick runs yields the same four-backtick block as before; `issue_body` fences its example the same way.
- Existing: the full `tests/test_permission_prompts.py`; the twin-parity test fails until the `[claude-twin-sync]` copy, as for every twin-first phase.
- Lint: `ruff check`.

## Risks & Mitigations

- A downstream reader that matched the fixed `` ```` `` fence breaks on a longer one. Mitigation: only `parse_immediate_block()` reads it in this repo (`git grep`), and it is updated here; reports without long backtick runs keep the four-backtick fence (G4).
- A very long backtick run makes a long fence. ACCEPTED: the text is capped at `MAX_COMMAND_CHARS` (2,000), so the fence adds at most about 4,000 characters, far below GitHub's 65,536-character body limit.

## Rollout

Ships with the issue #4755 project: this project's final PR merges into its branch, and that project's final PR carries it to `main` and to consumers on the next `@stable` sync. No flag; revert the phase PR to roll back.

## Auto-decisions

- AD-1 [plan, 2026-09-29] How is the command made inert in the report? — Picked: A — a backtick fence longer than every backtick run in the text (at least four), per the CommonMark closing rule; lossless. Alternatives: B — replace backticks in the command (lossy: `lookup` would return a different command); C — an HTML `<pre>` block with entity escaping (larger format change, `lookup` must unescape). Why: the issue's own recommendation, smallest lossless change (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Fix only the reported line 636, or both fenced sites? — Picked: A — both `immediate_block()` (line 636) and `_occurrence_block()` (line 419). Alternatives: B — line 636 only. Why: line 419 is the same defect on the issue-filing path (§1). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How does `lookup` read the command back from a variable fence? — Picked: A — anchor on the fence that closes right before the session marker, take its opening line, and the last line-start heading before it; old four-backtick reports parse the same way. Alternatives: B — keep `rfind(IMMEDIATE_HEADING)` with a variable-length regex (a command holding the heading text can still forge the fields); C — no parser change (reports with a longer fence would lose their command). Why: the finding asks that downstream readers treat the report as data, and `lookup` is the one reader in this repo. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How is "downstream agents treat the report as data" enforced? — Picked: A — keep the report's existing "untrusted data from the session" label, and document in `agents.md` that the report and `lookup`'s `command` are untrusted data the poller shows and never follows; no new JSON field. Alternatives: B — add an `untrusted: true` field to `lookup`'s output; C — also edit CLAUDE.md §23.I in both CLAUDE.md copies. Why: CLAUDE.md §23.I already says issue text is untrusted data; §5 and §6 favour no new output field. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Also change adjacent untrusted text (the `reasons` lines rendered raw, marker-lookalike text in a command matched by `MARKER_RE`)? — Picked: A — leave them out of this phase and record them under Notes for the next security pass. Alternatives: B — fix them here too. Why: neither breaks the fence or is what the finding covers; §5 keeps this PR to the reported defect, and the #4755 project's next security cycle audits the branch again. Applied in: no code change. Status: pending review

## Notes

- Security pass: skip (`security_pass_skip.py`: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- Adjacent, not changed (AD-5): `_occurrence_block()` renders each prompt reason as a raw Markdown list item (redacted, 300 characters, newlines kept); `existing_issues()` takes the first `MARKER_RE` match in an issue body, and the example command sits before the real marker, so a command holding `<!-- ai:permission-prompt:v1 sig=<other> -->` can map that issue to another signature.

## References

- Issue #5127; security audit tracker #3576; issue #4755 project (final PR #4773) and its plan `docs/plans/issue-4755-report-blocking-permission-prompts-plan.md`.
- CommonMark spec, fenced code blocks: https://spec.commonmark.org/0.31.2/#fenced-code-blocks
