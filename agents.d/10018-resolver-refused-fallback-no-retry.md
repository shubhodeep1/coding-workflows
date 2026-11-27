<!-- agents: section="Stable log prefixes (contractual)" -->
- `CONFLICT_RESOLVER_FALLBACK_REFUSED` — `scripts/review_conflict_resolve.sh` prints `CONFLICT_RESOLVER_FALLBACK_REFUSED rc=76 attempt=<n>` once when a resolver attempt exits 76 (the writer's engine fallback was refused, `AI_ENGINE_FALLBACK_REFUSED`, e.g. `reason=no_credential`). The run then ends through the existing final-attempt failure path without another attempt (exit 1, `RESOLVER_TERMINAL_EXIT_CLASS=other`). Refs #10018
LOG_PREFIX.name=CONFLICT_RESOLVER_FALLBACK_REFUSED
