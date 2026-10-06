<!-- changelog: fixed -->
- **Orchestrator state comments from other users no longer control project progress or resolver context.** The poller reads V1/V2 state only from the account authenticated by `GH_PAT`, and skips the tick rather than reconstructing state when it cannot verify that account. Integration-sync conflict preparation ignores other users' V1 state comments.

After rotating `GH_PAT` to a different account, re-post old state comments from the new account (or retain the original account); otherwise legacy projects without trusted state may enter the existing reconstruction path. No state schema or scheduler changes are required.
