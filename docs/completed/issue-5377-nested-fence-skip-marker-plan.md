# Match code fences by character and length in the skip-AI marker rule

Source issue: shubhodeep1/coding-workflows#5377 (https://github.com/shubhodeep1/coding-workflows/issues/5377)
Base branch: claude/implement-plan-issue-4985-skip-marker-review-stall
Security pass: skip (ai:security: automation-produced issue)

## Summary

The intentional skip-AI marker rule (issue #4985) toggles its "inside a code
fence" state on every line that starts with ```` ``` ```` or `~~~`. Inside a
four-backtick fence, a three-backtick example line flips it out of the fence,
so a `[skip ai]` example further down the quoted block counts as an opt-out
and the review gate and the sweeps skip the PR. This plan makes all three
copies of the rule track the opening fence's character and length and close
only on a valid matching fence, so every ambiguous case falls back to review.

## Context

- Security audit finding `nested-fence-skips-review` (A04:2021 Insecure
  Design, medium, confidence 9/10), filed as #5377 against
  `.github/workflows/review_autofix.yml:474`, `Refs #3576` (the audit
  tracker), with `Integration branch:
  claude/implement-plan-issue-4985-skip-marker-review-stall`.
- The rule was introduced by the #4985 project (still in flight; its final PR
  is #5031, a draft into `main`). It lives in three places that
  `tests/test_skip_ai_marker_rule.py` holds to one case table:
  - `SKIP_AI_BODY_AWK` in `.github/workflows/review_autofix.yml` (the review
    gate, "Evaluate review gate" step);
  - the identical `SKIP_AI_BODY_AWK` in
    `.github/workflows/review_autofix_sweep.yml` (the stale-review sweep);
  - `has_skip_ai_marker` in `.claude/scripts/check_in_status.py` (twin:
    `workflow-templates/.claude/scripts/check_in_status.py`), which
    `scripts/claude_pr_sweep.py` (the §26.H catch-all sweep) also calls.
- CommonMark fences: an opening fence is a run of at least three backticks or
  tildes; the block ends only at a closing fence of the **same** character,
  **at least as long** as the opener, indented at most 3 spaces, followed by
  nothing but blanks. An unclosed fence runs to the end of the document.

## Goals

- In all three copies, a fence line opens a block that only a valid matching
  closing fence ends (same character, length ≥ the opener's, ≤ 3 leading
  spaces, only trailing blanks).
- A three-backtick line inside a four-backtick fence, a `~~~` line inside a
  backtick fence, an indented (4+ spaces or tab) fence-looking line inside a
  fence, and a fence line with an info string inside a fence no longer end the
  block, so a `[skip ai]` line after them is not an opt-out.
- An unclosed fence keeps every later line inside it (review).
- Every existing case in the table keeps its answer.

## Non-goals

- The title rule (substring match) and the body-line rule
  (`^ {0,3}\[skip ai\][ \t]*$`) are unchanged.
- No other fence parser in the repo (`scripts/lint_pr_body_auto_close.py`,
  `scripts/ingest_implement_plan_lessons.py`) is touched.
- No full CommonMark parser (block quotes, list-item containers, HTML blocks).

## Constraints

- §1 security first: every ambiguity resolves toward review, never toward a
  skip.
- §6: `SKIP_AI_MARKER`, `SKIP_AI_BODY_LINE_RE`, `SKIP_AI_FENCE_RE`,
  `has_skip_ai_marker`, `SKIP_AI_BODY_AWK`, the skip reasons
  (`skip_ai_marker`, `draft_or_skip_ai`) and log keys stay. `SKIP_AI_FENCE_RE`
  keeps its exact match set; it only gains a capture of the whole fence run.
  The new closing-fence pattern is a new, unique name
  (`SKIP_AI_FENCE_CLOSE_RE`).
- §9: tabs in Python; YAML stays 2-space.
- §15: no new API calls (pure text parsing).
- §27: `review_autofix.yml` is 460,243 bytes; the change adds a few hundred
  bytes and stays far under the 480,000-byte guard.
- The awk program must run under both mawk and gawk (no `{n,}` interval
  expressions), stay a single line inside single quotes (the test extracts it
  with `^[ ]*SKIP_AI_BODY_AWK='([^']*)'$`), and read its whole input so
  `pipefail` never sees a SIGPIPE.
- `.claude/**` is a protected path (CLAUDE.md §28.C): this phase runs under
  the interim twin-first default and edits only
  `workflow-templates/.claude/scripts/check_in_status.py`.

## Approach

Replace the boolean toggle with "open fence run" state:

- Outside a fence, a line matching `^[ \t]*` + three or more backticks or
  tildes opens a fence; the state records the run (its character and length).
  The opener stays as liberal as today's toggle (any indentation, any info
  string), because treating a doubtful line as an opener only ever causes a
  review.
- Inside a fence, a line closes it only when it is ≤ 3 spaces, then a run of
  the recorded character at least as long as the opener, then only blanks.
  Every other line is content.
- Outside a fence, the unchanged body-line rule decides.

awk sketch (one line in the workflows):

```
{ sub(/\r$/, "") }
fl > 0 { if ($0 ~ /^ ? ? ?(```+|~~~+)[ \t]*$/) { r = $0; sub(/^ */, "", r); sub(/[ \t]*$/, "", r); if (substr(r, 1, 1) == fc && length(r) >= fl) fl = 0 } next }
match($0, /^[ \t]*(```+|~~~+)/) { r = substr($0, RSTART, RLENGTH); sub(/^[ \t]*/, "", r); fc = substr(r, 1, 1); fl = length(r); next }
/^ ? ? ?\[skip ai\][ \t]*$/ { found = 1 }
END { exit found ? 0 : 1 }
```

Python mirrors it with `SKIP_AI_FENCE_RE = ^[ \t]*(`{3,}|~{3,})` (same lines
as today) and `SKIP_AI_FENCE_CLOSE_RE = ^ {0,3}(`{3,}|~{3,})[ \t]*$`.

Alternatives considered: full CommonMark opener rules (≤ 3 spaces, no
backtick in a backtick fence's info string) would honour a few more real
markers but adds a way for the three copies to disagree with the renderer in
the skip direction; rejecting any body with nested or mismatched fences
outright over-reviews ordinary PRs. See AD-1.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan:
the three copies must change together (the case-table test fails when they
disagree), so the fix cannot be split.

1. **Phase 1 — fence-aware skip-AI marker rule in all three copies.**
   - Files: `.github/workflows/review_autofix.yml`,
     `.github/workflows/review_autofix_sweep.yml`,
     `workflow-templates/.claude/scripts/check_in_status.py` (twin of
     `.claude/scripts/check_in_status.py`, protected path: synced by the
     twin-sync step), `tests/test_skip_ai_marker_rule.py`, `agents.md`,
     `changelog.d/5377-nested-fence-skip-marker.md` [new].
   - Done when: the case table (old and new cases) passes for the Python twin
     and for the awk program under bash; both workflows carry the identical
     program; the existing #4985 tests still pass; after the twin sync,
     `tests/test_check_in_status.py::test_template_parity` passes.
   - Rollback: revert the phase PR; the rule returns to the toggle.

## Implementation Steps

Phase 1:

1. `workflow-templates/.claude/scripts/check_in_status.py` (~lines 161–170,
   259–281): widen `SKIP_AI_FENCE_RE` to capture the whole run (same match
   set), add `SKIP_AI_FENCE_CLOSE_RE`, and make `has_skip_ai_marker` record
   the opening run and close only on a matching fence. Update the comment and
   docstring.
2. `.github/workflows/review_autofix.yml` (~lines 464–474) and
   `.github/workflows/review_autofix_sweep.yml` (~lines 136–139): replace
   `SKIP_AI_BODY_AWK` with the fence-aware program (identical in both) and
   update the comments.
3. `tests/test_skip_ai_marker_rule.py`: load the Python rule from the twin
   (AD-2), and add cases: the issue's exploit (four-backtick fence holding a
   three-backtick line and a `[skip ai]` line), a longer closing fence, a
   `~~~` line inside a backtick fence, an info-string line inside a fence, a
   4-space and a tab-indented closer, an unclosed fence, and a marker after a
   correctly closed nested fence.
4. `agents.md` ("Skip-AI marker and review stalls"): state the fence rule.
5. `changelog.d/5377-nested-fence-skip-marker.md` (section `security`).

## Files & Modules

- `.github/workflows/review_autofix.yml`
- `.github/workflows/review_autofix_sweep.yml`
- `workflow-templates/.claude/scripts/check_in_status.py`
- `.claude/scripts/check_in_status.py` (twin sync only, not edited by the
  unattended session)
- `tests/test_skip_ai_marker_rule.py`
- `agents.md`
- `changelog.d/5377-nested-fence-skip-marker.md` [new]

## Tests

- Unit: `tests/test_skip_ai_marker_rule.py` (Python twin and awk under bash
  against one table, both workflows carry one program).
- Regression: `tests/test_check_in_status.py`,
  `tests/test_check_in_status_hand_back.py`, `tests/test_claude_pr_sweep.py`,
  `tests/test_workflow_file_size_limit.py`, and the workflow YAML parse.
- Parity: `tests/test_check_in_status.py::test_template_parity` is red until
  the twin sync copies the twin into `.claude/`, then green.
- The awk program is exercised locally with mawk (the container's awk); it
  uses only POSIX features so gawk on the runners behaves the same.

## Risks & Mitigations

- A PR whose body has an indented or list-nested fence that never closes by
  the strict rule gets reviewed even though a real `[skip ai]` line follows.
  ACCEPTED — the author can put the marker in the title; reviewing is the
  safe direction.
- awk dialect differences. Mitigation: no interval expressions, only
  `match`/`substr`/`sub`/`length`, tested with mawk.
- The three copies drift. Mitigation: the case-table test runs the Python and
  awk rules, and the parity test holds the twin and `.claude/` copy equal.

## Rollout

Ships with the #4985 project: this project's final PR merges into
`claude/implement-plan-issue-4985-skip-marker-review-stall`, which reaches
`main` through #5031. Consumer repos get the workflow change through their
`@stable` wrappers and the `.claude/` script through the normal `.claude/`
sync. No flag; rollback is a revert.

## Auto-decisions

- AD-1 [plan, 2026-09-30] How strictly should the three copies parse fences? — Picked: A — liberal opener (any indentation, any info string, run of ≥ 3 backticks or tildes), strict closer (≤ 3 spaces, same character, length ≥ opener, only trailing blanks), unclosed fence runs to the end. Alternatives: B — full CommonMark opener rules as well (≤ 3 spaces, no backtick in a backtick info string); C — treat any body with nested or mismatched fences as having no marker. Why: every doubtful line resolves toward review, as the finding recommends, while ordinary fenced bodies keep their answer. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] Which copy of `check_in_status.py` does `tests/test_skip_ai_marker_rule.py` load? — Picked: A — the `workflow-templates/.claude/` twin (the parity test in `tests/test_check_in_status.py` keeps it equal to `.claude/`). Alternatives: B — keep loading `.claude/scripts/check_in_status.py` (red until the twin sync). Why: the interim twin-first rule has tests read the twin so the phase passes before the sync. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Keep or replace the existing fence identifiers? — Picked: A — keep `SKIP_AI_FENCE_RE` (same match set, now capturing the whole run) and the `in_fence` flag, and add `SKIP_AI_FENCE_CLOSE_RE`. Alternatives: B — replace them with new names. Why: §6 naming immutability. Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py` for #5377: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- #5377 (this finding), #3576 (security audit tracker), #4985 (the marker
  rule), #5031 (the #4985 project's final PR).
- CommonMark spec, fenced code blocks: https://spec.commonmark.org/0.31.2/#fenced-code-blocks
