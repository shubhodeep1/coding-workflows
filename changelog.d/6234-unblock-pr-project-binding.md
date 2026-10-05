<!-- changelog: security -->
- **Project unblock fix-ups now require verified PR membership.** The unblock judge accepts project-base PRs only from same-repository `ai/issue-<n>` branches listed in the project's state; the poller rechecks membership before filing a fix-up and remembers rejected requests. Unverified PRs remain blocked.
