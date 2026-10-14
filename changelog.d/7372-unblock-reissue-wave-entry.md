<!-- changelog: fixed -->
- **A project no longer reads as failed after the unblock judge re-issues one of its wave issues.** The replacement now takes the original's place in the wave instead of sitting next to the closed original.

On 14 October the unblock judge re-issued #7357, a wave-1 issue of project #7355, as #7367 and closed #7357. The poller added #7367 to the wave but kept the closed #7357 there too. So the completion status turned `failed`, a CRITICAL "wave PR(s) closed without merge" alert went out, and the end-of-wave judge could no longer be skipped, even though #7367 was already done. Now a re-issue takes over the original's wave entry: same local id and dependents, new issue number, status `pending`, the same way stall recovery's `close_and_reissue` already does it. A re-issue filed before this fix is repaired on the next poll: the original entry is marked `skipped` with `unblock_superseded_by` set to its replacement. A re-issued security finding keeps its original open, so its replacement is still added next to it, and fix-up and descope requests are unchanged.

What this means for operators: a project whose blocked issue the unblock judge re-issues keeps going without a false failure alert. Project #7355 clears on the first poll after this merges.

### For contributors

`handle_unblock_judge_project_hooks` treats a trusted fix-up request as a re-issue when its title starts with `Re-issue of #<item>:` (written by `scripts/unblock_actions.py`) and its body has no `<!-- ai:security-finding:` marker. A re-issue no longer posts the "Waiting for fix-up" marker on the closed original. `UNBLOCK_PROJECT ... outcome=filed` gains `retarget=true|false`, and the repair logs `outcome=superseded`.
