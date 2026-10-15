<!-- agents: section="Implement scope-lock label" -->
Plan split to a project (Q151/Q152/Q164). The pipeline plan grant covers at most 50 files. When a pipeline-authored issue's plan lists more, `files_touched_scope_guard.py --plan-auto-approval-gate` holds with `reason=plan_too_large_for_grant` and `plan_path_count`. The plan stage then runs `scripts/plan_split_dispatch.sh` instead of holding:

- It starts `internal-orchestrate.yml` (this repo) or `ai-orchestrate.yml` (consumers) with `engine=claude`. The `project_description` carries the issue title, the full plan text, and instructions to keep the plan's phases, give every issue at most 50 `files_touched` entries, and put the phase that switches a behaviour on last. The `tracking_comment` starts with `<!-- ai:plan-split-source:v1 issue=N -->`.
- The source issue gets `<!-- ai:plan-split:v1 issue=N wrapper=... -->` from the pipeline account and moves from `ai:awaiting-approval` to `ai:split-to-project`. The step outputs `plan_split=true`, so no "plan awaiting approval" alert is sent. The dispatch is never retried, because a dispatch that timed out may already have started a project.
- `orchestrate.yml` comments the tracking-issue link on an open source issue that carries `ai:split-to-project`. If its run fails before a tracking issue exists, the "Release plan-split source issue" step posts `<!-- ai:plan-split-released:v1 issue=N -->` and removes the label. A split marker followed by a release marker no longer counts as a split, so a `/reclarify` can hand the plan to a new project.
- When the project's status is `complete`, the poller (`close_plan_split_source_if_needed`) closes the source issue as completed. It acts only on a marker comment from a trusted author (OWNER/MEMBER/COLLABORATOR or `github-actions[bot]`), and only if the issue is open and carries the label. The outcome is kept in the state file as `.plan_split_source`, so this costs one issue read once. A failed project leaves the source issue open.
- The plan is held as before (`exit 3`) when `PLAN_SPLIT_TO_PROJECT_ENABLED=false`, the issue is a project child (`ai:orchestrator-managed` or `Managed by: AI Orchestrator`), it is already split (label or marker), the wrapper is missing or switched off, the description is over 60,000 characters, the consumer wrapper predates the `tracking_comment` input, or the dispatch fails. Plans held before this change are not split; `/reclarify` re-plans them.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `PLAN_SPLIT` (`scripts/plan_split_dispatch.sh`: `issue=`, `outcome=dispatched|skipped`, `reason=disabled|orchestrator_managed|already_split|identity_unavailable|comments_unavailable|orchestrator_wrapper_inactive|orchestrator_wrapper_missing|default_branch_unavailable|description_too_large|wrapper_without_tracking_comment|dispatch_failed|issue_json_missing|plan_missing`, `wrapper=`, `paths=`)
- `PLAN_SPLIT_LINKED` (`.github/workflows/orchestrate.yml` "Create tracking issue": `source_issue=`, `tracking_issue=`)
- `PLAN_SPLIT_RELEASED` (`.github/workflows/orchestrate.yml` "Release plan-split source issue": `source_issue=`)
- `PLAN_SPLIT_SOURCE` (`scripts/orchestrate_poll_process.sh` `close_plan_split_source_if_needed`: `tracking_issue=`, `source_issue=`, `outcome=closed|already_closed|not_split|deferred`, `reason=source_lookup_failed|close_failed`)
LOG_PREFIX.name=PLAN_SPLIT
LOG_PREFIX.name=PLAN_SPLIT_LINKED
LOG_PREFIX.name=PLAN_SPLIT_RELEASED
LOG_PREFIX.name=PLAN_SPLIT_SOURCE
