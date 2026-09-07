<!-- changelog: fixed -->
- **Consumer-repo artifact cleanup no longer deletes a file the repo actually tracks.** A repo-owned root-level `agents.md` survives the review, judge, and orchestrator commit paths instead of being silently removed.

Three cleanup blocks removed workflow-staged artifacts from a consumer repo's working tree by name, without checking whether the repo tracked that path. Each is followed by a `git add -u` / `git add -A` staging pass, so the working-tree removal was recorded in the commit as a real deletion. `agents.md` is the collision that matters: CLAUDE.md §22.C and §24.F require DigitalOcean and Cloudflare resource IDs to live in the repo's root agents file, and the pipeline stages an artifact at exactly that path. Each block now skips any path reported by `git ls-files --error-unmatch` and logs `Preserving repo-tracked path during artifact cleanup: <path>`; untracked artifacts are still removed exactly as before.

| The numbers that matter | Value |
| --- | --- |
| Scripts fixed | `review_conflict_resolve.sh`, `review_rb_judge.sh`, `orchestrate_poll_process.sh` |
| Guarded paths per block | 5, 16, and 12 |
| Incident | `shubhodeep1/binance-blessings` PR #255, merge commit `b974f8b` |
| Regression test | `tests/test_consumer_artifact_cleanup_preserves_tracked.py` |

What this means for consumer repos: a tracked `agents.md`, `ai_pipeline.md`, `unattended_system_instructions.md`, or consumer-owned `scripts/` file is no longer deleted by an AI commit, and the false `⚠️ Editor changes lost` retry loop that followed such a deletion no longer starts.

### For contributors

The merge-conflict resolver deleted `binance-blessings`'s tracked `agents.md`, which carried the production App Platform ID, even though both merge parents still had the file. The next review round's editor tried to restore it, the restore was wiped by the "editor may not create new files" cleanup in `review_commit_changes.sh`, and the run dead-ended with `DID_COMMIT=false` and `EDITOR_CHANGES_LOST=true` (AI Review run 34099352704). `review_commit_changes.sh` already carried the tracked-path guard; the three other blocks did not. This is the same bug class as the ~10,700-line deletion in PRs #917/#931, where the remedy was the git-remote-URL gate. That gate only protects the coding-workflows checkout itself, so the per-path guard is the second layer that covers consumer repos legitimately owning one of these names.
