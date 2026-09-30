# Bind the flagger's consensus_id citation to its own structured finding

Source issue: shubhodeep1/coding-workflows#4975 (https://github.com/shubhodeep1/coding-workflows/issues/4975)
Base branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason
Security pass: skip (ai:security: automation-produced issue)

## Summary

`scripts/review_claude_fixer_nonblocking.py` accepts the flagger's tie between a pass-2 finding and a rejected pass-1 finding when the `consensus_id` string appears anywhere in the flagger's raw output (line 609). This plan binds that id to one structured `File:` finding record in the flagger's output, on the same file and overlapping lines, and keeps mismatched or ambiguous same-line findings blocking.

## Context

- Security audit follow-up #4975 (A04:2021 Insecure Design, high, confidence 9/10), filed against the #4586 project branch (`Refs #3576`, the audit tracker). Exploit: a pass-1 false positive and a different, real pass-2 defect sit on the same line. If the pass-2 ledger attaches the old id to the new defect, the flagger quoting that id anywhere in its raw output (prose, a withdrawn-finding note, a stray line) satisfies `cid not in outputs[flagger]`. Votes that reject the false positive then demote the real defect, and the round can take the zero-findings auto-merge path.
- Prior work on the same filter: #4586 (rejected single-reviewer findings become non-blocking), #4688 (votes bound to run IDs), #4687 (votes bound to pass-1 `consensus_id`, not proximity). #4687 added the "flagger must cite the id" rule this plan tightens.
- The reviewer's raw finding shape is fixed by `prompts/review-reviewer-checklist.txt` and the runner prompt in `scripts/review_run_reviewers.sh`: `File:` / `Line or code reference:` / `Problem:` / `Why it fails at runtime:` / `SEVERITY:` / `ISSUE_CONFIDENCE:`. The cross-pollination header (`build_cross_pollination_summary`) asks a reviewer re-reporting a pass-1 defect to add `consensus_id: <id>` to that finding.
- CLAUDE.md §1 (security first), §5 (minimal change), §6 (existing `CLAUDE_FIXER_NONBLOCKING_KEPT` reason keys are log keys and stay), §7 (README / agents.md on behaviour change), §9 (tabs in Python), §20 (changelog fragment).

## Goals

- A flagger citation counts only as a whole `consensus_id: <id>` line inside one `File:` finding record of the flagger's raw pass-2 output, outside any fenced code block.
- That record must be the only flagger record citing the id, and it must name the same file as the pass-1 entry and the pass-2 ledger entry, with a line reference overlapping both ranges. A record whose file or line cannot be read binds nothing.
- The entry stays blocking when the flagger reported any other finding in the same file within `LINE_TOLERANCE` (3) lines of the pass-1 or pass-2 range, or in the same file with no readable line.
- The id quoted in prose, in a `REJECTED_FINDING` line, in a code block, after the record's blank line, or on a standalone line outside a record binds nothing (`flagger_did_not_cite`).
- Every existing demotion test that models a well-formed citation still demotes.

## Non-goals

- No change to vote counting, the manifest, `--issue-ids`, `--annotate`, the summariser prompt, or the hand-off step.
- No semantic (model-graded or text-similarity) comparison of problem descriptions: deterministic structure checks only.

## Constraints

- §1: fail toward blocking. Every new unreadable or ambiguous case keeps the finding blocking.
- §6: keep every existing reason key (`flagger_did_not_cite` keeps its meaning of "no citation by the flagger"); new cases get new keys (`flagger_citation_mismatch`, `ambiguous_flagger_nearby`). No identifier is renamed or removed.
- §5: the change is confined to the flagger-citation check and the one-sentence header instruction.
- §9: tabs in Python.
- No network access and no GitHub API calls in the script (§15 unaffected).

## Approach

Add a parser `flagger_finding_records(text)` to `scripts/review_claude_fixer_nonblocking.py`. It returns one record per `File:` line of the flagger's raw output, outside fenced blocks, with the path and line range read from the `File:` value (`path:N[-M]`) or the `Line or code reference:` / `Line:` / `Lines:` field, and the `consensus_id` values of whole `consensus_id:` lines in the record. A record runs from its `File:` line up to the first blank line, the next `File:` or `Requirement:` line, a `REJECTED_FINDING` line, a Markdown or all-caps heading, or a fence. The check at line 609 then requires exactly one record citing the id, on P's file with an overlapping range that also overlaps the ledger entry. After the existing ambiguity checks, a new check keeps the entry blocking when another flagger record in the same file is near either range or has no readable line. The cross-pollination header gains one sentence telling reviewers to put the `consensus_id:` line inside the finding's own `File:` block, since an id anywhere else does not count.

Alternatives considered: a text-similarity check on problem descriptions (rejected: non-deterministic, and paraphrase by design); accepting any standalone `consensus_id:` line (rejected: a stray line still binds nothing to a specific finding).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the fix is one self-contained change to one filter and its tests, and the issue is built as one project.

1. **Phase 1: bind the flagger citation to a structured finding record.**
   - Files: `scripts/review_claude_fixer_nonblocking.py`, `scripts/review_run_reviewers.sh` (one header sentence), `tests/test_review_claude_fixer_nonblocking.py`, `README.md`, `agents.md`, `changelog.d/4975-bind-flagger-citation-to-finding.md` [new].
   - Done when: the new tests below pass, every existing test in `tests/test_review_claude_fixer_nonblocking.py` and `tests/test_review_autofix_claude_fixer_mode.py` passes, `ruff check --select E,F --ignore E501 scripts/*.py` passes, and README / agents.md list the new reason keys.
   - Rollback: revert the phase PR. The filter only ever demotes less than before, so reverting restores the #4687 behaviour with no data or state to clean up.

## Implementation Steps

Phase 1:
1. `scripts/review_claude_fixer_nonblocking.py`: add the record-field regexes and `flagger_finding_records(text)` (fence-aware, same fence rules as `_votes_in`), returning `{"path", "lines", "consensus_ids"}` per record.
2. Same file, `demote_with_diagnostics`: parse the flagger's records once per entry; replace `if cid not in outputs[flagger]` with: no record citing `cid` → `flagger_did_not_cite`; more than one citing record, or the citing record's path is missing or not P's file, or its lines are missing or do not overlap both P's range and the entry's range → `flagger_citation_mismatch`.
3. Same function, after `ambiguous_nearby`: another flagger record whose path is missing, or equals the entry's file with lines missing or within `LINE_TOLERANCE` of P's or the entry's range → `ambiguous_flagger_nearby`.
4. Same file: update the module docstring's rule list; the NON-BLOCKING FINDINGS preamble stays unchanged.
5. `scripts/review_run_reviewers.sh` `build_cross_pollination_summary`: one added `echo` telling reviewers to put the `consensus_id:` line inside that finding's own `File:` / `Line or code reference:` block, because an id anywhere else in the output does not count.
6. `tests/test_review_claude_fixer_nonblocking.py`: add the tests listed below; move the #4687 nearby-flaw fixture to structured records.
7. `README.md` (Claude-fixer non-blocking section) and `agents.md` (the matching paragraph): describe the structured binding and list the two new reason keys.
8. `changelog.d/4975-bind-flagger-citation-to-finding.md` [new], `<!-- changelog: security -->`, per §20.

## Files & Modules

- `scripts/review_claude_fixer_nonblocking.py`
- `scripts/review_run_reviewers.sh`
- `tests/test_review_claude_fixer_nonblocking.py`
- `README.md`
- `agents.md`
- `changelog.d/4975-bind-flagger-citation-to-finding.md` [new]

## Tests

Unit tests in `tests/test_review_claude_fixer_nonblocking.py`:
- The issue's exploit: the flagger reports a new defect on the rejected entry's line without the id and quotes the id in prose, on a standalone line, after a blank line, in a code block, or in a `REJECTED_FINDING` line; the ledger entry carries the id → stays blocking (`flagger_did_not_cite`).
- The citing record names another file, a non-overlapping line, or no readable line, or two records cite the id → `flagger_citation_mismatch`.
- The flagger also reports another finding on the same line (or within 3 lines, or in the same file with no readable line) without the id → `ambiguous_flagger_nearby`.
- A distant second flagger finding in the same file does not block demotion.
- Accepted record shapes demote: `File: README.md` + `Line or code reference: 1261`, `- **File:** \`README.md:1261\``, `Line or code reference: README.md:1261`, `Line: L1261`.
- The header test asserts the new instruction sentence.
- Existing suites: `tests/test_review_claude_fixer_nonblocking.py`, `tests/test_review_autofix_claude_fixer_mode.py`; ruff over `scripts/*.py`.

## Risks & Mitigations

- Reviewers that write the id outside the finding block, or split a finding with blank lines, lose demotion and cost one fixer round → ACCEPTED: that is the fail-toward-blocking direction (§1), and the header sentence (step 5) tells reviewers where the line goes.
- A reviewer whose `Line or code reference:` holds only code text has no readable line → ACCEPTED, stays blocking; the `CLAUDE_FIXER_NONBLOCKING_KEPT … reason=flagger_citation_mismatch` log line names it.
- A flagger that attaches the id to a different defect in its own structured record at the same location still binds it → ACCEPTED: the flagger itself vouches for the tie, which is the contract #4687 set; the summariser and prose paths the issue names are closed.

## Rollout

Ships with the #4586 project's final PR; consumer repos receive it on the next `@stable` sync. No flag, no migration. Rollback: revert the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-29] What counts as the flagger's "specific structured finding"? — Picked: A — a `File:` record in the flagger's raw pass-2 output, outside fences, ending at the first blank line, next `File:` / `Requirement:` / `REJECTED_FINDING` line, heading, or fence; only a whole `consensus_id: <id>` line inside it counts. Alternatives: B — any standalone `consensus_id:` line outside fences; C — a model-graded same-defect judgement. Why: the smallest deterministic binding to one finding; B still binds a stray line to no finding in particular, C is non-deterministic. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How to verify the citing record describes the same defect? — Picked: A — exactly one citing record, same file as the pass-1 and pass-2 entries, a line reference overlapping both ranges; an unreadable file or line binds nothing. Alternatives: B — A plus text similarity of the problem lines; C — file match only. Why: location is the only field both passes carry verbatim; problem text is paraphrased by design, and C would let a same-file defect bind. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] When is a same-line flagger finding ambiguous? — Picked: A — any other flagger record in the same file within 3 lines of either range, or in the same file (or an unreadable file) with no readable line, keeps the entry blocking. Alternatives: B — only an overlapping record; C — no flagger-side ambiguity check. Why: matches the existing `LINE_TOLERANCE` window of the ledger-side checks and fails toward blocking (§1). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Reason keys for the new cases? — Picked: A — keep `flagger_did_not_cite` for "no record cites the id" and add `flagger_citation_mismatch` and `ambiguous_flagger_nearby`. Alternatives: B — fold the new cases into existing keys. Why: §6 keeps existing log keys' meaning; separate keys make the log line say why a finding stayed blocking. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Tell reviewers where the `consensus_id:` line goes? — Picked: A — add one sentence to the cross-pollination header. Alternatives: B — no prompt change. Why: without it, well-behaved reviewers that place the id elsewhere lose demotion needlessly; one sentence is a minimal change. Applied in: phase 1 PR. Status: pending review

## Notes

- `.claude/scripts/security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 4975` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #4975; audit tracker #3576; prior issues #4586, #4687, #4688.
- `docs/completed/issue-4687-bind-rejections-to-consensus-ids-plan.md`.
