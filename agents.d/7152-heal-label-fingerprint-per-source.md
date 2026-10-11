<!-- agents: section="Workflow architecture" -->
Workflow failure heal reports for an orchestrator project carry its validation
runs: `select_failed_runs` (used by `build_issue_payload`) also picks failed runs
whose display title ends in `[tracking:<issue number>]`, the validate wrappers'
run name. A label-escalation report that still links no run, or whose linked
runs' job logs were all unreadable, is fingerprinted on
`label:<label>|source:<repo>#<n>` instead of `label:<label>`, so run-less
reports from different issues or repositories no longer share one heal lineage
(binance-blessings#249's first `ai:harness-broken` report was capped at
generation 4 by three unrelated heals).
