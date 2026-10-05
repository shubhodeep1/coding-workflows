<!-- changelog: fixed -->
- Review-blocked fixes now require a same-repository PR head and a verified origin-branch commit before preparing a writable worktree. Fork heads, moved branches, and failed fetches skip the fix push and retry on a later poll.
