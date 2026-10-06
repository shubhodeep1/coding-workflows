<!-- changelog: security -->
- **Wave-judge model calls no longer inherit GitHub write credentials.** Claude now runs in the isolated read profile, while the existing Codex fallback remains isolated and read-only. The trusted poller rejects judge-requested reverts outside the current wave or targeting an unrelated PR. Refs #6449; related to #3576.
