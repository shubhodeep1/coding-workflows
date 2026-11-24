<!-- changelog: changed -->
- **`AI Workflow Integrity` no longer starts on every push in consumer repos.** The wrapper's `push` trigger now has `paths: ['.github/workflows/**']`, so a push starts a run only when it changes a workflow file.

The consumer wrapper `workflow-templates/ai-workflow-integrity.yml` ran on `pull_request_target` and on every branch push. Only the pull request run posts the required status `coding-workflows/workflow-integrity`. A push run posts nothing, and for a push that changed no managed wrapper it ended at `no_managed_wrapper_changed` after taking a runner slot. Every managed wrapper is a top-level file under `.github/workflows/`, so for a push to an existing branch the filter removes only runs that did nothing. A push that creates a branch, or whose old tip is gone, is compared with the default branch, so a skipped run of that kind could have reported a stale wrapper; the pull request run still judges that branch. The `pull_request_target` trigger, the required status and the reusable workflow's checks are unchanged.

| The numbers that matter | Value |
| --- | --- |
| `AI Workflow Integrity` runs in three consumers, 2026-11-10 to 2026-11-17 (push plus pull request) | 850 / 809 / 265 |
| Push runs before | 1 per push, every branch |
| Push runs after | 1 per push that changes `.github/workflows/**` |
| Pull request runs | unchanged, 1 per `opened`, `synchronize`, `reopened` |
| Reaches consumers | next `@stable` sync (`.github/workflows/update_workflows.yml`) |

What this means for consumer repos: nothing to change by hand. The next sync pull request updates `ai-workflow-integrity.yml` together with its release pin, and the ruleset `coding-workflows managed wrappers` keeps requiring the same status. After it merges, ordinary code pushes no longer queue an integrity run.

### For contributors

GitHub evaluates a `paths:` filter on at most the first 300 changed files of a push, and runs the workflow anyway when it cannot compute the diff. A push that changes more than 300 files could therefore skip the push run even though a wrapper changed; the pull request run, which carries the verdict, is not filtered. `tests/test_workflow_integrity.py` pins the trigger and proves that the checker reports no managed candidate outside the top level of `.github/workflows/`.
