<!-- agents: section="Workflow architecture" -->
Workflow failure heal and the resolver's host-only stop (#6745): the conflict
resolver's host-only conflict stop (`conflict_resolver_sandbox_path_host_only`)
needs a manual merge, not a heal. Its per-run report, an
`identical_failure_cap` report repeating it (`repeated_failure_reason`, from
the gate's `FINGERPRINT_CAP_REASON` or the trusted head markers) and an
`ai:needs-human` label on an item whose newest autofix marker by the `GH_PAT`
account names it (on a PR, for its current head; provider-outage markers and
comments are skipped as in `count_identical_failures`) are skipped by the
reporters and the intake (`WORKFLOW_HEAL skip reason=host_only_conflict_manual_merge`).
The resolver's manual-merge error and the identical-failure cap are unchanged.
