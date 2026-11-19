<!-- changelog: fixed -->
- **The orchestrate-poll review-blocked tests pass again on `stable`.** Their test sandbox copied the repository's full `agents.md` (about 994,000 characters after the release fold), which pushed the review-blocked judge prompt past codex's 1048576-character stdin cap. The poller then skipped the judge and exited 0, so 33 tests failed with empty assert messages. The sandbox copy of `agents.md` is now cut to 262,144 characters, and failing review-blocked assertions show the tail of the poller's stdout. Runtime behaviour is unchanged.

Refs #9776.
