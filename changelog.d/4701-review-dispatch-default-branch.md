<!-- changelog: security -->
- **Review dispatches from the poller, merge train, and forward-merge fallback now use the default-branch workflow.** PR numbers are validated before dispatch; no PR head ref selects executable workflow code.

Consumer review dispatches now carry `AI Review [pr:<N>]` run names. The poller and merge train correlate those runs and existing internal PR-named runs with their pull requests, preserving legacy head-branch detection and avoiding duplicate dispatches or empty-commit pushes while a review is active.
