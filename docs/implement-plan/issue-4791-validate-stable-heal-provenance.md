# Implement-Plan Log — validate.yml authorizes a stable target only for a verified workflow-heal issue bound to the PR

- Plan: docs/plans/issue-4791-validate-stable-heal-provenance-plan.md
- Source issue: shubhodeep1/coding-workflows#4791
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4734-validate-stacked-stable-targets
- Project branch: claude/implement-plan-issue-4791-validate-stable-heal-provenance   Final PR: #4793 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (opened with this commit; number in the stage report and the checker's resume block)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-28
- Last note: phase 1 implemented — stable targets now need a verified heal issue (automation author, fp marker, `stable` branch line, label applied by the author within 120 s) opened by the target PR's author; tests/test_validate_target_ref_input.py 12 passed, 262 related tests passed, actionlint adds no finding, validate.yml 72,527 bytes; live check: #4667 (stable) and this project's branch still authorized.

## Phases
1. [ ] Phase 1 — verify heal provenance and PR binding for stable targets in validate.yml   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Which provenance rules decide a genuine heal issue? — Picked: A — the `security_pass_skip.py` rules (automation author, label applied at creation by the author, fp marker) plus the issue naming `stable` as its branch. Alternatives: B — author and marker only, no events read; C — a new provenance record written by the heal intake. Why: reuses the rules already reviewed for #4623 at one extra read; C needs a new producer and still would not bind the PR. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How is the target PR and its head bound to the issue? — Picked: A — the PR author login must equal the issue author login; the head stays pinned to the PR listing's 40-hex SHA. Alternatives: B — also require the head commit's author to be the issue author (one more read; commit author metadata is set by whoever commits); C — require a workflow-posted record naming the head SHA (no workflow writes one today). Why: A blocks the collaborator-opened PR in the finding at no extra read; B adds no real assurance and C needs a new producer. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Which changelog fragment records the change? — Picked: A — amend `changelog.d/4734-validate-stacked-and-stable-targets.md` in place. Alternatives: B — add `changelog.d/4791-…` with a `security` entry. Why: the label-only rule never shipped (it lives only on the unmerged #4734 project branch), so the fragment that ships should describe the final rule. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Update the #4734 plan doc's Goal 3 and Risks? — Picked: A — leave the #4734 plan and log to that project's chain. Alternatives: B — edit them in this PR. Why: they are that project's records and its chain records its security follow-ups itself; §5. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Issue mode; base branch `claude/implement-plan-issue-4734-validate-stacked-stable-targets` (project #4734, final PR #4746 into main, open); security pass: skip (`security_pass_skip.py`: `ai:security` created and labelled by the issue automation).
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4791#issuecomment-5868284059
- Protected paths: none (phase 1 touches no `.claude/**` path).
