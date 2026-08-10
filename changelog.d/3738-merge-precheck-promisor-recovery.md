<!-- changelog: fixed -->
- **The AI review workflow no longer reports a partial-clone blob-fetch failure as a merge conflict.** `review_autofix.yml`'s `Detect merge conflicts` step now backfills blobs and retries the merge once on the promisor-fetch signature, and downgrades a surviving failure from a hard `::error::` to a `::warning::` fail-open.

The `codex-agent` checkout uses `filter: blob:none`, so the merge precheck's 3-way merge lazy-fetches blob content from the promisor remote. That batched fetch fails hard with exit 128 when any single object in the batch is unreachable server-side, even though the merge itself is well-formed. The step had no recovery for it, so a green run still showed `::error::Merge precheck failed (exit 128)` in its annotations, and `MERGE_CONFLICT=true` came from an infrastructure failure rather than a merge outcome. In the three observed runs the conflicts happened to be real — `scripts/review_conflict_prepare.sh`'s replay found 3, 8, and 6 unmerged paths on the same inputs the precheck had just failed on — so the harm was a false red annotation on successful runs plus a latent path to invoking the Codex resolver on a PR with nothing to resolve. The two sibling merge probes, the pre-review merge-topology gate in the same workflow and that same replay in `review_conflict_prepare.sh`, already carried this recovery; the late precheck was the only one left without it.

| The numbers that matter | Value |
| --- | --- |
| Merge probes with promisor recovery | 2 of 3 → 3 of 3 |
| Merge retries on the promisor signature | 1 |
| Exit-128 causes that keep the hard `::error::` | all except a surviving promisor fetch failure |
| Consumer runs observed with the signature | 3 (`tele-funtoken-msg-scoring` 31303907566, 31305600310, 31312967313 — all concluded `success`) |
| New contract tests | 7 in `tests/test_review_autofix_merge_precheck.py` |

What this means for operators: a review run whose merge precheck trips over a lazy blob fetch now recovers silently instead of printing a red annotation on a successful run, and the Codex conflict resolver stops being invoked on infrastructure noise. Genuine exit-128 causes — untracked-file collisions, a corrupt index, unrelated histories — still fail loudly with the same annotations as before.

### For contributors

`MERGE_CONFLICT=true` is deliberately kept on the fail-open path: `scripts/review_conflict_prepare.sh` runs its own merge replay with its own blob backfill and clears the flag when that replay finds nothing to resolve, so the resolver step remains the single place that decides whether a conflict is real. The retry builds its own `_merge_backfill_refspecs` array rather than reusing `_merge_base_refspecs`, which is defined only inside the shallow-clone deepen branch and would be unset under `set -u` on a full clone.
