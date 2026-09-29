# Implement-Plan Log — Require owner authorization before protected `.claude` changes merge or ship

- Plan: docs/plans/issue-4919-gate-protected-path-merges-plan.md
- Source issue: shubhodeep1/coding-workflows#4919
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4678-edit-files-without-python-heredocs
- Project branch: claude/implement-plan-issue-4919-gate-protected-path-merges   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened from the issue base; phase 1 in progress.

## Phases
1. [ ] Phase 1 — protected-path merge and release authorization gate (scripts/protected_path_authorization.py, scripts/protected_path_gate.sh, merge-site wiring in review_enable_auto_merge.sh / review_rb_judge.sh / orchestrate_poll_process.sh, stage_workflow_support.sh, release gate in test-and-mark-stable.yml / mark-stable.yml, tests, ci.yml, CLAUDE.md §23.I, agents.md, changelog)

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header.

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] What trusted provenance authorizes a protected-equivalent change? — Picked: A — an unedited PR comment whose whole body is `/authorize-protected-paths <head SHA>`, by a `User` with OWNER/MEMBER/COLLABORATOR association and no `performed_via_github_app`. Alternatives: B — an approving review from a non-author collaborator; C — an `ai:protected-path-approved` label; D — the progress log's `Protected-path approval:` line. Why: head-bound, per-PR, and a Claude session cannot produce it; B is impossible with one account, C is not head-bound, D is ruled out by the finding. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Where is the gate enforced at merge? — Picked: A — a shared `protected_path_guarded_merge` wrapper around every `gh pr merge` in `scripts/*.sh`, with a static test. Alternatives: B — a required status check plus branch protection; C — only `review_enable_auto_merge.sh`. Why: A covers review, judge, and orchestrator paths without an admin operation. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Wrap `review_autofix.yml`'s deterministic-skip merge too? — Picked: A — no; `PROTECTED_SKIP_SUPPRESSED` already refuses the skip for `.claude/*`, `workflow-templates/*`, `scripts/*`. Alternatives: B — yes. Why: no added coverage for a near-limit workflow edit (§27). Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-29] Where is the gate enforced at release? — Picked: A — a `validate`-job step in `test-and-mark-stable.yml` and `mark-stable.yml`. Alternatives: B — also before the stable branch fast-forward; C — no release gate. Why: consumers read the tag the `release` job moves. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] How does the release gate treat pre-gate changes? — Picked: A — grandfather PRs merged at or before the gate script first landed on the candidate's first-parent history. Alternatives: B — no grandfathering. Why: B would block the first release on dozens of already-reviewed PRs. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] Which paths are protected-equivalent for the gate? — Picked: A — `.claude/**`, `workflow-templates/.claude/**`, `tests/test_claude_template_parity.py`, and the gate's own two scripts. Alternatives: B — plus CLAUDE.md and workflows; C — only the two trees. Why: A covers the finding and protects the gate itself; B widens past §5. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] How is a blocked merge surfaced? — Picked: A — one PR comment per head with marker `<!-- ai:protected-path-authorization:v1 head=<sha> -->`, no label. Alternatives: B — `ai:needs-human`; C — a new label. Why: a block label wakes Claude fixers that cannot authorize. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] Gate consumer repos too? — Picked: A — yes, only for PRs touching the protected set. Alternatives: B — coding-workflows only. Why: a consumer's `.claude/**` also steers its unattended sessions. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-29] What happens on a read error? — Picked: A — retry three times, then refuse the merge for this attempt. Alternatives: B — fail open. Why: §1. Applied in: phase 1 PR. Status: pending review
- AD-10 [plan, 2026-09-29] How is an authorized PR bound to its head on an unbound merge call? — Picked: A — the wrapper adds `--match-head-commit <authorized head>`. Alternatives: B — leave it. Why: otherwise a push between check and merge merges an unapproved head. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode: permission mode `auto` (recorded, not asked).
- Security pass skipped per `security_pass_skip.py`: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Base branch final PR #4684 is open (draft); the base has not moved (checked 2026-09-29).
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4919#issuecomment-5882706456
- This session had no `mcp__github__*` tools; `gh` was installed by the repo's SessionStart hook, and GitHub writes go through `gh api` routine calls and the allowlisted helpers.
