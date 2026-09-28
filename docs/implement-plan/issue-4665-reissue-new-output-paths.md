# Implement-Plan Log — Let a spot-fix reissue declare the new files its follow-up must create

- Plan: docs/plans/issue-4665-reissue-new-output-paths-plan.md
- Source issue: shubhodeep1/coding-workflows#4665
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: stable
- Project branch: claude/implement-plan-issue-4665-reissue-new-output-paths   Final PR: #4667 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #4668 (review round 2 after the round-1 fix)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01TM4SVVtTqLwEfcyXidUoE9 (hand-back and safety net re-armed after each push; ids in the stage report)
- Last updated: 2026-09-27
- Last note: review round 1 on PR #4668: fixed the fail-open existence check (a failed lookup at the closed head now skips the entry as `lookup_failed`, via a literal-pathspec `git ls-tree`) and added a notice for a present non-array `new_output_paths`; rejected the redundant-gate finding.

## Phases
1. [ ] Phase 1 — declare and union new output paths (`new_output_paths` in the judge contract, validated union into the spot-fix `files_touched` allowlist, tests, docs, changelog fragment)   — PR #4668 open (waiting); review rounds: 1; interventions: 0
   - [x] Prompt schema + rules in `prompts/mode-judge-review-blocked.txt` and `prompts/_templates/mode-judge-review-blocked.txt`
   - [x] Validated third union source in `scripts/review_rb_judge.sh` with `REISSUE_FILES_TOUCHED_NEW_OUTPUTS`
   - [x] Tests: `tests/test_review_rb_judge_label_propagation.py`, `tests/test_files_touched_scope_guard.py`, `tests/test_orchestrate_poll_workflow_contract.py`
   - [x] Docs: `README.md`, `agents.md`; fragment `changelog.d/4665-reissue-new-output-paths.md`
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
- AD-6 [phase 1/1, 2026-09-27] Which hand-off author login should the checker use? — Picked: A — shubhodeep1, the GH_PAT account whose workflow comments appear on #4665; verdict bot empty. Alternatives: B — leave both empty (fail closed, review hand-offs then reach the chain only through the §26.H sweep). Why: the review workflow posts hand-offs as the GH_PAT account and review_autofix_sweep.yml defaults to the same login; sibling project issue-4621 uses it. Applied in: no code change. Status: pending review

## Lessons
- [source:intervention] An existence check that gates an allowlist must tell "absent" from "lookup failed": `git cat-file -e <sha>:<path>` exits non-zero for both, so use `git --literal-pathspecs ls-tree --full-tree --name-only <sha> -- <path>` (exit 0 with empty output is the only proof of absence) and skip the entry on any other result. (files: scripts/review_rb_judge.sh)

## Notes
- Issue mode: base `stable` from the issue's `Target branch:` line; no merged PR has `stable` as its head, so the base did not move. Security pass skipped per the plan header. Activation n/a for a non-default base: the project ends after the final merge, which closes #4665 explicitly with `ai:merged`.
- Permission mode auto (no start-up question needed).
- `.github/workflows/review_autofix.yml` is 505,498 bytes on `stable`, above the §27 480,000-byte guard but under GitHub's 512,000 limit. `stable` predates `tests/test_workflow_file_size_limit.py`, and `main` has already split the file (451,394 bytes), so the next promotion fixes it. This project does not touch the file (§5).
