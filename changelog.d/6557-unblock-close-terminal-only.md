<!-- changelog: security -->
- **The unblock judge's model can no longer close or abandon a blocked item or project.** Closure now happens only after the fixed round and time limits, and only after the judge has re-read the item and confirmed it is still blocked.

The judge reads public issue and PR comments as evidence. Before this change, `close` was on its menu from the first round, so a comment that talked the model into choosing it could get a blocked issue closed, or a failed project abandoned, without its failure ever being fixed. `scripts/unblock_ledger.py` now offers `close` only once a deterministic terminal condition holds, and refuses a `close` verdict on any decision without one, even when that decision lists it. `scripts/unblock_judge.sh` re-reads the item just before it records a close, and stops without posting a verdict if the item was closed, unblocked, or moved to a different stop while the model ran, or if the read fails.

| The numbers that matter | Value |
| --- | --- |
| `close` offered when | item cap (2 rounds), project cap, 24 hours still blocked after the last round, or no other verdict left |
| `close` from the model before that | refused (`reason=invalid_verdict`); the item waits for the next round |
| Extra GitHub API calls | one issue read per run, only when closing |
| New log reasons | `block_state_changed` (`detail=closed\|unblocked\|stop_changed`), `block_state_recheck_unavailable` |

What this means for consumer repos: nothing to configure. Blocked items stay with the judge for its remaining rounds instead of being closed on the first one.

### For contributors

`prompts/mode-judge-unblock.txt` no longer lists `close` in the model's output schema. Tests: the close and terminal cases in `tests/test_unblock_judge.py`.
