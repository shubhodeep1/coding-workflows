<!-- changelog: fixed -->
- **The Claude pool broker asks `stable` when the `main` compare is unavailable.**

The broker's workflow-SHA check stopped at the first unavailable GitHub compare call (rate limit, 5xx, network error, bad JSON), so a SHA behind `stable` was refused with `403 workflow_sha_unverifiable` whenever the `main` compare failed. The check now goes on to the next branch after an unavailable answer: the SHA is trusted if any branch reports it `behind` or `identical` (HTTP 200), refused as `workflow_sha_unverifiable` if any branch was unavailable and none said yes, and refused as `workflow_sha_unverified` otherwise. Caching stays positive-only and the logging is unchanged.

| Setting | Value |
| --- | --- |
| `BROKER_SHA_CHECK_ALL_BRANCHES_ENABLED` default | unset = on (empty or `true` in any case is also on) |
| Off switch | `false` (any case) restores the old stop-at-first-unavailable check |
| Any other value | off, with one `CLAUDE_POOL broker workflow_sha_check_all_branches flag=invalid fallback=off` warning |

What this means for consumer repos: nothing to change. The fix takes effect after the Worker is redeployed from `main`; `scripts/claude_pool_token.sh` is unchanged.

Refs #9865
