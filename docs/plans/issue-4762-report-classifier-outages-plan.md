# Report Auto-mode classifier outages instead of filing them as permission-prompt issues

Source issue: shubhodeep1/coding-workflows#4762 (https://github.com/shubhodeep1/coding-workflows/issues/4762)
Base branch: main
Security pass: run

## Summary

Issue #4762 was filed for an Auto-mode denial whose only reason was
`Classifier unavailable`. In the same window, 2026-09-28 07:36Z to 08:19Z,
the classifier outage denied calls that an allow rule already covers. No
command shape or allow rule can prevent that denial. Teach
`.claude/scripts/permission_prompts.py` to count classifier-outage denials
in its report and to stop filing them as per-pattern `ai:permission-prompt`
issues.

## Context

- Issue #4762 (filed by `.claude/scripts/permission_prompts.py`): pattern
  `export * && timeout * -m * -q -p * -k * 2>& * | tail -2 ; python3 tests/test_conflict_dis…`,
  2 occurrences at 2026-09-28T08:03:26Z–08:03:30Z in session
  `session_01X34Rkb13B5HJabtRPdz8ni` (the `issue-4618-sweep-dispatch-default-branch`
  conformance 2/3 stage). Reason: `Classifier unavailable`.
- Two of its subcommands match no `permissions.allow` rule: `export
  PYTHONDONTWRITEBYTECODE=1` and `python3 tests/test_conflict_dispatch_active_run_visibility.py`.
  The rest match: `timeout` is a wrapper Claude Code strips before matching,
  `python3 -m pytest *` is allowlisted, and `2>&1` is not checked
  (https://code.claude.com/docs/en/permissions.md). So this call did need the
  classifier.
- The same outage also denied calls whose every part is allowlisted:
  - #4751: `git fetch -q … && git show …` (`Bash(git fetch *)`, `Bash(git show *)`).
  - #4750: a `get_session` call to the claude-code-remote MCP server, which
    `.claude/settings.json` allowlists as a whole server.
  
  Five more issues from the same window carry the same reason: #4749,
  #4759, #4760, #4761, and #4767. A session with server-side classifier review
  sends even read-only shell commands to the classifier
  (https://code.claude.com/docs/en/permission-modes.md, "How the classifier
  evaluates actions"). The docs call a failed classifier request "usually
  transient" (same page, "cannot determine the safety").
- `permission_prompts.py` groups every `PermissionRequest` and
  `PermissionDenied` record into a pattern, whatever its reason. It files
  one issue for each new pattern. So one classifier outage opened eight
  issues in 45 minutes, and each will start its own Claude issue project. The
  "How to fix" steps in those issue bodies cannot fix an outage.
- CLAUDE.md §23.I documents the prompt reports. `.claude/scripts/permission_prompts.py`
  and its byte-identical twin `workflow-templates/.claude/scripts/permission_prompts.py`
  are tested by `tests/test_permission_prompts.py`, which runs in its own
  `ci.yml` step.

## Automation (CLAUDE.md §18.E)

- No new script. The change extends `.claude/scripts/permission_prompts.py`
  (and its template twin).
- Entry point: `/implement-plan-claude` step 14 already runs
  `permission_prompts.py file` at the end of every stage. No new wiring.
- No supervisor, no DB work, and no `docs/scripts-pending-removal.md` entry.

## Goals

- A `PermissionDenied` record whose reason is `Classifier unavailable`
  (compared after trimming, ignoring case) is an **outage record**. It forms
  no pattern, so `file` never opens an issue for it and never comments on one.
- `report` and `file` print an `outages` object: the outage record count, the
  first and last timestamps, and the distinct tool names (at most 10). Stage
  reports show that the outage happened.
- Every other record, including a `PermissionRequest` with any reason and a
  denial with any other reason, is grouped and filed exactly as today.
  Signatures and `filed-state.json` counts do not change.
- CLAUDE.md §23.I, the script's module docstring, and `agents.md` say that
  classifier-outage denials are counted and never filed, and why. They cite
  issue #4762.
- Tests in `tests/test_permission_prompts.py` cover the split, the unchanged
  filing of other denials, the JSON field, and the CLAUDE.md text. The
  template twin stays byte-identical.

## Non-goals

- No new `permissions.allow` rule, and no command-shape rule for tests: the
  outage denied allowlisted calls too (AD-2).
- No change to `.claude/hooks/permission_prompt_logger.py` or to the log
  format.
- No change to the text of issue bodies for filed patterns.
- Sibling issues #4749, #4750, #4751, #4759, #4760, #4761, and #4767 are not
  folded in or closed here (AD-5). Closing an issue this session did not open
  is a §23.C ask-first operation.
- No retry of a denied call. The session already "went on without the call".

## Constraints

- §5: one constant and one record split in the script, the same bytes in the
  twin, one sentence each in CLAUDE.md §23.I and `agents.md`, tests, and a
  fragment.
- §6: existing JSON keys (`total`, `patterns`, `filed`, `commented`,
  `errors`, `skipped`, `dry_run`), the signature scheme, and the marker are
  unchanged. `outages` is a new key. The new identifiers
  (`OUTAGE_REASONS`, `is_outage_record`, `outages`) collide with nothing in
  `.claude/`, `scripts/`, `tests/`, or `workflow-templates/.claude/` (checked
  with grep).
- §9: tabs, like the rest of the script and its tests.
- §14/§20: the script ships to consumers through the `.claude/` sync, and
  CLAUDE.md through its symlink, so a `changelog.d/` fragment is required.
- §15: no new API call. Fewer POSTs are made, since outage denials file
  nothing.
- §23.I: the fix narrows filing and never widens a permission.
- §28.C: the phase edits the repository root's `.claude/scripts/`, a
  protected path, so it stops at `Status: BLOCKED` before it starts until a
  `Protected-path approval: phase 1` line is recorded.

## Approach

1. In `permission_prompts.py`, add `OUTAGE_REASONS = frozenset({"classifier unavailable"})`
   and `is_outage_record(record)`. It is true when `event` is
   `PermissionDenied` and the reason, trimmed and lower-cased, is in
   `OUTAGE_REASONS`.
2. `report()` and `file_patterns()` split the loaded records. Outage records
   feed only a new `outages` summary: `count`, `first_ts`, `last_ts`, and
   `tools` (distinct names, at most 10). `group_patterns()` gets only the
   remaining records. `total` stays the count of grouped records. `outages`
   is always present, with count 0 when there were none.
3. Copy the script byte for byte to `workflow-templates/.claude/scripts/`.
4. CLAUDE.md §23.I "Prompt reports" bullet: add one sentence, after the
   sentence on grouping into patterns: "A denial whose reason is
   `Classifier unavailable` is an Auto-mode classifier outage, not a rule or
   command-shape gap: the outage denies allowlisted calls too (issue #4762).
   It is counted under `outages` in the report and never filed."

Alternatives: see AD-2 (a CLAUDE.md command-shape rule, or new allow rules)
and AD-3 (one aggregate outage issue).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan:
the change is one script with its twin, its tests, and two doc sentences, and
it cannot be split usefully.

1. **Phase 1 — count classifier-outage denials instead of filing them.**
   *Protected paths: `.claude/scripts/permission_prompts.py`.*
   - Files: `.claude/scripts/permission_prompts.py`,
     `workflow-templates/.claude/scripts/permission_prompts.py`,
     `tests/test_permission_prompts.py`, `CLAUDE.md` (§23.I), `agents.md`,
     `changelog.d/4762-report-classifier-outages.md` [new].
   - Done when: outage records are excluded from patterns and filing and are
     counted under `outages`, the new tests pass,
     `tests/test_permission_prompts.py` and
     `tests/test_claude_md_section_numbers.py` pass, and the twin is
     byte-identical.
   - Rollback: revert the phase PR. Outage denials are then filed as
     per-pattern issues again, as today.

## Implementation Steps

1. Phase 1: in `.claude/scripts/permission_prompts.py`, add `OUTAGE_REASONS`
   near the other constants. Add `is_outage_record()` and an
   `outage_summary(records)` helper. Split the records in `report()` and
   `file_patterns()`, add the `outages` key, and describe it in the module
   docstring.
2. Phase 1: copy the file to `workflow-templates/.claude/scripts/permission_prompts.py`.
3. Phase 1: in `tests/test_permission_prompts.py`, add tests for these
   cases:
   - an outage-only log files nothing and makes no API call, and reports
     `outages.count`;
   - a mixed log files only the non-outage pattern;
   - the reason match ignores case and surrounding whitespace;
   - a `PermissionRequest` with that reason is still filed;
   - a denial with another reason is still filed;
   - CLAUDE.md §23.I documents the rule.
4. Phase 1: add the §23.I sentence to CLAUDE.md, and one line to the
   `agents.md` section "Unattended helpers and permission prompt reports".
5. Phase 1: add `changelog.d/4762-report-classifier-outages.md` with
   `<!-- changelog: fixed -->`, per §20.D and §20.G.

## Files & Modules

- `.claude/scripts/permission_prompts.py` (protected path)
- `workflow-templates/.claude/scripts/permission_prompts.py`
- `tests/test_permission_prompts.py`
- `CLAUDE.md` (and so `workflow-templates/CLAUDE.md`, a symlink to it)
- `agents.md`
- `changelog.d/4762-report-classifier-outages.md` [new]

## Tests

- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_permission_prompts.py tests/test_claude_md_section_numbers.py tests/test_changelog_fragment_contract.py`
- The new tests fail before the change and pass after it.
- `cmp .claude/scripts/permission_prompts.py workflow-templates/.claude/scripts/permission_prompts.py`
  prints nothing.
- Run by hand: `permission_prompts.py report --log-dir <fixture dir>` with
  one outage record and one other record. It shows one pattern and
  `outages.count` 1.

## Risks & Mitigations

- A long-lasting classifier failure, such as a Bedrock model-access problem,
  is no longer filed as issues. Mitigation: every stage report still shows
  the `outages` count, and CLAUDE.md names it.
- Claude Code may word the outage reason differently in a later version.
  Then those denials are filed as today, which fails safe (more issues, not
  fewer). The single constant is the only place to extend.
- A real rule gap hidden behind an outage denial, like the `export` and
  `python3 tests/*.py` parts of #4762, is not filed during the outage.
  ACCEPTED: the next occurrence outside an outage is filed as usual.

## Rollout

The script runs at the end of every `/implement-plan-claude` stage in
sessions that check out the merged code. It reaches consumer repos on the
next `@stable` sync, but they only report and file nothing. Nothing to
activate.

## Auto-decisions

- AD-1 [plan, 2026-09-28] Close #4762 as not planned (a transient outage), or fix something? — Picked: A — fix the reporter, and leave the issue to close when the final PR merges. Alternatives: B — close as not planned. Why: the outage opened eight issues in 45 minutes, and more outages will do the same. Closing an issue this session did not open is also a §23.C ask-first operation. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Where does the fix live? — Picked: A — `.claude/scripts/permission_prompts.py` (and twin) stops filing classifier-outage denials, documented in CLAUDE.md §23.I. Alternatives: B — a CLAUDE.md §23.I rule to run tests only in an allowlisted shape (no `export`, no `python3 tests/<file>.py`), which needs no protected-path edit; C — new allow rules such as `Bash(python3 tests/*)`. Why: the same outage denied fully allowlisted calls (#4751, #4750), so neither B nor C would have prevented the denial, and C widens a permission to arbitrary repository code. A protected-path stop is expected, and the issue body says so. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] What happens to an outage denial? — Picked: A — count it under `outages` in the report JSON and file nothing. Alternatives: B — file or comment one aggregate `classifier outage` issue; C — file as today with a note in the body. Why: A is the smallest change (§5), and code cannot fix an outage. B and C still route a Claude project to it. The stage report still shows the outage. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Which records count as outage records? — Picked: A — `PermissionDenied` records whose reason is exactly `Classifier unavailable`, trimmed and case-insensitive. Alternatives: B — also any reason mentioning "cannot determine the safety". Why: A matches the only wording seen in the logs, and any other wording is still filed, so the change fails safe. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] Fold sibling outage issues #4749, #4750, #4751, #4759, #4760, #4761, #4767 into this project? — Picked: A — no. List them in the plan and the final PR body as the same root cause. Alternatives: B — fold them in and close them. Why: `/implement-issue-claude` runs one issue per chain, and closing them is a §23.C ask-first operation. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-28] Does the change need a `changelog.d/` fragment? — Picked: A — yes, `fixed`. Alternatives: B — none. Why: §20.A requires one when what consumer repos receive on the next `@stable` sync changes, and both the script and CLAUDE.md are synced. Applied in: phase 1 PR. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` printed `{"skip": false, "label": null, "reason": "no skip label"}`, so the pass runs.
- In this session's container, `python3 -m pytest` fails with `No module named pytest`: pytest is installed only as a uv tool (`/root/.local/bin/pytest`, Python 3.11). The phase session runs the tests with whichever form is available and records it.

## References

- Issue #4762; outage-window siblings #4749, #4750, #4751, #4759, #4760, #4761, #4767
- Precedent: `docs/plans/issue-4678-edit-files-without-python-heredocs-plan.md` on `claude/implement-plan-issue-4678-edit-files-without-python-heredocs`
- CLAUDE.md §23.I, §28.C
- https://code.claude.com/docs/en/permissions.md (compound commands, stripped wrappers, redirects)
- https://code.claude.com/docs/en/permission-modes.md (how the classifier evaluates actions; failed classifier requests)
