<!-- changelog: changed -->
- **The orchestrator poller makes fewer GitHub API reads in two places, with no change in behaviour.** When it checks a project's final pull request without a cached copy, and when it re-issues an `ai:implementation-failed` issue, it now reads the object once instead of once per field.

`finalize_integration_merge_if_needed` in `scripts/orchestrate_poll_process.sh` read `pulls/<n>` twice, for the state and for `merged_at`. The implementation-failed sweep read `issues/<n>` twice, for the title and the body. Each now reads once and takes both fields from that response. A failed read still leaves both fields empty, and a missing or null field still reads as the literal `null`, as before. The comment on `_clear_final_merge_ineligibility_state` now says the helper has no caller in the poller and names the two places that do the same reset inline.

| The numbers that matter | Before | After |
| --- | --- | --- |
| REST reads per final-PR check without a cached copy | 2 | 1 |
| REST reads per implementation-failed re-issue | 2 | 1 |

Refs #7126
