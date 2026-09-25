<!-- changelog: added -->
- **Workflow failure heal now routes a self-inflicted review/autofix failure to the branch that caused it.** When a coding-workflows pull request breaks its own review run, the diagnosis is posted on that PR instead of opening a heal issue. When the PR's base branch introduced the break, the heal issue targets that base branch.

The autofix heal reporter (`scripts/workflow_failure_heal_autofix_report.sh`) now sends the PR's base branch, the script ref the run staged, the PR's changed files, and the crash file named by the run's stderr (a `…/scripts/<name>: line N:` shell error or an `::error::` line naming a `scripts/` or `.github/workflows/` path). For a report from this repository, the intake (`scripts/workflow_failure_heal_intake.sh`) decides who changed the crash file before the model runs. It checks the PR's own diff first, then `git diff --name-only origin/main origin/<base>` on its checkout. It logs `WORKFLOW_HEAL crash_ownership=<pr|base|none>` and passes the facts to the diagnosis prompt, which gains two tokens. `pr-self-inflicted` posts the diagnosis on the PR under "Workflow failure heal: this failure is caused by this pull request's own changes" and opens no issue. `base-self-inflicted` opens the `ai:workflow-heal` issue with `Target branch:` set to the base branch. For an `orchestrator/project-<N>` base, that issue also carries `Tracking issue: #N`, `Integration branch:`, `Refs #N` and the `ai:orchestrator-managed` label.

| The numbers that matter | Value |
| --- | --- |
| New classification tokens | `pr-self-inflicted`, `base-self-inflicted` |
| New payload fields (optional, `autofix_failure` only) | `base_branch`, `script_ref`, `changed_files` (at most 200 paths of 120 characters), `crash_file` |
| New GitHub API calls | 0. The PR diff comes from the report and the base diff from two depth-1 `git fetch`es |
| Kill switch | `WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED=false` |

What this means for operators: a PR like #4259, whose own change broke the editor guard, gets its diagnosis as a PR comment that the review-blocked judge can act on. An integration-branch defect like the one on `orchestrator/project-4139` lands as a child issue of that project. Neither case produces a fix aimed at `stable`. A token the computed ownership does not back is logged as `classification_remapped … to=workflow-defect` and takes the existing `workflow-defect` route. Consumer reports are not affected.

### For contributors

`scripts/workflow_failure_heal.py` adds `extract_crash_file`, `classify_crash_ownership` and the `classify-crash-ownership` subcommand, plus `build-autofix-payload --base-branch / --script-ref / --changed-files-file` and `compose-issue --integration-branch`. The reporter sends the new flags only when the staged helper advertises `classify-crash-ownership`, so an older helper still gets its report through. The reporter's evidence now includes the `failure_evidence_tail.txt` stage stderr that "Assemble failure evidence" writes, which is where the crash line appears. The base comparison is a tree diff between two depth-1 fetches, so it needs no merge base. It is skipped when the base is `main` or `WORKFLOW_HEAL_TARGET_BRANCH`.
