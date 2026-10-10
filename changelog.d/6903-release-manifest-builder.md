<!-- changelog: added -->
- **A builder now writes the release manifest: the exact list of files the consumer updater may copy from a release.** `scripts/release_manifest.py build` records each file's path, sha256, size and git mode, refuses unexpected symlinks, path escapes and non-regular files, and writes byte-identical JSON for the same tree (schema `ai-memory/schemas/release_manifest.v1.json`).

Nothing calls the builder yet, so the updater and every release run behave exactly as before. A later phase of the safeguarded updater project (#6902) attests the manifest at release time and has the updater verify it before copying anything.

What this means for operators: nothing to configure; no workflow changes in this release.
