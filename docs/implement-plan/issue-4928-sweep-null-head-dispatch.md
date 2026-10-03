# Implement-Plan Log — Keep PR-named sweep dispatch runs with a null head branch in the active-run snapshot

- Plan: docs/completed/issue-4928-sweep-null-head-dispatch-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4928
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4701-review-dispatch-default-branch
- Project branch: claude/implement-plan-issue-4928-sweep-null-head-dispatch   Final PR: #4961 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4701-review-dispatch-default-branch)
- Waiting on: completion PR (the number is in the validation 1/3 resume stage report and on the #4928 progress comment)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_016XLmFbbEyqMyNBSRgbxB3k (reused)   safety net and hand-back in the validation 1/3 resume stage report
- Last updated: 2026-09-29
- Last note: validation skipped on operator answer Q1: A (covered by #4701's project validation); plan moved to docs/completed/ in the completion PR; next: final-merge 1/1.

## Phases
1. [x] Phase 1 — keep PR-keyed dispatch runs with a null head branch in the sweep snapshot   — PR #4998 merged 2026-09-29 (ea2139e); review rounds: 0; interventions: 0

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — no fixes (pre-validation; security skipped)

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header (`security_pass_skip.py` verified).

## Validation
- Skipped (covered by #4701's project validation) — operator answer Q1: A on #4928, 2026-09-29. `validate.yml` authorizes `target_ref` only for an open final PR into `main`, and this project's final PR #4961 targets #4701's project branch (long-term fix: #4734). #4701's chain re-runs its security audit and runtime validation on a project branch that will contain this fix before anything reaches `main`.

## Completion
- Completion PR (claude/implement-plan-issue-4928-sweep-null-head-dispatch-complete) open 2026-09-29 — doc moved to docs/completed/issue-4928-sweep-null-head-dispatch-plan.md
- Final PR #4961 draft (into claude/implement-plan-issue-4701-review-dispatch-default-branch)

## Activation
- n/a: the base is #4701's project branch, so the change goes live with that project's final PR #4709. The final-merge stage closes #4928 and labels it `ai:merged` once #4961 merges.

## Auto-decisions
- AD-1 [plan, 2026-09-29] The finding names only the sweep; should the other PR-named run lookups change too? — Picked: A — sweep only. Alternatives: B — also rewrite the poller and merge-train lookups. Why: they already match a PR-named run whatever its head branch, so B changes nothing and breaks §5. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-29] How should a run with no head branch and no PR key be handled? — Picked: A — keep dropping it, as the finding recommends. Alternatives: B — key it by `head_sha`; C — count it under a shared placeholder key. Why: B needs data the snapshot does not have and adds keys no PR check reads, and C would suppress unrelated PRs. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which changelog section does the fragment use? — Picked: A — `security`. Alternatives: B — `fixed`. Why: the change closes a security-audit finding, and §20.B lists `security` for exactly that. Applied in: phase 1 PR. Status: pending review
- AD-4 [validation 1/3 — resume, 2026-09-29] The log on the project branch still read `Status: IN_PROGRESS` / `Stage: phase 1/1` (the conformance stage opened no PR, so its log update was never committed), while the #4928 blocked comment, the idle project checker, and the operator's `/reclarify` showed the project stopped at validation 1/3. Resume or report "already in progress"? — Picked: A — resume at validation 1/3 with the operator's answer. Alternatives: B — report "already in progress" and stop. Why: the checker had no pending check-in and the stage session was waiting on the answer, so B would stall the project. Applied in: no code change. Status: pending review

## Lessons
- [source:plan-deviation] An issue-mode project stacked on another project's branch cannot run runtime validation until `validate.yml` authorizes non-default targets (#4734); ask once and record the parent project's validation as the cover instead of dispatching a run that is bound to fail. (files: .github/workflows/validate.yml, .claude/commands/implement-plan-claude.md)

## Notes
- Issue-mode start session session_01VcPAwfSyoF5qnMhP7gSfdd (started by the Claude issue dispatcher routine; the repo was attached with add_repo, so the session-start hook ran by hand to install `gh`).
- The `<!-- ai:claude-issue-progress:v1 -->` comment on #4928 was **not** posted by the start session: it had no `mcp__github__add_issue_comment` tool, and the Auto-mode classifier denied posting it with `curl`. The conformance 1/3 stage session (session_01KcUQnmWV3eTgAvj1HxN2cS) posted it on 2026-09-29.
- 2026-09-29 (conformance 1/3, session_01KcUQnmWV3eTgAvj1HxN2cS): CONFORMANT with no fix PR; security skipped per the plan header; validation blocked before dispatch (`validate.yml`'s "Authorize explicit validation target" step accepts `target_ref` only for a final PR into `main`; same failure as #4665's run 36378523375). Asked Q1 on #4928 (`ai:claude-blocked`).
- 2026-09-29: operator answer Q1: A (skip validation here; covered by #4701's project validation; same answer as #4687 and #4688), then `/reclarify`.
- 2026-09-29 (validation 1/3 — resume, session_01XsaoiiVp4Rve2wvXv6FUHx): the base branch has not merged (#4709 open, draft), so no base move. The project branch was already up to date with it (head 998db08). Re-ran the checks on that head: `pytest tests/test_review_autofix_sweep_stale_queued.py tests/test_claude_pr_sweep.py tests/test_review_autofix_sweep_zero_candidate_fast_exit.py tests/test_changelog_fragment_contract.py tests/test_assemble_changelog.py tests/test_review_commit_changelog_fragment_preserved.py tests/test_lint_plan_archival_completeness.py` 108 passed; `tests/test_workflow_file_size_limit.py` passed. This session had no GitHub MCP tools either; it installed `gh` with the session-start hook and used `gh api` (proxy-authenticated) for the label, PR, and comment writes.
