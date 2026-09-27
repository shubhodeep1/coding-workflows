# Implement-Plan Log — Let a spot-fix reissue declare the new files its follow-up must create

- Plan: docs/plans/issue-4665-reissue-new-output-paths-plan.md
- Source issue: shubhodeep1/coding-workflows#4665
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: stable
- Project branch: claude/implement-plan-issue-4665-reissue-new-output-paths   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-27
- Last note: project branch opened from stable; phase 1 in progress.

## Phases
1. [ ] Phase 1 — declare and union new output paths (`new_output_paths` in the judge contract, validated union into the spot-fix `files_touched` allowlist, tests, docs, changelog fragment)
   - [ ] Prompt schema + rules in `prompts/mode-judge-review-blocked.txt` and `prompts/_templates/mode-judge-review-blocked.txt`
   - [ ] Validated third union source in `scripts/review_rb_judge.sh` with `REISSUE_FILES_TOUCHED_NEW_OUTPUTS`
   - [ ] Tests: `tests/test_review_rb_judge_label_propagation.py`, `tests/test_files_touched_scope_guard.py`, `tests/test_orchestrate_poll_workflow_contract.py`
   - [ ] Docs: `README.md`, `agents.md`; fragment `changelog.d/4665-reissue-new-output-paths.md`
   - Done: new tests pass, existing reissue / scope-guard / prompt / contract suites pass, and a reissue without the field is byte-identical.

## Conformance

## Security pass
- Skipped (ai:workflow-heal: automation-produced issue)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Should this project also repair #4664 (edit its `files_touched`, remove `ai:scope-blocked`)? — Picked: A — no; leave it to the existing human-gated procedure and say so on the issue. Alternatives: B — edit #4664's body and labels from this session. Why: the issue calls that procedure human-gated, and editing another issue's scope allowlist is outside this code fix. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-27] Where does the judge declare new files? — Picked: A — a top-level `new_output_paths` string array. Alternatives: B — a `new_files` list on each `remaining_issues[]` entry. Why: a changelog fragment or shared fixture belongs to no single finding, and a flat list is simpler to validate. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] What happens to an invalid declared path? — Picked: A — skip it, count it, log the reason, keep spot-fix. Alternatives: B — fall back to `redo` like an invalid `remaining_issues[].file`. Why: matches the closed-PR union's skip rule and keeps the preserved baseline; the worst case is today's behaviour. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] May a declared path already exist at the closed head? — Picked: A — no; it must be absent (neither file nor directory), and globs and trailing `/` are rejected. Alternatives: B — allow existing files too. Why: existing files belong in `remaining_issues[]` with line anchors, and absence rules out any existing-directory exemption. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] Cap the number of declared paths? — Picked: A — a fixed cap of 10, excess entries skipped as `over_cap`. Alternatives: B — no cap; C — a new repo var. Why: bounds a runaway judge without a new configuration surface (§4, §5). Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode: base `stable` from the issue's `Target branch:` line; no merged PR has `stable` as its head, so the base did not move. Security pass skipped per the plan header. Activation n/a for a non-default base: the project ends after the final merge, which closes #4665 explicitly with `ai:merged`.
- Permission mode auto (no start-up question needed).
