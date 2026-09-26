<!-- changelog: security -->
- Block Git metadata and common credential files from targeted prompt context, keep Git authentication out of repository URLs and config, and require trusted SHA-bound approval before review-blocked terminal actions.

- **Model-facing workflow phases no longer inherit reusable provider or repository credentials.** A bounded loopback broker retains the real OpenRouter key while issue analysis, orchestrator polling, implementation and repair, validation, source and consumer retros, review consolidation, review editing, review-blocked judging/fixing, workflow audits, and the unprivileged conflict writer receive only an ephemeral run token in clean environments.

The poller now executes support exclusively from the reusable workflow's immutable identity. Resolver retry tiers trust producer-signed V2 comments instead of editable PR bodies, GitHub side effects run in a head-revalidating trusted actuator, and workflow-log reports must pass a strict structural validator before atomic publication.
