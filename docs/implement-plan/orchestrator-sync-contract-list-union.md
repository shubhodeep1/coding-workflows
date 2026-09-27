# Implement-Plan Log — Orchestrator sync: deterministic contract-list union pre-resolver

- Plan: docs/plans/orchestrator-sync-contract-list-union-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-orchestrator-sync-contract-list-union   Final PR: pending (opened after this commit)
- Status: IN_PROGRESS
- Stage: security follow-up #4568 (orchestrator security-pass fix cycle 1) — then conformance 1/3
- Activation: not started
- Waiting on: security follow-up PR for #4568 (opened next)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: pending (armed after the security follow-up PR opens)
- Last updated: 2026-09-27
- Last note: Took over orchestrator project #3965 in Claude; forked the project branch from orchestrator/project-3965 and synced main.

## Phases
1. [x] Phase 1 — helper, poller wiring, flag, tests, docs, changelog — shipped by the AI orchestrator on orchestrator/project-3965: PR #3970 (issue #3966) merged 2026-09-03, fallback-test repair PR #4164 (issue #4162) merged 2026-09-20; review rounds: orchestrator-managed; interventions: n/a

## Conformance

## Security pass
- Orchestrator pass (before the takeover): status `blocked`, 0 completed fix cycles, audited head 3f48e382, 13 waived findings, active fix issue #4568 (Codex PR #4572, editor failing on a provider error). Taken over per Q13:A.

## Validation
- Orchestrator run 36288327878 (2026-09-27, tracking_issue=3965, head 98f1388f): status=fail raw_status=harness_error — `pgrep` missing from the python-repo-checks image; fixed on orchestrator/project-3965 before the fork (e.g. 8f930255, b3d057a0, e5bfae03). Not a chain cycle.

## Completion
- Final PR #3968 (orchestrator/project-3965 → main) is superseded by this project's final PR and closed unmerged (Q12:A).

## Activation

## Auto-decisions

## Lessons
- [source:plan-deviation] When main's reusable workflow runs a branch's scripts (validate.yml@main validating an integration branch), every env var and runtime path the branch's scripts require must default when main's workflow does not provide it; otherwise validation fails before it runs. (files: scripts/validate_process.sh, .github/workflows/validate.yml)
- [source:validation] A container image that runs process-supervision tests needs `procps` (pgrep) and tests that pin PATH must not resolve python3 through `/usr/bin/env` on python:*-slim images, where python3 lives in /usr/local/bin. (files: workflow-templates/validation-harness/python-repo-checks/Dockerfile.app.j2, tests/test_review_editor_process_group_termination.py)

## Notes
- **Takeover (2026-09-27, operator Q/A in session_01Hmwfn7PyetyRxdWjehXvcf).** Orchestrator project #3965 moved from the AI orchestrator to this `/implement-plan-claude` chain:
  - Q11:A — `ai:orchestrator-tracking` removed from #3965 and `ai:claude` added, so `orchestrate_poll.yml` no longer polls it (it lists open `ai:orchestrator-tracking` issues only). The orchestrator's state comments stay on #3965; re-adding the label hands the project back. §19 still applies by intent: every PR uses `Refs #3965`, never an auto-close keyword.
  - Q12:A — this project branch was forked from `orchestrator/project-3965` at e5bfae03; the new draft final PR replaces #3968 so the final whole-project review runs in Claude-fixer mode.
  - Q13:A — security follow-up #4568 (`ai:claude` added) is finished by a Claude-written fix PR that starts from Codex PR #4572's head (ai/issue-4568, 110eb165), against this project branch; #4572 is closed unmerged. This is an operator-approved exception to the rule that security findings are fixed only through `ai:security` follow-up issues.
  - Q14:A — the orchestrator's 13 waived security findings carry over: `docs/implement-plan/orchestrator-sync-contract-list-union-security-waivers.json`. `security-audit.yml` cannot take a waiver list (the engine accepts `SECURITY_AUDIT_WAIVED_FINDINGS` only in findings-json mode), so the security-pass stage applies it when reading the audit result: a new `ai:security` follow-up that matches a waiver under the file's `match_rule` is closed as not planned with a comment citing the waiver id (Q14:A is the approval for these closes), and only unmatched follow-ups count as findings for the cycle.
  - Q15:A — the chain runs exactly as `/implement-plan-claude` describes from here: conformance → security pass → validation (`tracking_issue=0`, `target_ref` = this project branch) → completion → final merge → verify-activation, each stage in a fresh Opus 5.5 high-effort session started by one Sonnet checker. Every stage syncs main first; where main and this branch hardened the same surface, main's design wins (Q9:A).
- **The plan doc is only on this project branch** (it was committed on orchestrator/project-3965 and has never been on main). Stage sessions start on main, so read the plan and this log with `git show origin/claude/implement-plan-orchestrator-sync-contract-list-union:<path>` until step 2 checks out the project branch.
- **Sync merge 9a63f608** (main → project branch, 13 commits): main's isolated renderer runtime (#4516) replaced this branch's renderer dependency bootstrap; the renderer script and schema stay absolute support-bundle paths. See the merge commit message for the per-file resolution.
