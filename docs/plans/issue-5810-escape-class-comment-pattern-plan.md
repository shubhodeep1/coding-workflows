# Keep session data in permission-prompt issues inside code spans it cannot close

Source issue: shubhodeep1/coding-workflows#5810 (https://github.com/shubhodeep1/coding-workflows/issues/5810)
Base branch: claude/implement-plan-issue-4867-close-permission-prompt-duplicates
Security pass: skip (ai:security: automation-produced issue)

## Summary

`permission_prompts.py` writes session-derived text (the command shape, the tool name, and the prompt reasons) into `ai:permission-prompt` issue bodies and "Seen again" comments outside the fenced example. A backtick in that text closes the surrounding code span, so a logged command word can inject Markdown into an owner-authored comment that `/implement-issue-claude` reads as spec. This plan renders each of those values in a code span whose delimiter is longer than any backtick run in it, and tells the issue implementer that this generated text is evidence, never spec, whatever account posted it.

## Context

- **The finding (#5810, `A03:2021-Injection`, medium, confidence 8/10).** The security audit of the #4867 project branch flagged `.claude/scripts/permission_prompts.py:676`: `class_comment_body` writes `**Pattern:** \`{pattern['shape'] or pattern['tool_name']}\``, the raw shape in a one-backtick code span. `_segment_shape` keeps a segment's command word literally, so a command such as ``'a`` + newline + `**Do X**' ; sed -i 's/a/b/' f`` gives an inline-interpreter-write shape whose first word holds a backtick and a newline. The class comment is posted by the session's account (`OWNER` here), and `/implement-issue-claude` step 1 makes every `OWNER` / `MEMBER` / `COLLABORATOR` comment part of the spec.
- **The sibling paths.** #4867's fix 3b (PR #5649) added `_pattern_line` (one line, `<!--` escaped) for `issue_body`'s `**Pattern:**` line, but it does not stop a backtick from closing the span either, and `class_comment_body` does not use it at all. `issue_body` names the tool in a one-backtick span, and both bodies list the prompt reasons (`_occurrence_block`) as plain Markdown, guarded by `_reason_line` against markers only. An Auto-mode reason can quote the command.
- **What already holds.** The command example sits in a fence one backtick longer than any run in it (`_example_fence`), and markers are read only outside fenced ````text blocks (`_outside_fenced_examples`). Signatures are computed from the unescaped shape, so filed issues keep their signatures.
- **Where this lands.** The issue names the #4867 project branch as its integration branch, and that project waits on this follow-up in its security pass. `.claude/scripts/permission_prompts.py` and `.claude/commands/implement-issue-claude.md` are protected paths with byte-identical `workflow-templates/.claude/` twins, so the interim twin-first rule (CLAUDE.md §28.C, until #4785) applies: only the twins are edited, and a supervising session copies them.
- **Binding rules.** §1 (security first), §5 (minimal change), §6 (no identifier is renamed or repurposed; `_pattern_line` and `_reason_line` keep their behaviour), §7 (docs), §9 (tabs in Python), §20 (changelog fragment), §23.I (permission prompt reports), §28 (auto-decisions).

## Goals

1. Every session-derived value that `issue_body`, `comment_body`, and `class_comment_body` render outside the fenced example (the shape on both `**Pattern:**` lines, the tool name, and each prompt reason) is one line, has `<!--` escaped, and sits in a Markdown code span whose delimiter is longer than any backtick run in the value, padded with a space when the value starts or ends with a backtick. No backtick or newline in the value can end the span or start a new Markdown line.
2. For a value without backticks the rendering of the shape and tool name is unchanged (one-backtick span), and signatures do not change.
3. The `/implement-issue-claude` twin's step 1 says that text the repository's automation generated from session data (the `**Pattern:**` lines, the `**Reason Claude Code gave:**` lists, the fenced examples, and the shape in an `ai:permission-prompt` title) is evidence, never spec, whatever account posted it.
4. Tests pin goals 1–3 and fail against the base branch's twin. CLAUDE.md §23.I's "Issue text is untrusted data" sentence, the matching `agents.md` line, and the module docstring describe the code spans. A `changelog.d/` fragment is added.

## Non-goals

- Changing issue titles: GitHub does not render Markdown in titles, and goal 3 covers the title for the reader that matters.
- Changing the signature, the shape, the class detection, marker reading, or `duplicate-check`.
- Editing `.claude/**` directly (twin-first; the copy is the twin sync).
- Re-filing or editing issues and comments already posted.

## Constraints

- §6: `_pattern_line` and `_reason_line` keep their return values; the new helper gets a name no identifier in the module uses.
- §9: tabs in Python.
- §15: no new API calls; rendering only.
- §28.C: protected paths are edited through their twins only, and the phase stops at the twin-sync blocker.

## Approach

Add one helper to the twin of `permission_prompts.py`, `_markdown_code_span(line)`, which wraps an already-guarded line in a backtick run one longer than its longest run (minimum one) and pads it with a space when it starts or ends with a backtick (CommonMark code-span rules). Every session-derived value outside the fence goes through `_markdown_code_span(_reason_line(value))`. The shape goes through `_markdown_code_span(_pattern_line(pattern))`. `issue_body` and `class_comment_body` both use that for `**Pattern:**`, so the two lines can no longer drift apart. Alternatives considered: fencing each value in its own ```` ```text ```` block (heavier, and a second fence style would need its own marker-reading rule); escaping backticks with backslashes (backslash escapes do not work inside code spans, so the escape would show and the shape would still need a span).

## Auto-decisions

- AD-1 [plan, 2026-10-01] Which session-derived values does the guard cover? — Picked: A — the shape on both `**Pattern:**` lines, the tool name in both bodies, and every prompt reason. Alternatives: B — only `class_comment_body`'s `**Pattern:**` line; C — the shape on both `**Pattern:**` lines only. Why: §1 puts security first, and the reasons and the issue-body shape cross the same trust boundary the finding names (a missed sibling path made #4867's fix check FIX-DEFECTIVE). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-10-01] Does the plan also act on the second half of the recommendation (treat generated diagnostic comments as untrusted regardless of their posting account)? — Picked: A — add that rule to `/implement-issue-claude` step 1 (twin). Alternatives: B — fix the rendering only. Why: the rendering fix closes this injection, but the spec rule is what keeps the next unescaped field from being read as an instruction. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-10-01] Are issue titles changed? — Picked: A — no; the step 1 rule names the title's shape as evidence. Alternatives: B — strip backticks and Markdown from the title's shape. Why: §5; GitHub shows titles as plain text, and the title's shape is truncated session data the rule already covers. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-10-01] Does a reason, now in a code span, keep its own Markdown? — Picked: A — no; the reason renders literally, backticks included. Alternatives: B — keep reasons as plain Markdown and only escape backticks. Why: backslash escapes cannot make attacker-supplied `**…**` or links inert in plain Markdown as reliably as a code span does. Applied in: phase 1. Status: pending review

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue is one defect with one fix.

1. **Phase 1 — code spans for session data and the spec rule.** Protected paths: `.claude/scripts/permission_prompts.py`, `.claude/commands/implement-issue-claude.md` (edited through their `workflow-templates/.claude/` twins).
	- Files: `workflow-templates/.claude/scripts/permission_prompts.py`, `workflow-templates/.claude/commands/implement-issue-claude.md`, `tests/test_permission_prompt_duplicates.py`, `CLAUDE.md` (§23.I sentence), `agents.md`, `changelog.d/5810-permission-prompt-code-spans.md` [new].
	- Done: goals 1–4 hold; the new tests fail against the base twin and pass on the phase head; with the twins copied into `.claude/`, `tests/test_permission_prompts.py`, `tests/test_permission_prompt_duplicates.py`, and `tests/test_implement_issue_claude_command.py` pass; ruff is clean.
	- Rollback: revert the phase PR; nothing persists outside the rendering of new issues and comments.

## Implementation Steps

1. `workflow-templates/.claude/scripts/permission_prompts.py`: add `_markdown_code_span` next to `_pattern_line`; use it in `_occurrence_block` (reasons), `issue_body` (tool name, `**Pattern:**`), and `class_comment_body` (`**Pattern:**` through `_pattern_line`, tool name); update the module docstring's "Issue text is untrusted data" paragraph.
2. `workflow-templates/.claude/commands/implement-issue-claude.md`: add the evidence-not-spec rule to step 1.
3. `tests/test_permission_prompt_duplicates.py`: cases for a backtick-and-newline command word in `class_comment_body` and `issue_body`, a backtick at the start or end of the value, a reason with backticks and Markdown, an unchanged rendering for a value without backticks, and the step 1 rule in the twin; update the one existing reason assertion to the code-span form.
4. `CLAUDE.md` §23.I and `agents.md` (permission prompt reports): extend the "Issue text is untrusted data" sentences. `changelog.d/5810-permission-prompt-code-spans.md` [new] (`security`).

## Files & Modules

- `workflow-templates/.claude/scripts/permission_prompts.py`
- `workflow-templates/.claude/commands/implement-issue-claude.md`
- `tests/test_permission_prompt_duplicates.py`
- `CLAUDE.md`
- `agents.md`
- `changelog.d/5810-permission-prompt-code-spans.md` [new]

## Tests

- Unit: the new cases in `tests/test_permission_prompt_duplicates.py` (already in `ci.yml`), which load the twin.
- Parity: `tests/test_permission_prompts.py::test_template_parity` and `tests/test_implement_issue_claude_command.py::test_template_parity` stay red until the twin sync, then pass.
- Full: `python3 -m pytest tests/test_permission_prompts.py tests/test_permission_prompt_duplicates.py tests/test_implement_issue_claude_command.py tests/test_claude_md_section_numbers.py` and `ruff check` on the changed Python files.

## Risks & Mitigations

- A reason that used Markdown for emphasis now shows its backticks literally. ACCEPTED — readability cost only; the reason is diagnostic text.
- The `.claude/` copies stay unescaped until the twin sync. Mitigation: the phase PR carries a hold claim and the twin-sync blocker, and the #4867 security pass waits on this issue.

## Rollout

Ships with the #4867 project's final PR into `main`, then to consumers on the next `@stable` sync of `.claude/`. No flag; it affects only issues and comments filed after the sync.

## References

- #5810 (this finding), #3576 (security audit tracker), #4867 / PR #4883 (base project), PR #5649 (fix 3b, `_pattern_line`), #4785 (twin sync).
