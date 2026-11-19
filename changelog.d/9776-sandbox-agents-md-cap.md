<!-- changelog: fixed -->
- **The orchestrate-poll review-blocked tests now show the reason when they fail.** When the review-blocked judge was skipped, the poller still exited 0, so 33 tests failed with empty assert messages. Their stdout assertions now include the tail of the poller's stdout. A new test checks that the poller test sandbox copies `agents.md` through the existing 524,288-byte bound from #9753. That bound keeps the inline judge prompt under codex's 1048576-character stdin cap. Runtime behaviour is unchanged.

Refs #9776.
