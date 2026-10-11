<!-- changelog: added -->
- **A verifier now checks a release tree against its release manifest and rejects anything the manifest does not vouch for.** `scripts/verify_release_manifest.py verify` requires the manifest to match the expected repository and release commit, then checks every listed file's sha256, size, git mode and symlink target. With `--paths-file` it also rejects any path the updater would read that is not in the manifest. Any parse error, mismatch, unreadable file or unexpected error rejects. The script prints one `UPDATER_MANIFEST_VERIFY outcome=ok|rejected reason=<token> count=<n>` line and exits non-zero on rejection.

The attestation signature on the manifest is checked separately, by the updater step that calls this script. The consumer updater runs both only when `UPDATER_VERIFY_RELEASE_MANIFEST` is `true` (default `false`).

What this means for operators: nothing to configure unless you turn that variable on.
