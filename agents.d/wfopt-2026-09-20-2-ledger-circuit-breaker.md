<!-- agents: section="Run-substate ledger + state-snapshot contract" -->
- Job-scoped circuit breaker in `scripts/ledger_emit_substate.sh`
  (`LEDGER_EMIT_CIRCUIT_BREAKER_ENABLED`, defaulted in the helper: unset,
  empty or `true` in any letter case is on, `false` is off, and any other
  value is off with a `::warning::` that does not print the value). The
  sentinel is the empty file `${marker_dir}/circuit_open`, in the same
  per-job marker directory as the dedupe locks (`<seen file>.d`, scoped by
  run id, run attempt and job, or by `LEDGER_SUBSTATES_SEEN_FILE`), so a
  re-run attempt starts closed. An `emit_failed:timeout:*` result, or an
  `emit_failed` detail matching (case-insensitive) `authentication failed`,
  `could not read username`, `bad credentials`, `http 401`, `http 403`,
  `returned error: 403` or `permission to .* denied`, creates the sentinel
  with noclobber; only the caller that created it prints
  `::warning::ledger_emit_substate circuit opened (reason=timeout|auth); later substates in this job are skipped`.
  While the sentinel exists and the switch is on, the helper exits 0 before
  building metadata or calling `record-run-event`, with no output; those
  events are dropped. Other `emit_failed` results keep the per-call warning
  and leave the circuit closed. Output tokens, `LEDGER_SUBSTATES_ENABLED`
  and the timeout parsing are unchanged, and the helper still exits 0 on
  every telemetry failure. The switch is read only from the helper's
  environment: `implement.yml`, `review_autofix.yml` and `validate.yml` do
  not pass it from `vars.*` (unlike `LEDGER_EMIT_TIMEOUT_SECONDS`), so a
  repository variable alone does not turn the breaker off.
