<!-- changelog: added -->
- **Every stable release now publishes an attested release manifest.** After the version tag is pushed, the `release` job of `mark-stable.yml` and `test-and-mark-stable.yml` builds the manifest from the tagged commit (`scripts/release_manifest_publish.sh`), signs it with a GitHub build-provenance attestation, and uploads it to the GitHub Release as `release-manifest.v1.json`.

If building, attesting or uploading the manifest fails, the release job stops with a `RELEASE_MANIFEST outcome=failed reason=<token>` error before consumer repositories are notified; re-running the job is safe. Dry runs skip all three steps. Only the `release` job gains the `id-token: write` and `attestations: write` permissions.

What this means for consumers: nothing changes yet. The updater does not read the manifest; a later phase of the safeguarded updater project (#6902) verifies it before copying files.
