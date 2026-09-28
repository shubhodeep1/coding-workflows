# Report Auto-mode classifier-outage denials without filing them as permission-prompt issues

Source issue: shubhodeep1/coding-workflows#4761 (https://github.com/shubhodeep1/coding-workflows/issues/4761)
Base branch: main
Security pass: run

## Summary

`.claude/scripts/permission_prompts.py` files every Auto-mode denial as an `ai:permission-prompt` fix issue, including denials Claude Code produced only because its classifier gave no verdict (`reason: "Classifier unavailable"`). No allow rule, helper, or command shape can prevent those, so each outage becomes a set of fix issues nobody can act on. Issue #4761 is one of them. Keep reporting these denials, but stop filing or commenting on issues for them.

## Context

- #4761 records one `PermissionDenied` for `git status -sb | head -3 && git fetch origin main <branch> 2>&1 | tail -2` at 2026-09-28T07:36:54Z, with the reason `Classifier unavailable`. `git status *` and `git fetch *` are already in `.claude/settings.json` `permissions.allow`. `head` and `tail` are Claude Code built-in read-only commands.
- Four sibling issues come from the same outage window (07:36 UTC), all with the same reason: #4749, #4751 (`git fetch -q * && git show *`, both parts allowlisted), #4760, and #4767.
- Claude Code documentation (permission-modes, "How the classifier evaluates actions" and "When auto mode falls back"): in a session with server-side classifier review, read-only shell commands wait for that review. An action the classifier gives no verdict for is denied. No setting turns that denial into a prompt or a retry, and a `PermissionDenied` hook's `retry: true` is ignored when there was no verdict. The hooks reference uses `"reason": "Classifier unavailable"` as its `PermissionDenied` example.
- `.claude/hooks/permission_prompt_logger.py` already stores the payload's `reason` on every line, and `permission_prompts.py` groups lines by event, tool, and command shape (`group_patterns`, `.claude/scripts/permission_prompts.py:276`). Then `file_patterns` (`:446`) files or comments per pattern, using `filed-state.json` counts.
- `workflow-templates/.claude/scripts/permission_prompts.py` must stay byte-identical (`tests/test_permission_prompts.py::test_template_parity`).

## Goals

- A `PermissionDenied` record whose `reason` is a no-verdict reason (AD-2) is a **no-verdict denial**. `file` never opens an issue or posts a comment for it. A pattern that has only no-verdict denials files nothing. A pattern that also has real denials or prompts files only those, and its occurrence count excludes the no-verdict ones.
- No-verdict denials stay visible. `report` and `file` output keep `total` and `patterns` exactly as today (same meaning: every logged record). Each pattern gains `no_verdict_count`, and the summary gains `no_verdict` (the total), so the stage report can say `n were classifier outages, not filed`.
- `PermissionRequest` records are never no-verdict, whatever their reason.
- `filed-state.json` stays compatible: it stores the filed (non-no-verdict) count per signature. A state file written before this change, holding a larger count, files nothing extra.
- API budget unchanged (§15). A log with only no-verdict denials makes **zero** API calls.
- Tests cover each rule. Docs (`agents.md`, `README.md`, CLAUDE.md §23.I) and a `changelog.d/` fragment describe the behaviour.

## Non-goals

- Adding `permissions.allow` rules (`Bash(head *)`, `Bash(tail *)`, …) so such pipelines bypass the classifier (AD-1).
- Closing or editing the sibling issues #4749, #4751, #4760, #4767 (AD-3). This PR references them only.
- Changing the logger hook, the issue text for real prompts, the `ai:permission-prompt` marker, the labels, or the signature algorithm.
- Retrying denied calls, or changing Claude Code's Auto-mode fallback (not configurable).

## Constraints

- §1 security: the fix widens no permission. It only changes what gets filed.
- §5: the smallest change that stops outage denials from producing fix issues.
- §6: `total`, `patterns`, `filed`, `commented`, `errors`, `skipped`, `dry_run`, the marker, `STATE_FILE`, and every existing function name keep their names and meanings. The new identifiers (`NO_VERDICT_REASONS`, `is_no_verdict_denial`, `no_verdict_count`, `no_verdict`) do not collide with any existing name (checked with `grep` across `.claude/`, `scripts/`, `tests/`).
- §9: Python with tabs.
- §14 / template parity: the `workflow-templates/.claude/` copy changes byte-identically, and consumers get it through the existing `.claude/` sync. Consumers only report, so for them the change only adds the new fields.
- §15: no new API call.
- §20: a changelog fragment (the filing behaviour changes).
- §28.C: the phase edits `.claude/scripts/permission_prompts.py`, a protected path, so `/implement-plan-claude` stops before the phase and asks how to run it.

## Approach

In `permission_prompts.py`:

1. Add `NO_VERDICT_REASONS = ("classifier unavailable",)` and `is_no_verdict_denial(record)`. It returns true when `record["event"] == "PermissionDenied"` and the reason, trimmed and lowercased, equals one of those strings.
2. `group_patterns` counts `no_verdict_count` per pattern. The `reasons` list and `example` keep their current behaviour, so the report still shows the outage reason.
3. `report` adds `no_verdict_count` to each pattern entry and `no_verdict` to the summary.
4. `file_patterns` works out the pending count as `(count - no_verdict_count) - state.get(sig, 0)`. It files or comments only when that is positive, and stores `count - no_verdict_count` in the state.
5. Update the module docstring to say no-verdict denials are reported, not filed.

Alternatives are in AD-1: allow rules for `head`/`tail` would widen out-of-working-directory reads to fix one shape, and closing the issue with no change leaves the next outage filing a new set of issues.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the change is one script, its template copy, tests, and docs. Split up, it would leave the template-parity test red.

1. **Phase 1 — do not file no-verdict Auto-mode denials** (`protected paths: .claude/scripts/permission_prompts.py, workflow-templates/.claude/scripts/permission_prompts.py`).
   - Files: `.claude/scripts/permission_prompts.py`, `workflow-templates/.claude/scripts/permission_prompts.py`, `tests/test_permission_prompts.py`, `agents.md`, `README.md`, `CLAUDE.md` (§23.I, one sentence), `changelog.d/4761-skip-classifier-outage-denials.md` [new].
   - Done when: `python3 -m pytest tests/test_permission_prompts.py` passes (new tests included), the template copy is byte-identical, and `ruff check` is clean on the changed Python files.
   - Rollback: revert the PR. Filing goes back to issuing fix issues for outage denials. No state migration is needed.

## Implementation Steps

1. `.claude/scripts/permission_prompts.py`: steps 1–5 of Approach.
2. Copy the file byte-for-byte to `workflow-templates/.claude/scripts/permission_prompts.py`.
3. `tests/test_permission_prompts.py`: add the tests below.
4. `agents.md` "Unattended helpers and permission prompt reports" and `README.md` §23.I note: one sentence each saying no-verdict denials (`Classifier unavailable`) are reported under `no_verdict` and never filed.
5. `CLAUDE.md` §23.I, in the `permission_prompts.py file` bullet: add that Auto-mode denials the classifier gave no verdict for (an outage) are reported but not filed.
6. `changelog.d/4761-skip-classifier-outage-denials.md` (`<!-- changelog: fixed -->`), following §20.D.

## Files & Modules

- `.claude/scripts/permission_prompts.py`
- `workflow-templates/.claude/scripts/permission_prompts.py`
- `tests/test_permission_prompts.py`
- `agents.md`
- `README.md`
- `CLAUDE.md`
- `changelog.d/4761-skip-classifier-outage-denials.md` [new]

## Tests

Unit (pytest, `tests/test_permission_prompts.py`):
- `is_no_verdict_denial`: true for `PermissionDenied` + `Classifier unavailable` (any case, surrounding whitespace). False for `PermissionRequest` with the same reason, for another denial reason, and for an empty reason.
- A log with only no-verdict denials: `file` makes no reads and no posts, `filed == []`, `no_verdict == 1`, and `total == 1`.
- A mixed pattern (one no-verdict denial and one real denial of the same shape): the new issue's `**Occurrences:**` is 1. The state stores 1, and a second run files nothing.
- A pre-existing state file whose count includes no-verdict records files nothing and does not error.
- `report` carries `no_verdict_count` per pattern and `no_verdict` in the summary, and `total` is unchanged.
- The existing tests (template parity, settings wiring, CLAUDE.md, ci.yml) still pass.

End to end: `python3 .claude/scripts/permission_prompts.py file --log-dir <tmp> --dry-run` on a log holding the #4761 record reports it under `no_verdict` and files nothing.

## Risks & Mitigations

- A future Claude Code version could use another reason string for a no-verdict denial. Those would still be filed, so the failure mode is noise, not silence. ACCEPTED: `NO_VERDICT_REASONS` is one tuple, easy to extend.
- A real policy denial could carry `Classifier unavailable`. It cannot: that reason means the classifier returned no verdict.
- A stage report line that reads `total` now also sees `no_verdict`. The field is additive (§6).

## Rollout

Ships on merge to `main`. Consumers receive the script with the next `@stable` sync, where it only reports. No flag and no migration.

## Auto-decisions

- AD-1 [planning, 2026-09-28] What should the fix for a denial caused by `Classifier unavailable` change? — Picked: A — stop filing (and commenting on) issues for no-verdict Auto-mode denials in `permission_prompts.py`, and keep reporting them. Alternatives: B — add `Bash(head *)` / `Bash(tail *)` allow rules so this pipeline resolves before the classifier; C — close #4761 as not planned with no code change. Why: B widens reads outside the working directory without a prompt (§1) and fixes one shape only; C is a §23.C close and leaves every future outage filing new issues. Applied in: phase 1. Status: pending review
- AD-2 [planning, 2026-09-28] Which denial reasons count as "no verdict"? — Picked: A — exactly `Classifier unavailable` (trimmed, any case), kept in a `NO_VERDICT_REASONS` tuple. Alternatives: B — also the outage messages in the Claude Code errors page (`temporarily unavailable`, `gave no verdict`, …), matched as substrings. Why: only `Classifier unavailable` is documented as a `PermissionDenied` reason, and an unmatched string fails toward filing, not toward hiding a real denial. Applied in: phase 1. Status: pending review
- AD-3 [planning, 2026-09-28] Should this project also close the sibling outage issues #4749, #4751, #4760, #4767? — Picked: A — no: reference them (`Refs`) in the PR bodies and the progress comment only. Alternatives: B — `Fixes` them in the final PR. Why: one issue per project, and closing issues this session did not open is a §23.C ask-first write. Applied in: no code change. Status: pending review
- AD-4 [planning, 2026-09-28] How should the output show no-verdict denials? — Picked: A — keep `total` / `patterns` unchanged and add `no_verdict_count` per pattern plus a `no_verdict` total. Alternatives: B — drop no-verdict records from `patterns` and `total`. Why: B changes the meaning of existing output fields (§6). Applied in: phase 1. Status: pending review
- AD-5 [planning, 2026-09-28] Treat the issue's "How to fix" steps 1–3 (change the command file, add a helper, add an allow rule) as required? — Picked: A — no: the denial came from a free-form model command during a classifier outage, not from a command file, and no rule or helper can stop an outage denial. Alternatives: B — follow step 3 and add allow rules. Why: see AD-1. Applied in: no code change. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` → `{"skip": false, "label": null, "reason": "no skip label"}`, so `Security pass: run`.
- The phase edits `.claude/**`, so the chain stops before it and asks on the issue (CLAUDE.md §28.C), as the issue itself expects.

## References

- Issue #4761; sibling outage issues #4749, #4751, #4760, #4767.
- https://code.claude.com/docs/en/permission-modes (classifier fallback), https://code.claude.com/docs/en/hooks (`PermissionDenied` payload).
