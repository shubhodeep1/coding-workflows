<!-- changelog: fixed -->
- **Release smoke PRs now reach review even when they overlap an older PR.** The merge-train gate no longer queues a PR marked `IS_SMOKE_TEST=true`; it also clears a prior queue label after retiring its marker. Ordinary overlapping PRs remain queued.
