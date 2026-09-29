# Implement-Plan Log — Keep PR-named sweep dispatch runs with a null head branch in the active-run snapshot

- Plan: docs/plans/issue-4928-sweep-null-head-dispatch-plan.md
- Source issue: shubhodeep1/coding-workflows#4928
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4701-review-dispatch-default-branch
- Project branch: claude/implement-plan-issue-4928-sweep-null-head-dispatch   Final PR: #4961 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented and verified locally (380 sweep-related tests, 52 changelog tests, yamllint, actionlint; the full suite did not finish within 50 minutes locally, so CI is the full-suite check); phase PR opened against the project branch.

## Phases
1. [ ] Phase 1 — keep PR-keyed dispatch runs with a null head branch in the sweep snapshot

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header (`security_pass_skip.py` verified).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] The finding names only the sweep; should the other PR-named run lookups change too? — Picked: A — sweep only. Alternatives: B — also rewrite the poller and merge-train lookups. Why: they already match a PR-named run whatever its head branch, so B changes nothing and breaks §5. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-29] How should a run with no head branch and no PR key be handled? — Picked: A — keep dropping it, as the finding recommends. Alternatives: B — key it by `head_sha`; C — count it under a shared placeholder key. Why: B needs data the snapshot does not have and adds keys no PR check reads, and C would suppress unrelated PRs. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which changelog section does the fragment use? — Picked: A — `security`. Alternatives: B — `fixed`. Why: the change closes a security-audit finding, and §20.B lists `security` for exactly that. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue-mode start session session_01VcPAwfSyoF5qnMhP7gSfdd (started by the Claude issue dispatcher routine; the repo was attached with add_repo, so the session-start hook ran by hand to install `gh`).
- The `<!-- ai:claude-issue-progress:v1 -->` comment on #4928 was **not** posted: this session had no `mcp__github__add_issue_comment` tool, and the Auto-mode classifier denied posting it with `curl`. The next stage session that has the GitHub MCP tools creates it.
