<!-- changelog: added -->
- **Interactive Claude Code sessions now read OpenRouter spend themselves with the `OR_MGMT_KEY` management key (CLAUDE.md §29).** Questions like "what did the review panel cost this month" get answered from OpenRouter's own numbers, not from list-price estimates.

When a task needs OpenRouter data, the session calls the management API directly, without asking first: `GET /api/v1/activity` for per-model daily cost and tokens, `GET /api/v1/keys` for per-key spend totals, and `GET /api/v1/credits` for account credit. Creating, deleting, disabling, or limiting keys and any billing change still need the user's approval in the §2 Q/A format. The key lives only in the session environment. No Actions workflow reads it, and §29.D forbids adding one that does. `README.md` lists it next to the other session-only credentials.

| The numbers that matter | Value |
| --- | --- |
| `/activity` history | last 30 completed UTC days (older dates return HTTP 400) |
| `/activity` granularity | day × model × provider endpoint, account-wide |
| `/keys` `usage_monthly` | current calendar month, not a rolling 30 days |

What this means for operators: add `OR_MGMT_KEY` to the Claude Code session environment to enable this. Without it, sessions say so once and carry on. The management API's account-wide totals do not provide per-workflow or per-repo splits; cost is pinned to a pipeline role only when that role is the sole user of a model slug. Some direct requests set `HTTP-Referer` or `X-Title` (`scripts/analyze_soft_errors.py`, `scripts/summarize_unselected_runs.py`), but those headers do not add a workflow or repo breakdown to the management totals.
