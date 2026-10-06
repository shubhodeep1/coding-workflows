<!-- changelog: fixed -->
- **Review-blocked fixes now reach the isolated writer.** The judge passes its per-PR work tree through the sandbox's validated workspace path while keeping the checkout's Git directory available for snapshotting.

The judge previously set `GITHUB_WORKSPACE` to the per-PR work tree, which has no `.git`. Sandbox preparation rejected it before the fix writer could start. The checkout layout also ignores a stale inherited workspace path, so snapshots and transferred edits follow the tree the judge is editing. The sandbox's path checks and transfer rules are unchanged.
