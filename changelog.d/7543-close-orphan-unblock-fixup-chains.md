<!-- changelog: fixed -->
- **Open unblock fix-up issues are now closed once the issue they were filed for is closed.** The orchestrate poll tick closes them as not planned with one comment, nearest the closed issue first, so a chain of fix-ups that grew behind a closed issue clears instead of staying open forever.

The unblock judge files a fix-up issue (`<!-- ai:unblock-fixup:v1 item=<n> round=<k> -->` on its first line) for a blocked issue `<n>` and waits on it. When `<n>` was closed another way (the implementer found the work already merged, a person closed it, a re-issue replaced it), the fix-up stayed open and blocked, the judge filed a fix-up of that fix-up, and the chain kept growing: 17 deep in `digital_pa`, with 64 of the 82 open fix-ups across five consumer repos (`digital_pa`, `hylifegroup.com`, `drhyg_ecommerce_automation`, `bitsafe.io`, `radateeree-resort.com`) sitting behind a closed issue on 2026-10-17. The new `close_orphan_fixup_chains_sweep` in `scripts/orchestrate_poll_process.sh` runs at the top of `run_unblock_scan`, so it covers both the full tick and the `UNBLOCK_SCAN_SWEEP_ONLY` tick, and it runs while `UNBLOCK_JUDGE_ENABLED=false` too. `scripts/orphan_fixup_close.py` decides; the shell only reads and closes.

| The numbers that matter | Value |
| --- | --- |
| Closes per tick (`ORPHAN_FIXUP_CLOSE_MAX_PER_TICK`) | 5, nearest the closed issue first; later batches of one chain about an hour apart (the grace applies to fix-ups the sweep closed) |
| Grace after the source issue closed (`ORPHAN_FIXUP_CLOSE_GRACE_MINUTES`) | 60 minutes |
| Extra API calls with nothing to close | 1 REST list page per 100 open issues, at most 5 pages |
| Extra API calls when something closes | 1 aliased GraphQL read per 20 unknown issues, 1 timeline read per fix-up checked, 1 close per closed fix-up |
| Switches | `ORPHAN_FIXUP_CLOSE_ENABLED` (default `true`), `ORPHAN_FIXUP_CLOSE_DRY_RUN` (default `false`) |

What this means for operators: nothing to set. Only issues authored by the pipeline login that carry no label other than a block label (`ai:blocked`, `ai:scope-blocked`, `ai:destructive-blocked`, `ai:needs-human`, `ai:unblock-closed`, `ai:security`, `ai:done`) are closed, and never a project issue, one with an open or unreadable linked pull request, one that was reopened, or one under a fix-up that stays open. Every timeline is read before anything closes, so a failed read closes nothing that tick. Re-issues and `Accepted with this follow-up` issues are new work and are left alone. Set `ORPHAN_FIXUP_CLOSE_DRY_RUN=true` to see `ORPHAN_FIXUP_CLOSE ... outcome=would_close` lines first, or `ORPHAN_FIXUP_CLOSE_ENABLED=false` to turn it off. Reopen a fix-up you still want; the sweep never closes it again.

### For contributors

The helper is staged with the other unblock scan helpers (`scripts/orphan_fixup_close.py` in `orchestrate_poll.yml` "Stage workflow support files"); a support checkout without it logs `ORPHAN_FIXUP_CLOSE outcome=skip reason=support_missing`. Project fix-ups (`ai:orchestrator-managed`, filed through `ai:unblock-fixup-request:v1`) and fix-ups of pull requests are not handled here. Tests: `tests/test_orphan_fixup_close.py`, run by the `Unblock judge, scan and orphan fix-up close tests` CI step.
