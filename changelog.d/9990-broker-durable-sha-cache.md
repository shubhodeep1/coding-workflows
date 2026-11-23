<!-- changelog: fixed -->
- **The Claude pool broker keeps verified workflow SHAs across redeploys, and the daily pool report waits out a temporary broker refusal.**

The broker refused pool requests with `403 workflow_sha_unverifiable` whenever the account's GitHub REST budget was empty, even for a SHA it had verified minutes earlier, because its cache lived only in isolate memory and every pool-secret sync redeploys the Worker. A SHA GitHub reports as an ancestor of `main` or `stable` is now also stored in the Worker's existing `CHATGPT_ACCOUNTS` Durable Object (instance `workflow-sha-cache`, positive-only; a read failure falls through to the GitHub check and never grants). Every compare call logs `CLAUDE_POOL broker workflow_sha_compare` with its HTTP status and `x-ratelimit-remaining`, and Worker observability is on so those lines are kept. `scripts/claude_pool_daily_usage.sh send` no longer spends the day's report on such a refusal: with no probes and a `broker_refused_workflow_sha_unverifiable` or `broker_unavailable_NNN` reason it defers (`outcome=deferred`, `deferred=true`) for up to `CLAUDE_POOL_DAILY_USAGE_RETRY_HOURS` after the slot opens, capped at hour 23, then sends as before.

| The numbers that matter | Value |
| --- | --- |
| Durable verified-SHA TTL | 7 days (in-memory cache stays 6 hours) |
| Durable cache size cap | 256 entries |
| `CLAUDE_POOL_DAILY_USAGE_RETRY_HOURS` default | `2` (`0` turns deferral off; invalid values fall back to `2`) |
| Last hour a report can be deferred | 22 UTC (the window ends no later than hour 23) |

What this means for consumer repos: nothing to change. The broker part takes effect after the Worker is redeployed from `main`; the daily report runs only in coding-workflows.

Refs #9990
