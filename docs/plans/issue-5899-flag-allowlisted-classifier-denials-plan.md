# Flag Auto-mode denials of already-allowlisted commands

Source issue: shubhodeep1/coding-workflows#5899 (https://github.com/shubhodeep1/coding-workflows/issues/5899)
Base branch: main
Security pass: run

## Summary

Two unattended stage sessions hit an Auto-mode classifier denial (`[Modify Shared Resources]`) for a standalone `git merge --no-edit origin/<issue base>` even though `.claude/settings.json` allowlists `Bash(git merge *)`. This plan makes the permission prompt report say when a denied command already matches an allow rule, so these issues stop asking for a reshaped command or another allow rule. It also states the project-branch sync merge as a routine write in CLAUDE.md §23.B, which the classifier reads.

## Context

- Issue #5899 (`ai:permission-prompt`, sig `cc3a510041a7`) was filed by `.claude/scripts/permission_prompts.py`. It covers two occurrences, both the issue-mode project-branch sync (`/implement-plan-claude` step 2 and Issue Mode: `git merge --no-edit origin/<issue base>`, where `<issue base>` is another project's `claude/implement-plan-*` branch):
  - session `session_01PnXcbxf3thESKMAr71wzhB` (issue-5809 review round) merged `origin/claude/implement-plan-issue-4867-close-permission-prompt-duplicates`;
  - session `session_01LXFrYb2Bxgncqt2FeLPyHu` (issue-5582 validation read) merged `origin/claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason`.
- #5293 fixed the earlier chained form (`git fetch … | tail && git merge … ; git status …`) by requiring each git command to run alone, where it "matches its `permissions.allow` rule" (`.claude/commands/implement-plan-claude.md`, Helpers). Both #5899 occurrences already ran alone, so that fix does not cover them.
- Findings while planning (2026-10-01):
  - `.claude/settings.json:19` holds `Bash(git merge *)`. Per the Claude Code permission docs, a narrow allow rule resolves before the Auto-mode classifier, and `Bash(git merge *)` is not in the list of rules dropped on entering Auto mode.
  - Both PreToolUse Bash hooks (`pr_merge_status_guard.py`, `gh_api_write_guard.py`) return no decision for the denied command. Each was run with the exact payload and printed nothing (exit 0).
  - The same standalone merge of `origin/claude/implement-plan-issue-4867-close-permission-prompt-duplicates`, run on a scratch branch in the planning session, completed with no prompt.
  - Later in the same planning session, an allowlisted `git fetch -q origin <several refs>` was also stopped by the classifier (`[Auto-Mode Bypass]`). So an allowlisted command can reach the classifier, but the planning session could not establish when or why without probing the permission system further. CLAUDE.md §8 calls for diagnostics first in that case.
- `.claude/hooks/permission_prompt_logger.py` already records `permission_mode` and `cwd` for each event. `permission_prompts.py` drops both from the issue text and never checks the settings allow list, so every issue gives the same "reshape the command or add an allow rule" guidance, even when an allow rule already matches.

## Goals

- When a filed or commented Bash pattern's latest command is a single command (no shell operators) that matches a `Bash(...)` rule in the checkout's `.claude/settings.json` `permissions.allow`, the issue body and the "Seen again" comment name that rule. They also say that reshaping the command or adding another allow rule cannot clear the pattern.
- The issue body and comment show the permission mode(s) the occurrences were logged under.
- `permission_prompts.py report` includes the matching rule (`allow_rule`, or `null`) for each pattern.
- CLAUDE.md §23.B lists the local `git merge` that syncs a `claude/*` branch with `origin/<default>`, `origin/<issue base>` (possibly another project's `claude/*` branch), or a PR's base branch as a routine write.
- Nothing changes for a command that matches no allow rule: same signature, same title, same guidance.

## Non-goals

- Adding, widening, or removing any `permissions.allow` rule (it already matches).
- Moving the merge into a new helper script: a helper is approved by the same allow-rule mechanism, which the evidence shows is the part that did not take effect.
- Changing Claude Code's classifier or Auto-mode settings. `autoMode` is not read from project settings, and user or managed settings are outside this repo.
- Closing #5899 as not planned: the denial is not an ask-first operation or a protected-path edit.

## Constraints

- §5: smallest change. Diagnostics plus one CLAUDE.md bullet.
- §6: no rename. Pattern signatures, the marker, `issue_title`, and the existing JSON keys stay unchanged; `allow_rule` and `permission_modes` are new keys.
- §8: diagnostic first. The cause is unconfirmed, so this phase makes the next occurrence self-describing instead of guessing a fix.
- §9: tabs in Python, as in the existing files.
- §15: no new API calls. The allow-list check reads a local file.
- §20: a `changelog.d/` fragment, because issue text and report output change.
- §23.I: issue text stays untrusted data. The rule string comes from the local settings file, never from the session log.
- §28.C (interim twin-first default): the phase edits `.claude/scripts/permission_prompts.py`, so it edits only the `workflow-templates/.claude/` twin and stops for the `[claude-twin-sync]` copy.

## Approach

`permission_prompts.py` gains `allow_rule_for(command, settings_path)`. It returns the first `Bash(...)` entry in `permissions.allow` that matches the whole command, using Claude Code's documented wildcard semantics: `*` matches any run of characters, and a trailing ` *` also matches the bare command. It returns `None` when the command contains a shell operator or newline (`;`, `&`, `|`, `<`, `>`, `(`, `)`, `` ` ``, `$(`), when settings cannot be read, or when nothing matches. Chained commands are not checked, because #5293 already covers them and a prefix rule does not approve a chain.

`group_patterns` stores the latest example's `allow_rule` and the set of `permission_mode` values on each pattern. `_occurrence_block`, which both the issue body and the comment use, adds a `**Permission mode:**` line, and a `**Already allowlisted:**` paragraph when `allow_rule` is set. The settings path defaults to `.claude/settings.json` beside the script's own `.claude/` directory and can be overridden for tests.

CLAUDE.md §23.B gets one bullet for the sync merge. The classifier reads CLAUDE.md (Claude Code auto-mode docs), so the routine nature of the step is stated where the classifier sees it. `workflow-templates/CLAUDE.md` is a symlink to the root file, so the edit covers both.

Alternatives considered: AD-1 and AD-3 below.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — report allowlisted denials and document the sync merge.**
   - Files: `workflow-templates/.claude/scripts/permission_prompts.py` (twin; `.claude/scripts/permission_prompts.py` is reached only by the `[claude-twin-sync]` copy), `tests/test_permission_prompts.py`, `CLAUDE.md` (§23.B), `agents.md` (prompt-report bullet), `changelog.d/5899-flag-allowlisted-classifier-denials.md` [new], `docs/implement-plan/issue-5899-flag-allowlisted-classifier-denials.md` [new, log].
   - Protected paths: `.claude/scripts/permission_prompts.py` (twin-first).
   - Done: the new tests pass against the twin. After the twin sync, the full `tests/test_permission_prompts.py` (template parity included) and `tests/test_claude_md_section_numbers.py` pass. A dry-run `file` on a log holding the #5899 command prints a body with the `Already allowlisted` paragraph naming `Bash(git merge *)`.
   - Rollback: revert the phase PR. The added JSON keys and issue lines go away; nothing persists.

## Implementation Steps

1. `workflow-templates/.claude/scripts/permission_prompts.py`:
   - Add `SETTINGS_PATH` (default `Path(__file__).resolve().parent.parent / "settings.json"`).
   - Add `allow_rule_for(command, settings_path)` (the matcher above) and `_bash_allow_rules(settings_path)`, which reads `permissions.allow` and keeps only `Bash(...)` entries. Any read or parse error returns `[]`.
   - In `group_patterns`, set `pattern["allow_rule"]` from the latest Bash record and collect `pattern["permission_modes"]`. `group_patterns` and `report` take an optional `settings_path`.
   - `report` adds `allow_rule` per pattern.
   - `_occurrence_block` adds the `Permission mode` line and the `Already allowlisted` paragraph.
   - The module docstring describes both.
2. `tests/test_permission_prompts.py`: new tests that load the twin script:
   - matcher cases: trailing ` *` matches the bare command and arguments; an interior `*`; exact rules; no match for chained, piped, redirected, or substituted commands; no match for a non-Bash rule; unreadable or invalid settings give `None`;
   - grouping and the report carry `allow_rule` and `permission_modes`;
   - the issue body and the comment include the paragraph only when a rule matches;
   - the #5899 command against the real `.claude/settings.json` gives `Bash(git merge *)`.
3. `CLAUDE.md` §23.B: add the routine sync-merge bullet.
4. `agents.md`: extend the `permission_prompts.py file` bullet with the allow-rule and permission-mode lines.
5. `changelog.d/5899-flag-allowlisted-classifier-denials.md` (`<!-- changelog: changed -->`).

## Files & Modules

- `workflow-templates/.claude/scripts/permission_prompts.py`
- `.claude/scripts/permission_prompts.py` (by the `[claude-twin-sync]` copy only)
- `tests/test_permission_prompts.py`
- `CLAUDE.md` (and `workflow-templates/CLAUDE.md` through its symlink)
- `agents.md`
- `changelog.d/5899-flag-allowlisted-classifier-denials.md` [new]
- `docs/plans/issue-5899-flag-allowlisted-classifier-denials-plan.md` [new]
- `docs/implement-plan/issue-5899-flag-allowlisted-classifier-denials.md` [new]

## Tests

- Unit: the new cases in `tests/test_permission_prompts.py` (step 2), run with `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_permission_prompts.py -q -p no:cacheprovider`.
- Regression: the existing permission-prompt tests and `tests/test_claude_md_section_numbers.py`. `test_template_parity` stays red until the twin sync, as expected under twin-first.
- End-to-end: a dry-run `permission_prompts.py file` against a temporary log holding the #5899 record, checking the printed summary. Output only: a dry run posts nothing.

## Risks & Mitigations

- The matcher differs from Claude Code's own on an edge case → it reports only and never approves anything. A false "already allowlisted" costs one misleading line in an issue. Tests pin the documented semantics.
- The CLAUDE.md bullet does not change the classifier's decision → ACCEPTED: the diagnostic half still makes the next occurrence self-describing. A recurrence comments "Seen again" on #5899 with the rule named.
- Conflict with in-flight edits to `permission_prompts.py` (issue-4867 and issue-5809 projects) → the change is additive and local to `group_patterns`, `report`, and `_occurrence_block`. The chain's step 2 sync and conflict rounds resolve overlaps.

## Rollout

Ships with the next `@stable` sync, through the `.claude/` and `CLAUDE.md` templates. No flag, no migration, no new env var. Rollback is a revert.

## Auto-decisions

- AD-1 [plan, 2026-10-01] The root cause of the denial cannot be confirmed (standalone command, matching allow rule, no hook decision, not reproducible on demand). Which fix? — Picked: A — diagnostics in the prompt report plus a CLAUDE.md §23.B routine-write bullet. Alternatives: B — close #5899 as not planned; C — move the sync merge into a new allowlisted helper script with its own rule. Why: §8 asks for diagnostics when the cause is unclear; C relies on the same allow-rule mechanism that did not take effect; B leaves recurrences unexplained. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] Where should the sync merge be described for the classifier? — Picked: A — a bullet in CLAUDE.md §23.B (routine repository writes). Alternatives: B — only in `.claude/commands/implement-plan-claude.md`; C — nowhere. Why: the classifier reads CLAUDE.md, and §23.B is where routine writes are listed; B would also add a protected-path edit. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] How should filed issues present an allowlisted denial? — Picked: A — a conditional "Already allowlisted" paragraph and a permission-mode line in the occurrence block, leaving the generic "How to fix" list unchanged. Alternatives: B — rewrite the generic guidance for every issue; C — a separate label for allowlisted denials. Why: smallest change (§5) with no effect on other patterns; a new label would need a label-contract change (§6). Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` returned `{"skip": false, "label": null, "reason": "no skip label"}`.

## References

- #5899 (this issue), #5293 (the chained-merge fix this follows), #4785 (twin-sync sunset), #4750 (`get_session` prompted in Auto mode despite the allowlist).
- Claude Code docs: permission-modes ("How the classifier evaluates actions"), auto-mode-config.
