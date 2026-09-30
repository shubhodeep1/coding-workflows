# Implement-Plan Log — Gate review auto-merge on hold claims and twin parity

- Plan: docs/plans/issue-5316-gate-auto-merge-on-hold-claims-plan.md
- Source issue: shubhodeep1/coding-workflows#5316
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5316-gate-auto-merge-on-hold-claims   Final PR: #5323 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5382
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01BtNVKXov4PLM6UsWo5KkSK (reused; per-wait trigger ids in the stage report)
- Last updated: 2026-09-30
- Last note: review round 1 on head 7ba2481 (session_01Vxjiyt81uvFdNj1QfewT6Z): fixed the stable log-prefix registry gap; rejected the GITHUB_REPOSITORY/REPOSITORY consistency finding and the pipeline-contract test-file task gap with reasons on the PR

## Phases
1. [ ] Phase 1 — merge gate on hold claims and twin parity (scripts/claude_merge_hold_gate.py, review_enable_auto_merge.sh, review_autofix.yml deterministic-skip-merge, tests, docs) — PR #5382 open (waiting); review rounds: 1; interventions: 0

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which claims block the merge? — Picked: A — only a `hold` that is the latest trusted claim on the current head. Alternatives: B — any live claim; C — any hold on the head even after a newer claim. Why: B blocks the convergence path; C keeps a head parked after the fixer resumed. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Which PRs does the gate read claims for? — Picked: A — heads starting with `claude/` only. Alternatives: B — every PR. Why: claims are only posted on `claude/*` PRs; B adds a comment read to every merge (§15). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What twin-parity condition fails the merge? — Picked: A — a twin the PR changed whose pair was in parity at the merge base and is out of parity at the head. Alternatives: B — any changed twin that differs from `.claude/` on the head; C — no parity check. Why: B blocks edits to the five intentionally divergent twins forever; C leaves a stage that dies before its hold uncovered. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] What happens when the gate cannot decide? — Picked: A — refuse the merge (`gate_unavailable`) with a warning. Alternatives: B — allow the merge. Why: §1; matches the helper's existing fail-closed guards. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Which merge paths get the gate? — Picked: A — `review_enable_auto_merge.sh` and the `deterministic-skip-merge` job. Alternatives: B — also the `review_rb_judge.sh` merges. Why: the judge never runs in Claude-fixer mode (§5). Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Where does the gate live? — Picked: A — new `scripts/claude_merge_hold_gate.py` importing `check_in_status.py` by path. Alternatives: B — a new mode in `.claude/scripts/check_in_status.py`. Why: no protected-path edit; `scripts/claude_pr_sweep.py` precedent. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Which logins count as trusted in the workflow? — Picked: A — the PR author plus `vars.CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` when set. Alternatives: B — also the `GH_PAT` login via `gh api user`. Why: holds are posted by the PR author's sessions. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-30] Update CLAUDE.md §26.H to mention the merge gate? — Picked: A — no; README and `agents.md` only. Alternatives: B — edit CLAUDE.md and its twin. Why: §26.H makes no claim the gate contradicts (§5). Applied in: no code change. Status: pending review

## Lessons
- [source:intervention] A PR that adds a new log key must register it in agents.md "Stable log prefixes (contractual)" in both the bullet list and the `LOG_PREFIX.name=` block, with a test that pins it. (files: agents.md)

## Notes
- Security pass: run (`security_pass_skip.py`: no skip label).
- Phase 1 has no protected paths (no `.claude/**` edit, AD-6).
- Local verification env (2026-09-30): Python 3.11 container; `tests/test_workflow_retro.py` cannot be collected (3.12 f-string syntax in scripts/workflow_retro.py) and 74 validation-template / validation-runner / workflow-retro-fanout tests fail identically on origin/main (missing toolchain). Not caused by this project.
- Plan deviation (phase 1): the plan put the `review_enable_auto_merge.sh` shell cases in `tests/test_review_autofix_review_pipeline_contract.py`. They live in `tests/test_claude_merge_hold_gate.py` (`test_helper_*`, `test_deterministic_skip_*`) with their own fake-`gh` harness, which also drives the gate. The coverage the plan specifies is complete; only the file differs.
