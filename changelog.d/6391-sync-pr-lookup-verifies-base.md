<!-- changelog: security -->
- **Live Claude copy sync now verifies its PR target.** The post-push sync no longer mistakes a PR into another branch or from a fork for its PR into `main`. When no matching PR exists, it opens the intended PR instead of leaving the live copies out of date.
