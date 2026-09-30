# Implement-Plan Log — Claude-fixer review: a failed-slot ledger must cover every reviewer the runner ran

- Plan: docs/plans/issue-5297-ledger-must-cover-reviewer-roster-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5297-ledger-must-cover-reviewer-roster   Final PR: #5303 draft
- Source issue: shubhodeep1/coding-workflows#5297   Base branch: claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote   Security pass: skip (ai:security: automation-produced issue)
- Status: BLOCKED
- Stage: validation 1/3
- Activation: not started
- Waiting on: Q1 answer on #5297 (comment 5902716254), then `/reclarify`
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01U5bovtxjovuW9C4AdVFSvq (idle, kept for the resumed project; no pending check-in)   safety net none   hand-back none
- Last updated: 2026-09-30
- Last note: conformance 1/3 CONFORMANT (correctness PASS, no fixes); security skipped (AD-4); validation not dispatched because `validate.yml` authorizes a `target_ref` only when its open PR targets `main`, and final PR #5303 targets the #4835 project branch. Skipping validation is never auto-decided (CLAUDE.md §28.C), so Q1 was asked on #5297 (comment 5902716254) with the `ai:claude-blocked` label. A `/reclarify` resume continues from this BLOCKED state.

## Phases
1. [x] Phase 1 — Ledger must cover the runner's reviewer roster (failed-slot path of the Claude-fixer clean-ledger check, tests, docs, changelog)   — PR #5306 merged 2026-09-30 (df2266c); review rounds: 1; interventions: 0
   - scripts/review_autofix_step_claude_fixer_handoff.sh: roster from status_review_<slug>.txt and review_<slug>.txt; every roster slot needs a ledger block; empty roster or bad slug fails closed
   - tests/test_review_autofix_claude_fixer_mode.py: exploit (6 reviewers, min 4, 1 failed, 1 omitted finding) hands off; omitted clean slot, output-only slot, bad slug hand off; existing tests pass
   - README.md, agents.md, changelog.d/5297-ledger-must-cover-reviewer-roster.md
   - Done: new and existing Claude-fixer tests pass, workflow size test and review_autofix contract tests pass, shellcheck clean

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (correctness PASS) — no fixes (pre-security). Audited the project branch at df2266c: every plan goal maps to scripts/review_autofix_step_claude_fixer_handoff.sh:361-397; no sibling implementation of the check exists; the roster globs match only reviewer slot files in both single- and two-pass mode (pass 2 uses the `review` prefix; consensus, manifest and active-model files live in RUNTIME_DIR); skipped slots write review_<slug>.txt, so the summariser gives them a block. Checks: `pytest tests/test_review_autofix_claude_fixer_mode.py tests/test_workflow_file_size_limit.py tests/test_review_autofix_step_scripts_contract.py tests/test_review_autofix_review_pipeline_contract.py` (235 passed, mawk/gawk roster test not skipped), `pytest tests/test_changelog_fragment_contract.py tests/test_assemble_changelog.py` (47 passed), shellcheck, bash -n.

## Security pass
- Skipped: ai:security: automation-produced issue (security_pass_skip.py, AD-4)

## Validation
- Cycle 1 — 2026-09-30: not dispatched. `validate.yml` ("Authorize explicit validation target", `.github/workflows/validate.yml:176-210` on main) accepts `target_ref` only when exactly one open PR has that head and `base=<default branch>`. Final PR #5303 targets `claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote`, so the run would fail before validating. Validating the default branch or the base branch in its place is not allowed, so this is a stop (CLAUDE.md §28.C), asked on #5297 (comment 5902716254). Same blocker and question as #4885 and #5114; long-term fix #4734.

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Should the roster check also apply to ledgers with no failed slot? — Picked: A — no, only the failed-slot path. Alternatives: B — every ledger. Why: the issue and the audited line are the failed-slot quorum; the every-block-clean rule is main's pre-existing behaviour, kept by #4835 AD-1 and #5114 AD-1 (§5, §12.D). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What is the runner-generated roster? — Picked: A — every slug with a status_review_<slug>.txt or review_<slug>.txt in PREVIOUS_REVIEWS_DIR. Alternatives: B — a new roster file written by the runner; C — the status files only. Why: A covers the summariser's inputs and every slot the runner recorded with no runner change. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Add the roster size to the CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS log line? — Picked: A — no; the omission gets its own ::warning:: line. Alternatives: B — append roster=<n>. Why: §5 and §6. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Security pass for this project? — Picked: A — skip, as security_pass_skip.py verified. Alternatives: B — run. Why: the issue is itself an audit follow-up; the parent project's security pass re-audits the branch. Applied in: no code change. Status: pending review

## Lessons
- [source:validation] An issue-mode project whose base is another project branch cannot run `validate.yml` with `target_ref`, because the target must have an open PR into the default branch; plan the validation answer (skip, covered by the parent project's validation) up front instead of discovering it at the validation stage. (files: .github/workflows/validate.yml, .claude/commands/implement-plan-claude.md)
- [source:intervention] Every new awk program in a review_autofix step script needs its own mawk and gawk test run; a parametrised test that covers another awk program in the same script does not cover it. (files: scripts/review_autofix_step_claude_fixer_handoff.sh, tests/test_review_autofix_claude_fixer_mode.py)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher; session session_01PNBt8chdKoRBRJDwPFFsHj in Auto mode.
- Issue progress comment id 5901135136.
- security_pass_skip.py: {"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}.
- Issue base check (2026-09-29): PR #4847 (head = issue base) is open and draft, so the base has not moved.
