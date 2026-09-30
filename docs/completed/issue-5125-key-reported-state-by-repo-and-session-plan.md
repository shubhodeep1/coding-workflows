# Skip only permission-prompt occurrences already delivered to the filing repository

Source issue: shubhodeep1/coding-workflows#5125 (https://github.com/shubhodeep1/coding-workflows/issues/5125)
Base branch: claude/implement-plan-issue-4755-report-blocking-permission-prompts
Security pass: skip (ai:security: automation-produced issue)

## Summary

The security audit (issue #5125, `A09:2021-Security Logging and Monitoring
Failures`, medium) found that `permission_prompts.py file` skips a pattern as
`already_reported` when **any** session reported it to **any** repository.
A `report-now` that went to a consumer repo's PR, or another session's report,
therefore stops coding-workflows from filing fresh occurrences centrally. The
fix keys the reported state by repository and by the session whose log holds
the occurrences, and skips only occurrences confirmed delivered to the
repository `file` files into.

## Context

- `.claude/scripts/permission_prompts.py:669-690` `_file_pending`: builds
  `reported` from every session bucket in `immediate-state.json`, keeping every
  entry whose target is not `pending`, whatever repository the report went to,
  and line 683 skips the whole pattern when its signature is in that set.
- `.claude/scripts/permission_prompts.py:810-856` `_report_now`: records
  `{"target", "ts", "session"}` per signature and session. The entry does not
  say which repository the report went to (`_report_to_filing_repo` for
  coding-workflows, `_report_to_own_thread` for a consumer's PR or issue), nor
  which log file (hook `session_id`) the occurrences came from.
- `~/.claude/permission-prompts/` is shared by every session on a host (a
  self-hosted runner's home directory), and `file` reads every `*.jsonl` there,
  so its patterns mix sessions and repositories.
- The logger hook writes the Claude Code `session_id` into every record
  (`.claude/hooks/permission_prompt_logger.py` `build_record`), so `file` can
  tell which session each occurrence belongs to.
- Base branch: the issue names the issue-4755 project branch, where
  `report-now` was introduced (project final PR #4773, still a draft).

## Goals

- G1: A `report-now` delivered to another repository (a consumer PR or issue)
  never makes `file` in coding-workflows skip that signature.
- G2: A `report-now` delivered by one session never makes `file` skip
  occurrences logged by another session.
- G3: A session's own occurrences of a signature it already reported to the
  filing repository are still skipped (`already_reported`), as today.
- G4: State written before this change (entries without the new fields) never
  suppresses filing.

## Non-goals

- Changing where `report-now` posts, its cap, its kill switch, or `lookup`.
- Changing the `filed-state.json` format.
- Syncing the `.claude/` copy (twin-first, see Rollout).

## Constraints

- §5 minimal change set: only `_report_now`'s entry, `group_patterns`'
  per-pattern data, and `_file_pending`'s skip change.
- §6: no identifier is renamed or removed; `already_reported` keeps its shape
  (`{"signature", "target"}`). New entry fields `repo` and `log_session` and the
  pattern key `sessions` are new names that collide with nothing in scope.
- §15: no new API calls.
- §20: a `changelog.d/` fragment (`security`).
- §28.C / implement-plan step 4: `.claude/scripts/permission_prompts.py` is a
  protected path; this repo has `workflow-templates/.claude/`, so the phase runs
  under the interim twin-first default.

## Approach

1. `_report_now` adds `"repo": <slug, lowercased>` and
   `"log_session": <the record's session_id>` to the reservation entry.
2. `group_patterns` keeps a per-pattern `sessions` map
   (`{record session_id: occurrences}`). `report` still selects its output keys
   explicitly, so its JSON is unchanged.
3. `_file_pending` counts an entry as delivered only when its target is not
   `pending`, its `repo` equals the slug being filed into (case-insensitive),
   and it names a `log_session`. For each pattern, the occurrences from
   delivered log sessions are covered. When every occurrence is covered, the
   pattern is listed under `already_reported` as today. Otherwise
   `new_count = min(count - filed_state[sig], uncovered occurrences)`, so only
   the other sessions' occurrences are filed.

Alternatives are listed under Auto-decisions.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
issue is one defect in one function.

1. **Phase 1 — key reported state by repository and log session.**
   Files: `workflow-templates/.claude/scripts/permission_prompts.py` (the twin;
   `.claude/scripts/permission_prompts.py` follows through the operator's
   `[claude-twin-sync]`), `tests/test_permission_prompts.py`, `CLAUDE.md`,
   `workflow-templates/CLAUDE.md`, `agents.md`,
   `changelog.d/5125-permission-prompt-report-scope.md` [new].
   Done: the new tests pass against the twin; every other test in
   `tests/test_permission_prompts.py` passes except `test_template_parity`,
   which stays red until the twin sync.
   Rollback: revert the PR; `immediate-state.json` entries with the extra fields
   are read by the old code unchanged.

## Implementation Steps

Phase 1:
1. Twin `_report_now`: add `repo` and `log_session` to the entry.
2. Twin `group_patterns`: add the `sessions` map.
3. Twin `_file_pending`: the delivered-by-repo-and-session skip and the capped
   `new_count`; update the module docstring's `file` paragraph.
4. Tests: load `pp` from the twin (twin-first) and add tests for G1–G4.
5. Docs: CLAUDE.md §23.I (both copies) and agents.md say `file` skips only a
   session's own occurrences that `report-now` delivered to this repository.
6. Changelog fragment.

## Files & Modules

- `workflow-templates/.claude/scripts/permission_prompts.py`
- `tests/test_permission_prompts.py`
- `CLAUDE.md`, `workflow-templates/CLAUDE.md`, `agents.md`
- `changelog.d/5125-permission-prompt-report-scope.md` [new]

## Tests

Unit tests in `tests/test_permission_prompts.py`:
- a consumer-repo report (`reported: PR #77`) followed by `file` in
  coding-workflows files the pattern (G1);
- a report by one log session followed by another session's occurrence files
  only the other session's occurrence (G2);
- a session's own later occurrence is still `already_reported` (G3, the
  existing `test_file_skips_signatures_already_reported`);
- an entry without `repo` / `log_session` does not skip (G4);
- `report-now` writes `repo` and `log_session`.
Full file run with `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest
tests/test_permission_prompts.py`.

## Risks & Mitigations

- A pattern's comment may count a covered session's later occurrence once
  when a previous `file` run already filed the other session's occurrences —
  ACCEPTED: it errs toward reporting, never toward suppression.
- Legacy entries lose their skip, so one extra "Seen again" comment is possible
  per signature after upgrade — ACCEPTED: same reason (G4).
- The `.claude/` copy is unchanged until the twin sync — mitigated by the
  twin-sync blocker the chain posts.

## Rollout

Twin-first: the phase PR changes the twin; the operator copies it to
`.claude/scripts/permission_prompts.py` as a `[claude-twin-sync]` commit.
Consumers receive it with the next `@stable` sync after the issue-4755 project
merges into `main`.

## Auto-decisions

- AD-1 [plan, 2026-09-29] How should `file` decide an occurrence was already reported? — Picked: A — record the delivery repository and the hook log session on each `report-now` entry, and skip only occurrences from log sessions whose report reached the repository `file` files into; entries without those fields do not skip. Alternatives: B — key by repository only (another session's report still suppresses); C — drop the `already_reported` skip (a session's own prompt is reported twice). Why: A is the audit's recommendation and keeps the once-per-session rule. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How do the tests exercise the change before the twin sync? — Picked: A — `tests/test_permission_prompts.py` loads `permission_prompts` from the `workflow-templates/.claude` twin; `test_template_parity` keeps both copies identical after the sync. Alternatives: B — a second module object for the new tests only (the fixtures patch one module); C — tests against `.claude/` only (red until the sync). Why: the twin-first rule says tests read the twin, and the parity test keeps coverage of `.claude/`. Applied in: phase 1 PR. Status: pending review

## References

- Issue #5125 (this finding), audit tracker #3576.
- Issue #4755 and its project (`docs/implement-plan/issue-4755-report-blocking-permission-prompts.md`, final PR #4773).
