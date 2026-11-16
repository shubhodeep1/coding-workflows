<!-- changelog: fixed -->
- **The unblock judge closes nothing-to-implement issues even when no fix-up wait is pending.** A standalone `ai:blocked` issue whose newest trusted "Planning blocked" comment gives the reason `work is already on the planning ref; nothing to implement` is now closed as completed. This applies when the issue has no fix-up wait, or has one that finished before the block. Before, it stayed open and raised a CRITICAL escalation for work that was already done.

Before this change, `scripts/unblock_judge.sh` ran this close only right after a fix-up wait ended. #9215 was blocked at 07:46Z, after its fix-up wait had already finished at 03:49Z, so the judge escalated it and it had to be closed by hand. The judge now runs the same close, with the same checks, after its deterministic exits and before the model verdict:

- the block comes from the pipeline login and carries the exact reason line;
- no trusted implementation plan was posted after the block;
- the issue has no tracking issue (orchestrator items are unchanged);
- the block is newer than the time the latest fix-up wait ended (its last update when it was already done, the fix-up's close time when it closed without merging, or this run when the judge times it out), so a block filed while the fix-up was still unresolved stays with the judge.

The comment says no fix-up is pending. The marker and the existing `op=nothing_to_implement` log line carry `fixup=none`. A `fixup=none` marker counts as already posted only if it is no older than the newest block. The fix-up-wait path is unchanged. Setting `UNBLOCK_NOTHING_TO_IMPLEMENT_CLOSE_ENABLED=false` (default `true`) turns off both closes.

Refs #9518.
