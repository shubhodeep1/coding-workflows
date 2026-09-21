<!-- changelog: fixed -->
- **Stable releases now push the assembled changelog.** The `release` job's changelog step named the branch as a bare `stable`, which git rejects because `stable` is also a tag.

Every release from the `stable` branch retried the changelog push four times, logged "dst refspec stable matches more than one", and released without folding `changelog.d/` (the v1.29.7 gate, run 35570966035, carried 113 unfolded fragments). The push in `test-and-mark-stable.yml` and `mark-stable.yml` now targets `HEAD:refs/heads/<branch>`, and the stale-tip check inside the retry loop reads the tip with `git ls-remote origin refs/heads/<branch>` instead of a `git fetch` that resolved to the tag and never refreshed `origin/stable`.

What this means for operators: the next stable release folds the accumulated fragments into `CHANGELOG.md` on the `stable` branch and the release notes are extracted from the assembled file. Releases from `main` were never affected, because no tag is named `main`.
