<!-- changelog: fixed -->
- The mandatory orchestrator security pass now recovers when an externally merged final PR deletes its integration branch.

When the integration branch is confirmed absent, the poller audits only the final PR's verified immutable head SHA and rechecks it after analysis. Transient branch-fetch failures still fail closed, and unavailable or mismatched PR-head evidence cannot fall back to the default branch. This keeps external-finalize and validation-complete projects gated until the intended project snapshot receives a clean pass.
