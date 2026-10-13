<!-- agents: section="Workflow architecture" -->
Consumer sync verification (#7128): the `release` jobs of `test-and-mark-stable.yml` and `mark-stable.yml` run `scripts/build_sync_manifest.py build` on the final release commit, attest the manifest with `actions/attest-build-provenance@v3` before the tag step (`id-token: write`, `attestations: write`), and upload `sync-manifest.json` to the release (`--clobber`; a rerun rebuilds it byte for byte). The attest and upload steps run only when the release is published (`!inputs.dry_run` plus the `release_switch` and `release_tagging` outputs), like the release-manifest steps. The manifest schema is `coding-workflows.sync-manifest.v1`. It carries `files` (every root-level entry plus `workflow-templates/` and `scripts/`: a sha256 per regular file, `symlink:<target>` per symlink) and `allowed_consumer_paths`. In an allowlist entry `*` does not match `/`.

In `update_workflows.yml`, the step "Verify signed sync manifest" (`verify_manifest`) runs directly after the fetch and before any upstream script. It performs four checks:
- the `vX.Y.Z` tag whose peeled commit equals the fetched `stable` SHA;
- `gh release download` of that tag's `sync-manifest.json`;
- `gh attestation verify` with the identity regex for the two release workflows on `refs/heads/stable` and `--deny-self-hosted-runners`;
- the manifest's commit, version and exact file set, compared by an inline verifier that imports nothing from the tree it checks.

"Commit and push updates" (`commit_push`) rejects any staged path outside the allowlist. It then delivers through `ai-sync/stable`:
- an open PR is reused only when its tree already matches; otherwise it is closed and a new PR is opened, because a push to an open PR fires `synchronize` and the head gate would withdraw auto-merge. The branch push is leased to the tip read just before it (`reason=lease_mismatch_or_push_rejected` on a race);
- a PR left `awaiting_merge` sends the Telegram notification at `WARNING`;
- the updater enables auto-merge, falls back to a direct merge, or else leaves the PR `awaiting_merge`;
- `update_via_pull_request: false` restores the direct push.

The consumer template gates the job on `vars.AI_WORKFLOW_AUTO_UPDATE_ENABLED != 'false'` and grants `pull-requests: write`. The review gate (`scripts/review_autofix_step_evaluate_gate.sh`, before the deterministic skip) runs `scripts/review_workflow_sync_skip.sh` from the verified head-gate checkout. It skips the panel (`SKIP_REASON=workflow_sync_managed`) only for an `ai-sync/stable` head in this repository with a complete file list (rename sources included) inside the allowlist and no `force-review`, and it never sets `DETERMINISTIC_SKIP`, labels, comments, posts a status or merges. An `ai-sync/stable` head that fails that check is reviewed normally: the gate drops any deterministic skip it would otherwise get (`AUTOFIX_GATE_DET_SKIP_SUPPRESSED reason=workflow_sync_unmanaged`), so it never reaches `deterministic-skip-merge`. Both checks share one paginated `/pulls/{n}/files` read (`gate_fetch_pr_files`). The skip posts no `ai-review/head-gate` success, so a consumer that requires that check merges the sync PR by hand. Failure codes:
- `ERR_SYNC_MANIFEST_NO_VERSION_TAG`
- `ERR_SYNC_MANIFEST_MISSING`
- `ERR_SYNC_MANIFEST_SIGNATURE`
- `ERR_SYNC_MANIFEST_COMMIT_MISMATCH`
- `ERR_SYNC_MANIFEST_FILE_MISMATCH`
- `ERR_SYNC_PATH_NOT_ALLOWED`

<!-- agents: section="Stable log prefixes (contractual)" -->
- `SYNC_MANIFEST` (`scripts/build_sync_manifest.py` and the release/updater steps: `outcome=built|uploaded|upload_failed|verified|rejected reason= version= commit= files=`)
- `SYNC_DELIVERY` (`update_workflows.yml` "Commit and push updates": `outcome=pushed|merged|auto_merge_enabled|awaiting_merge|replaced_stale_pr|failed pr= branch=ai-sync/stable version= reused=`)
- `AUTOFIX_GATE_WORKFLOW_SYNC_SKIP` (`review_autofix.yml` gate: `pr= head_ref= outcome=skipped|review files= reason=`)
LOG_PREFIX.name=SYNC_MANIFEST
LOG_PREFIX.name=SYNC_DELIVERY
LOG_PREFIX.name=AUTOFIX_GATE_WORKFLOW_SYNC_SKIP
