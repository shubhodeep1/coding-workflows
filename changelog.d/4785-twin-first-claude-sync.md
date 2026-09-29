<!-- changelog: added -->
- **`.claude/` changes land without a watched session: unattended sessions edit only the `workflow-templates/.claude/` twin, and a new workflow copies each change into `.claude/` through one sync PR after it merges.**

Claude Code never auto-approves an edit under `.claude/**`, so until now every `/implement-plan-claude` phase that touched a hook, a setting, a command, or a session script stopped at `Status: BLOCKED` until an operator-watched session applied it. CLAUDE.md §28.C now makes the twin the only place unattended sessions edit. The new `.github/workflows/claude-twin-sync.yml` runs `scripts/claude_twin_sync.py` after a twin change reaches `main` and opens (or updates) one PR on `claude/claude-twin-sync-<sha>`. A sync PR that changes only commands or scripts merges itself once every check on its head passed. One that changes `.claude/hooks/**` or `.claude/settings.json`, or that finds a `.claude/` file edited directly (a conflict, never overwritten), is labelled `ai:claude-sync-approval`, alerted on Telegram, and left for the repository owner to review and merge.

| The numbers that matter | Value |
| --- | --- |
| Triggers | push to `main` touching `workflow-templates/.claude/**`, hourly at :41, `workflow_dispatch`, `CI` completed on a sync branch, a review on a sync PR |
| Guard paths that need the owner | `.claude/hooks/**`, `.claude/settings.json`, `.claude/settings.local.json` |
| Files never synced (`UPSTREAM_ONLY_PATHS`) | 6: five consumer-variant commands and `claude-issue-pickup.md` |
| Twin history searched for the conflict rule | up to 500 revisions per file |
| New label / status | `ai:claude-sync-approval` / `claude-twin-sync/owner-approval` |

What this means for operators: phases that change `.claude/**` no longer wait for a watched session. Expect one small sync PR after each such project merges. Command and script sync PRs need nothing from you. Hook and settings sync PRs arrive with a Telegram alert, and you review and merge them yourself. The workflow never approves or merges them, and `review_autofix.yml` and the catch-all fixer sweep skip every sync branch. Consumer repos are unaffected: they already receive the twin at `@stable` through `update_workflows.yml`.

### For contributors

Tests now load the twin, and every former byte-parity assert calls `tests/claude_twin_state.py::assert_claude_not_ahead`: the twin may be ahead of `.claude/` while a sync PR is pending, but `.claude/` may never be ahead of it. The new CI step "Claude twin sync state (CLAUDE.md §28.C)" enforces that on every PR into `main` and every push to `main` with `scripts/claude_twin_sync.py check` (a `stable` promotion spans many already-checked PRs and is skipped). Because `GH_PAT` authenticates as the owner account, GitHub will not let the owner approve a sync PR it opened, so the `claude-twin-sync/owner-approval` status stays `pending` and the owner merges by hand. Making that status a required check in the `main` ruleset is an optional operator step.
