<!-- changelog: fixed -->
- **Stalled standalone issues are now recovered in repositories with no open orchestrator project.** Before this, when no `ai:orchestrator-tracking` issue was open, only the sweep-only recovery step (#7183) ran, so the full poller and the sweeps after its loop never ran there (issue #7284, an activation gap found on PR #7044).

The poll workflow started its full poller, which holds standalone stall recovery, only when a tracking issue was open. Now, when no tracking issue is open, the `Find active tracking issues` step asks GitHub one batched question: how many open issues carry a pipeline phase label, or have a standalone stall-state or clarification-questions marker in their comments. If any do, the full poller runs. Its tracking loop has nothing to do, and the steps after it run as usual:
- standalone stall recovery;
- the staged-support latch release and the unblock scan;
- the close-merged sweep and the standalone conflict sweep.

If the probe fails or returns something unexpected, the poller runs anyway and the step logs a warning. Each outcome is logged as `STANDALONE_STALL_PROBE`.

| The numbers that matter | Value |
| --- | --- |
| Extra GitHub API calls on a tick with no open tracking issue | 1 GraphQL request |
| Extra calls on a tick with an open tracking issue | 0 |
| New settings | none |

What this means for operators:
- `ENABLE_STANDALONE_STALL_RECOVERY=false` turns the probe off, and the poller then stays idle when no tracking issue is open, as before.
- In a repository with candidate standalone issues, every tick now does the full setup, including the `OPENROUTER_API_KEY` check. A missing key now fails the run instead of being silently skipped.
