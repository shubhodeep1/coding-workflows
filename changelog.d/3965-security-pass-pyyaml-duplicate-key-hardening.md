<!-- changelog: security -->
- **Deterministic contract-list union now uses a hash-locked isolated PyYAML and rejects duplicate mapping keys.** The poller installs PyYAML 6.0.3 from a verified wheel hash set before persisting Git credentials, then validates every input and generated YAML mapping with unique-key enforcement.

Dependency or duplicate-key validation failures safely return the conflict to the existing resolver path instead of using an ambient package or pushing an ambiguous contract.
