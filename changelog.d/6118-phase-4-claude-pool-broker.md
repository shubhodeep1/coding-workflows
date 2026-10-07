<!-- changelog: added -->
- **A token broker now hands Claude account tokens to trusted Actions jobs, and every AI wrapper grants the `id-token: write` permission it needs.** No role uses Claude yet; until the account secrets exist in `shubhodeep1/claude-workers`, the broker answers `503 pool_empty` and every role stays on codex.

Phase 4 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` adds the `claude-pool-broker` Cloudflare Worker (source `tools/claude-pool-broker/`, deployed at `https://claude-pool-broker.shubhodeep.workers.dev`). It verifies the caller's GitHub OIDC token (signature, issuer, audience `coding-workflows-claude-pool`, expiry, at most 10 minutes old), requires `repository_owner` `shubhodeep1`, a repository that is coding-workflows or listed in `.github/ai/consumer_repos.json`, and a `job_workflow_ref` under `shubhodeep1/coding-workflows/.github/workflows/`, and refuses anything else with `403` and a reason code. The new `.github/actions/claude-pool-token` action fetches the pool once per job, masks every token, probes each account with Haiku 4.5, writes the accounts under the `0.9` gate (least used first) to `$RUNNER_TEMP/claude-pool` for `scripts/ai_engine.sh`, and deletes them in its post step. It never fails a job: any problem is `available=false` with a reason, and the job runs codex.

| The numbers that matter | Value |
| --- | --- |
| Wrapper templates gaining `id-token: write` | 11 `workflow-templates/*.yml` |
| coding-workflows callers and workflows gaining it | 14 |
| OIDC token maximum age | 10 minutes |
| Consumer registry cache in the Worker | 10 minutes, last good copy kept |
| New GitHub API calls | 0 |

What this means for consumer repos: the next `@stable` sync adds `id-token: write` to the AI wrappers; nothing else changes until a role's cutover. Repos whose sync is stale keep running codex.

### For contributors

`scripts/claude_pool_token.sh` does the work for the Node 24 action (`main.js` / `post.js`); the OIDC and pool tokens never reach argv. `.github/ai/claude_engine.json` `broker_url` now points at the Worker. The account secrets (`CLAUDE_POOL_TOKEN_<NAME>`) and `CF_BROKER_DEPLOY_TOKEN` live only in `shubhodeep1/claude-workers`, whose `claude-pool-key-sync.yml` writes the Worker's `CLAUDE_POOL_TOKENS` secret; `tests/test_claude_pool_token.py` fails if any coding-workflows workflow or template mentions them. Worker tests run with vitest in `ci.yml`.
