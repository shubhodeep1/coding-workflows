<!-- changelog: security -->
- **Automatic plan approval no longer approves plans that change workflow automation.** Issue #6769 (from the #6766 unblock step).

A plan that lists `.github/`, `.claude/`, `scripts/`, `prompts/` or `workflow-templates/` paths is no longer approved automatically by `plan.yml` or by the poller's stall recovery (`auto_approve`, and `retrigger_implement` before the first approval). The new repository variable `AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED` defaults to `false`, which holds every such plan for a human `/approved`. Set to `true`, a plan is approved only when every protected path it lists has an exact `files_touched` entry in an issue written by the pipeline account or a repository member; a pipeline-authored issue without `files_touched` (for example a security finding) still needs a human. Plans that touch no protected path are approved as before.

Each hold logs `AI_PHASE_GATE_V1 ... gate=auto_approve reason=<reason> outcome=defer issue=<n> paths=<missing paths>`, plan.yml also writes the paths to the job summary and sends its usual plan notification, and the poller logs `STALL_SKIP ... reason=automation_path_plan_hold` without counting a recovery attempt. The implement-time automation-path guard is unchanged, and the issue GraphQL batch the poller already makes now also reads the issue author (no new API call).

What this means for operators: in repositories whose plans usually touch `scripts/` or `.github/`, expect to reply `/approved` by hand until you set the variable to `true` once required checks pass.
