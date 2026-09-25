<!-- changelog: fixed -->
- **Workflow failure heal now recognises a pull request that breaks its own review run even when the failure names no file.** Such failures get a diagnosis comment on the PR instead of a new `ai:workflow-heal` issue against the shared workflow.

Self-inflicted routing used to need a crash file taken from the evidence (a `scripts/<name>: line N:` shell error or an `::error::` line naming a path). On PRs #4323, #4332, #4376 and #4379, the branch's own sandbox made every reviewer slot, the summariser and the editor exit with systemd status 226 and no output. With no file named, the intake built no `## Ownership facts` block, and the diagnosis had to call it a `workflow-defect`. That filed #4329, #4353 and #4377, whose fix PRs sat on the same broken branches and failed the same way. `scripts/workflow_failure_heal_intake.sh` now adds a second basis when there is no crash file: the run staged the PR head's own scripts (`script_ref` equals the head SHA) and the PR changes pipeline files. The intake then reports ownership `pr`, lists those files in the facts block, and logs `WORKFLOW_HEAL crash_ownership=pr crash_file=none basis=pipeline_files files=<n>`. `prompts/mode-workflow-failure-heal.txt` allows `pr-self-inflicted` on that basis only when the whole pipeline fails the same way.

| The numbers that matter | Value |
| --- | --- |
| Pipeline paths | `scripts/`, `.github/actions/`, `.github/workflows/review_autofix.yml` |
| Files listed in the facts block | up to 20 |
| Extra API or git calls | none (uses the report's `changed_files`, `script_ref` and `head_sha`) |
| Switch | `WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED` (unchanged, default `true`) |

What this means for operators: a PR whose own pipeline changes stop every model from running now gets the diagnosis on the PR for the review-blocked judge, instead of a chain of heal issues that retarget the same broken branch. Base-branch ownership still needs a crash file, and consumer reports are unaffected.
