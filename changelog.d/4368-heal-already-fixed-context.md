<!-- changelog: fixed -->
- **The workflow failure heal no longer re-files failures that are already fixed, and its diagnosis now reads what the failing step printed rather than the step's source.** Promote-cycle gate failures now join their existing heal lineage, and a failure fixed after the commit that ran is closed out with a note instead of an issue.

Issue #4368 showed three gaps in the heal intake (`scripts/workflow_failure_heal_intake.sh`). First, each promote cycle's Test & Mark Stable Release run carries a unique `[cycle:<id>]` suffix in its run name, and that suffix went into the dedup fingerprint, so every cycle's failure looked new. Second, the error signature and the log tail the model reads came mostly from the step script GitHub echoes at the top of each `run:` step, not from the step's output. Third, the model was never told that the branch had moved past the failing commit. #4368 was filed for a Phase 4b failure whose fix (#4351) had reached `main` 13 minutes after the gate started. The pipeline then planned it and ended with the implementer answering BLOCKED.

| The numbers that matter | Value |
| --- | --- |
| Echoed script lines in the #4368 gate job log (now dropped) | 2,340 of 3,726 |
| Extra GitHub API calls per heal intake | 1 (`compare/<failing sha>...<branch>`) |
| Commits / changed files shown to the model | newest 60 / first 200 |
| Earlier heal issues shown to the model | up to 5, same fingerprint or lineage root |

What this means for operators: expect fewer `ai:workflow-heal` issues that end in `ai:blocked` with "fix already exists". A failure that is already fixed now logs `WORKFLOW_HEAL no_issue classification=already-fixed` and sends a DEBUG Telegram note. A consumer report also gets a comment saying the next wrapper sync picks up the fix. The one-time cost: fingerprints computed from Actions job logs change with this release, so a recurrence of a failure that already has an open heal issue may open one new issue instead of adding an occurrence comment.

### For contributors

- `fingerprint()` in `scripts/workflow_failure_heal.py` drops a trailing `[cycle:<digits>]` from the workflow name. `filter_log()` removes the ANSI-cyan script lines inside `##[group]Run` headers. The first signature bucket also matches `##[error]`.
- `already-fixed` is honoured only when `check-already-fixed` finds a commit from the compare list cited under the diagnosis's `## Fixed by` section. Otherwise the report is filed as `inconclusive` and the intake logs `warn already_fixed_unverified reason=…`.
- The heal-issue list query also returns `title`, `closed_at` and `state_reason` in the same call. The branch-tip checkout (`HEAL_BRANCH_TIP_DIR`) uses `git fetch`, not the API.
