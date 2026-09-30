# Claude-fixer review: a clean vote needs a strict verdict format

Source issue: shubhodeep1/coding-workflows#5298 (https://github.com/shubhodeep1/coding-workflows/issues/5298)
Base branch: claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote
Security pass: skip (ai:security: automation-produced issue)

## Summary

On the failed-slot path, the Claude-fixer hand-off step counts a reviewer as a clean vote when its runner output has a `NONE` line and no *labelled* finding field. An unlabelled finding next to `NONE` (`- scripts/auth.sh:42 | severity=critical`) still counts. This plan replaces that check with a strict verdict grammar and rejects any other text that cites a code location or a severity, so extra or unparseable finding text never becomes a clean vote.

## Context

- Issue #5298 is a `high` security-audit finding (A08:2021, confidence 8/10) against `scripts/review_autofix_step_claude_fixer_handoff.sh:207` on the #4835 project branch. Exploit: with one verified failed slot, a reviewer output of `NONE` plus `- scripts/auth.sh:42 | severity=critical` is classified clean. If the consensus ledger (model output) also labels that block clean, five such votes auto-merge the PR despite the reported finding.
- #5114 added `claude_fixer_runner_output_state()` (lines 150–230): a clean vote needs at least one `NONE`, no labelled finding or task-gap field, and, when any lens heading appears, all nine lenses each followed by `NONE`. Its plan listed this exact gap as an accepted risk ("a reviewer that writes a finding as unlabelled prose next to a `NONE` verdict still counts"). The audit re-flagged it.
- The reviewer contract (`prompts/review-reviewer-checklist.txt`): nine lens headings, each answered by findings in the labelled format or the literal word `NONE`. Findings under the first eight lenses must cite a file and line. Without the checklist the contract is a bare `NONE` (`prompts/_nag_reminders.txt`). The runner prompt also asks for an advisory `HARDENING_SUGGESTIONS:` section (`scripts/review_run_reviewers.sh:2576`).
- Real outputs sized the rule: 63 `review_<slug>.txt` files from 11 `internal-review.yml` runs on `claude/*` heads (2026-09-29: 36554461316, 36555902078, 36557218274, 36559481375, 36561672233, 36641346955, 36641456604, 36641522648, 36643499101, 36643774863, 36643824863). Clean reviewers add agent narration before the verdicts and a summary after them. Some reorder the lenses. One adds `HARDENING_SUGGESTIONS:` / `NONE`. Pass-2 reviewers add a "cross-pollination verdict" that discusses earlier findings with file:line cites and severities. The current rule accepts 34 of 63. The rule below accepts 22. Every extra rejection is text citing a code location or a severity outside the verdicts, which is the ambiguous text the audit asks to reject. It accepts no output the current rule rejects.

## Goals

- On the failed-slot path, a clean-ledger block counts toward `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` only when `review_<slug>.txt` matches the strict verdict grammar (Approach), in addition to today's `success` status.
- The issue's exploit (`NONE` plus `- scripts/auth.sh:42 | severity=critical`, in bare or checklist form, before, between, or after the verdicts) is handed off with a `::warning::` naming the slug and the reason, never auto-merged.
- Existing #4835 / #4885 / #5114 behaviour is otherwise unchanged: failed-slot verification, the minimum, the slug and duplicate guards, the fresh-check snapshot, the hand-off body, and every rejection reason #5114 already emits.

## Non-goals

- Ledgers with no failed slot keep today's every-block-clean rule, as #5114's AD-1 decided (AD-4).
- No change to the reviewer runner, the prompts, the summariser, or the workflow YAML.
- No new env var, repo var, or log key.

## Constraints

- §1: security first; every new check fails closed (hand-off, never auto-merge).
- §5: the change stays in `claude_fixer_runner_output_state()` and its comments, plus tests, docs, and a changelog fragment.
- §6: nothing renamed. The function name, `claude_fixer_checklist_lens_headings`, `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS`, `CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS`, the warning text, and the existing reason strings stay. New reasons are added. New awk variables are local to the awk program.
- §9: the script keeps its 2-space indentation (§5, no reformatting); the test file keeps tabs.
- §15: no GitHub API calls added.
- §20: one `changelog.d/5298-strict-clean-vote-format.md` fragment, `security` section.
- §27: `review_autofix.yml` is not touched.
- The awk must run under mawk and gawk (the tests force both); no regex interval expressions.

## Approach

`claude_fixer_runner_output_state()` keeps its missing / unreadable / empty checks and its first three reasons, in the same precedence. It then parses the non-blank, trimmed lines against this grammar (AD-1):

1. **Finding field anywhere** → `reports a finding or task gap` (unchanged).
2. **No `NONE` line** → `has no NONE verdict` (unchanged).
3. **Verdict.** If any line is a lens heading (today's normalisation: markdown markers, list numbering, trailing `*_`:` ignored), the verdict is the checklist block. It starts at the first heading and is a contiguous run of heading-then-`NONE` pairs. Each of the nine headings appears exactly once, in any order (AD-3).
   - A heading later in the output, past the end of that run → `has text between its checklist verdicts` (new).
   - A heading not directly followed by `NONE`, a repeated heading, or fewer than nine headings → `leaves a checklist lens without a NONE verdict` (unchanged reason).
   - With no lens heading, the verdict is the one `NONE` line (the first).
4. **Everything else is free text** (narration before, summary after). The pair `HARDENING_SUGGESTIONS:` directly followed by `NONE` is allowed (AD-2). Any other free-text line fails the vote if:
   - it is `NONE` → `has a stray NONE verdict` (new);
   - it cites a code location → `cites a code location or severity outside its verdicts` (new). A location is a letter, digit, or `_` directly followed by `:` and a digit (`auth.sh:42`, `Makefile:3`), `#L` plus a digit, or the word `line` or `lines` followed by a space and a digit;
   - it carries a severity or confidence marker → same reason. Markers are `severity`, `issue_confidence`, `task_gap`, `risk_score` anywhere, and the whole words `blocker`, `major`, `critical`, `nit`, all case-insensitive.

The script comment lists the grammar. The README row and the agents.md paragraph describe it.

Alternatives (AD-1): **B**, no free text at all, accepts 11 of 63 real outputs and would mostly disable the failed-slot path #4835 exists for. **C**, keeping today's rule and only adding the location and severity scan, still accepts location-free prose between two lens verdicts, where the contract allows only `NONE` or a labelled finding.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for a standalone issue. The fix is one function in one script plus its tests and docs, and it cannot be split into independently useful parts.

1. **Phase 1 — strict verdict grammar for clean votes.** Files: `scripts/review_autofix_step_claude_fixer_handoff.sh`, `tests/test_review_autofix_claude_fixer_mode.py`, `README.md`, `agents.md`, `changelog.d/5298-strict-clean-vote-format.md` [new]. Done when: the new and existing Claude-fixer tests pass with the default awk and with mawk and gawk forced (gawk when installed), the whole `tests/test_review_autofix_claude_fixer_mode.py`, `tests/test_workflow_file_size_limit.py`, and the review_autofix contract tests pass, `bash -n` (and `shellcheck` when installed) is clean, and the docs describe the grammar. Rollback: revert the phase PR; the base branch returns to the #5114 rule.

## Implementation Steps

Phase 1:
1. `scripts/review_autofix_step_claude_fixer_handoff.sh`, clean-ledger comment (lines ~150–161): replace the rule description with the grammar and cite issue #5298.
2. Same file, `claude_fixer_runner_output_state()` awk program (lines ~184–228): collect trimmed non-blank lines with their per-line flags (NONE, heading, field, location, severity, hardening label), then apply steps 1–4 of Approach in `END`. Keep the here-string input and `return 0`.
3. Same file, header comment (lines ~20–28): mention the strict format.
4. `tests/test_review_autofix_claude_fixer_mode.py`: add tests for the issue's exploit in bare form and after, before, and between checklist verdicts; each location and severity marker; a stray `NONE`; a heading past the block; a repeated heading; a reordered complete block (clean); `HARDENING_SUGGESTIONS:` / `NONE` (clean) and with content (rejected); narration and a location-free summary (clean); and mawk/gawk parity for the new reasons. Existing tests keep their reasons.
5. `README.md` (the `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` row) and `agents.md` (the Claude-fixer paragraph): describe the grammar.
6. `changelog.d/5298-strict-clean-vote-format.md` [new]: `<!-- changelog: security -->` entry per §20.

## Files & Modules

- `scripts/review_autofix_step_claude_fixer_handoff.sh`
- `tests/test_review_autofix_claude_fixer_mode.py`
- `README.md`
- `agents.md`
- `changelog.d/5298-strict-clean-vote-format.md` [new]

## Tests

- Unit (pytest, stubbed `gh`): step 4's cases, with the default `awk` and with `mawk` and `gawk` forced through `PATH`.
- Regression: all of `tests/test_review_autofix_claude_fixer_mode.py`, `tests/test_workflow_file_size_limit.py`, and the review_autofix contract tests.
- Static: `bash -n`; `shellcheck` when available.
- End-to-end: the chain's conformance audit and runtime validation on the project branch.

## Risks & Mitigations

- Clean reviewers whose summary cites a line or a severity no longer count, so a failed-slot ledger hands off more often (22 of 63 accepted, down from 34). ACCEPTED: this fails closed, and it is the ambiguity the audit asks to reject.
- A finding written as prose with no location and no severity marker, outside the verdicts, still counts. ACCEPTED: the reviewer contract requires a file and line for lens 1–8 findings and labels for task gaps. That reviewer's own complete `NONE` verdict contradicts it, and the ledger must also read clean for that block.
- A future rename of a lens heading would change the grammar. Mitigation: the existing test pins the heading list to the prompt file.

## Rollout

No flag: the check only narrows when the failed-slot path may auto-merge. It ships with the #4835 project's final PR into `main`, then to consumers on the next `@stable` release with no wrapper change. Rollback: revert this project's PR on the base branch.

## Auto-decisions

- AD-1 [plan, 2026-09-29] What format must a clean reviewer's `review_<slug>.txt` have? — Picked: A — the strict verdict grammar (a contiguous, complete heading/`NONE` block or one bare `NONE`), with free text allowed only when it has no stray verdict, code location, or severity marker. Alternatives: B — no free text at all; C — today's rule plus a location and severity scan. Why: A rejects the exploit in every position and accepts 22 of 63 real outputs. B accepts 11 of 63 and brings back most of the #4835 stall. C still accepts location-free prose between two lens verdicts, where the contract allows only `NONE` or a labelled finding. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Is `HARDENING_SUGGESTIONS:` followed by `NONE` allowed next to the verdicts? — Picked: A — yes, as that exact pair only. Alternatives: B — no, it is a stray `NONE`. Why: it is the runner prompt's own advisory section (`scripts/review_run_reviewers.sh:2576`), seen on real clean outputs, and the pair states that nothing was found. Any content in the section is still judged as free text. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Must the nine lens headings appear in the prompt's order? — Picked: A — no, any order, each exactly once. Alternatives: B — the prompt's exact order. Why: a reordered, complete, contiguous block is just as unambiguous, and a real clean output reordered the lenses. Order adds no protection against hidden findings. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Should the stricter rule also apply to ledgers with no failed slot? — Picked: A — no, only the failed-slot path. Alternatives: B — every ledger. Why: the issue and the audited code are the failed-slot path. The no-failed-slot rule is main's pre-existing behaviour, left alone by #5114's AD-1, and changing it alters every Claude-fixer PR's merge contract (§5, §12.D). Flagged for human review. Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5298` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #5298 (this finding), issue #5114 and its plan `docs/completed/issue-5114-verify-clean-votes-from-runner-output-plan.md`, issue #4835 and its final PR #4847 (base branch), issue #4885, tracker #3576.
- `prompts/review-reviewer-checklist.txt`, `prompts/_nag_reminders.txt`, `scripts/review_run_reviewers.sh`.
