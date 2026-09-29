# Stop legacy filed counts from hiding real permission-prompt denials

Source issue: shubhodeep1/coding-workflows#5012 (https://github.com/shubhodeep1/coding-workflows/issues/5012)
Base branch: claude/implement-plan-issue-4750-classifier-outage-get-session
Security pass: skip (ai:security: automation-produced issue)

## Summary

`permission_prompts.py file` remembers what it has filed as one cumulative
count per pattern signature in `filed-state.json`. Before #4750, classifier-
outage denials were grouped into the same patterns as real denials, so saved
counts include them. #4750 now drops outage records before grouping, so a
pattern's current count can sit below its saved count, and a later real denial
with the same tool and command shape files no issue and no comment until the
real count passes the stale number. This plan replaces the cumulative count
with a per-record filed set and migrates the legacy counts exactly, so a real
denial is never hidden by an outage that was counted earlier.

## Context

- Issue #5012 (security audit follow-up, `A09:2021`, medium, confidence 9/10)
  points at `.claude/scripts/permission_prompts.py:510`, the
  `split_classifier_outages` call in `file_patterns`. Its recommendation:
  migrate saved counts to exclude previously filed outage records, or
  deduplicate by individual record rather than a cumulative count.
- On the base branch (`claude/implement-plan-issue-4750-classifier-outage-get-session`,
  project PR #4770, still a draft into `main`):
  - `file_patterns` (`permission_prompts.py:507-550`) computes
    `new_count = pattern["count"] - state.get(sig, 0)` (`:517`) and skips any
    pattern with `new_count <= 0` (`:518`); after a post it stores
    `state[sig] = pattern["count"]` (`:547`).
  - `_load_state` / `_save_state` (`:482-492`) read and write
    `filed-state.json` as a flat `{signature: int}` map.
  - `load_records` (`:279-295`) reads every `*.jsonl` in sorted file-name
    order, line by line; the logger only appends
    (`.claude/hooks/permission_prompt_logger.py`), so a signature's saved count
    `N` written by any earlier version covered the first `N` of its records in
    that order.
  - `main` still has the pre-#4750 filer (no outage handling), so every
    `filed-state.json` written by a released version counted outage records.
- `workflow-templates/.claude/scripts/permission_prompts.py` is a byte-identical
  twin (asserted by `tests/test_permission_prompts.py`).

## Goals

- A real (non-outage) record that has not been filed is always filed or
  commented on, whatever an older `filed-state.json` holds.
- Records already filed are never filed again when the state was written by
  this version.
- A legacy `{signature: int}` state is migrated without suppressing any real
  record; the only permitted error is one extra "Seen again" comment.
- `report` output, the `file` JSON summary keys, the issue and comment text,
  and the API-call budget (§15) are unchanged.

## Non-goals

- No change to classifier-outage detection (`CLASSIFIER_OUTAGE_REASON_RE`),
  grouping, signatures, redaction, or the logger.
- No change to the hook wiring, `settings.json`, or any command file.
- No cleanup of old log files or state files.

## Constraints

- §6: `filed-state.json`, `STATE_FILE`, `_load_state`, `_save_state`,
  `load_records`, `file_patterns`, and every summary key keep their names and
  signatures; new helpers get new, non-colliding names.
- §5: only the filer's state handling changes.
- §9: tabs in Python.
- §15: no new GitHub API calls.
- §20: a `changelog.d/5012-legacy-outage-filed-counts.md` fragment
  (`security`).
- §28.C: the phase edits `.claude/scripts/permission_prompts.py` (and its
  `workflow-templates/.claude/` twin), a protected path. The chain stops
  before the phase until a `Protected-path approval: phase 1` line is
  recorded; the operator's standing answer is the interim twin-first rule
  (Q40, `docs/operations/master-session.md`).

## Approach

AD-1 (below): deduplicate by record, not by count.

- Each loaded record gets a stable key `<log file name>:<line index>` (0-based
  over every line of the file, so skipped invalid lines never shift later
  keys). Logs are append-only, so the key never moves.
- `filed-state.json` becomes `{"version": 2, "filed": {signature: [keys]}}`,
  holding the keys of the **real** records already filed per signature.
- Pending records for a pattern = its real records whose key is not in the
  filed set; `new_count` = their number. After a successful post the
  pattern's real record keys are added to the set.
- **Legacy migration** (a state file without `"version": 2`, all values
  ints): for each signature with saved count `N`, re-derive the signature
  over **all** records, outage records included (the grouping every earlier
  version used), take the first `N` in load order, and mark only the real
  ones among them as filed. Outage records in that prefix were counted but
  are not real; they no longer hide anything. A real record after the prefix
  is pending.
- The migrated state is written back only when a later post succeeds (as
  today), so a dry run changes nothing on disk.

Alternatives considered: subtracting each signature's outage count from the
saved count (wrong when outages arrived after the last filing: it re-files
already-reported records or still hides new ones); discarding legacy state
(re-comments every pattern once).

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
change is one function's state handling plus its tests and docs, and any
split would ship a half-migrated state format.

1. **Phase 1 — record-level filed state with legacy migration.**
   - Files: `.claude/scripts/permission_prompts.py`,
     `workflow-templates/.claude/scripts/permission_prompts.py`,
     `tests/test_permission_prompts.py`, `agents.md`,
     `changelog.d/5012-legacy-outage-filed-counts.md`.
   - Protected paths: `.claude/scripts/permission_prompts.py`.
   - Done: the tests below pass, the twins are byte-identical, and the full
     `tests/test_permission_prompts.py` suite passes.
   - Rollback: revert the PR. An older filer reading a v2 state ignores the
     `filed` map and treats `version` as an unknown signature, so it
     re-files each pattern once (over-reporting, never suppression).

## Implementation Steps

Phase 1:

1. `permission_prompts.py`: add `load_keyed_records(log_dir)` returning
   `(key, record)` pairs in the same order and filter as `load_records`;
   have `load_records` return the records of it (unchanged output).
2. Add `_load_filed_records(log_dir, keyed_records)` that reads
   `filed-state.json`: a v2 file → its `filed` map (lists of strings only);
   a legacy int map → the migration in [Approach](#approach); anything else
   → empty. Keep `_load_state` / `_save_state` for compatibility.
3. Add `_save_filed_records(log_dir, filed)` writing the v2 document.
4. In `file_patterns`: load keyed records once, split outages, group the real
   records, compute per-signature pending keys from the filed map, use their
   count as `new_count`, and on a successful post add the signature's real
   keys. The summary, dry-run, consumer-repo, and error paths stay as they
   are.
5. Update the module docstring (the `filed-state.json` paragraph).
6. Copy the file byte-for-byte to the `workflow-templates/.claude/` twin.
7. Tests in `tests/test_permission_prompts.py`:
   - a legacy state whose count included outage records, followed by a new
     real denial of the same shape → one "Seen again" comment with
     `occurrences: 1` (the #5012 scenario; fails on the base branch);
   - a v2 state → a second `file` run posts nothing; a new record → exactly
     that one;
   - legacy count equal to the real count with no outages → nothing posted;
   - a dry run writes no state;
   - update the existing state-shape assertion
     (`test_mixed_log_files_only_the_real_pattern`) to the v2 document.
8. `agents.md` (the `filed-state.json` bullet near line 1108): describe the
   per-record state and the legacy migration.
9. `changelog.d/5012-legacy-outage-filed-counts.md` (`security`).

## Files & Modules

- `.claude/scripts/permission_prompts.py`
- `workflow-templates/.claude/scripts/permission_prompts.py`
- `tests/test_permission_prompts.py`
- `agents.md`
- `changelog.d/5012-legacy-outage-filed-counts.md` [new]

## Testing

`PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_permission_prompts.py -q`,
with the new #5012 regression test shown failing on the base branch first.

## Risks

- A legacy entry written by the unreleased #4750 project code (which already
  excluded outages) is migrated as outage-inclusive (AD-2): at most one
  duplicate "Seen again" comment, never a hidden denial.
- A new session log whose name sorts before an older one would break a
  file-order prefix for legacy counts, and could mark its never-filed real
  denials as filed. The migration therefore takes the prefix in logging order
  (each record's `ts`, ties in load order), which the file names do not
  affect (PR #5028 review round 1).
- State size grows with filed records; it lives next to the logs, which
  already hold every record.

## Rollout

Ships with the #4770 project (this project's base) and reaches consumers on
the next `@stable` sync. No flag, no operator step.

## Auto-decisions

- AD-1 [plan, 2026-09-29] How should the filer stop legacy counts from hiding real denials? — Picked: A — track filed records by key (`<log file>:<line>`) per signature in a versioned `filed-state.json`, migrating legacy counts by the load-order prefix. Alternatives: B — keep counts and subtract each signature's outage records once; C — discard legacy state. Why: A is exact for both old and new state and is the issue's second recommendation; B is wrong when outages arrived after the last filing; C re-comments every pattern. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How is an unversioned state treated when it may have been written by the unreleased #4750 code (outage-exclusive counts)? — Picked: A — always as outage-inclusive (the released behaviour). Alternatives: B — as outage-exclusive; C — guess per signature. Why: A can only over-report (one extra comment), B can still hide real denials, which is the defect. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Does the state file keep its name? — Picked: A — keep `filed-state.json`, add `"version": 2`. Alternatives: B — a new file name. Why: §6; the version key tells the formats apart. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5012`
  returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
