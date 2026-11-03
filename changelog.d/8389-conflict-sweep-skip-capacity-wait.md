<!-- changelog: fixed -->
- **The standalone PR conflict sweep no longer pages "resolve by hand" for PRs waiting for Claude capacity.** A conflicted PR labelled `ai:claude-capacity-wait` is now left to the capacity-resume pass.

On 2026-11-02 every Claude pool account hit its usage gate. Five conflicted PRs (#8239, #8346, #8348, #8373, #8389) carried `ai:claude-capacity-wait`, yet the conflict sweep in `scripts/orchestrate_poll_process.sh` dispatched a review for each of them every tick. Every one of those reviews deferred for capacity without running the resolver, and the sweep counted each deferral as a resolution attempt, so it sent the WARNING "still in merge conflict after 3 automatic resolution attempt(s)" for all five. When capacity returned, the resume pass removed the label at 23:08 UTC and re-dispatched them, and all five had resolved by 23:19 UTC. The sweep now skips such a PR before update-branch, dispatch and counting, and logs `STANDALONE_CONFLICT_STUCK pr=<N> outcome=skip reason=claude_capacity_wait`.

| The numbers that matter | Value |
| --- | --- |
| Deferred dispatches counted per PR | 9 (21:25 to 22:50 UTC) |
| PRs falsely paged | 5 |
| Extra API calls | 0 (the label comes from the PR read the sweep already makes) |

What this means for operators: a "resolve the conflicts by hand" WARNING now means the resolver really ran and failed, not that the Claude pool was full.
