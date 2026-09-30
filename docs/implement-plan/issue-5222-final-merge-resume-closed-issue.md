# Implement-Plan Log — Resume an issue-mode project with /reclarify after the final merge closed its issue

- Plan: docs/plans/issue-5222-final-merge-resume-closed-issue-plan.md
- Source issue: shubhodeep1/coding-workflows#5222 (https://github.com/shubhodeep1/coding-workflows/issues/5222)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5222-final-merge-resume-closed-issue   Final PR: #5225 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5271
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01KfkvbMJgMjDKZBQ8DTiARb   safety net and hand-back: see the review-round-1 stage report
- Last updated: 2026-09-30
- Last note: review round 3 on head a7f0e32: clarify.yml gh_retry_to_file fallback now validates GH_RETRY_MAX_ATTEMPTS (non-numeric or 0 → 3), caps it at 5 (at most 20s of backoff), and stops on the real helper's permanent-failure patterns; waiting on review round 4

## Phases
1. [ ] Phase 1 — final-merge resume on a closed issue   — PR #5271 open (waiting); review rounds: 3; interventions: 0; protected paths: .claude/commands/implement-issue-claude.md, .claude/commands/implement-plan-claude.md (twin-first)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which fix direction? — Picked: A — intake route `final_merge_resume` in clarify, intake, and `/implement-issue-claude`. Alternatives: B — swap the final PR's `Fixes` for `Refs` at a final-merge block; C — the project checker resumes on the merge. Why: the issue lists A first and asks for closed-issue route tests; it keeps the `Fixes` contract and also covers an issue that already closed. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] New trigger value or new route reason? — Picked: A — keep `trigger: reclarify` and add route reason `final_merge_resume`. Alternatives: B — a new `final_merge_resume` trigger in `VALID_TRIGGERS` and the payload. Why: §5, no payload or fire-text schema change, and the intake re-derives eligibility from live data anyway. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Who may author the blocked comment that unlocks the route? — Picked: A — a trusted `User` only (`OWNER` / `MEMBER` / `COLLABORATOR`). Alternatives: B — also `github-actions[bot]`. Why: §1; the sessions post blocked comments through MCP as a user, never as the Actions bot. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] How is the blocked stage read? — Picked: A — the first `Stage:` / `**Stage:**` line of the latest trusted blocked comment, whose value starts with `final-merge`. Alternatives: B — a new marker attribute `<!-- ai:claude-blocked:v1 stage=… -->`. Why: A matches the comments already on blocked issues (#5119), and B would strand them. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Where is `ai:claude-blocked` removed? — Picked: A — the resumed `/implement-issue-claude` session's step 2 Claim (existing behaviour, now reached for the closed issue). Alternatives: B — the clarify handoff script. Why: the label stays the visible "blocked" signal until a session really resumes, and the label gate keeps a repeated `/reclarify` from re-routing after that. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] Should the handoff's routing comment differ for the resume? — Picked: A — yes, a resume-specific body under the same `ai:claude-issue-routed:v1` marker. Alternatives: B — reuse the "a Claude session will implement this issue" text. Why: the generic text says the issue closes when the completion PR merges, which is wrong for a closed issue. Applied in: phase 1. Status: pending review

## Lessons
- [source:plan-deviation] A full local `pytest tests` run does not finish within 40 minutes on a cloud session runner; verify a phase by running each test file that references the changed paths separately with a per-file timeout, and leave the full suite to CI. (files: tests/test_orchestrate_poll_process.py)
- [source:intervention] A new stable `AI_PHASE_GATE_V1` telemetry line must also be pinned in tests/test_phase_skip_gate_telemetry_contract.py, and a workflow step's inline fallback for a gh_helpers.sh function should keep bounded retries and warn when it is used. (files: .github/workflows/clarify.yml, tests/test_phase_skip_gate_telemetry_contract.py)
- [source:intervention] Once a `[claude-twin-sync]` copy lands on a twin-first phase PR, update the PR body's twin-first section in the same stage; a body that still says the sync is pending makes reviewers flag the synced `.claude/` diff as a task gap. (files: .claude/commands/implement-plan-claude.md)
- [source:intervention] An inline fallback for a shared shell helper must keep the helper's termination guarantees: validate any env-var loop bound before an integer test (a failed `[ -ge ]` inside `if` is just false, so `while :` never ends) and stop on the helper's permanent-failure patterns. (files: .github/workflows/clarify.yml, scripts/gh_helpers.sh)

## Notes
- 2026-09-29 twin-sync resume stage (session_01QCEtm4U1UmHqXfWHjCRj5Q): owner answered Q1: A; [claude-twin-sync] 63ee422 verified (both .claude/commands sha256 match the twins); ai:claude-blocked removed from #5222; project branch synced with main (a06a40e, clean merge); wait armed on PR #5271.
- 2026-09-29 review round 1 (session_019WoLVxBYnoYozUC6qZQHLn): finding clarify.yml:426-428 (no-retry gh_retry_to_file fallback) fixed with bounded retries and a degraded-mode warning; task gap in tests/test_phase_skip_gate_telemetry_contract.py fixed with a final_merge_resume assertion.
- 2026-09-30 review round 2 (session_01N33hrjC3qxNQNw3GyAuXEV): finding clarify.yml:425-428 (fallback logged no per-retry warning) fixed with a warning per failed attempt and a final one after the last; task gap "`.claude/commands/` edited in a twin-first PR" rejected: the `.claude/` edits are the owner-approved `[claude-twin-sync]` 63ee422 (Q1: A), and both copies match their twins byte for byte; the PR body's twin-first section, which still said the sync was pending, was corrected.
- 2026-09-30 review round 3 (session_01MEAuT2pSkwNS6CpB5QsXYz): all three consensus findings on clarify.yml:426-450 fixed in the gh_retry_to_file fallback: a non-numeric GH_RETRY_MAX_ATTEMPTS made the `-ge` test fail inside `if`, so `while :` never ended (reproduced: the old step hung until killed); the count is now validated and capped at 5, and the real helper's `_is_gh_permanent_failure` patterns (404, 422, resource not accessible) stop the retries. Tests: test_clarify_fallback_validates_and_caps_max_attempts, test_clarify_fallback_does_not_retry_a_permanent_failure. Project branch synced with main (ad84c01, clean merge of analysis/ docs).
- Issue progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5222#issuecomment-5898432937
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
