<!-- agents: section="Workflow architecture" -->
Scheduled validation (issue #7553): `workflow-templates/ai-validate.yml` adds
`schedule: - cron: "17 4 * * 1"` with the job gate
`if: github.event_name != 'schedule' || vars.AI_VALIDATE_SCHEDULE_ENABLED == 'true'`
(off unless a consumer opts in). In `validate.yml` the job env
`VALIDATE_SCHEDULED_MODE` is `true` only for `github.event_name == 'schedule'`
with tracking issue `0` and an empty `target_ref` (a called workflow keeps the
caller's event; no new `workflow_call` input). In that mode "Run validation
process" gets `MAX_SELF_HEAL_ATTEMPTS=0`, `VALIDATE_DETERMINISTIC_ONLY=true` and
`ALERT_MSG_LEVEL=SILENT`. `scripts/validate_process.sh` (switch defaulted off in
the script; unset/empty/`false` off, `true` in any letter case on, anything else
off with a warning; blocks between `# BEGIN/END deterministic-only gate (#7553)`)
then skips every model phase: no committed `.ai/validate.yml` ends the run with
`raw_status=harness_missing`, and failing tests end it before diagnose with
`raw_status=tests_failed`, so no fix-up issue is filed. The step "Report
scheduled validation result" runs `scripts/validate_scheduled_sink.sh` from
`VALIDATE_HELPER_STAGE_DIR` (never the checkout; a missing or symlinked copy
logs `reason=support_missing`). The sink trusts only an open issue the `GH_PAT`
login wrote whose first body line is `<!-- ai:scheduled-validation:v1 -->`
(lowest number wins): a red run opens one `ai:scheduled-validation-failed` issue
with one Telegram WARNING or edits the open one in place; a green run closes it
and then comments; a failed `/user` or issue-list read writes nothing. Clarify's
predicate excludes `ai:scheduled-validation-failed`. API calls: one `/user`, one
paginated issue listing, at most one label create and one create, PATCH, or
close plus comment.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `VALIDATE_SCHEDULED_SINK` (`scripts/validate_scheduled_sink.sh`: `outcome=opened|updated|closed|noop issue=`, `outcome=closed issue= comment=failed`, `outcome=skip reason=identity_unavailable|list_failed|repository_invalid|support_missing`, `outcome=failed reason=create_failed|update_failed|close_failed|body_failed|tmpdir_unavailable`)
- `VALIDATE_DETERMINISTIC_ONLY` (`scripts/validate_process.sh`: `phase=discover|diagnose|self_heal outcome=skip reason=no_committed_manifest|deterministic_only`)

LOG_PREFIX.name=VALIDATE_SCHEDULED_SINK
LOG_PREFIX.name=VALIDATE_DETERMINISTIC_ONLY
