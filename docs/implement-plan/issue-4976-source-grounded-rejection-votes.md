# Implement-Plan Log — Rejection votes must carry source-grounded evidence before they demote a finding

- Plan: docs/completed/issue-4976-source-grounded-rejection-votes-plan.md (moved from docs/plans/ by the completion PR)
- Source issue: shubhodeep1/coding-workflows#4976
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason
- Project branch: claude/implement-plan-issue-4976-source-grounded-rejection-votes   Final PR: #5027 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason)
- Waiting on: the completion PR (into the project branch), then final PR #5027
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01NwDrHgAEhyW5sjmKdPXo2X (reused for every wait)   safety net and hand-back: the ids are in the completion stage's report
- Last updated: 2026-09-29
- Last note: conformance run 1/3 CONFORMANT with no fix PR; validation skipped per `Q1: A` (non-default base); completion PR opened

## Phases
1. [x] Phase 1 — source-grounded rejection votes   — PR #5034 merged 2026-09-29; review rounds: 1; interventions: 0

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — no fixes (pre-security). G1–G5 trace to `scripts/review_claude_fixer_nonblocking.py` (`VOTE_EVIDENCE_RE` l.191, `git_commit_available` / `git_source` l.537–573, `_verify_evidence` l.590–619, `_votes_in` l.636–666, CLI log lines l.918–922), `scripts/review_autofix_step_claude_fixer_handoff.sh` l.144–145 (its `HEAD_SHA` is `INITIAL_HEAD_SHA`, `review_autofix.yml` l.4550), and `build_cross_pollination_summary` in `scripts/review_run_reviewers.sh`. Checks: the ci.yml Claude-fixer pytest step (222 passed), `tests/test_changelog_fragment_contract.py` and `tests/test_workflow_file_size_limit.py` (23 passed), `tests/inventory_parity.py`, `bash -n` on both edited scripts, pyflakes clean; shellcheck could not run (not installed).

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified it).

## Validation
- Skipped (covered by #4586's project validation) — `Q1: A` (owner, 2026-09-29, on #4976, under the standing decision Q17: A): `validate.yml` binds `target_ref` only to an open PR into the default branch, and final PR #5027 targets `claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason`. Project #4586 validates a branch that contains this fix before anything reaches `main`. Long-term fix: #4734.

## Completion
- Completion PR (this commit) — doc moved to docs/completed/issue-4976-source-grounded-rejection-votes-plan.md
- Merged PRs into the project branch: #5034 (phase 1/1, merged 2026-09-29)

## Activation
- Not applicable: the base branch is `claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason`, not the default branch.

## Auto-decisions
- AD-1 [plan, 2026-09-29] What must a rejection vote carry to count? — Picked: A — an `evidence: <file>:<range>` citation plus a verbatim `quote:` that the gate checks against the reviewed commit. Alternatives: B — never let a demotion authorize auto-merge (hand the round to the fixer instead); C — drop demotion entirely. Why: A is the issue's recommendation and keeps #4586's purpose; B and C revert it (§5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Where may the evidence point? — Picked: A — the voted entry's own file, within 10 lines of its range, at most 20 lines long. Alternatives: B — any file in the repository; C — only the flagged lines themselves. Why: binds the evidence to the finding (§1) while still allowing a nearby guard to be cited. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How is the quote matched? — Picked: A — at least 10 non-whitespace characters, compared with whitespace runs collapsed, also accepting one pair of wrapping backticks removed. Alternatives: B — an exact byte match; C — any length. Why: tolerates reviewer formatting without letting a trivial quote such as `}` through. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Where does the gate read the source? — Picked: A — `git cat-file blob <HEAD_SHA>:<path>` in `GITHUB_WORKSPACE`, the reviewed commit. Alternatives: B — the working tree files; C — the GitHub contents API. Why: binds the check to the reviewed commit, with no API calls (§15). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Does the summariser prompt change? — Picked: A — no. Alternatives: B — document the new fields there too. Why: it already drops every `REJECTED_FINDING … | reason: …` line, and the new fields follow `reason:` (§5). Applied in: no code change. Status: pending review
- AD-6 [phase 1/1 — review round 1, 2026-09-29] Four reviewers flagged that `_safe_source_path` accepts `:`, claiming `git cat-file blob <sha>:<path>` could read a different blob; that claim does not reproduce (git 2.43 reads everything after the SHA's colon as a literal path). Fix or reject? — Picked: A — refuse `:` in evidence source paths as defense-in-depth, and say in the reply that the wrong-blob premise does not reproduce. Alternatives: B — reject the finding through the dedicated-bot verdict and convergence dispatch. Why: A only makes the gate stricter (no tracked path contains `:`), and it takes the read out of git's revision-syntax parsing (§1). B needs a verdict bot this session cannot verify, so the round would stop at BLOCKED. Applied in: PR #5034. Status: pending review

## Lessons
- [source:intervention] `git cat-file blob <sha>:<path>` reads everything after the SHA's colon as a literal path, so a `:` in the path cannot redirect the read to another blob. Reproduce a reviewer's git revision-syntax claim in a scratch repo before you act on it. Gates still refuse `:` so their reads never depend on revision syntax. (files: scripts/review_claude_fixer_nonblocking.py)
- [source:plan-deviation] Before a plan names a new parameter, check the function body for a local of that name: the plan's `source` keyword would have collided with the `source = pass1_by_id.get(cid)` local in `demote_with_diagnostics`, so the implementation used `source_reader` (CLAUDE.md §6 new-identifier uniqueness). (files: scripts/review_claude_fixer_nonblocking.py)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher in session session_01KViqS5xRtntZ347wDryDoM (permission mode auto). Base branch `claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason` (the issue's `Integration branch:` line), so the final PR targets it and the final-merge stage closes #4976 explicitly with `ai:merged`; activation (steps 12–13) is n/a for this base.
- The session had no `gh` and no GitHub MCP tools at start (the repo was attached after SessionStart); `.claude/hooks/session-start.sh` was run by hand to install `gh`. GitHub writes in this stage go through `gh api` REST.
- Validation 1/3 stopped at `Status: BLOCKED` (2026-09-29, session_01VUoAbxApXVfiQhDnWFjjmD) and asked Q1 on #4976 (a failure escalation, CLAUDE.md §28.C). The owner answered `Q1: A` the same day; the same session resumed on that answer, removed `ai:claude-blocked`, and opened the completion PR.
