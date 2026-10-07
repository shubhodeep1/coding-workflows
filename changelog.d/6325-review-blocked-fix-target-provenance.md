<!-- changelog: security -->
- **Review-blocked fixes now verify their PR target before writing.** The poller rejects fork or unrelated PR heads, checks the fetched branch tip against the PR head SHA, and rechecks the head before pushing a fix. Failed verification leaves the branch untouched for the next poll.
