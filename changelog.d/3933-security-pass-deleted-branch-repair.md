<!-- changelog: changed -->
- Made deleted-integration-branch security findings repairable through the normal automated fix loop.

The orchestrator now recreates a confirmed-absent integration branch only at the verified immutable final-PR head, verifies race winners without overwriting them, and clears stale final-delivery state before opening the consolidated fix issue. Repairs therefore advance the tree re-audited by the security pass and flow through replacement validation and final delivery.
