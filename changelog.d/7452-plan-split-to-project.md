<!-- changelog: added -->
- **A plan too large for one pipeline grant now becomes an orchestrator project instead of waiting for approval.** When a pipeline-authored issue's plan lists more than 50 files, the plan stage starts an orchestrator project for it on the Claude engine.

Until now such a plan sat in `ai:awaiting-approval` with a "plan auto-approval withheld" warning, because the plan stage writes an exact `files_touched` grant for at most 50 files. The new step (`scripts/plan_split_dispatch.sh`, called from the "Auto-approve clear plan" step of `plan.yml`) dispatches `internal-orchestrate.yml` or `ai-orchestrate.yml` with `engine=claude` and the full plan. The orchestrator splits it into issues of at most 50 files in waves on one integration branch, with the phase that switches a behaviour on placed last. The original issue moves to the new `ai:split-to-project` label and links its tracking issue. The poller closes it when the project completes. Project child issues are never split again. A plan the step cannot hand off is held exactly as before: switched-off or missing wrapper, consumer wrapper not yet synced, plan over 60,000 characters, or a failed dispatch.

| The numbers that matter | Value |
| --- | --- |
| Files one pipeline grant covers | 50 |
| Largest plan handed off (`project_description`) | 60,000 characters |
| New variable | `PLAN_SPLIT_TO_PROJECT_ENABLED` (default `true`) |
| New label | `ai:split-to-project` |
| New wrapper input (`workflow-templates/ai-orchestrate.yml`) | `tracking_comment` |

What this means for operators: large pipeline issues keep moving without a manual `/approved`, and each one shows up as a Claude orchestrator project you can follow from the original issue. A consumer repository splits plans after its next `@stable` sync brings the `tracking_comment` input, and only while its `ai-orchestrate.yml` workflow is enabled. Set `PLAN_SPLIT_TO_PROJECT_ENABLED=false` to keep holding such plans.
