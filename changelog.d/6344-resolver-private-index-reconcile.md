<!-- changelog: fixed -->
- **The source-repository conflict resolver now commits marker-free resolutions acknowledged only in its private index.** It stages initially unmerged, allowlisted paths on the real index only when the model's private-index result matches the validated worktree, including deletions, symlinks, and file modes.

Previously, a model could stage an already marker-free modify/delete conflict without editing its worktree file. The real index then retained unmerged entries and the final completeness guard refused the merge commit. Unacknowledged or mismatched resolutions still fail closed.
