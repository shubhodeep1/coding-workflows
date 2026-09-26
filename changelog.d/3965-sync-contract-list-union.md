<!-- changelog: changed -->
- **Contract entrypoint-list appends can now sync without an expensive review/autofix resolver dispatch.** The orchestrator poller deterministically keeps both sides of eligible `read_entrypoints` and `write_entrypoints` additions, validates the merged YAML, and pushes the ordinary two-parent sync merge itself.

The path is default-on through `ORCH_SYNC_CONTRACT_LIST_UNION_ENABLED=true`; setting the repo variable to `false` restores the previous resolver flow exactly. Unsupported paths, non-additive edits, invalid YAML, missing PyYAML, helper failures, and exhausted push retries all fail open to that existing flow.

| The numbers that matter | Value |
| --- | --- |
| Motivating tracking issue / final PR | #3862 / #3868 |
| Poll run that observed the two-line conflict | 33581471652 |
| Resolver run previously required | 33581805826 |
| Deterministic push attempts | 3 |

What this means for operators: adjacent contract-list appends no longer consume the single default integration-resolver attempt or its model runtime. Successful polls log `SYNC_LIST_UNION_V1` and record `merged-deterministic`, the resolved paths, and the UTC resolution time in sync state.

Refs #3965.
