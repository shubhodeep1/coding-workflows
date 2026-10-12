<!-- agents: section="Workflow architecture" -->
Weekly consumer backfill (issue #6712): `propagate-consumer-secrets.yml` also
runs on a weekly schedule (`23 5 * * 1`) against every registry entry with
`PROPAGATE_ONLY_MISSING=true`. `scripts/propagate_consumer_secrets.sh` then
lists each consumer's secret names once before any write and sets only the
missing ones (`status=skipped_present` for the rest); a failed listing writes
nothing to that consumer and turns the run red
(`status=failed reason=presence_list_failed`). Push and dispatch runs keep
overwriting. `PROPAGATE_ONLY_MISSING` defaults to `false` in the script and
accepts only `true` or `false`.
