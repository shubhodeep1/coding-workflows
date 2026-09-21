<!-- changelog: security -->
- **Hardened orchestration and security-audit Python trust boundaries.** Orchestrator parsing, prompt rendering, transcript archiving, and audit filtering now run in isolated Python environments; security audits use immutable installer and exclusion policy sources, reject path escapes and matcher-less suppression rules, and no longer trust audited-checkout overrides.
