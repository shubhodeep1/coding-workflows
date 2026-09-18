<!-- changelog: fixed -->
- **Security-pass fix cycles now advance only when the merged fix PR targets the project's integration branch.**

Security-pass polling now validates a merged fix PR's base branch before consuming a fix cycle. A PR merged into `main` or another branch no longer advances a project whose fix belongs on its integration branch, even when the fix issue already carries `ai:merged`. Both the batched GraphQL path and the conditional timeline fallback enforce the same check while preserving retry behavior when evidence lookup fails. The fallback reuses the PR payload it already fetches, so polling gains no unconditional API request.

| The numbers that matter | Value |
| --- | --- |
| Merged-evidence paths protected | 2 |
| New unconditional API calls per poll | 0 |
| Wrong or missing candidate bases accepted | 0 |

What this means for operators: security-pass cycle budgets now reflect fixes that actually reached the integration branch, rather than unrelated or misdirected merges.

### For contributors

Regression coverage exercises both cached GraphQL evidence and direct-lookup timeline evidence with an initial `ai:merged` label, while retaining the valid integration-branch path.
