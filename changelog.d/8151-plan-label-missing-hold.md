<!-- changelog: fixed -->
- **Planning no longer fails on a CI-triage issue that is missing its `ai:check-triage` label.** Plan now holds the issue the way clarify already does, instead of failing the run and filing a `plan_failed` heal report.

An issue that carries the check-failure-triage markers but not the `ai:check-triage` label (and, when it is `ai:orchestrator-managed`, no `- Tracking issue: #N` line) is refused by the integration-ref resolver with `reason=label_missing`. Clarify holds such an issue with one comment and `ai:blocked` (#7516). The unblock judge then resumed it with `/answer`, which starts planning, and the "Resolve integration ref" step in `.github/workflows/plan.yml` failed the job on the same refusal. That failure filed a heal report for a stop the pipeline already knew about (btc_sweeper#223, run 40202986616). Plan now posts the same hold comment once (it shares clarify's `ai:integration-ref-label-missing:v1` marker, counted only on comments by the pipeline account), adds `ai:blocked`, removes `ai:planning` if an earlier run added it, and skips planning. It logs `INTEGRATION_REF_RESOLVE outcome=held reason=label_missing ... phase=plan`.

| The numbers that matter | Value |
| --- | --- |
| Hold comments per issue, across clarify and plan | 1 (until a `/reclarify` releases the hold) |
| Plan runs that fail on a `label_missing` refusal | 0 (a failed comment or label write still fails the run) |
| Other CI-triage refusal reasons | still fail the run, as before |

What this means for operators: fix such an issue as the hold comment says (restore `ai:check-triage` and remove `ai:orchestrator-managed`, or add the tracking line), then comment `/reclarify`. The unblock judge can still resume the issue with `/answer`, which now ends in the same quiet hold.
