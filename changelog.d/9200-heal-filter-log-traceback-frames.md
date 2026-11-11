<!-- changelog: fixed -->
- **Workflow failure heal evidence now carries the stack frames and exception line of a Python traceback, not just its `Traceback (most recent call last):` header.** `filter_log` in `scripts/workflow_failure_heal.py` kept only lines matching `_SOFT_LOG_PATTERNS`, which matches the header but not `  File "...", line N, in ...` frames or a `ValueError: ...` line (`\bERROR\b` has no word boundary inside `ValueError`).

On 10 and 11 November 2026 every AI Plan and review prompt render crashed with a chained `ValueError` in `scripts/render_prompt.py` (the Python 3.14 argparse help-string check). The heal evidence for issues #9190 and #9200 held two bare traceback headers, so both were filed as `inconclusive` and #9200 escalated to a human, although the raw log of plan run 42929246385 named the failing line. `filter_log` now keeps, after each traceback header, the indented frame and source lines, the exception line, and a following "The above exception was the direct cause of the following exception:" or "During handling of the above exception, another exception occurred:" marker. A chained traceback's next header starts its own block. The GitHub Actions timestamp prefix is ignored when classifying lines.

| The numbers that matter | Value |
| --- | --- |
| Lines kept after one traceback header | at most 30 (the innermost ones when the traceback is longer) |
| Lines scanned per traceback | at most 400, then on to the exception line (and a chain marker after it) if it has not appeared yet |
| Kept lines of the plan run 42929246385 traceback | 33 of 34 (only a trailing blank line dropped; both `ValueError` lines kept) |
| New GitHub API calls | 0 |

What this means for operators: a heal issue for a Python crash now quotes the exception and the frame that raised it, so the diagnosis can name the fix instead of asking a person to read the raw job log.

### For contributors

The extra lines also reach `error_signature`. Its `^\s*\w+Error:` bucket only decides the fingerprint when the log has no `##[error]` or `*_FAILED` line, so a traceback outside the 400-line tail of such a log now adds its exception line to the signature. The overall `max_lines` / `max_bytes` cut of `filter_log` is unchanged. Tests: `test_filter_log_keeps_chained_traceback_frames_and_exception_lines_outside_tail`, `test_filter_log_keeps_the_innermost_lines_of_a_long_traceback`, `test_filter_log_keeps_the_exception_line_of_a_traceback_longer_than_the_scan_window` and `test_filter_log_keeps_the_chain_marker_after_a_traceback_longer_than_the_scan_window` in `tests/test_workflow_failure_heal.py`.

Refs #9190, Refs #9200.
