<!-- changelog: fixed -->
- **A slow clone no longer cancels a release after stable has moved.** The `resolve-version` job in `mark-stable.yml` and `test-and-mark-stable.yml` now has 10 minutes instead of 2.

`resolve-version` checks out the full history (`fetch-depth: 0`) to read the tag list. On 2026-10-15 that clone took 87 seconds at 02:21 and more than 2 minutes at 10:04. The 10:04 run (test-and-mark-stable run 38717659089) was cancelled mid-clone after `promote-main-to-stable` had already fast-forwarded `stable`. That left `stable` with no release tag and no consumer update dispatch. The limit now matches the 10 minutes the promote job already gives its own full clone. A contract test keeps every full-clone job in both release workflows at 10 minutes or more.

| The numbers that matter | Value |
| --- | --- |
| `resolve-version` timeout | 2 → 10 minutes |
| Last good clone time | 87 s (run 38670511643) |

What this means for operators: a promotion that moves `stable` also publishes its release, so consumers receive the new pin without a manual re-run.
