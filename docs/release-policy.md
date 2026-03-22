# Release Policy

## Semantic Versioning

All releases follow semver:

- **Patch** (`v1.0.x`): Bug fixes, no interface changes
- **Minor** (`v1.x.0`): Additive features, backward compatible
- **Major** (`vX.0.0`): Breaking changes to workflow inputs/secrets/behavior

## Release Channels

### Stable (`@stable` / `@v1`)
- Production-ready releases
- Updated only after canary validation passes
- Consumer repos should pin to this channel

### Canary (`@canary`)
- Pre-stable testing channel
- Pilot repos validate changes here first
- Promoted to stable after acceptance criteria pass

### Immutable Tags (`@v1.0.0`)
- For repos requiring strict reproducibility
- Never moved after creation

## Release Process

1. Create a release branch from `main`
2. Run CI validation (YAML lint, script lint, contract tests)
3. Tag as canary: `git tag -f canary && git push -f origin canary`
4. Validate on 1-2 pilot repos through full issue lifecycle
5. If pass: tag stable release (`v1.x.x`) and update `stable` pointer
6. If fail: fix and repeat from step 2

## Rollback

1. Repoint `stable` tag to the previous known-good release
2. Announce rollback in changelog
3. Run smoke tests on canary + one production repo
