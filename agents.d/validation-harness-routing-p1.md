<!-- agents: section="Stable log prefixes (contractual)" -->
- `VALIDATION_OWNER` (`scripts/validate_process.sh`: `owner=harness|project|mixed|none reason= stage= fp= harness_paths= project_paths=`, plus `::warning::VALIDATION_OWNER outcome=fail_open|default reason=`; plan `docs/plans/validation-failure-harness-routing-plan.md`, Phase 1)
- `VALIDATE_SUPPORT_SHA` (`.github/workflows/validate.yml` "Fetch workflow support files": `sha=<40hex>`, the verified commit the validation support scripts were staged from; printed after the pinned-commit check and exported to `GITHUB_ENV` as `VALIDATE_SUPPORT_SHA`, which `validate_process.sh` writes into the `validation-harness-owner:v1` marker)
LOG_PREFIX.name=VALIDATION_OWNER
LOG_PREFIX.name=VALIDATE_SUPPORT_SHA

<!-- agents: section="Orchestrator tracking-issue comment markers" -->
Harness-owned validation failures (plan `docs/plans/validation-failure-harness-routing-plan.md`, Phase 1) add two markers on the tracking issue. Both are the last line of their comment, and the poller trusts them only from the `GH_PAT` login.

| Marker | Writer | Purpose |
|---|---|---|
| `<!-- validation-harness-owner:v1 fp=<64hex> stage=<stage> reason=<reason> support=<40hex> run=<run id> -->` | `scripts/validate_process.sh` (harness-error comment) | Records that a failed validation run is owned by the validation harness coding-workflows ships, with the fingerprint the run's `heal-report` job reports and the support commit (`VALIDATE_SUPPORT_SHA`). |
| `<!-- workflow-failure-heal:validation-route:v1 fp=<64hex> outcome=opened\|duplicate\|already-fixed\|bounced\|skipped issue=<n\|none> classification=<classification> run=<run id> -->` | `scripts/workflow_failure_heal_intake.sh` (`validation_failure` report) | Records what the heal intake did with that report; the poller holds or releases the project from it. |

`validate.yml` passes the ownership on as `workflow_call` outputs `failure_owner`, `harness_report`, `harness_stage`, `harness_reason` and `harness_fp`, each checked against a fixed vocabulary or pattern in `collect_status`. Its `heal-report` job (`needs: validate`, `permissions: id-token: write`, failed validate job with `harness_report == 'true'` and a tracking issue other than `0`) resolves its support ref like `implement.yml`'s, verifies the checkout HEAD, and runs `scripts/workflow_failure_heal_phase_report.sh` with `WORKFLOW_HEAL_PHASE=validate`. The error signature travels as the job output `harness_signature_b64` (at most 1024 bytes, base64 alphabet checked on both sides). `VALIDATION_HARNESS_HEAL_ENABLED=false` skips the job.
