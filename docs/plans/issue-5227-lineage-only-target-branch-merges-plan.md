# Finalize issue lineage only for merges the target-branch gate accepted

Source issue: shubhodeep1/coding-workflows#5227 (https://github.com/shubhodeep1/coding-workflows/issues/5227)
Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
Security pass: skip (ai:security: automation-produced issue)

## Summary

`issue_pr_status.yml` skips the `ai:merged` label and the close for an issue whose linked PR merged into a branch that is not the issue's target branch (issue #4813), but the next step, `Finalize linked issue lineage state`, still writes that issue's AI-memory lineage as `merged`. This plan makes the lineage step finalize only the issues the gate accepted.

## Context

- The security audit reported this against the #4813 project branch as `rejected-merge-still-finalizes-lineage` (STRIDE-Tampering, medium, `.github/workflows/issue_pr_status.yml:545`). Recommendation: export a separate, per-issue list of merges accepted by the target-branch gate and finalize lineage only for that list.
- Step `Update linked issue labels when PR closes` builds `ISSUE_NUMBERS`, loops over it, and for a merged PR whose base is neither the default branch, the issue's `Integration branch:` / `Target branch:`, nor (for a labelled managed child) its `orchestrator/project-<T>` branch, logs `PR merged into <base>, which is not issue #<n>'s target branch …` and `continue`s. Afterwards `export_orchestrator_issue_classification` writes `LINKED_ISSUE_NUMBERS` (every linked issue, rejected ones included) to `$GITHUB_ENV`.
- Step `Finalize linked issue lineage state` loops over `LINKED_ISSUE_NUMBERS` and calls `memory_finalize_task --final-state merged` (or `closed` for an unmerged PR) for each. The lineage record is what the AI-memory pipeline reads as the issue's outcome, so a PR merged into an unrelated branch that mentions an issue marks that issue's task lineage finished.
- `LINKED_ISSUE_NUMBERS` is also read by the Telegram alert and the tracked-message cleanup steps; those keep their input.

## Goals

- On a merged PR, an issue rejected by the target-branch gate gets no `memory_finalize_task` call.
- On a merged PR, an issue the gate accepted (default-branch merge, declared integration/target branch merge, managed child on its project branch) is finalized as `merged` exactly as today.
- On an unmerged close, and for orchestrator-tracking issues, lineage finalization is unchanged (AD-1).
- The lineage step logs and emits telemetry when linked issues exist but none was accepted.

## Non-goals

- Changing which issues the gate accepts (#4813 / #4957 rules stay).
- Changing the Telegram alert or cleanup steps, which keep reading `LINKED_ISSUE_NUMBERS` (AD-4).
- Changing lineage behaviour for tracking issues or for PRs closed without merging (AD-1).

## Constraints

- §6: `LINKED_ISSUE_NUMBERS`, step names, and existing log lines are unchanged; the new env var `LINEAGE_FINALIZE_ISSUE_NUMBERS` and telemetry reason `no_accepted_issues` are new and unique in the repo (checked with `grep`).
- §9: YAML stays 2-space indented; tests use tabs.
- §15: no new GitHub API call; the list is built from data the gate loop already has.
- §20: security fix, so one `changelog.d/5227-…` fragment (`<!-- changelog: security -->`).
- §27: `issue_pr_status.yml` is ~844 lines, far below the 480,000-byte guard.

## Approach

In the gate loop, keep a positive allow-list `LINEAGE_FINALIZE_ISSUE_NUMBERS`: append the issue number on every path that does not `continue` past the gate (issues that get the label), and for orchestrator-tracking issues before their `continue` (unchanged lineage behaviour, AD-1). `export_orchestrator_issue_classification` writes it to `$GITHUB_ENV` next to `LINKED_ISSUE_NUMBERS`, so every exit path (including the early "no linked issues" exit) exports it. The lineage step loops over `LINEAGE_FINALIZE_ISSUE_NUMBERS`; when `LINKED_ISSUE_NUMBERS` is non-empty but the accepted list is empty it logs `No linked issue was accepted by the target-branch gate; skipping lineage finalization.` and emits `AI_MEMORY_TELEMETRY` with `reason":"no_accepted_issues"`. A positive list fails closed: an issue reaches lineage only if the gate loop accepted it (AD-2).

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan; the change is one workflow file plus its tests, and splitting it would leave a half-wired export.

1. **Phase 1 — lineage allow-list.** Files: `.github/workflows/issue_pr_status.yml`, `tests/test_issue_pr_status_target_branch_gate.py`, `README.md` (the `issue_pr_status.yml` row), `changelog.d/5227-lineage-only-on-target-branch-merges.md`. Done when the new tests pass (rejected merge → no finalize call; accepted merges → finalize `merged`; unmerged close → finalize `closed`; tracking issue → finalize as before), the existing `test_issue_pr_status_*` tests pass, and `actionlint`/YAML parse is clean. Rollback: revert the phase PR; the step returns to finalizing every linked issue.

## Implementation Steps

1. `issue_pr_status.yml`, step `Update linked issue labels when PR closes`: initialise `LINEAGE_FINALIZE_ISSUE_NUMBERS=""` with the other lists; in the loop append the issue before the tracking-issue `continue` and right before the label call; write it in `export_orchestrator_issue_classification` as a heredoc env var; extend the comment above the export.
2. Same file, step `Finalize linked issue lineage state`: after the `LINKED_ISSUE_NUMBERS` empty check and the memory-enabled check, skip with the new log and telemetry when `LINEAGE_FINALIZE_ISSUE_NUMBERS` is empty; loop over it instead of `LINKED_ISSUE_NUMBERS`.
3. Tests: extend `_run_step` in `tests/test_issue_pr_status_target_branch_gate.py` to return the `$GITHUB_ENV` lists; add assertions to the existing rejected/accepted cases; add a runtime test that runs the lineage step with a stub `scripts/memory_helpers.sh` recording `memory_finalize_task` calls.
4. `README.md` `issue_pr_status.yml` row: one sentence that lineage is finalized only for issues the gate accepted.
5. `changelog.d/5227-lineage-only-on-target-branch-merges.md` (§20).

## Files & Modules

- `.github/workflows/issue_pr_status.yml`
- `tests/test_issue_pr_status_target_branch_gate.py`
- `README.md`
- `changelog.d/5227-lineage-only-on-target-branch-merges.md` [new]

## Testing

- `python3 -m pytest tests/test_issue_pr_status_target_branch_gate.py tests/test_issue_pr_status_payload_fallback_contract.py -q`
- Any other test that reads `issue_pr_status.yml` (`grep -l issue_pr_status tests/`), and a YAML parse of the workflow.

## Risks

- A consumer running an older `scripts/` copy: not affected, the change is inside the reusable workflow only and needs no helper.
- A lineage record never finalized for a rejected merge: intended; the issue's real target-branch merge finalizes it later.

## Rollout

Ships with the #4813 project's final PR into `main`, then to consumers on the next `@stable` sync through the `ai-issue-pr-status.yml` wrapper. No variable, no migration.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which issues does the lineage step skip? — Picked: A — only issues the target-branch gate rejected on a merged PR; tracking issues and unmerged closes keep today's finalization. Alternatives: B — also stop finalizing orchestrator-tracking issues. Why: §5, the finding names the gate only; tracking-issue lineage is a separate behaviour. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How is the per-issue list represented? — Picked: A — a positive allow-list `LINEAGE_FINALIZE_ISSUE_NUMBERS` exported next to `LINKED_ISSUE_NUMBERS`. Alternatives: B — export a rejected list and subtract it in the lineage step. Why: fails closed (an issue the loop never accepted is never finalized) and matches the finding's recommendation. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Does this need a changelog fragment? — Picked: A — yes, `changelog.d/5227-lineage-only-on-target-branch-merges.md` under `security`. Alternatives: B — none. Why: §20.A lists security fixes. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Should the Telegram alert and cleanup steps also use the accepted list? — Picked: A — no, they keep `LINKED_ISSUE_NUMBERS`. Alternatives: B — switch them too. Why: §5; they are not lineage writes and the finding does not cover them. Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` result: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
