<!-- agents: section="Workflow architecture" -->
Model-provider outage alert dedupe (issue #6633): `opencode_emit_failure_alert`
(`scripts/opencode_helpers.sh`) runs `scripts/provider_outage.py probe` before a
per-call Telegram ERROR for a runtime failure (setup classes such as `config_*`
are not probed). It caches the result in `RUNTIME_DIR/provider_outage_alert_probe.json`
(re-probed after `PROBE_CACHE_TTL_SECS`, 300 s), separate from the classifier's
cache. When the probe reports `down`, it opens or reuses the `ai:provider-outage`
tracker itself (`provider_outage.py open`; fail-open callers can let the job
succeed without reaching the failure-path tracker step), sends that tracker's
CRITICAL alert only when this call opened it, and logs
`PROVIDER_OUTAGE op=alert outcome=suppressed reason=provider_down`. If the
tracker cannot be confirmed it logs `outcome=sent reason=tracker_unconfirmed`
and sends the per-call ERROR. A missing `RUNTIME_DIR` or helper,
`PROVIDER_OUTAGE_CLASSIFY_ENABLED=false`, or an `up`/`unknown` probe keeps the
existing alert.
