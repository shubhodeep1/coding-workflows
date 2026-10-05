<!-- changelog: fixed -->
- Review-blocked fixes now require a same-repository PR head and a verified origin-branch commit before preparing a writable worktree. Fork heads cannot drive a judge decision; moved branches or failed fetches defer the judge without consuming a fix retry and are checked again on the next poll.
