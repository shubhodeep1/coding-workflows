<!-- changelog: security -->
- **Review-blocked judge merges now fail closed when a pull request head changes after evaluation.** The poller compares the live head with the judged snapshot, binds automatic and direct squash merges with `--match-head-commit`, and leaves stale-head issues in `ai:review-blocked` without creating deferred follow-ups.
