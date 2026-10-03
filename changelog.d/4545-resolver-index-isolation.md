<!-- changelog: fixed -->
- **Review/autofix keeps model-issued staging out of the live merge index during source-repository conflict resolution.** The resolver still fails closed on real-index drift and out-of-scope edits.

Each model attempt receives a disposable copy of the unmerged index. The resolver disables OpenCode session snapshots locally so conflicted paths remain visible to the model, while trusted validation and staging continue against the live merge index. Scope-check failures now include a fixed reason code without exposing path or exception text. Consumer-repository model invocations retain their existing index behavior.
