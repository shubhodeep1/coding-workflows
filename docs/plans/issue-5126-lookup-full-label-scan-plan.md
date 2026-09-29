# Never report a permission-prompt lookup as not found because a fixed window ran out

Source issue: shubhodeep1/coding-workflows#5126 (https://github.com/shubhodeep1/coding-workflows/issues/5126)
Base branch: claude/implement-plan-issue-4755-report-blocking-permission-prompts
Security pass: skip (ai:security: automation-produced issue)

## Summary

`permission_prompts.py lookup --session <id>` returns `found: false` for a
blocked session whose immediate report exists, whenever that report sits
outside a fixed 3-item window. This plan makes `lookup` check every candidate
it can read, and treat a result it cannot prove complete as a failed read
(exit 2) instead of "not found".

## Context

- Security audit finding #5126 (`A09:2021`, medium, confidence 9/10,
  `.claude/scripts/permission_prompts.py:910`), filed by
  `.github/workflows/security-audit.yml` against the project branch of issue
  #4755, which names it as `Integration branch:`.
- `lookup` serves the operator's master poller (issue #4755, AD-5 of that
  project): the poller runs it for a session whose status reads
  `Waiting on permission: …` and puts the command and link in its alert.
- The poller runs in Claude Code Web, whose agent proxy refuses
  `search/issues` (HTTP 403). Conformance fix #5081 of the #4755 project
  added the fallback that now reads the repository-scoped
  `ai:permission-prompt` issue list, but slices it to `LOOKUP_MAX_HITS` (3)
  newest-updated issues (`_lookup_candidates`, line 910), and `lookup` slices
  the candidates again (line 917).
- Exploit scenario (from the issue): when the search is refused, activity on
  three other labelled issues pushes the blocked session's report issue out
  of the window, and `lookup` returns `found: false` though the report
  exists. The poller then alerts without the command, or not at all, and a
  blocked unattended session stays unnoticed (a monitoring failure).
- The search path has the same shape: `per_page=3` and one read, so a
  session with more than three hits (up to `MAX_IMMEDIATE_REPORTS` = 5
  reports, plus other mentions) can also be cut off.
- The repo currently holds 38 `ai:permission-prompt` issues with 225
  comments in total.

## Goals

- G1. In the search-refused fallback, `lookup` checks **every**
  `ai:permission-prompt` issue (all pages, newest-updated first) before it
  returns `found: false`.
- G2. In the search path, `lookup` checks **every** search hit (all pages)
  before it returns `found: false`.
- G3. When the search says its own hits are incomplete
  (`incomplete_results: true`) and none of them holds the report, `lookup`
  exits 2 with an `error`, never `found: false`.
- G4. A labelled issue whose `comments` count is `0` costs no comments read;
  every other issue is read as before (§15).
- G5. Output shape, exit codes, the trusted-author rule (OWNER, MEMBER,
  COLLABORATOR only), and the CLI are unchanged.

## Non-goals

- A repository-scoped session-to-report index (new stored state); see
  AD-1.
- Finding reports that `report-now` posted outside coding-workflows (on a
  PR or a `claude/implement-plan-issue-<N>` issue) when the search is
  refused. The fallback only covers where `report-now` reports in
  `FILING_REPO`, as today.
- Wiring `lookup` into the master poller (an operator step of #4755).

## Constraints

- §5 — only `lookup`'s candidate selection changes; `report-now`, `file`,
  `session-meta` and the report format are untouched.
- §6 — `LOOKUP_MAX_HITS`, `_lookup_candidates`, `lookup`, the CLI flags, and
  the JSON keys keep their names and meaning; `LOOKUP_MAX_HITS` stays
  defined (AD-3). No new module-level identifier is added.
- §15 — every new read goes through the existing paginated helpers
  (`check_in_status.gh_api_list`, `check_in_status._gh_api_paginated_object`),
  REST only; the docstring's API-call budget is updated.
- §23.I / §28.C — `.claude/scripts/permission_prompts.py` is a protected
  path: under the interim twin-first rule the phase edits only
  `workflow-templates/.claude/scripts/permission_prompts.py`, and the
  operator copies it into `.claude/` with a `[claude-twin-sync]` commit.
- §20 — one `changelog.d/` fragment (observable behaviour and a security
  fix).
- §7 — `agents.md` documents the lookup budget and must follow.

## Approach

1. `_lookup_candidates` reads the search through
   `check_in_status._gh_api_paginated_object(…, "items")` (100 per page,
   stops at `total_count`, at most `MAX_PAGINATED_API_PAGES` pages) instead
   of one `per_page=3` read. A search read that fails, returns a malformed
   page, or runs past the page cap falls back to the labelled list exactly
   as a refused search does today. When the search answers with
   `incomplete_results: true` it raises `ReadError` (AD-2).
2. The fallback returns the whole paginated labelled-issue list (no slice).
3. `lookup` drops the `[:LOOKUP_MAX_HITS]` slice and, for each candidate,
   checks the body, then reads the comments unless the candidate's
   `comments` field is the integer `0`.

Alternatives considered: a session-to-report index (AD-1 B) needs new
state, a writer in `report-now`, and a migration for existing reports; a
wider fixed window (e.g. 10) keeps the defect.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the change is
one function pair plus its tests and docs, and it cannot be split into
independently useful parts.

1. **Phase 1 — lookup checks every candidate.**
   - Files: `workflow-templates/.claude/scripts/permission_prompts.py`
     (twin of `.claude/scripts/permission_prompts.py`),
     `tests/test_permission_prompts.py`, `agents.md`,
     `changelog.d/5126-lookup-full-label-scan.md` [new].
   - Protected paths: `.claude/scripts/permission_prompts.py` (twin-first:
     only the `workflow-templates/.claude/` twin is edited).
   - Done: the new and updated lookup tests pass against the twin; the rest
     of `tests/test_permission_prompts.py` passes except
     `test_template_parity`, which stays red until the operator's
     `[claude-twin-sync]`; ruff is clean.
   - Rollback: revert the phase PR; `lookup` goes back to the 3-item window.

## Implementation Steps

Phase 1:

1. `workflow-templates/.claude/scripts/permission_prompts.py`
   `_lookup_candidates`: search through `_gh_api_paginated_object`, raise
   `ReadError` on `incomplete_results: true`, return the full labelled list
   in the fallback; update its docstring (inputs, outputs, API calls,
   fail-open behaviour, per §15).
2. Same file, `lookup`: iterate every candidate; skip the comments read when
   `comments` is the integer `0`.
3. Same file: the module docstring's `lookup` paragraph and API-call budget;
   a comment on `LOOKUP_MAX_HITS` saying `lookup` no longer caps its
   candidates and the name is kept for existing importers (§6).
4. `tests/test_permission_prompts.py`: load the twin (AD-4) and cover G1–G5.
5. `agents.md`: the `lookup` bullet's budget and fallback wording.
6. `changelog.d/5126-lookup-full-label-scan.md` (`fixed`).

## Files & Modules

- `workflow-templates/.claude/scripts/permission_prompts.py`
- `.claude/scripts/permission_prompts.py` (operator `[claude-twin-sync]`
  copy of the twin; not edited by the session)
- `tests/test_permission_prompts.py`
- `agents.md`
- `changelog.d/5126-lookup-full-label-scan.md` [new]

## Tests

Unit tests in `tests/test_permission_prompts.py`, run against the twin:

- Fallback, the issue's scenario: three newer labelled issues without the
  report, the report on the fourth → `found: true`.
- Fallback: an issue with `comments: 0` is not read; one with a positive or
  missing count is.
- Fallback: `found: false` only after every labelled issue was checked.
- Search: more than three hits, the report on the fourth → `found: true`;
  the search read asks for 100 per page.
- Search: `incomplete_results: true` with no match → `ReadError`, and
  `main` exits 2 with `found: false` and an `error`.
- Existing lookup tests keep passing (trusted-author filter, newest
  trusted comment, refused search, read failure, bad arguments).
- Verification: `python3 -m pytest tests/test_permission_prompts.py` plus
  an overlay run with the twin copied over `.claude/` (all green, parity
  included); `ruff check` on the changed Python files.

## Risks & Mitigations

- A not-found lookup now costs one read per 100 labelled issues plus one
  comments read per labelled issue with comments (about 39 reads at today's
  38 issues). ACCEPTED — lookup runs only for a session already waiting on
  a permission prompt, and a wrong negative defeats its purpose.
- `incomplete_results: true` turns a partial search into exit 2 even when
  the poller could have used a partial answer. ACCEPTED — the poller
  already treats exit 2 as "unknown" and retries; a false "not found" is
  the defect being fixed.
- The twin and `.claude/` differ until the operator's `[claude-twin-sync]`.
  Mitigation: `test_template_parity` stays red until then, and the phase PR
  is held with a `hold` claim and the twin-sync blocker.

## Rollout

No flag. It ships with the #4755 project when that project's final PR
(#4773) merges into `main`, and reaches consumers on the next `@stable`
sync of `.claude/`. Rollback: revert the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-29] How should `lookup` stop missing reports outside its window? — Picked: A — check every paginated candidate (search hits, or every labelled issue in the fallback), skipping comment reads for issues with `comments: 0`. Alternatives: B — a repository-scoped session-to-report index kept by `report-now`; C — keep the window and exit 2 when it is exhausted. Why: smallest change (§5) that removes the false negative using existing paginated helpers (§15); B needs new state and a migration, C leaves the poller without the command. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What should `lookup` do when the search reports `incomplete_results: true`? — Picked: A — raise `ReadError`, so `lookup` exits 2 with an `error`. Alternatives: B — scan the partial hits and return `found: false` when none matches; C — fall back to the labelled-issue scan. Why: B is the false negative the issue forbids; C misses reports outside coding-workflows. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] What happens to `LOOKUP_MAX_HITS`, which no longer caps anything? — Picked: A — keep it defined with a comment that `lookup` no longer uses it. Alternatives: B — delete it; C — reuse it as the page size. Why: §6 forbids removing or repurposing an identifier. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How do the tests cover a change made only in the `workflow-templates/.claude/` twin? — Picked: A — the lookup tests load the twin and share the main module's `check_in_status`, so they pass before the `[claude-twin-sync]` and stay valid after it. Alternatives: B — test the `.claude/` copy and leave the tests red until the sync. Why: the interim twin-first rule (CLAUDE.md §28.C) says tests read the twin. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5126`
  printed `{"skip": true, "label": "ai:security", "reason": "ai:security:
  created and labelled by the issue automation"}`.

## References

- Issue #5126 (this finding); issue #4755 and its plan
  `docs/plans/issue-4755-report-blocking-permission-prompts-plan.md`;
  conformance fix PR #5081; final PR #4773.
- `.claude/scripts/check_in_status.py` `gh_api_list`,
  `_gh_api_paginated_object`.
