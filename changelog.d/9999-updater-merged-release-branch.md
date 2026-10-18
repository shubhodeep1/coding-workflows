<!-- changelog: fixed -->
- **The daily workflow sync no longer fails after a release's update pull request has merged.** When that pull request has merged and its `auto/update-workflows-<sha12>` branch still exists, `update_workflows.yml` now logs `UPDATER_PR_DELIVERY outcome=skipped reason=release_pr_merged` and exits successfully instead of rewriting the branch.

On 2026-10-17 the scheduled 04:00 UTC sync failed in bitsafe.io and tele-funtoken-msg-scoring with `lease_mismatch_or_push_rejected`. The v1.36.0 update pull requests had merged about 30 minutes earlier. The only remaining change was a changelog fragment fold. The updater rebuilt its commit on the new default branch and force-pushed it to the merged branch, and the consumers' rulesets block that (`non_fast_forward`, and `deletion` rules out deleting the branch). The fold now waits for the next release's pull request. A deleted branch still gets a new pull request as before.

What this means for operators: a red `AI Update Workflows` run shortly after a release no longer appears for this case. The skip shows as a notice with the number of changed files it left for the next release.
