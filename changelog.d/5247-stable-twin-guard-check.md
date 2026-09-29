<!-- changelog: security -->
- **The Claude twin sync-state check now runs on `stable`, and a hook or settings change reaches `stable` only after it landed on `main`.** Before, a PR into `stable` could change `.claude/settings.json` or a `.claude/hooks/**` file without its twin, and the review workflow could auto-merge it.

The CI step "Claude twin sync state (CLAUDE.md §28.C)" in `.github/workflows/ci.yml` used to skip every `stable` event. It now checks PRs into `stable` against the PR's `stable` base, and checks pushes to `stable` against `github.event.before`. On `stable`, `scripts/claude_twin_sync.py check` also gets the new `--guard-provenance-ref <main tip>`. With it, a changed guard path must equal `main`'s copy even when it matches its twin, so hook and settings content that never passed `main`'s owner-reviewed sync PR fails CI. A push to `stable` that `main` already contains (a `promote-main-to-stable.yml` promotion) is still skipped. That is decided by one compare read, and a failed read checks the range instead.

| The numbers that matter | Value |
| --- | --- |
| Events newly checked | PRs into `stable`, pushes to `stable` that are not a `main` commit |
| Guard paths held to `main`'s copy on `stable` | `.claude/hooks/**`, `.claude/settings.json`, `.claude/settings.local.json` |
| GitHub API calls added | 1 compare read per push to `stable` |
| Security finding | `stable-pr-guard-check-skipped` (#5247, A01:2021, medium) |

What this means for operators: a hook or settings fix meant for `stable` lands on `main` first, then reaches `stable` by promotion or a backport PR with the same content. A PR into `stable` that changes a `.claude/` file without its twin now fails CI. PRs into `main` and pushes to `main` are checked exactly as before.
