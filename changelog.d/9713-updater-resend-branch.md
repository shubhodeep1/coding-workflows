<!-- changelog: fixed -->
- **The consumer updater can now refresh a stale sync pull request in repositories that forbid force-pushes.** When a refresh of `auto/update-workflows-<sha12>` is refused, the updater moves the sync to a new `-r2` .. `-r5` branch and closes the old pull request.

Some consumer rulesets put `non_fast_forward` on every branch (`~ALL`). There, the updater's daily refresh of a conflicted or stale sync pull request failed with `GH013 ... Cannot force-push to this branch`, and the pull request stayed stuck until the next release (fun-token-multi-chain #889, run 44234623225). Now, when the refresh push is refused for that reason, the "Commit and push updates" step of `.github/workflows/update_workflows.yml` pushes the rebuilt sync to the first free `<branch>-r2` .. `-r5`. It opens the sync pull request from that branch with the same title and body, and the `verify` job handles auto-merge as usual. It then closes the old pull request, if the `GH_PAT` account opened it, with one comment linking the new one. Any other push failure, such as a lease mismatch, a network error or an auth error, still fails the run with `reason=lease_mismatch_or_push_rejected`.

| The numbers that matter | Value |
| --- | --- |
| New branches per run | at most 1 |
| Highest suffix | `-r5` (then `reason=resend_cap_reached`) |
| Branches deleted | none |
| Extra GitHub API calls on a resend | 1 (`gh pr close`) |
| New log prefix | `UPDATER_PR_RESEND` |

What this means for consumer repos: after the next `@stable` sync, a stuck sync pull request in a repository with a no-force-push ruleset is replaced by a fresh one on the next daily run, with no ruleset change and no human step. Old sync branches stay in place, because these rulesets also block deletion.

### For contributors

Later runs for the same release continue on the highest `-rN` branch on the remote, found with one `git ls-remote`. The review pipeline's updater-branch guards and the drift audit's release check accept the `-r2` .. `-r5` suffix, so a resend pull request is never edited, pushed to or merged by review. Tests: `tests/test_update_workflows_resend_branch.py`.
