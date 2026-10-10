<!-- agents: section="Workflow architecture" -->
Check-failure triage historical mode (#7093): `check_failure_triage.yml` takes
optional `historical_run_id` / `historical_job_id` /
`historical_repro_artifact_prefix` inputs (empty keeps the normal path). With
both ids set, `scripts/check_failure_triage.sh` triages a closed same-repo PR
(fork guard kept; missing head repo fails closed). Its fingerprint adds
`|run=<id>|job=<id>`, the dedup listing uses `state=all`, and the base gate is
bypassed. It binds the run and job (repository, `ci.yml` path,
`pull_request` event, head SHA and branch, failed conclusion) before
fetching the job log on the host. The log is redacted with
`workflow_failure_heal.py redact-stream` (now staged into trusted support),
and only the first failure window reaches the isolated diagnosis. On a 404/410
log, it parses same-run reproduction artifacts host-side and recounts them
(`reproduced` / `complete_non_reproduction` / `incomplete`; complete needs
every group, shard and step and zero jq/PyYAML skips). The single-use
`check-failure-triage-historical.yml` (push to `main` + dispatch, PR #6611
run 37922279826 / job 113792812346) drives it. Its guard dedups on trusted
issues and caps failed runs at 3, and its `repro` matrix runs without
secrets. Log keys: `CHECK_TRIAGE historical_mode`,
`historical_evidence source=log|repro outcome=…`, `error
historical_binding_mismatch|historical_evidence_unavailable|historical_inputs_incomplete`,
and `HISTORICAL_TRIAGE` in the workflow.
