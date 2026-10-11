<!-- changelog: fixed -->
- **Heal issues target the branch that has the failing code.** A consumer report whose pinned release is not on `stable` now opens its heal issue against the default branch.

`scripts/workflow_failure_heal_intake.sh` used to send every consumer-reported workflow defect to `stable`. This repository's own projects validate on `main`, so a heal for one of them could point at a script `stable` did not have, and planning blocked (#7063). The intake now reads the compare it already makes (`compare/<pin>...stable`): when `stable` does not contain the pin, the issue targets the default branch and the log says `target_branch_retarget ... reason=wrapper_pin_not_on_target`.

| The numbers that matter | Value |
| --- | --- |
| Extra GitHub API calls | 0 |
| Compare statuses that retarget | `behind`, `diverged` |
| New `target_branch_source` token | `wrapper_pin_not_on_target` |

What this means for operators: heal issues from projects running on `main` are planned against `main` instead of blocking on a missing file. Consumers pinned to `stable` releases see no change.
