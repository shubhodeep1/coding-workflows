# Stop classifier-outage denials from stalling stage preflight and filing permission-prompt issues

Source issue: shubhodeep1/coding-workflows#4750 (https://github.com/shubhodeep1/coding-workflows/issues/4750)
Base branch: main
Security pass: run

## Summary

Issue #4750 reports an Auto-mode denial of `get_session` (claude-code-remote
MCP) with the reason `Classifier unavailable`. The call was already
allowlisted. The denial came from a transient outage of the Auto-mode
classifier, not from a missing rule. This plan makes `/implement-plan-claude`
resume stages stop depending on a self `get_session` call in step 0, stops
retrying a denied `get_session` in a loop, and makes
`.claude/scripts/permission_prompts.py` report classifier-outage denials as a
count instead of filing them as fixable `ai:permission-prompt` issues.

## Context

- The issue body: event `PermissionDenied`, tool
  `mcp__bf7c680d-5fdc-5ef4-b4a0-abadb619bf0a__get_session`, input `{}` (the
  self lookup), reason `Classifier unavailable`. There were 5 occurrences in
  session `session_011pBDarB1ebFsodR5wUM4YS` (07:36:09Z–07:37:11Z), and a
  comment adds 5 more in `session_01X34Rkb13B5HJabtRPdz8ni`
  (07:36:15Z–07:36:54Z). Both sessions are `/implement-plan-claude` resume
  stages: `implement-plan issue-4687-… — completion` and
  `implement-plan issue-4618-… — conformance 2/3`. Both were created around
  07:33Z and denied during step 0.
- The call is already allowlisted, exactly and as a whole server:
  `.claude/settings.json:96` (`mcp__bf7c680d-5fdc-5ef4-b4a0-abadb619bf0a`) and
  `.claude/settings.json:104` (`…__get_session`). Step 3 of the issue's
  "How to fix" has nothing left to add. `.claude/commands/implement-plan-claude.md:372`
  already records that the Auto-mode classifier "has been seen to prompt on
  `get_session` in a checker". That is why the checker reads its own id from
  `CLAUDE_CODE_REMOTE_SESSION_ID`
  (`.claude/commands/implement-plan-claude.md:235,249`; also
  `.claude/commands/fix-claude-pr.md:13` and CLAUDE.md §26.B / §26.D).
- It was an outage, not a per-command gap. Every `ai:permission-prompt` issue
  filed in the same window gives the same reason, `Classifier unavailable`:
  #4749, #4751 (session `…011p…`, 07:36Z), #4759, #4760, #4761 (session
  `…01X3…`, 07:36Z), #4762 (08:03Z), and #4767 (session `…01RT…`, 08:19Z).
  These are plain Bash reads like `git status`, `git fetch`, and `grep`.
  `permission_prompts.py` (`group_patterns`, `file_patterns`) files each one
  as a separate `ai:permission-prompt` + `ai:claude` issue. Each issue starts
  a full `/implement-issue-claude` Opus project for something no repository
  change can fix.
- `/implement-plan-claude` step 0 (`.claude/commands/implement-plan-claude.md:9`)
  tells every session, resume stages included, to call `get_session` with no
  `session_id` to record its id and `permission_mode`. A resume stage skips the
  Auto-mode check (`:10`). The invoking session already recorded the mode in
  the log's `Stage model: … Permission mode: <mode>` line (log format, `:289`).
  A resume stage therefore needs neither value from `get_session`.
- `.claude/hooks/permission_prompt_logger.py:64-73` stores the reason Claude
  Code gives (`reason` or `decision_reason`) on every record, so the filer can
  tell an outage denial apart.

## Goals

- `/implement-plan-claude` step 0 gives a resume stage its own session id from
  Bash (`echo "session_${CLAUDE_CODE_REMOTE_SESSION_ID#cse_}"`) and its
  permission mode from the log's `Permission mode:` line, so a resume stage
  makes no self `get_session` call. It still falls back to `get_session` when
  the log has no such line.
- Step 0 retries a denied `get_session` call once, never in a loop. It states
  what happens when the second call is also denied:
  - the invoking session: stop outside issue mode; in issue mode, record the
    mode as unknown and continue;
  - resume hygiene: leave the previous stage session unarchived and say so.
- `permission_prompts.py report` and `file` leave out every `PermissionDenied`
  record whose reason matches `classifier unavailable` (case-insensitive) from
  `patterns`, `total`, and filing. They count those records under a new
  `outage_denials` summary key. `file` never opens an issue or posts a comment
  for them.
- The workflow-templates mirrors (`workflow-templates/.claude/…`,
  `workflow-templates/CLAUDE.md`) stay byte-identical to the repo copies.

## Non-goals

- No `.claude/settings.json` change. The allow rules already exist.
- No change to `claude-issue-pickup.md`, `implement-issue-claude.md`,
  `fix-claude-pr.md`, or the checker prompt (AD-4). The observed denials all
  came from `/implement-plan-claude` step 0.
- No change to `permission_prompt_logger.py`. It keeps recording every event.
- No closing of the sibling outage issues (#4749, #4751, #4759–#4762, #4767).
  Closing issues this session did not open is a CLAUDE.md §23.C ask-first
  operation, and one issue per project is the rule.

## Constraints

- §5 minimal change set: only the step-0 text, the filer's outage split, and
  the docs that describe them.
- §6 naming immutability: existing summary keys (`total`, `patterns`,
  `filed`, `commented`, `errors`, `skipped`, `dry_run`) keep their names. The
  outage count is a new key, `outage_denials`. New identifiers
  (`CLASSIFIER_OUTAGE_REASON_RE`, `is_classifier_outage`) must not collide
  with anything in `permission_prompts.py` or the modules it loads.
- §7 / §23.I: CLAUDE.md §23.I, `agents.md` (permission prompts section), and
  `README.md` describe what the filer files, so they are updated in the same
  PR.
- §9 style: tabs, opening braces on a new line where the language allows
  them. Python keeps the file's existing tab indentation.
- §15: the filer issues fewer API calls (none for an outage-only log), never
  more.
- §20: the filer's observable behaviour changes, so the PR adds a
  `changelog.d/` fragment.
- §28.C: every file this phase edits under `.claude/**` is a protected path,
  so the phase stops before it starts and asks how to run it.

## Approach

1. Command file: rewrite step 0 of `/implement-plan-claude` so that a resume
   stage reads its id from the environment and its mode from the log. Only
   the invoking session calls `get_session` on itself. A denial gets one retry
   and a defined outcome. This removes the `{}` self lookup that produced
   #4750's ten denials, and caps any remaining `get_session` denial at two
   calls.
2. Filer: add `is_classifier_outage(record)` (a `PermissionDenied` event whose
   reason matches `CLASSIFIER_OUTAGE_REASON_RE`). `report` and `file_patterns`
   group only the other records, and add
   `outage_denials: {count, tools, first_ts, last_ts}`. An outage-only log
   files nothing and makes no API call.

Alternatives considered (see Auto-decisions): an allow rule, which already
exists and did not prevent the denial; filing one shared outage issue, which
would still start a Claude project for something that cannot be fixed; and
changing every command that calls `get_session`, which is wider than the
observed failure.

## Phases & Merge Strategy

This plan is **one phase**. Issue mode (CLAUDE.md §28.A) authorises a
single-phase plan for a standalone issue.

1. **Phase 1 — outage-tolerant step 0 and outage-aware permission-prompt
   filing.**
   - Files: the list under Files & Modules.
   - Protected paths: `.claude/commands/implement-plan-claude.md`,
     `.claude/scripts/permission_prompts.py`, and their
     `workflow-templates/.claude/` mirrors.
   - Done when:
     - step 0 in both copies of `implement-plan-claude.md` carries the
       env-id, log-mode, and one-retry rules;
     - `permission_prompts.py` (both copies) excludes classifier-outage
       denials from filing and reports `outage_denials`;
     - the docs are updated and the changelog fragment is added;
     - `tests/test_permission_prompts.py` covers the split;
     - the full `pytest tests/test_permission_prompts.py` run and the repo's
       template-parity checks pass.
   - Rollback: revert the phase PR. There is no data or state migration, and
     `filed-state.json` keys are unchanged.

## Implementation Steps

Phase 1:
1. `.claude/scripts/permission_prompts.py`:
   - add `CLASSIFIER_OUTAGE_REASON_RE` and `is_classifier_outage`;
   - make `report()` and `file_patterns()` group only non-outage records and
     add the `outage_denials` summary;
   - document the rule in the module docstring.
2. `.claude/commands/implement-plan-claude.md`: step 0 (session id from the
   environment, a resume stage's mode from the log, one retry, the
   denied-twice outcomes); the Permission prompt report line (`:164`); and the
   Output Format `Permission prompts:` line (`:356`), which now shows outage
   denials as not filed.
3. Mirror both files byte-for-byte into `workflow-templates/.claude/…`.
4. CLAUDE.md §23.I (and `workflow-templates/CLAUDE.md`), `agents.md`
   permission-prompt bullets, `README.md` note: classifier-outage denials are
   counted, not filed.
5. `tests/test_permission_prompts.py`: an outage-only log files nothing and
   calls no API. A mixed log files only the real pattern and counts the
   outage. `report` shows `outage_denials`. A non-outage denial reason is
   still filed.
6. `changelog.d/4750-classifier-outage-denials.md` (§20, section `fixed`).

## Files & Modules

- `.claude/scripts/permission_prompts.py`
- `workflow-templates/.claude/scripts/permission_prompts.py`
- `.claude/commands/implement-plan-claude.md`
- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `CLAUDE.md`
- `workflow-templates/CLAUDE.md`
- `agents.md`
- `README.md`
- `tests/test_permission_prompts.py`
- `changelog.d/4750-classifier-outage-denials.md` [new]

## Tests

- Unit: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_permission_prompts.py -q`
  with the new cases from step 5.
- Parity: the existing tests that compare `.claude/` and `CLAUDE.md` with
  their `workflow-templates/` copies (for example the template checks in
  `tests/test_permission_prompts.py` and `tests/test_implement_issue_claude_command.py`),
  run in full.
- The repo's `ci.yml` Python test steps that cover the changed files.

## Risks & Mitigations

- A real permission gap whose reason happens to contain "classifier
  unavailable" would go unfiled. Mitigation: the regex matches only that
  outage text, only on `PermissionDenied`, and the count stays visible in
  every stage report's `Permission prompts:` line.
- A resume stage's log could lack a `Permission mode:` line (older projects).
  Mitigation: step 0 falls back to a self `get_session` in that case.
- The phase edits protected paths. ACCEPTED: §28.C stops the phase for a
  human decision before it starts. The issue text itself says to expect this
  stop.

## Rollout

The change lands on `main` through the project's final PR and reaches consumer
repos on the next `@stable` sync (the `workflow-templates/` mirrors). There is
no flag. Rollback is a revert.

## References

- Issue #4750. Sibling outage issues: #4749, #4751, #4759, #4760, #4761,
  #4762, #4767.
- CLAUDE.md §23.I, §26.B, §28.
- `.claude/commands/implement-plan-claude.md:9-12,372`.

## Auto-decisions

- AD-1 [plan, 2026-09-28] How should #4750 be fixed, given the call was
  already allowlisted and the reason was `Classifier unavailable`?
  - Picked: A. Treat it as a classifier outage. Remove the resume stage's
    self `get_session` from step 0, cap `get_session` retries at one, and
    stop filing outage denials.
  - Alternatives:
    - B. Only the command-file change.
    - C. Only the filer change.
    - D. Add an allow rule. It already exists at `.claude/settings.json:104`
      and changes nothing.
  - Why: the command change removes the ten observed denials, and the filer
    change stops one outage from starting N Opus projects.
  - Applied in: phase 1 PR. Status: pending review.
- AD-2 [plan, 2026-09-28] What should the filer do with classifier-outage
  denials?
  - Picked: A. Report them as a count under a new `outage_denials` key and
    never file them.
  - Alternatives:
    - B. File one shared outage issue and comment on it at each outage.
    - C. Keep filing them per pattern.
  - Why: no repository change can fix an outage, and every filed issue is
    routed to a Claude project.
  - Applied in: phase 1 PR. Status: pending review.
- AD-3 [plan, 2026-09-28] How is an outage denial recognised?
  - Picked: A. A `PermissionDenied` record whose reason matches
    `classifier unavailable` (case-insensitive).
  - Alternatives:
    - B. Any `PermissionDenied` whose reason mentions "classifier".
    - C. Any `PermissionDenied`.
  - Why: this is the exact text in all eight issues, and anything wider could
    hide real gaps.
  - Applied in: phase 1 PR. Status: pending review.
- AD-4 [plan, 2026-09-28] Which command files change?
  - Picked: A. Only `/implement-plan-claude` step 0 and its template mirror,
    where every observed denial came from.
  - Alternatives:
    - B. Also `implement-issue-claude.md` and `claude-issue-pickup.md`.
  - Why: §5 minimal change set.
  - Applied in: phase 1 PR. Status: pending review.

## Notes

- `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`.
  The security pass runs.
- The sibling outage issues (#4749, #4751, #4759–#4762, #4767) have the same
  root cause. They are left open, and a human decides whether to close them
  once this lands. Closing them is §23.C.
