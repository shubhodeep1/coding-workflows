<!-- changelog: added -->
- **Roll `@stable` back to the last known good release in one dispatch.** The new `rollback-stable.yml` workflow moves the `stable` tag and the major tag (`v1`) back to an earlier release that is already signed, holds automatic re-releases, and dispatches `coding-workflows-stable-released` to every registered consumer.

The target is either `target_version` or, when that is blank, the highest release below the current one. It must be in the same major, on the `stable` branch, and its GitHub Release must carry both `sync-manifest.json` and `release-manifest.v1.json`, so consumers verify it without re-signing. Before any tag moves, the run writes a hold (`stable-hold.json` on branch `ai/stable-hold`) and cancels in-flight release and promote runs. While the hold is active, `auto-release-stable.yml` and the daily promote cycle skip (`reason=stable_hold_active`, and `reason=stable_hold_unknown` when the hold cannot be read). A deliberate release clears the hold after it publishes its tags. Every outcome logs one `STABLE_ROLLBACK` line and sends a CRITICAL Telegram message.

| The numbers that matter | Value |
| --- | --- |
| Dispatches needed to roll back | 1 (run `dry_run=true` first) |
| Extra API reads per auto-release / promote-cycle run | 1 |
| Kill switch | `STABLE_ROLLBACK_ENABLED=false` |

What this means for consumer repos: nothing to change. A rollback reaches you through the usual `coding-workflows-stable-released` dispatch, or through the 04:00 UTC sync, and the updater renders the older release from `refs/tags/stable`. Operators: see "Roll back @stable" in the README.
