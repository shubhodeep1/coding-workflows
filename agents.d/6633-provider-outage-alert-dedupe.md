<!-- agents: section="Workflow architecture" -->
Model-provider outage alert dedupe (issue #6633): `opencode_emit_failure_alert`
(`scripts/opencode_helpers.sh`) runs `scripts/provider_outage.py probe` before a
per-call Telegram ERROR for a runtime failure (setup classes such as `config_*`
are not probed). It caches the result in `RUNTIME_DIR/provider_outage_alert_probe.json`,
separate from the classifier's cache. When the probe reports `down`, it logs
`PROVIDER_OUTAGE op=alert outcome=suppressed reason=provider_down` and sends
nothing, so one outage produces only the `ai:provider-outage` tracker alert. A
missing `RUNTIME_DIR` or helper, `PROVIDER_OUTAGE_CLASSIFY_ENABLED=false`, or an
`up`/`unknown` probe keeps the existing alert.
