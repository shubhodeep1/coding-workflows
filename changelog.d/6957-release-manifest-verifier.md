<!-- changelog: added -->
- **A verifier now checks a release tree against its release manifest and rejects anything the manifest does not vouch for.** `scripts/verify_release_manifest.py verify` requires the manifest to match the expected repository and release commit, then checks every listed file's sha256, size, git mode and symlink target. With `--paths-file` it also rejects any path the updater would read that is not in the manifest. Any parse error, mismatch, unreadable file or unexpected error rejects. The script prints one `UPDATER_MANIFEST_VERIFY outcome=ok|rejected reason=<token> count=<n>` line and exits non-zero on rejection.

The attestation signature on the manifest is checked separately, by the step that will call this script. A later phase of the safeguarded updater project (#6902) wires both into the consumer updater behind a default-off flag.

What this means for operators: nothing to configure; nothing calls the verifier yet, and there are no workflow changes in this release.
