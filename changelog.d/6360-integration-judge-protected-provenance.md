<!-- changelog: security -->
- Integration-conflict judge resolutions cannot introduce new lines in conflicted protected files.

The scheduled orchestrator poller rejects a resolution when a conflicted protected file contains lines absent from both merge sides. The protected set includes automation directories such as `scripts/` and `.github/`, agent-instruction files, and build, dependency, config and script files. Unprotected application code can still combine or synthesize lines, and rejected resolutions are not pushed.

What this means for operators: an integration-conflict judge cannot publish invented executable lines through a protected file during merge recovery.
