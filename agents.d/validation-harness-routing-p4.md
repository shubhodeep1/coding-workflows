<!-- agents: section="Stable log prefixes (contractual)" -->
- `VALIDATION_CYCLE_SOURCE` (`scripts/validate_process.sh` `validation_cycle_resolve`: `source=env|dispatch_comment|default cycle=<n>`, and on `source=default` also `reason=invalid_env|not_tracking_run|login_unavailable|comments_unavailable|no_dispatch_comment|malformed_cycle`; when `VALIDATION_CYCLE` is unset the cycle comes from the `- Cycle: <n>` line of the newest `## 🧪 Runtime validation dispatched` tracking comment by the pipeline login, else 1)
LOG_PREFIX.name=VALIDATION_CYCLE_SOURCE
