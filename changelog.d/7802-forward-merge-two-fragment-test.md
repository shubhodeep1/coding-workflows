<!-- changelog: fixed -->
- **A regression test now covers the exact conflict that stopped PR #7794.** One merge on a forward-merge head leaves an `agents.d` fragment and a `changelog.d` fragment at index stages 1 and 3, and the test checks that both keep `main`'s copy and the merge commits without the model resolver.

The fix itself landed with heal #7796 in `scripts/review_conflict_prepare.sh` and is unchanged; the earlier test covered only one `changelog.d` fragment (Refs #7802).
