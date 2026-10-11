<!-- changelog: added -->
- **A builder now writes the release manifest: the exact list of files the consumer updater may copy from a release.** `scripts/release_manifest.py build` records each file's path, sha256, size and git mode, refuses unexpected symlinks, path escapes and non-regular files, and writes byte-identical JSON for the same tree (schema `ai-memory/schemas/release_manifest.v1.json`).

The stable release jobs run the builder on every release (see the attested release manifest entry), and the consumer updater checks the result only when `UPDATER_VERIFY_RELEASE_MANIFEST` is `true`.

What this means for operators: nothing to configure for the builder itself.
