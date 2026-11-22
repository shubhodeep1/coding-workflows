<!-- changelog: fixed -->
- **The integration fingerprint verifier now accepts a file the default branch deleted after capture.** A merged sub-issue's `must_contain` on that file no longer blocks the integration-sync resolver from taking the default branch's deletion. Refs #9421.

`scripts/verify_integration_fingerprints.py` used to count a `must_contain` on a missing file as a violation in every case. When the default branch deleted or moved a file after a sub-issue's fingerprint was captured, every resolver attempt that took the deletion was rejected. Project #9421 hit this when main deleted `.github/workflows/lint-pr-body-auto-close.yml` in #9698.

The verifier now treats that `must_contain` as satisfied and logs `FINGERPRINT_BASE_DELETION_SUPERSEDED_V1 issue=#<n> pr=#<p> file=<path> commit=<sha12> base_ref=<label>` on stderr, but only when all of these hold:
- The path is absent from the base history.
- The newest commit in that history deleting the path is dated after the fingerprint's `captured_at`.
- That commit is not an `[ai-merge-resolve]` resolver commit.

The base history is read from the `--ref` in ref mode. In working-tree mode it is read from `MERGE_HEAD`, or from `refs/remotes/origin/$BASE_BRANCH` when no merge is in progress. Compare-against-baseline mode now also reaches this check.

Every git error, every missing input and a deletion dated before capture keep the old violation. `INTEGRATION_FINGERPRINT_BASE_DELETION_SUPERSEDE_ENABLED=false` (or any value other than unset, empty or `true`) turns the check off. A `must_not_contain` on a deleted path was already satisfied and is unchanged.
