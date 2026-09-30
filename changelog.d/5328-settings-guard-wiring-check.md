<!-- changelog: security -->
- **A settings-only change can no longer switch off a guard hook unnoticed.** The `Guard differential check (issue #5174)` step now also runs when `.claude/settings.json` or `workflow-templates/.claude/settings.json` changes. It fails a pull request whose change to a `*_guard.py` hook's wiring cannot be verified to still run the guard.

Security audit finding `settings-only-guard-disablement` (#5328) showed that a PR could rewire a guard's command to `python3 -c 'pass' "$CLAUDE_PROJECT_DIR"/.claude/hooks/<guard>.py` in both settings copies. The existing wiring tests only look for the hook path in the command string, so they accepted it, and `scripts/guard_differential.py` skipped the PR because no hook file changed. The check now compares every hook entry that names a guard. A base entry must keep a head counterpart with the same event, an unchanged command or exactly `python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/<hook>.py`, a matcher that covers the old one, a timeout no lower than before (an omitted timeout counts as Claude Code's documented default, 600 seconds for a command hook), and no other key changed. Setting `disableAllHooks`, changing the top-level `env` object (which reaches every hook, so it could set `CLAUDE_PR_MERGE_GUARD=off` or put another `python3` first on `PATH`), or deleting the settings file or making it unparseable, fails too. So does a PR against a base whose committed settings file is unparseable, because there is no base wiring to compare the head with. The file is parsed as strict JSON, as Claude Code reads it, so a `NaN` or `Infinity` timeout counts as unparseable.

| The numbers that matter | Value |
| --- | --- |
| Settings files checked | 2 (`.claude/settings.json`, `workflow-templates/.claude/settings.json`) |
| Guard entries wired in each today | 4 (`pr_merge_status_guard` ×2, `gh_api_write_guard`, `pr_watch_guard`) |
| Failure reasons | `removed`, `command`, `matcher`, `timeout`, `keys`, `disableAllHooks`, `env`, `unparseable`, `base-unparseable` |
| New GitHub API calls | 0 |

What this means for operators: a guard's wiring reaches `main` only in a form the check has verified, or after the PR lists the printed `settings:<file>:<event>:<matcher>:<hook>` identity under `Intended loosening:`. A listed change counts as loosening, so a #4785 sync that carries it waits for you.

### For contributors

A matcher change passes only when it provably keeps every tool the old matcher selected: the same matcher, a match-all one (absent, empty, or `*`), or a superset of a plain `A|B` tool-name list. New non-guard hook entries and `permissions.*` rule changes are not checked here. The output adds `GUARD_DIFFERENTIAL wiring_regression …` and `intended_wiring_change …` lines, and the summary line ends with `settings=… wiring_regressions=<n>`. Details are in `agents.md` under "Guard differential check".
