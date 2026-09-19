<!-- changelog: security -->
- **Isolate workflow model writers and immutable support.** Implementation and validation agents now run under dedicated least-privilege identities, while workflow helper bundles, including orchestrate and orchestrate-poll, fail closed on missing or checkout-shadowed transitive dependencies.
