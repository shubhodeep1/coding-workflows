# Implement-Plan Log — Finalize issue lineage only for merges the target-branch gate accepted

- Plan: docs/plans/issue-5227-lineage-only-target-branch-merges-plan.md
- Source issue: shubhodeep1/coding-workflows#5227
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
- Project branch: claude/implement-plan-issue-5227-lineage-only-target-branch-merges   Final PR: #5257 draft
- Status: IN_PROGRESS
- Stage: conformance 2/3
- Activation: not started
- Waiting on: conformance fix PR (branch claude/implement-plan-issue-5227-lineage-only-target-branch-merges-conformance-fix-2)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01PDr5T95Ca99NuYW2c1g8XD (project checker, reused)
- Last updated: 2026-09-30
- Last note: conformance fix 1 PR #5313 merged 2026-09-30; conformance run 2 CONFORMANT with one CONCERN (the nine #5227 lineage tests sit after the test file's `__main__` block, so CI's script run never executes them), fixed in the conformance-fix-2 PR.

## Phases
1. [x] Phase 1 — lineage allow-list in `issue_pr_status.yml` (workflow + tests + README row + changelog fragment)   — PR #5269 merged 2026-09-29; review rounds: 0; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (Implemented COMPLETE, Correctness CONCERNS) — fix PR #5313 merged 2026-09-30 (pre-security; security pass skipped for this project)
- Run 2 — 2026-09-30: CONFORMANT (Implemented COMPLETE, Correctness CONCERNS) — fix PR on branch claude/implement-plan-issue-5227-lineage-only-target-branch-merges-conformance-fix-2: `tests/test_issue_pr_status_target_branch_gate.py` ran only 12 of its 21 tests as a script (ci.yml), the `__main__` block now runs last and calls all 21 (pre-security; security pass skipped)

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which issues does the lineage step skip? — Picked: A — only issues the target-branch gate rejected on a merged PR; tracking issues and unmerged closes keep today's finalization. Alternatives: B — also stop finalizing orchestrator-tracking issues. Why: §5, the finding names the gate only; tracking-issue lineage is a separate behaviour. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How is the per-issue list represented? — Picked: A — a positive allow-list `LINEAGE_FINALIZE_ISSUE_NUMBERS` exported next to `LINKED_ISSUE_NUMBERS`. Alternatives: B — export a rejected list and subtract it in the lineage step. Why: fails closed and matches the finding's recommendation. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Does this need a changelog fragment? — Picked: A — yes, `changelog.d/5227-lineage-only-on-target-branch-merges.md` under `security`. Alternatives: B — none. Why: §20.A lists security fixes. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Should the Telegram alert and cleanup steps also use the accepted list? — Picked: A — no, they keep `LINKED_ISSUE_NUMBERS`. Alternatives: B — switch them too. Why: §5; they are not lineage writes and the finding does not cover them. Applied in: no code change. Status: pending review
- AD-5 [conformance 1/3, 2026-09-30] How should lineage treat an issue whose classification lookups both failed (the gate skips it as tracking without reading its body)? — Picked: A — finalize it only on an unmerged close or a default-branch merge, where no target branch is needed; skip it on a merge into any other branch. Alternatives: B — never finalize an unclassified issue; C — leave as is (finalize like a tracking issue). Why: fails closed on the #5227 path while keeping default-branch and unmerged lineage during an API blip; confirmed tracking issues keep AD-1. Applied in: conformance-fix-1 PR. Status: pending review

## Lessons
- [source:conformance] When a gate fills a positive allow-list, every bucket that bypasses the gate (such as a conservative "treat as tracking" fallback on a failed lookup) must be checked too, or the allow-list stops failing closed. (files: .github/workflows/issue_pr_status.yml)
- [source:conformance] A test file that CI runs as a script (`python3 tests/<file>.py`) executes only the tests its `__main__` block calls, so tests appended below that block never run in CI; keep the block last and call every test from it, or run the file under pytest. (files: tests/test_issue_pr_status_target_branch_gate.py, .github/workflows/ci.yml)

## Notes
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5227#issuecomment-5899512080
- Security pass: skip (`security_pass_skip.py`: ai:security created and labelled by the issue automation).
