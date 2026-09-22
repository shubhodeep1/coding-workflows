<!-- changelog: fixed -->
- **Conflicted pull requests now receive the same automatic close cleanup as conflict-free pull requests.**

Repositories using the cancel-on-close wrapper now recover cleanup work when GitHub suppresses the `pull_request.closed` event for a conflicted pull request. The existing event path still handles ordinary closures immediately. A scheduled fallback validates every linked pull request before cancelling orphaned runs, preserving runs whenever state is missing, partial, or non-terminal. The release smoke test records mergeability diagnostics and recognizes either cleanup path without masking workflow-list API failures.

| The numbers that matter | Value |
| --- | --- |
| Scheduled fallback cadence | Every 5 minutes |
| Pull requests per GraphQL batch | Up to 50 |
| Active-run states scanned | `queued`, `in_progress` |

What this means for operators: conflicted closures converge without administrator policy changes, while uncertain or reopened pull-request associations remain untouched for a later safe retry.

### For contributors

The wrappers continue to use trusted default-branch workflow code, and the scheduled cleanup shares repository-scoped concurrency with the immediate event path.
