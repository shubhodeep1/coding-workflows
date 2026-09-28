# Stop filing Auto-mode classifier outages as permission-prompt issues

Source issue: shubhodeep1/coding-workflows#4751 (https://github.com/shubhodeep1/coding-workflows/issues/4751)
Base branch: main
Security pass: run

## Summary

`permission_prompts.py file` keeps listing Auto-mode denials whose reason is `Classifier unavailable` in the stage report, but no longer files them as `ai:permission-prompt` issues. That reason means the Auto-mode classifier was down: it says nothing about the command. Issue #4751 is one of these. Its command (`git fetch -q … && git show …`) already matches the exact allow rules `Bash(git fetch *)` and `Bash(git show *)` in `.claude/settings.json`, so no command file, helper, or allow rule in this repo could have prevented the denial.

## Context

- Issue #4751 was filed by `.claude/scripts/permission_prompts.py` (CLAUDE.md §23.I) for one `PermissionDenied` event at 2026-09-28T07:36:41Z in session `session_011pBDarB1ebFsodR5wUM4YS` (the `issue-4687-bind-rejections-to-consensus-ids — completion` stage). The reason given was `Classifier unavailable`.
- The command splits on `&&` into `git fetch -q origin <branches> main` and `git show origin/<branch>:<path>`. Claude Code matches each subcommand on its own. `Bash(git fetch *)` and `Bash(git show *)` cover both, and have been in `.claude/settings.json` since #4526 (2026-09-26).
- Claude Code's permission-modes documentation says that when the Auto-mode classifier is unavailable, auto mode "cannot determine the safety of an action" and denies the call, and the `PermissionDenied` hook fires. It is an outage, not a verdict on the command.
- The same outage produced six more issues within an hour, all with the reason `Classifier unavailable`: #4749, #4750, #4759, #4760, #4761, #4762, and #4767 at 08:19Z. #4750 is `get_session` on the claude-code-remote server, which `.claude/settings.json` allowlists by name. Each issue starts a full Opus issue-mode project through the Claude issue pickup, so every outage costs several projects that have nothing to fix.
- `permission_prompts.py` groups records by event, tool, and command shape (`group_patterns`), and `file_patterns` files every pattern with new occurrences. Nothing in it looks at the reason.

## Goals

- `permission_prompts.py file` opens no issue and posts no comment for a `PermissionDenied` record whose reason is a classifier outage (`Classifier unavailable`, case-insensitive, leading and trailing whitespace ignored, any text after it allowed).
- Those records still show up: `report` and `file` keep them in `total` and in their pattern's `count`, and name them in a new `transient` count (total and per pattern), so the stage report's `Permission prompts:` line still shows the outage.
- `file`'s JSON gains a `not_filed_transient` list (`signature`, `occurrences`) for patterns that had outage records nobody filed.
- A pattern with both outage and real denials or prompts is filed with the real ones only: its occurrence count, reasons, and example come from the non-outage records.
- Every other record (every `PermissionRequest`, and every `PermissionDenied` with any other reason or none) is filed exactly as before.

## Non-goals

- Closing or commenting on the sibling outage issues (#4749, #4750, #4759, #4760, #4761, #4762, #4767). Each is its own issue with its own chain, and closing an issue this session did not open is a §23.C ask-first write. The progress comment lists them for a human to close as duplicates of this fix.
- Changing `.claude/settings.json` allow rules. The #4751 command is already approved by exact rules.
- Changing the logger hook (`.claude/hooks/permission_prompt_logger.py`). It keeps logging every event, so the report still sees outages.
- Retrying a denied call or detecting a classifier outage in the session.

## Constraints

- §5: the change stays in `permission_prompts.py` (and its byte-identical template copy), its tests, and the docs that describe filing.
- §6: no existing identifier, JSON key, label, or marker is renamed or removed. `report` and `file` keep every current key; `transient` and `not_filed_transient` are new keys. New names (`TRANSIENT_DENIAL_REASONS`, `is_transient_denial`) do not collide with anything in the module or the tests.
- §9: tabs, and opening braces are N/A in Python.
- §15: no new API calls. Removing outage records can only reduce the one list read and the per-pattern POSTs.
- §23.I: filing rules are documented in CLAUDE.md §23.I and agents.md, and both are updated in the same PR.
- §27: no workflow file grows.
- §28.C: the phase edits `.claude/scripts/permission_prompts.py` and `workflow-templates/.claude/scripts/permission_prompts.py` (protected paths), so it stops before it starts until a `Protected-path approval: phase 1` line is recorded.
- `tests/test_permission_prompts.py::test_template_parity` requires the template copy to stay byte-identical.

## Approach

Add a module constant `TRANSIENT_DENIAL_REASONS = ("classifier unavailable",)` and a helper `is_transient_denial(record)`. The helper returns true when the record's `event` is `PermissionDenied` and its reason, stripped and lower-cased, starts with one of those prefixes. `group_patterns` counts each pattern's transient records in a new `transient` field. `report` adds a top-level `transient` total and a per-pattern `transient` count. `file_patterns` groups only the non-transient records for filing (so the `filed-state.json` counts, occurrence counts, reasons, and examples come from filable records only). It lists every pattern that had transient records and no filable new occurrences under `not_filed_transient`.

Alternatives considered (AD-1, AD-3): closing the issue with no code change leaves the next outage to file the same noise again; adding allow rules or rewriting the command cannot help, because the rules already match; dropping the records in the logger hook would hide outages from the stage report.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` writes one phase per issue.

1. **Phase 1: report classifier outages without filing them.**
   - Files: `.claude/scripts/permission_prompts.py`, `workflow-templates/.claude/scripts/permission_prompts.py`, `tests/test_permission_prompts.py`, `CLAUDE.md` (§23.I), `agents.md`, `changelog.d/4751-skip-classifier-outage-denials.md`.
   - protected paths: `.claude/scripts/permission_prompts.py`, `workflow-templates/.claude/scripts/permission_prompts.py`
   - Done when: the new tests pass, `tests/test_permission_prompts.py` passes in full (template parity included), a `file --dry-run` over a log holding only outage denials files nothing and lists the pattern under `not_filed_transient`, and the docs describe the rule.
   - Rollback: revert the PR. The script goes back to filing every pattern, and no stored state is migrated.

## Implementation Steps

1. `.claude/scripts/permission_prompts.py`: add `TRANSIENT_DENIAL_REASONS` and `is_transient_denial`; add the `transient` count to `group_patterns` and `report`; filter transient records in `file_patterns` and add `not_filed_transient`; document the rule in the module docstring.
2. Copy the script byte for byte to `workflow-templates/.claude/scripts/permission_prompts.py`.
3. `tests/test_permission_prompts.py`: add tests that
   - an outage-only log files nothing, makes no API call, and lists `not_filed_transient`;
   - a mixed pattern files only its non-outage occurrences, with a non-outage reason;
   - the match ignores case and whitespace, accepts a suffix, and applies only to `PermissionDenied` (a `PermissionRequest` with that reason is still filed);
   - `report` counts outages in `total`, `count`, and `transient`.
4. CLAUDE.md §23.I and agents.md: one sentence each saying outage denials are reported and never filed.
5. Add the `changelog.d/4751-skip-classifier-outage-denials.md` fragment (§20, `changed`).

## Files & Modules

- `.claude/scripts/permission_prompts.py`
- `workflow-templates/.claude/scripts/permission_prompts.py`
- `tests/test_permission_prompts.py`
- `CLAUDE.md` (the root file; `workflow-templates/CLAUDE.md` is a symlink to it)
- `agents.md`
- `changelog.d/4751-skip-classifier-outage-denials.md` [new]

## Tests

- Unit: the new cases in `tests/test_permission_prompts.py` above, plus the existing suite unchanged (signatures, shapes, redaction, state, parity, wiring).
- Local run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_permission_prompts.py`, and `permission_prompts.py file --dry-run --log-dir <tmp>` over a hand-written outage log.

## Risks & Mitigations

- A real denial whose reason starts with "Classifier unavailable" would go unfiled. ACCEPTED: that reason is Claude Code's fixed outage text, and the report still counts it.
- Claude Code may change the outage wording. Mitigation: the prefix list is one constant; a new wording only files issues again, which is today's behaviour.
- A long outage in a consumer repo is unaffected, because consumers only report.

## Rollout

The change ships with the next `@stable` sync of `.claude/` (consumer copies only report, so nothing changes there). In coding-workflows it takes effect at the next stage that runs `permission_prompts.py file` after merge. There is no flag and no stored-state migration.

## References

- Issue #4751; sibling outage issues #4749, #4750, #4759, #4760, #4761, #4762, #4767.
- CLAUDE.md §23.I (permission prompt reports), §28.C (protected paths).
- Claude Code permission modes: https://code.claude.com/docs/en/permission-modes (classifier unavailable → the call is denied and `PermissionDenied` fires).

## Auto-decisions

- AD-1 [plan, 2026-09-28] How should issue #4751 be resolved, given that its command already matches exact allow rules and was denied only because the classifier was unavailable? — Picked: A — stop filing classifier-outage denials as issues, and keep them in the report. Alternatives: B — close #4751 as not planned with no code change; C — add or reshape allow rules for the command. Why: the rules already match, so only the filing rule can stop the next outage from spawning a project per command shape. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Which records count as a classifier outage? — Picked: A — `PermissionDenied` records whose reason, stripped and lower-cased, starts with `classifier unavailable`. Alternatives: B — exact string only; C — also denials with no reason. Why: this is the narrowest match that survives a suffix or a case change, and a denial with no reason may be a real block. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Where does the filter live? — Picked: A — in `permission_prompts.py file`, with the records kept in the report. Alternatives: B — skip logging them in `permission_prompt_logger.py`. Why: the stage report must still show that an outage denied calls. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] What happens to the sibling outage issues #4749, #4750, #4759, #4760, #4761, #4762, #4767? — Picked: A — leave them alone and list them in this issue's progress comment for a human to close. Alternatives: B — close them as duplicates from this session. Why: one issue per chain, and closing an issue this session did not open is §23.C ask-first. Applied in: no code change. Status: pending review

## Notes

- Permission mode of the planning session: `auto`.
- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 4751` → `{"skip": false, "label": null, "reason": "no skip label"}`.
