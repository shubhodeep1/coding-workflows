<!-- changelog: changed -->
- **`/deploy-activate` now finds the activation log of a pull request in another repository.** The command named every pull request's log `docs/deploy-activation/pr-<N>.md`, whatever repository the pull request was in. In tele-funtoken-msg-scoring the log for shubhodeep1/mongo-explorer PR 37 is `pr-mongo-explorer-37.md`, so a re-run missed it and started the runbook again, and the `pr-37` name could also clash with that repository's own PR #37 (Refs shubhodeep1/tele-funtoken-msg-scoring#4627).

A pull request in another repository now gets `pr-<repo>-<N>`, with the bare repository name in kebab case. Before creating a log, the command looks in `docs/deploy-activation/` for one in either form and resumes it. It resumes an old `pr-<N>.md` for an external pull request only when that log's `- Reference:` line names the pull request. Issue, plan and feature names, and pull requests in the same repository, keep their names.

What this means for operators: existing logs are not renamed or moved. Consumers get the change on their next `@stable` sync.
