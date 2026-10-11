<!-- changelog: fixed -->
- **The unblock judge no longer closes a newly blocked item on its first round because of an old round on a different block.** Its 24-hour "still blocked" clock now counts only rounds on the same stop and fingerprint.

binance-blessings#249 had one unblock round on 2026-10-08 for a validation-sandbox block that was later cleared. When a new, unrelated harness error blocked it on 2026-10-11, the judge measured 24 hours from that old round and closed the orchestrator project immediately (`Terminal: still_blocked_24h`) without trying any verdict. `decide` in `scripts/unblock_ledger.py` now takes the clock only from rounds whose stop and fingerprint match the current block. The two-rounds-per-item and per-project caps still bound an item whose fingerprint keeps changing.

What this means for operators: a new block gets its own round before the judge can close the item.
