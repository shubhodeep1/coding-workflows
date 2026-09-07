<!-- changelog: fixed -->
- **A large editor log no longer costs a job its entire budget.** The run-substate ledger helper now reads only the tail of the token log it parses, and bounds the ai-memory write it makes.

Implement, review-autofix and validate runs call `scripts/ledger_emit_substate.sh` after every editor attempt to record token counts. The helper scanned the whole editor log for a JSON `usage` object, probing every `{` in the document. `json.JSONDecodeError` counts newlines from byte 0 of the document each time a probe fails, so the scan is quadratic in file size: a 30 MB codex log costs over an hour of CPU, twice per attempt, with no log line to say why. Run 34074649678 on issue #4017 lost a 3-hour implement job to exactly that. Codex had finished editing at 02:36 and the job was cancelled at 04:57 with no branch, no pull request and no diagnosis. The helper now reads the trailing `LEDGER_TOKENS_LOG_MAX_BYTES` (default 1 MiB, snapped to a line boundary) instead. Every parser in it keeps its last match and editors write their usage summary at the end, so the tail carries the same answer. The `record-run-event` write it makes is now bounded by `LEDGER_EMIT_TIMEOUT_SECONDS` too, because it runs while the helper holds an exclusive lock.

| The numbers that matter | Value |
| --- | --- |
| Log that triggered the stall | 31,922,047 bytes |
| Time the two parses consumed | ~141 min of a 180 min job budget |
| Same log parsed under the new default | under 2 s |
| `LEDGER_TOKENS_LOG_MAX_BYTES` default | `1048576` (`0` reads the whole file) |
| `LEDGER_EMIT_TIMEOUT_SECONDS` default | `120` (`0` disables the bound) |

What this means for operators: an implement, review-autofix or validate run whose editor produces a very large log now finishes and opens its pull request instead of being cancelled at the job timeout with its work discarded. Both bounds fail open, so telemetry that cannot be written is still never able to fail or stall the run that produced it.

### For contributors

The fix lives in `scripts/ledger_emit_substate.sh`, so all six callers that pass `--tokens-log-file` are covered without touching their call sites. Token values are unchanged for any log at or under the bound, which is every log the helper saw before this class of run. `tests/test_run_substate_ledger.py` covers the tail bound, its configurability, the disable path, and the emit timeout.
