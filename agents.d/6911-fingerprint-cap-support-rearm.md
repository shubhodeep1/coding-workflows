<!-- agents: section="Workflow architecture" -->
Non-retryable cap re-arm on a new support commit (#6911): a non-retryable
review/autofix cap (`NON_RETRYABLE_FAILURE_REASONS`) gets one more run on the
same head when the gate's verified review-support SHA is on no trusted
`review-autofix-failure:v1` marker for that head yet, for at most 3 distinct
support SHAs per head (`DEFAULT_FAILURE_CAP_MAX_REARMS`); the gate logs
`AUTOFIX_FINGERPRINT_CAP_REARMED pr= head= fp= reason= prior_support= support=`.
`count_identical_failures` still filters markers by support version (#6625),
but when that leaves no counted marker it reads the head's trusted markers of
every support version for the re-arm decision, so an older non-retryable stop
past the bound keeps the head capped. The re-armed run's own marker carries
`support=` and restores the cap if it fails again. The 3-strikes cap, the
poller's `NOOP_RECOVERY_SKIP_FINGERPRINT_CAP` skip and the API calls are
unchanged; `autofix-identical-failure-count` adds `rearm=` and
`prior_support=` output lines.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `AUTOFIX_FINGERPRINT_CAP_REARMED`
LOG_PREFIX.name=AUTOFIX_FINGERPRINT_CAP_REARMED
