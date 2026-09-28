# Implement-Plan Log — Close and label issues only on merges into their target branch

- Plan: docs/plans/issue-4813-close-sweep-target-branch-merges-plan.md
- Source issue: shubhodeep1/coding-workflows#4813
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-28
- Last note: project branch opened from main; implementing phase 1.

## Phases
1. [ ] Phase 1 — gate issue close and `ai:merged` on the issue's target branch (sweep + issue_pr_status.yml + tests + changelog)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Which merges count as an issue's finished work for the sweep and for `ai:merged`? — Picked: A — the default branch, the branch the issue's `Integration branch:` / `Target branch:` line names, or any base for an orchestrator-managed child. Alternatives: B — the default branch only, as the issue words it; C — change only the label source and leave the sweep's rule. Why: B strands orchestrator child issues and Codex-built security follow-ups whose PRs target the named project branch; A still leaves #4688 open on #4748. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Where does the default branch come from? — Picked: A — `.base.repo.default_branch` in the `pulls/<n>` JSON the sweep already fetches (empty fails closed), `github.event.pull_request.base.repo.default_branch` in the workflow (empty falls back to `main`). Alternatives: B — one extra `repos/<r>` read per sweep. Why: §15. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Should `issue_pr_status.yml`'s close gate keep the literal `main`? — Picked: A — compare against the resolved default branch. Alternatives: B — keep `main` for the close gate only. Why: item 2 asks for default-branch semantics. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Where does the integration-branch parser live for shell callers? — Picked: A — `issue_body_integration_branch` in `scripts/gh_helpers.sh` with a parity test against `orchestrate_lib.extract_integration_branch`. Alternatives: B — inline `python3` in each caller. Why: one copy, kept in step by a test. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] Does item 2's "the chain applies `ai:merged` itself after its final merge" need a change? — Picked: A — no; Issue Mode already specifies it, so no `.claude/**` edit. Alternatives: B — restate it in the command file. Why: behaviour exists. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-28] What happens to an issue whose only implementation PR merged into a non-target branch? — Picked: A — log `rejected=non_target_base` and fall through to the existing no-merged-PR policy of its label class. Alternatives: B — a new silent skip. Why: the issue asks for the existing policy. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4813#issuecomment-5870080698
- Security pass: run (`security_pass_skip.py`: no skip label).
