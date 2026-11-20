<!-- changelog: changed -->
- **Stall recovery re-triggers review by dispatching the review workflow, not by pushing an empty commit.** The empty `stall recovery: re-trigger review for issue #N` commit is now only a fallback for when the dispatch fails.

When the poller finds an issue stalled at `ai:done` with an open implementation PR, both stall-recovery arms in `scripts/orchestrate_poll_process.sh` (project items and standalone issues) now call the existing review dispatch helper first, behind the same merge-train, capacity, wrong-PR and in-flight-review guards as before. Each empty commit used to start a full `CI` run on code that had not changed. The empty commit is pushed only when the dispatch itself fails, and that case is logged with the new action value `retrigger_review_empty_commit_fallback`. A live or already-dispatched review run still skips recovery without spending a recovery attempt.

| The numbers that matter | Value |
| --- | --- |
| Empty stall-recovery commits, 2026-11-10 to 2026-11-15 | 74 (12% of PR CI runs), each a full CI run |
| New action value | `retrigger_review_empty_commit_fallback` |
| API calls per re-trigger | up to 3 run listings and 1 `workflow_dispatch`, in place of a full CI run |

What this means for operators: stalled PRs are re-reviewed without a new commit on their branch, so their CI does not re-run and the branch history stays clean. Consumer repositories get the same behaviour on the next `@stable` sync.
