<!-- changelog: fixed -->
- **The post-merge standalone validation dispatch now retries removing `ai:orchestrator-validate-required`.** A transient GitHub failure no longer leaves the label on the issue, so a rerun of the `post-merge-validate-dispatch` job of `review_autofix.yml` no longer dispatches validation a second time.

The dispatch itself (`gh workflow run`) still makes one attempt, because a dispatch whose response was lost may already have started a run, and a retry would start a duplicate validation run. When the dispatch fails, the label stays on the issue as before. When the label removal still fails after the retries, the job logs the same warning as before.

Source: `analysis/workflow-optimization-2026-09-21.md`, finding BUG-002.
