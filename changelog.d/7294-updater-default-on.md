<!-- changelog: changed -->
- **Consumer repos now verify each release's attested manifest and receive workflow updates as a verified pull request by default.**

`UPDATER_VERIFY_RELEASE_MANIFEST` and `UPDATER_PR_DELIVERY_ENABLED` now default to `true` in `.github/workflows/update_workflows.yml`. On the next sync, each consumer's `ai-update-workflows` run checks the `release-manifest.v1.json` attestation of the stable release before it writes any file. It then opens or refreshes one pull request from `auto/update-workflows-<release-sha-12>` instead of pushing to the default branch. The `verify` job compares the pull request with the attested release and enables auto-merge only after that check passes. A dry run of the verify step against v1.32.0 passed (`UPDATER_MANIFEST_VERIFY outcome=ok reason=ok tag=v1.32.0`, 53 paths).

The verify job now posts its `ai-update-workflows/verify` statuses (pending, success and failure) with `GH_PAT` when the job token is refused. A consumer wrapper written before the template granted `statuses: write` would otherwise leave its first update pull request, the one that adds the permission, waiting for a human.

| The numbers that matter | Value |
| --- | --- |
| Consumer repos in `.github/ai/consumer_repos.json` | 13 |
| First release that passes verification | v1.32.0 |

What this means for operators: each consumer's `GH_PAT` must be able to open pull requests and enable auto-merge in that repository, and the repository must allow auto-merge. Set both variables to `false` in a consumer to go back to the unverified direct push. `UPDATER_PR_DELIVERY_ENABLED=false` alone pushes directly after verifying the manifest, and `UPDATER_VERIFY_RELEASE_MANIFEST=false` alone still opens a pull request, which then waits for a human because auto-merge needs a verified release.
