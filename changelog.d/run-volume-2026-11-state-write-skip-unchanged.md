<!-- changelog: changed -->
- **The orchestrator poller no longer re-posts a state that has not changed.** A write whose content matches the state already on the tracking issue now posts nothing.

A large project's state is several comments, and every state write posted all of them. On 2026-11-17 tracking issue #8291, a 567 KB state split into 12 parts, received a full 12-part write whose content was identical to the previous one, and other runs wrote all 12 parts several times an hour. `post_state_comment` in `scripts/orchestrate_poll_process.sh` now compares the state's content hash with the chain it read for the tracking issue and the last chain it wrote, and skips the write when they match. Readers see the same newest chain either way. Deciding costs no API call.

| The numbers that matter | Value |
| --- | --- |
| #8291 state size | 567 KB, 12 comment parts per write |
| Comments saved per skipped #8291 write | 12 |
| New repository variable | `ORCH_STATE_SKIP_UNCHANGED`, default `true` |
| New log line | `STATE_WRITE tracking_issue=<n> outcome=skip reason=unchanged` |

What this means for operators: nothing to configure. Set `ORCH_STATE_SKIP_UNCHANGED=false` to post every write again. Writes that change the state, including writes of a single field, still post the full chain.
