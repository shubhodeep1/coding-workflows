<!-- changelog: fixed -->
- **Standalone stall recovery now records why a merge retry did not merge.** The `attempt_merge` step of `run_standalone_stall_recovery` in `scripts/orchestrate_poll_process.sh` used to run its merge gates in one chain and report only "attempted merge retry", and a failed merge call was hidden. It now names the first gate that refused (`state_not_open`, `not_mergeable`, `bad_head_sha`, `checks_incomplete`, `base_not_fresh` or `merge_call_failed`) in a `STANDALONE_STALL_MERGE` log line, in the effective action `attempt_merge_refused_<reason>` and at the end of the DEBUG notification. The gates, their order, the merge-lease wait and the recovery-attempt count are unchanged.

Refs #9715, #9520.
