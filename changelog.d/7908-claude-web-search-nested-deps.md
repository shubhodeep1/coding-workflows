<!-- changelog: added -->
- **Claude can search the web, and subfolder Node projects get their dependencies.** Every Claude run, implement included, now has the `WebSearch` tool, and the dependency phase installs up to 6 subfolder Node projects that have a lockfile.

Claude's web search runs at Anthropic through the host relay. The sandbox container itself still has no network, `WebFetch` stays off, and the relay accepts only the exact search tool the pinned CLI sends. Web fetch, code execution and MCP tools are still refused. Separately, `codex_isolated_exec.sh prepare --deps` now finds Node projects below the repository root (for example `cloudflare/funtoken-competition-worker/`, whose missing install blocked tele-funtoken-msg-scoring#4770 with "npm registry unavailable for npm ci"). It installs them with `--ignore-scripts` through the same approved-registry proxy, and their `node_modules` stays in the sandbox copy.

| The numbers that matter | Value |
| --- | --- |
| Claude launch paths with `WebSearch` | 3 (`claude_run`, clarify, review editor) |
| Searches per WebSearch call (`max_uses`) | at most 8 |
| Nested Node projects installed per run | at most 6, at most 4 levels deep |
| Switch | `.github/ai/claude_engine.json` `web_search` (default `true`) |

What this means for operators: Claude can look up current documentation during clarify, plan, implement and review, and implement runs in repositories with a Worker or app in a subfolder now start with that project's dependencies installed. Set `web_search` to `false` in `.github/ai/claude_engine.json` to turn Claude's search off everywhere. Codex is unchanged.

### For contributors

The relay flag is `claude_anthropic_relay.py broker <socket> <token_file> <models> --allow-web-search`; without it nothing changes. A newer search tool version than `web_search_20250305` stays refused until the CLI pin and `WEB_SEARCH_TOOL_TYPE` move together. Nested projects need a lockfile; a `package.json` alone below the root is ignored.
