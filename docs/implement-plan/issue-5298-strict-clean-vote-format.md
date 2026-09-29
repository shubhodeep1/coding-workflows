# Implement-Plan Log — Claude-fixer review: a clean vote needs a strict verdict format

- Plan: docs/plans/issue-5298-strict-clean-vote-format-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5298-strict-clean-vote-format   Final PR: #5305 draft
- Source issue: shubhodeep1/coding-workflows#5298   Base branch: claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote   Security pass: skip (ai:security: automation-produced issue)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (branch claude/implement-plan-issue-5298-strict-clean-vote-format-phase-1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented and verified (89 Claude-fixer tests incl. mawk/gawk parity, 232 regression tests, 52 changelog tests, shellcheck clean); phase PR opened against the project branch.

## Phases
1. [ ] Phase 1 — Strict verdict grammar for clean votes (failed-slot path of the Claude-fixer hand-off)   — PR open (waiting); review rounds: 0; interventions: 0
   - scripts/review_autofix_step_claude_fixer_handoff.sh: `claude_fixer_runner_output_state()` accepts only a contiguous, complete heading/`NONE` block or one bare `NONE`; free text fails on a stray `NONE`, a code location, or a severity/confidence marker; `HARDENING_SUGGESTIONS:` + `NONE` allowed
   - tests/test_review_autofix_claude_fixer_mode.py: exploit in bare form and before/between/after checklist verdicts, each location and severity marker, stray NONE, heading past the block, repeated heading, reordered block, hardening pair, clean narration/summary, mawk/gawk parity
   - README.md, agents.md, changelog.d/5298-strict-clean-vote-format.md
   - Done: new and existing Claude-fixer tests pass (default awk, mawk, gawk when installed), workflow size and review_autofix contract tests pass, bash -n (shellcheck when installed) clean, docs describe the grammar

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] What format must a clean reviewer's `review_<slug>.txt` have? — Picked: A — the strict verdict grammar (a contiguous, complete heading/`NONE` block or one bare `NONE`), with free text allowed only when it has no stray verdict, code location, or severity marker. Alternatives: B — no free text at all; C — today's rule plus a location and severity scan. Why: A rejects the exploit in every position and accepts 22 of 63 real outputs; B accepts 11 of 63 and brings back most of the #4835 stall; C still accepts location-free prose between two lens verdicts. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Is `HARDENING_SUGGESTIONS:` followed by `NONE` allowed next to the verdicts? — Picked: A — yes, as that exact pair only. Alternatives: B — no, it is a stray `NONE`. Why: it is the runner prompt's own advisory section and the pair states that nothing was found; any content in the section is still judged as free text. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Must the nine lens headings appear in the prompt's order? — Picked: A — no, any order, each exactly once. Alternatives: B — the prompt's exact order. Why: a reordered, complete, contiguous block is just as unambiguous, and a real clean output reordered the lenses. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Should the stricter rule also apply to ledgers with no failed slot? — Picked: A — no, only the failed-slot path. Alternatives: B — every ledger. Why: the issue and the audited code are the failed-slot path; the no-failed-slot rule is main's pre-existing behaviour, left alone by #5114's AD-1. Flagged for human review. Applied in: phase 1. Status: pending review

## Lessons
- [source:plan-deviation] Size a reviewer-output rule against real `review_<slug>.txt` files from several review runs (the reviewer-logs artifact) before choosing it: real clean outputs carry agent narration, summaries, reordered lenses, and a HARDENING_SUGGESTIONS section, so a rule tested only on synthetic outputs over- or under-rejects. (files: scripts/review_autofix_step_claude_fixer_handoff.sh)

## Notes
- Issue progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5298#issuecomment-5901171187
- security_pass_skip.py returned skip: true (ai:security: created and labelled by the issue automation).
