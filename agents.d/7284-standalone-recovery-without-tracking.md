<!-- agents: section="Workflow architecture" -->
Standalone stall recovery without a tracking project (issue #7284, activation
gap from PR #7044): `run_standalone_stall_recovery` runs only inside the full
poller, which `orchestrate_poll.yml` started only when an open
`ai:orchestrator-tracking` issue existed. When there is none, the
`Find active tracking issues` step (`id: find_tracking`) now makes one aliased
GraphQL count probe while `ENABLE_STANDALONE_STALL_RECOVERY` is on (default
`true`, same truthy rule as the poller). It counts open issues for each of the
poller's seven pipeline labels, plus open issues whose comments carry
`AI_STANDALONE_STALL_STATE_V1` or `ai:clarification-questions`. These are the
same searches as `_fetch_standalone_marker_issues_graphql`. Any count above
`0` sets `has_work=true`, so the full poller runs with an empty
`tracking_issues.json`; its post-loop runs standalone recovery, the
staged-support latch release, the unblock scan, the close-merged sweep and the
standalone conflict sweep. A failed or malformed probe also sets
`has_work=true` (fail open, with a warning). All counts `0`, or the switch
off, keep `has_work=false`, and the no-project sweep steps run as before. The
#7183 `standalone_recovery` output is unchanged (`true` while the switch is on),
so on an all-zero probe "Run standalone stall recovery without active
projects" still runs recovery. `has_tracking` still means an open tracking
issue exists. Only counts are read and logged.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `STANDALONE_STALL_PROBE` (`.github/workflows/orchestrate_poll.yml` "Find active tracking issues", only when no tracking issue is open: `outcome=candidates|none labeled= state_marker= clarify_marker=`, `outcome=skip reason=disabled`, `outcome=probe_failed reason=graphql_failed|malformed`)
LOG_PREFIX.name=STANDALONE_STALL_PROBE
