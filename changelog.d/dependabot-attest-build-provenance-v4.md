<!-- changelog: changed -->
- **The release jobs now attest their manifests with `actions/attest-build-provenance@v4`.** The four attestation steps in `.github/workflows/test-and-mark-stable.yml` and `.github/workflows/mark-stable.yml` (two per workflow: "Attest sync manifest" and "Attest release manifest") moved from `@v3` to `@v4`.

The step names, `if:` release gates, `subject-path` inputs and the `release` job's `id-token: write` / `attestations: write` permissions are unchanged, and the pin is still a major-version tag. A failed sync-manifest attestation still fails the job before any tag is pushed. Upstream, v4 is a wrapper on top of `actions/attest`.

What this means for consumer repos: nothing to do. The `update_workflows.yml` sync keeps verifying the attested `sync-manifest.json` and `release-manifest.v1.json` assets the same way.

### For contributors

`tests/test_release_sync_manifest_contract.py` now expects `uses: actions/attest-build-provenance@v4` in the "Attest sync manifest" step, and the two `agents.md` mentions of the pin read `@v4`.
