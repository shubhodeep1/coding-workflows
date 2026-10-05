<!-- changelog: security -->
- **Unblock judge verifies project PR ownership before posting project actions.** Project-targeting PRs must have a same-repository `ai/issue-<n>` head and a matching child issue in pipeline-authored project state; unverifiable bindings are skipped without writes.
