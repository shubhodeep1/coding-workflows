# Implement-Plan Log — Claude-fixer review: a clean vote must come from the reviewer's own output

- Plan: docs/completed/issue-5114-verify-clean-votes-from-runner-output-plan.md (was docs/plans/issue-5114-verify-clean-votes-from-runner-output-plan.md)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5114-verify-clean-votes-from-runner-output   Final PR: #5134 draft
- Source issue: shubhodeep1/coding-workflows#5114   Base branch: claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote   Security pass: skip (ai:security: automation-produced issue)
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote)
- Waiting on: completion PR (claude/implement-plan-issue-5114-verify-clean-votes-from-runner-output-complete)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_014e1U6XSZ2C1s4ufzyV1P4Q (project checker, reused; safety-net and hand-back ids in the stage report)
- Last updated: 2026-09-29
- Last note: Q1 answered A on #5114 (standing decision Q17, confirmed as Q18): validation skipped, covered by the #4835 project's validation; completion PR moves the plan to docs/completed/, then the final merge of #5134 into the #4835 project branch.

## Phases
1. [x] Phase 1 — Clean votes verified against the runner output (runner-output classifier in the Claude-fixer failed-slot clean check, tests, docs, changelog)   — PR #5143 merged 2026-09-29; review rounds: 0; interventions: 0
   - scripts/review_autofix_step_claude_fixer_handoff.sh: `clean:success` counts only when review_<slug>.txt is an unambiguous no-findings result (NONE present, no finding / task-gap field label, all nine lens verdicts NONE when the checklist is used); otherwise warn and hand off
   - tests/test_review_autofix_claude_fixer_mode.py: exploit case (ledger clean, runner finding), task gap, markdown labels, missing / empty / unreadable output, no NONE, lens with prose, missing lens, prose around verdicts, bare NONE, heading list pinned to the checklist prompt, mawk / gawk parity, no-failed-slot path unchanged
   - README.md, agents.md, changelog.d/5114-verify-clean-votes-from-runner-output.md
   - Done: new and existing Claude-fixer tests pass (default awk, mawk, gawk), workflow size and review_autofix contract tests pass, shellcheck clean, docs describe the requirement

## Conformance
- Run 1 — 2026-09-29: CONFORMANT (correctness CONCERNS) — fix PR #5172 merged 2026-09-29 (23b16e6) from branch claude/implement-plan-issue-5114-verify-clean-votes-from-runner-output-conformance-fix-1 (pre-security); review rounds: 0; interventions: 0. Finding: lens headings written as a numbered or bulleted list were not recognised, so a lens answered with prose instead of NONE still counted as a clean vote (scripts/review_autofix_step_claude_fixer_handoff.sh:207-208).
- Run 2 — 2026-09-29: CONFORMANT (correctness PASS) — no fixes (pre-security). Re-audited the merged #5172 fix on the project branch. Checks: `pytest tests/test_review_autofix_claude_fixer_mode.py tests/test_workflow_file_size_limit.py tests/test_review_autofix_step_scripts_contract.py tests/test_review_autofix_review_pipeline_contract.py` (225 passed), shellcheck, bash -n, classifier probes under mawk and gawk.

## Security pass
- Skipped: ai:security: automation-produced issue (security_pass_skip.py)

## Validation
- Cycle 1 — 2026-09-29: not dispatched. `validate.yml` ("Authorize explicit validation target", `.github/workflows/validate.yml:176-210`) accepts `target_ref` only when exactly one open PR has that head and `base=<default branch>`. Final PR #5134 targets `claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote`, so the run would fail before validating. Validating the default branch or the base branch in its place is not allowed, so this was a stop (CLAUDE.md §28.C), asked on #5114 (comment 5893338617).
- Validation: skipped (covered by parent project #4835 (non-default base)). Q1: A answered on #5114 (comment 5894219626, 2026-09-29) under the master session's standing decision Q17 (confirmed as Q18); the #4835 project runs its security audit and runtime validation on a branch carrying this change before anything reaches `main`. Long-term fix: #4734.

## Completion
- Completion PR (branch claude/implement-plan-issue-5114-verify-clean-votes-from-runner-output-complete) opened 2026-09-29 — doc moved to docs/completed/issue-5114-verify-clean-votes-from-runner-output-plan.md
- Final PR #5134 draft (into the #4835 project branch)

## Activation
- n/a (base claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote)

## Auto-decisions
- AD-1 [plan, 2026-09-29] Should the runner-output check also apply to ledgers with no failed slot? — Picked: A — no, only the failed-slot path. Alternatives: B — every ledger. Why: the issue and the audited code are the failed-slot path; the every-block-clean rule is main's pre-existing behaviour shared with the GPT path, and changing it alters every Claude-fixer PR's merge contract (§5, §12.D). Flagged for human review. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What counts as an unambiguous no-findings result in review_<slug>.txt? — Picked: A — at least one NONE, no finding or task-gap field label (markdown-tolerant), and when checklist headings appear, all nine present, each followed by NONE. Alternatives: B — only headings and NONE lines; C — the runner's reviewer_output_has_findings / _has_explicit_none pair. Why: on real outputs A accepts 5 of 6 reviewers on a clean run and rejects every output with a finding; B accepts 1 of 6 and brings back the #4835 stall; C accepts a lens left without a verdict and misses markdown-decorated labels. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Where do the lens headings come from? — Picked: A — a fixed list in the script, pinned by a test to prompts/review-reviewer-checklist.txt. Alternatives: B — parse the prompt file at run time. Why: no run-time dependency on the prompts dir, and CI catches drift; B would fail open if the prompt file were missing. Applied in: phase 1. Status: pending review
- AD-4 [conformance 1/3, 2026-09-29] Should the lens rule (rule 4) recognise lens headings written as a numbered or bulleted markdown list (`1. CORRECTNESS & LOGIC`, `- **CORRECTNESS & LOGIC**`), which the plan's heading normalisation (leading `#`/`*`, trailing `*`/`:`) does not? — Picked: A — yes, normalise headings with the same markdown markers and list numbering as the field scan. Alternatives: B — keep the plan's normalisation literally. Why: B lets a numbered checklist with a lens answered by prose count as a clean vote, which the plan's goal (ambiguous output is not clean) forbids; A only narrows acceptance, and numbered headings each followed by NONE still count. Applied in: conformance-fix-1 PR. Status: pending review
- AD-5 [conformance 1/3, 2026-09-29] The rejection warning reads `… review_<slug>.txt is not an unambiguous no-findings result (<reason>); the ledger is not clean.` with reasons `reports a finding or task gap`, `has no NONE verdict`, `leaves a checklist lens without a NONE verdict`, where the plan's Approach sketched `… is <state>, not an unambiguous no-findings result; …`. Align it? — Picked: A — leave as is. Alternatives: B — reword to the Approach sketch. Why: the goal (a warning naming the slug and the reason) is met, the text is not a log key or contract, and tests pin the shipped wording (§5). Applied in: no code change. Status: pending review
- AD-6 [completion, 2026-09-29] `/implement-issue-claude` step 4 stops with `already in progress` when the log reads `Status: IN_PROGRESS` and its checker is live. Here the committed log still read `IN_PROGRESS` (the validation 1/3 stage stopped at BLOCKED with no PR in flight and did not commit the log), the checker `session_014e1U6XSZ2C1s4ufzyV1P4Q` was idle with no pending check-in, the issue carried `ai:claude-blocked`, and Q1 had been answered with `/reclarify`. Resume or stop? — Picked: A — resume from the recorded BLOCKED stage. Alternatives: B — stop as `already in progress`. Why: the blocked comment and the human answer are newer than the log, no stage session or check-in was running, and B would leave the answered project stalled. Applied in: no code change. Status: pending review

## Lessons
- [source:conformance] A tolerant line parser over model output must normalise every structural line the same way: when field labels accept markdown markers and list numbering, section headings must too, or a numbered layout silently skips the per-section rule. (files: scripts/review_autofix_step_claude_fixer_handoff.sh)
- [source:validation] A stage that stops at `Status: BLOCKED` with no PR in flight must still persist the BLOCKED state to the project branch log, or the `/reclarify` resume reads a stale `IN_PROGRESS` with a live checker and `/implement-issue-claude` step 4 refuses to resume. (files: .claude/commands/implement-issue-claude.md, .claude/commands/implement-plan-claude.md)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher routine; session session_01DMNTkZSE9rs2PJLh4orpGN in Auto mode.
- Issue progress comment id 5889573899.
- security_pass_skip.py: {"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}.
- The session had no GitHub MCP tools and no `gh` at start (repo attached mid-session, so the SessionStart hook had not run); ran `.claude/hooks/session-start.sh` to install `gh`, and used `gh api` REST plus the Helpers for GitHub writes.
- Issue base PR #4847 (the #4835 project's final PR) is open and unmerged at start.
- Resumed 2026-09-29 by session session_01GdoroFuKGC7jSuhHFwmqyB (dispatched on `/reclarify`); removed `ai:claude-blocked` from #5114. Issue base PR #4847 still open and unmerged, so the base did not move.
