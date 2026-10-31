<!-- changelog: added -->
- **New pure helper `classify_failure_shape()` in `scripts/workflow_failure_heal.py` sorts a heal failure's error signature into one of six classes.** Nothing calls it yet.

It takes the `error_signature()` output, the failed job's conclusion and whether failing tests were found, and returns the first match in this order: `stall`, `timeout` (a `timed_out` conclusion or the signature), `test_failure` (failing tests found or the signature), `crash`, `api_error`, otherwise `other`. Matching is case-insensitive because `error_signature()` lowercases every line it keeps.

| The numbers that matter | Value |
| --- | --- |
| Classes | 6 |
| Extra GitHub API calls | 0 |
| Default behaviour | unchanged (no caller yet) |

What this means for operators: nothing changes until the heal trends (P3) and root-cause grouping (P5) phases wire it in.
