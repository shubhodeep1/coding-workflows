<!-- changelog: fixed -->
- **Review/autofix on integration-sync PRs no longer stops when the only problem is a conflict on the generated `.ai/.workspace_source_manifest.txt`.** The merge-conflict prepare step skipped that file on `orchestrator/project-*` branches. The resolver's sandbox excludes `.ai/`, so it refused the whole conflict set, and every review run on the PR failed the same way (PR #6043, run 37663738492).

`scripts/review_conflict_prepare.sh` now resolves the manifest on integration-sync branches as well. When the base branch deletes the manifest (index stages `1 2`) or this branch deletes it (`1 3`), it is untracked only if the merged `.gitignore` ignores it. Any other delete/modify conflict on the manifest is left for the resolver, which still refuses `.ai/`. On integration-sync branches a manifest-only merge is not committed right away:

- **Fingerprint check returns no paths:** the merge is committed as `[ai-merge-resolve]` and the model is skipped.
- **Fingerprint check adds paths:** the resolver runs on those paths only.
- **Fingerprint check cannot be verified:** the step logs `::error::Manifest union-merge: deferred integration-sync commit cannot verify fingerprints (reason=…)`, aborts the merge and fails.

No new environment variables or GitHub API calls. `CONFLICT_MANIFEST_UNION_ENABLED=false` still turns the whole behaviour off.

### For contributors

Review runs execute support scripts from the verified `main` commit, not from the PR head. This fix is on `orchestrator/project-6031`, so it unblocks PR #6043's review only once the same change is on `main`. Expect `review_conflict_prepare.sh` and `tests/test_conflict_manifest_union_contract.py` to conflict when `main` is next merged into this branch.
