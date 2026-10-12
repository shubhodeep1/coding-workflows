<!-- agents: section="Workflow architecture" -->
- **Check-failure triage integration branch (#6726).** With
  `TRIAGE_PR_HEAD_BRANCH_METADATA_ENABLED` on (default `true`; `false` turns it off) the only
  routing key the triage body validator accepts is one header line
  `- **Integration branch:** \`orchestrator/project-<N>\``, written by
  `scripts/check_failure_triage.sh` when the GitHub API reports a
  same-repository PR head of that shape and the branch exists; it is never
  inferred from logs or issue prose. Such a failure skips the base-branch gate
  (`CHECK_TRIAGE base_gate outcome=file reason=integration_branch`), because
  the fix lands on the failing PR's own head branch.
