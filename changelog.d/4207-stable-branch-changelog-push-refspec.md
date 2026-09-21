<!-- changelog: fixed -->
- **Stable releases now push the assembled changelog.** The `release` job named the source branch as a bare `stable`, which git refuses because `stable` is also the moving release tag.

Every release cut from the `stable` branch retried the changelog push four times, logged "dst refspec stable matches more than one", and then released without folding `changelog.d/` at all. The v1.29.7 gate (run 35570966035) left 113 fragments unfolded that way. The push in `test-and-mark-stable.yml` and `mark-stable.yml` now targets `HEAD:refs/heads/<branch>`, and both `git fetch` calls in the same step use an explicit `+refs/heads/<branch>:refs/remotes/origin/<branch>` refspec, so the retry rebase and the fail-open reset see the real branch tip instead of the tag.

What this means for operators: the next stable release folds the accumulated fragments into `CHANGELOG.md` on `stable` and extracts the release notes from the assembled file. Releases cut from `main` were never affected, because no tag is named `main`.
