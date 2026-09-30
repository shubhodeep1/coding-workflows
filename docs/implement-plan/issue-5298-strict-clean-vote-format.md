# Implement-Plan Log — Claude-fixer review: a clean vote needs a strict verdict format

- Plan: docs/completed/issue-5298-strict-clean-vote-format-plan.md (was docs/plans/issue-5298-strict-clean-vote-format-plan.md)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5298-strict-clean-vote-format   Final PR: #5305 draft
- Source issue: shubhodeep1/coding-workflows#5298   Base branch: claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote   Security pass: skip (ai:security: automation-produced issue)
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote)
- Waiting on: completion PR (claude/implement-plan-issue-5298-strict-clean-vote-format-complete)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01T7tapRxJXqichy27mtbMUp (project checker, reused; safety-net and hand-back ids in the stage report)
- Last updated: 2026-09-30
- Last note: Q1 answered A on #5298 (standing decision Q17): validation skipped, covered by the #4835 project's validation; completion PR moves the plan to docs/completed/, then the final merge of #5305 into the #4835 project branch.

## Phases
1. [x] Phase 1 — Strict verdict grammar for clean votes (failed-slot path of the Claude-fixer hand-off)   — PR #5308 merged 2026-09-30 (9cb4b78); review rounds: 0; interventions: 0
   - scripts/review_autofix_step_claude_fixer_handoff.sh: `claude_fixer_runner_output_state()` accepts only a contiguous, complete heading/`NONE` block or one bare `NONE`; free text fails on a stray `NONE`, a code location, or a severity/confidence marker; `HARDENING_SUGGESTIONS:` + `NONE` allowed
   - tests/test_review_autofix_claude_fixer_mode.py: exploit in bare form and before/between/after checklist verdicts, each location and severity marker, stray NONE, heading past the block, repeated heading, reordered block, hardening pair, clean narration/summary, mawk/gawk parity
   - README.md, agents.md, changelog.d/5298-strict-clean-vote-format.md
   - Done: new and existing Claude-fixer tests pass (default awk, mawk, gawk when installed), workflow size and review_autofix contract tests pass, bash -n (shellcheck when installed) clean, docs describe the grammar

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (correctness PASS) — no fixes (pre-security). Every plan criterion traces to the merged code. Checks: `pytest tests/test_review_autofix_claude_fixer_mode.py tests/test_workflow_file_size_limit.py tests/test_review_autofix_review_pipeline_contract.py tests/test_review_autofix_step_scripts_contract.py` (232 passed, mawk and gawk installed), bash -n, shellcheck, 19 extra classifier edge-case probes under mawk and gawk.

## Security pass
- Skipped: ai:security: automation-produced issue (security_pass_skip.py)

## Validation
- Cycle 1 — 2026-09-30: not dispatched. `validate.yml` ("Authorize explicit validation target", `.github/workflows/validate.yml:176-210`) accepts `target_ref` only when exactly one open PR has that head and `base=<default branch>`. Final PR #5305 targets `claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote`, so the run would fail before validating. Validating the default branch or the base branch in its place is not allowed, so this was a stop (CLAUDE.md §28.C), asked on #5298 (comment 5901996379).
- Validation: skipped (covered by parent project #4835 (non-default base)). Q1: A answered on #5298 (comment 5902201934, 2026-09-30) under the master session's standing decision Q17; the #4835 project runs its security audit and runtime validation on a branch carrying this change before anything reaches `main`. Long-term fix: #4734.

## Completion
- Completion PR (branch claude/implement-plan-issue-5298-strict-clean-vote-format-complete) opened 2026-09-30 — doc moved to docs/completed/issue-5298-strict-clean-vote-format-plan.md
- Final PR #5305 draft (into the #4835 project branch)

## Activation
- n/a (base claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote)

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
- Resumed 2026-09-30 by session session_01Nt5188WSCyrbLaYvt8FECX (dispatched on `/reclarify`); removed `ai:claude-blocked` from #5298. The committed log still read `Status: IN_PROGRESS` / `Stage: phase 1/1` (the conformance 1/3 stage stopped at BLOCKED with no PR in flight), but its `Check-in:` line was `none`, so `/implement-issue-claude` step 4 resumed instead of stopping. Issue base PR #4847 still open and unmerged, so the base did not move.
- The conformance 1/3 stage session session_01MegRDFkTC2eEUcjWj7sBis, which posted the validation blocker, is left open (it reads `need_input`; a session waiting on the user is never archived).
