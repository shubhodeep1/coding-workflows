# Claude-fixer review: a clean vote must come from the reviewer's own output

Source issue: shubhodeep1/coding-workflows#5114 (https://github.com/shubhodeep1/coding-workflows/issues/5114)
Base branch: claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote
Security pass: skip (ai:security: automation-produced issue)

## Summary

When a reviewer slot failed, the Claude-fixer hand-off step counts a ledger block that reads `(No findings reported.)` as a clean vote once the slot's status file reads `success`. It never checks what that reviewer actually wrote. This plan makes each clean vote on that path depend on the runner-written `review_<slug>.txt` holding an unambiguous no-findings result, so a consensus ledger that mislabels a reviewer's findings can no longer authorize auto-merge.

## Context

- Issue #5114 is a `high` security-audit finding (A08:2021, confidence 9/10) against `scripts/review_autofix_step_claude_fixer_handoff.sh:217` on the #4835 project branch. Exploit: with one genuine failed slot, the consensus ledger (model output, `scripts/summarize_reviewer_consensus.sh`) can label a successful reviewer's block clean while its `review_<slug>.txt` reports a finding. Five `success` statuses then pass `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` and the round auto-merges.
- #4835 (base branch) introduced the failed-slot path. #4885 already binds each **failed** block to the runner's exact failure line in `review_<slug>.txt`. The **clean** blocks are still trusted on ledger text plus `status_review_<slug>.txt` = `success`, and `success` only means the reviewer finished, not that it found nothing.
- The reviewer contract defines "found nothing" as the literal word `NONE`: with the lens checklist (`prompts/review-reviewer-checklist.txt`, enabled by default in the review job, `review_autofix.yml:2330`), `NONE` under each of the nine lens headings; without it, `NONE` alone (`prompts/_nag_reminders.txt:2`). Findings use the labelled issue format (`File:`, `Problem:`, …) and task gaps the TASK_GAP format (`Requirement:`, `Evidence of absence:`, …).
- Reviewer outputs from five `claude/implement-plan-*` review runs on 2026-09-29 (runs 36559481375, 36561672233, 36557218274, 36555902078, 36554461316; 30 files) were used to size the rule. On the fully clean run 36559481375, the chosen rule accepts 5 of 6 reviewers. It rejects the one whose whole output was a narration line. It rejects every output that carried a finding.

## Goals

- On the failed-slot path, a ledger block classified clean counts toward `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` only when `status_review_<slug>.txt` reads `success` **and** `review_<slug>.txt` is an unambiguous no-findings result (rule in Approach).
- A clean block whose runner output is missing, empty, unreadable, reports a finding or task gap, or is otherwise ambiguous makes the ledger not clean: the round is handed off, with a `::warning::` naming the slug and the reason.
- Existing #4835 / #4885 behaviour is otherwise unchanged: failed-slot verification, the minimum, the duplicate-block and slug guards, the fresh-check snapshot, the hand-off body.

## Non-goals

- Ledgers with no failed slot keep today's every-block-clean rule (AD-1). That path is main's pre-existing behaviour, shared with the GPT editor path, and is not the audited change.
- No change to the reviewer runner, the summariser prompt, the reviewer checklist prompt, or the workflow YAML.
- No new env var, repo var, or log key.

## Constraints

- §1: security first; every new check fails closed (hand-off, never auto-merge).
- §5: the change stays inside the failed-slot branch of the clean-ledger check (`claude_fixer_handoff.sh`, the `clean:success` case) plus its comments, tests, and docs.
- §6: no identifier renamed; `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS`, `CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS`, and the hand-off markers are unchanged. New shell identifiers use the `claude_fixer_` prefix and are checked unique in the script.
- §9: the script keeps its existing 2-space indentation (shell body sourced by the workflow; the file already uses spaces, §5 forbids reformatting).
- §15: no GitHub API calls added (local file reads only).
- §20: one `changelog.d/5114-…` fragment in the `security` section.
- §27: `review_autofix.yml` is not touched.
- The awk must run under both mawk and gawk (the #4835 tests run both); no regex interval expressions.

## Approach

Add a small classifier to the hand-off script that reads the runner's `review_<slug>.txt` for a clean block, and require it in the `clean:success` case. A clean vote needs all of the following (AD-2):

1. The file exists under `PREVIOUS_REVIEWS_DIR` (same slug guard as today), is readable, and is not empty.
2. At least one line, trimmed, is exactly `NONE`.
3. No line is a finding or task-gap field: after optional leading whitespace and markdown markers (`-`, `*`, `>`, `#`, `_`, backtick), no line starts case-insensitively with `File`, `Line or code reference`, `Problem`, `Why it fails at runtime`, `Requirement`, `Expected change site`, `Evidence of absence`, `SEVERITY`, or `ISSUE_CONFIDENCE` followed by optional markdown markers and `:`.
4. If any of the nine checklist lens headings appears as a line (trimmed, leading `#`/`*` and trailing `*`/`:` ignored), all nine must appear, and the next non-blank line after each heading must be exactly `NONE`.

Prose outside the lens verdicts, before or after them, is allowed. The measured outputs carry such prose, and it is not in the finding format. The nine headings are held in the script (AD-3), and a test pins them to `prompts/review-reviewer-checklist.txt`, so a prompt change that renames a lens fails CI instead of silently loosening rule 4.

A block that fails the classifier is logged as `::warning::Claude-fixer ledger block '<slug>' reads clean but the reviewer runner's review_<slug>.txt is <state>, not an unambiguous no-findings result; the ledger is not clean.`. Here `<state>` is `missing`, `empty`, `unreadable`, `a finding or task gap`, `without a NONE verdict`, or `a lens without a NONE verdict`. The block then sets `claude_fixer_failed_slots_verified=false`, as the other mismatches do.

Alternatives considered: the strict rule (only headings and `NONE` lines) accepted 1 of the 6 reviewers on the clean run, which would bring back the #4835 stall. The existing `reviewer_output_has_findings` / `reviewer_output_has_explicit_none` pair from the runner accepts a lens left without a verdict and only matches unformatted labels (AD-2).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for a standalone issue. The fix is one check in one script plus its tests and docs, and it cannot be split into independently useful parts.

1. **Phase 1 — clean votes verified against the runner output.** Files: `scripts/review_autofix_step_claude_fixer_handoff.sh`, `tests/test_review_autofix_claude_fixer_mode.py`, `README.md`, `agents.md`, `changelog.d/5114-verify-clean-votes-from-runner-output.md` [new]. Done when: the new and updated Claude-fixer tests pass under the default awk and with mawk and gawk forced, the full `tests/test_review_autofix_claude_fixer_mode.py` and the `review_autofix` contract / workflow-size tests pass, `shellcheck` is clean on the script, and the docs describe the runner-output requirement. Rollback: revert the phase PR; the base branch returns to the #4835 / #4885 behaviour.

## Implementation Steps

Phase 1:
1. `scripts/review_autofix_step_claude_fixer_handoff.sh`, clean-ledger check comment (lines ~123–143): state that a clean vote on the failed-slot path also needs the runner output to be an unambiguous no-findings result (issue #5114), and list the four rules.
2. Same file, before the check: add `claude_fixer_runner_output_state()` (prints `clean`, `missing`, `empty`, `unreadable`, `finding`, `no-none`, or `lens`), a mawk/gawk-portable awk program applying rules 2–4, with the nine lens headings in one place.
3. Same file, `clean:success)` case (line ~217): call the classifier on `${PREVIOUS_REVIEWS_DIR}/review_<slug>.txt` (same slug and dir guards as the status read). Count the vote only on `clean`; otherwise emit the warning and set `claude_fixer_failed_slots_verified=false`.
4. Same file, header comment (lines ~20–27 and the Inputs list): mention that clean votes are verified against `review_<slug>.txt`.
5. `tests/test_review_autofix_claude_fixer_mode.py`: add a checklist-shaped clean runner output and a bare `NONE` output. Extend `_runner_outputs` so panel tests write clean outputs for clean slots. Add tests for: the issue's exploit (ledger clean, runner output has a labelled finding, then hand-off); a TASK_GAP in the runner output; markdown-decorated labels; a missing, empty, or unreadable clean output; output without `NONE`; a lens heading followed by prose; a missing lens; prose around a full NONE verdict still counting; bare `NONE` counting; the heading list matching `prompts/review-reviewer-checklist.txt`; mawk and gawk parity; and the no-failed-slot ledger still not reading runner outputs.
6. `README.md` (the `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` row) and `agents.md` (the Claude-fixer paragraph): add the runner-output requirement for clean votes.
7. `changelog.d/5114-verify-clean-votes-from-runner-output.md` [new]: `<!-- changelog: security -->` entry per §20.

## Files & Modules

- `scripts/review_autofix_step_claude_fixer_handoff.sh`
- `tests/test_review_autofix_claude_fixer_mode.py`
- `README.md`
- `agents.md`
- `changelog.d/5114-verify-clean-votes-from-runner-output.md` [new]

## Tests

- Unit (pytest, stubbed `gh`): the cases in step 5, run with the default `awk` and with `mawk` and `gawk` forced through `PATH`.
- Regression: all of `tests/test_review_autofix_claude_fixer_mode.py` (existing #4835 / #4885 cases updated to write clean runner outputs where they expect a clean ledger), `tests/test_workflow_file_size_limit.py`, and the review_autofix contract tests.
- Static: `shellcheck` on the script; `bash -n`.
- End-to-end: covered by the chain's conformance audit and runtime validation on the project branch.

## Risks & Mitigations

- A clean reviewer whose output strays from the contract (narration only, or a lens left without `NONE`) no longer counts, so a failed-slot ledger hands off more often. ACCEPTED: this fails closed, and on the measured clean run 5 of 6 reviewers still count, which meets the default minimum of 5 when one slot fails.
- A reviewer that writes a finding as unlabelled prose next to a `NONE` verdict still counts. ACCEPTED: such text is not in the reviewer's finding format. The lens verdicts (rule 4) and label scan (rule 3) cover the contract formats, and the consensus ledger must also read clean for that block.
- A future rename of a checklist lens would loosen rule 4. Mitigation: the test pins the heading list to the prompt file.

## Rollout

No flag: the check only narrows when the existing failed-slot path may auto-merge. It ships with the #4835 project's final PR into `main`, then to consumers on the next `@stable` release with no wrapper change. Rollback: revert this project's PR on the base branch.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Should the runner-output check also apply to ledgers with no failed slot? — Picked: A — no, only the failed-slot path. Alternatives: B — every ledger. Why: the issue and the audited code are the failed-slot path. The every-block-clean rule is main's pre-existing behaviour, shared with the GPT path, and changing it alters every Claude-fixer PR's merge contract (§5, §12.D). Flagged for human review. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What counts as an unambiguous no-findings result in `review_<slug>.txt`? — Picked: A — at least one `NONE`, no finding or task-gap field label (markdown-tolerant), and when checklist headings appear, all nine present, each followed by `NONE`. Alternatives: B — only headings and `NONE` lines; C — the runner's `reviewer_output_has_findings` / `_has_explicit_none` pair. Why: on real outputs A accepts 5 of 6 reviewers on a clean run and rejects every output with a finding. B accepts 1 of 6 and brings back the #4835 stall. C accepts a lens left without a verdict and misses markdown-decorated labels. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Where do the lens headings come from? — Picked: A — a fixed list in the script, pinned by a test to `prompts/review-reviewer-checklist.txt`. Alternatives: B — parse the prompt file at run time. Why: A has no run-time dependency on the prompts dir, and CI catches drift. B would fail open if the prompt file were missing. Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5114` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #5114 (this finding), issue #4835 and its final PR #4847 (base branch), issue #4885 (failed-slot runner-line check), tracker #3576.
- `scripts/review_run_reviewers.sh` (`reviewer_output_has_explicit_none`, `reviewer_output_has_findings`, status and output writes), `scripts/summarize_reviewer_consensus.sh`, `prompts/review-reviewer-checklist.txt`, `prompts/_nag_reminders.txt`.
