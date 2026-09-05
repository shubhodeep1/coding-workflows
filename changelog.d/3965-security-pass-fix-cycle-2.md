<!-- changelog: security -->
- Poller model agents now run read-only without GitHub, Telegram, Git, or state-auth credentials; trusted shell actuators validate decisions before redispatching existing automation, normalize exhausted or merged-PR fix actions to bounded terminal paths, and retry transient conflict-judge failures within the existing lifetime cap.
- Orchestrator state signatures now use a dedicated rotation-capable keyring bound to immutable producer IDs, with legacy PAT signatures retained only for migration.
- Deterministic contract-list union now validates YAML node parents and all unaffected values, and targeted file context now enforces one strict rendered-output budget with bounded paths.
