<!-- changelog: security -->
- **The twin sync-state check now refuses `.claude/` hook and settings changes that did not come from a sync PR.** A PR that changed a `.claude/hooks/**` or `.claude/settings.json` file and its `workflow-templates/.claude/` twin to the same content used to pass CI and could reach `main` without the owner-only merge that guard sync PRs require.

The CI step "Claude twin sync state (CLAUDE.md §28.C)" runs `scripts/claude_twin_sync.py check`. For `.claude/hooks/**`, `.claude/settings.json`, and `.claude/settings.local.json` it now requires the new content and file mode to equal the twin on the base commit, the reviewed twin already on `main`. Matching the twin in the same PR no longer counts. On a PR into `main`, a guard path may change only from a same-repository `claude/claude-twin-sync-*` branch. On a push to `main`, the base is the push's `before` commit. Deleting a guard file always fails the check. Commands, scripts, and other `.claude/` files keep the old rule.

| The numbers that matter | Value |
| --- | --- |
| Guard paths | `.claude/hooks/**`, `.claude/settings.json`, `.claude/settings.local.json` |
| New `check` flags | `--event {push,pull_request}` (default `push`), `--pr-head-ref`, `--pr-head-repo`, `--repo` |
| Issue | #5246 (security audit finding `guard-sync-provenance-bypass`) |

What this means for operators: change a hook or setting by editing `workflow-templates/.claude/` only, then merge the sync PR it produces. A direct `.claude/` guard change now turns CI red, and the repository owner has to merge it by hand. `main` has no required status checks, so to let GitHub block such a PR on its own, add `lint` as a required check in the `main` ruleset. That check runs the PR's own copy of `.github/workflows/ci.yml` and `scripts/claude_twin_sync.py`, so it cannot stop a PR that also edits either file; review those files by hand when a PR changes them alongside a hook or setting.

### For contributors

`check_not_ahead` gains keyword-only `event`, `pr_head_ref`, `pr_head_repo`, and `base_repo` arguments, plus `event` and `sync_pr` result keys. `ci.yml` passes the PR head ref and repository through `env:` in `--flag=value` form. Nothing new calls the GitHub API.
