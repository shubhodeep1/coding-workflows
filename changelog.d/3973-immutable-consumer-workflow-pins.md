<!-- changelog: fixed -->
- Consumer workflow wrappers now pin reusable coding-workflows calls to the immutable release commit SHA while retaining `# stable` for readability. Stable syncs refresh existing wrappers, including the self-updater, and drift audits report stale pins. Refs #3973.
