<!-- changelog: fixed -->
- **The late merge-conflict precheck in `review_autofix.yml` no longer turns a blobless-clone fetch failure into a hard `::error::` and a phantom merge conflict.** It now backfills blobs and retries the merge once, the same way the pre-review merge topology gate and `scripts/review_conflict_prepare.sh` already did.

The `Detect merge conflicts` step runs `git merge --no-commit --no-ff origin/<base>` against a checkout made with `filter: blob:none`. That 3-way merge lazy-fetches blob content from the promisor remote, and the batched on-demand fetch fails with exit 128 when any single OID in the batch is unreachable server-side, emitting `not our ref` / `could not fetch … from promisor remote`. The step read that as a merge outcome: it printed `::error::Merge precheck failed (exit 128)` on runs that otherwise concluded `success`, then set `MERGE_CONFLICT=true` off an infrastructure failure and fired the Codex conflict resolver against a PR with no conflict. The step's own header comment already named this as the hazard to avoid. Observed on `shubhodeep1/tele-funtoken-msg-scoring` runs 31303907566, 31305600310, and 31312967313, all of them green.

On that stderr signature the step now unsets `remote.origin.partialclonefilter`, runs `git fetch --no-tags --prune --refetch origin` against the base branch (plus `TARGET_BRANCH` when it is set), and retries the merge once so every blob is materialised locally. A promisor failure that survives the retry is downgraded from `::error::` to a `::warning::` fail-open, matching the wording the pre-review gate already uses. `MERGE_CONFLICT` stays `true` in that case, so the conflict-resolver path still runs and `review_conflict_prepare.sh` clears the flag after its own replay finds nothing to resolve.

| The numbers that matter | Value |
| --- | --- |
| Merge retries after blob backfill | 1 |
| Refs refetched | base branch, plus `TARGET_BRANCH` when set |
| Config key unset before the retry | `remote.origin.partialclonefilter` |
| Annotation on a surviving promisor failure | `::warning::` (was `::error::`) |
| `MERGE_CONFLICT` on a surviving promisor failure | `true`, cleared later by `review_conflict_prepare.sh` |

What this means for operators: review-autofix runs on repos with blobless checkouts stop showing a red merge-precheck annotation on green runs, and stop spending a Codex resolver invocation on a conflict that does not exist. Genuine exit-128 aborts and `refusing to merge unrelated histories` still surface as `::error::` exactly as before.

### For contributors

The backfill block sits between the initial merge and the `MERGE_STASH` restore loop, so the retry runs against the same clean working tree the first merge saw. It builds its own `_merge_backfill_refspecs` array rather than reusing `_merge_base_refspecs`, which the deepen block above only defines on shallow clones and would be unset under `set -u` otherwise. The initial promisor stderr is captured before the retry and appended to the annotation when the post-retry stderr differs, so the log keeps both halves of the failure.
