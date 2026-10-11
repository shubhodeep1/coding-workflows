<!-- agents: section="Workflow architecture" -->
An integration-conflict judge answer that is not a valid diagnosis (non-zero
exit, unparseable output, wrong schema, or a `diagnosis` /
`resolution_guidance` over 2000 characters) no longer fails the project.
`invoke_judge_for_integration_conflict` returns 5, logs
`INTEGRATION_JUDGE_INVALID` with the reason and prints the last 20 lines of
the judge log in a `::group::`. The caller spends one lifetime slot
(`integration_conflict_total_dispatches`), keeps `integration_sync_status`
at `healing`, and the judge runs again on a later tick. A judge that never
answers validly still ends at `INTEGRATION_CONFLICT_LIFETIME_MAX`.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `INTEGRATION_JUDGE_INVALID` (`scripts/orchestrate_poll_process.sh`: `pr=`, `reason=exit_<rc>|unparseable|too_long|schema`, `rc=`, `output_bytes=`)
- `INTEGRATION_JUDGE_RETRY` (`scripts/orchestrate_poll_process.sh`: `pr=`, `total_dispatches=`, `lifetime_max=`)
LOG_PREFIX.name=INTEGRATION_JUDGE_INVALID
LOG_PREFIX.name=INTEGRATION_JUDGE_RETRY
