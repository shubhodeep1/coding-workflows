<!-- changelog: fixed -->
- **Implement's guard-block handler now retries its safety labels on transient GitHub errors.** A 5xx, network error or rate limit no longer leaves `ai:needs-human`, `ai:scope-blocked` or `ai:destructive-blocked` unapplied with only an "unknown latch state" warning.

`scripts/implement_handle_guard_block.sh` applied those labels and read them back with single raw `gh` calls. `implement.yml` ran it from a `${RUNTIME_DIR}` copy with no helpers beside it. The `Stage workflow support files` step now also copies `scripts/gh_helpers.sh` into `${RUNTIME_DIR}`, and the handler sources it. The label add and remove, the verification read and label creation go through `gh_retry`. A label that already exists is still accepted on the first attempt, without retries. The rejection comment is still posted once, so a retry cannot duplicate it. When `gh_helpers.sh` is missing or fails to load, the handler logs a `::warning::` and runs the raw calls as before.

| The numbers that matter | Value |
| --- | --- |
| Attempts per label write or read | up to 5 (`GH_RETRY_MAX_ATTEMPTS`) |
| Attempts per rejection comment | 1 |

What this means for operators: a guard rejection during a brief GitHub outage now ends with the latch label confirmed. The order of writes, the latch-status lines, the markers and the exit codes are unchanged.
