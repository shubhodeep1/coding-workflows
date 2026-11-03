<!-- changelog: added -->
- **Memory write telemetry now says why a push was retried.**

`persist_memory_operation` in `scripts/ai_memory_lib.py` returns a new `push_retry_reasons` list next to `push_attempts`: one class per push attempt that was retried, in order, at most 10 entries. Each entry is one of `non_fast_forward` (rejected push, fetch first, ref-lock contention), `auth`, `network`, `rate_limited` (only when the git output says so) or `other`. `record-run-event` and `record-candidate` copy it into their `AI_MEMORY_TELEMETRY` lines, and every result that reports `push_attempts` defaults it to an empty list. The git output is only matched and then discarded, so no stderr, URL or token appears in the field. Retries, backoff and errors are unchanged. Refs #8726.
