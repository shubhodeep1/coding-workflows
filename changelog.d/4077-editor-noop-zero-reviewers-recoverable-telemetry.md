<!-- changelog: fixed -->
- **review_autofix now flags an editor that failed on every attempt as no-op suspicious even when no reviewer succeeded, and records that failure class in the run summary.** Validator Check 1c sets `EDITOR_NOOP_SUSPICIOUS=true` itself, and `REVIEW_AUTOFIX_RUN_SUMMARY_V1` gains `recoverable_failure` / `editor_noop_recoverable_failure`.

Two gaps left out of PR #4083 (the "Editor no-op suspicious" alert fix for PR #4077) are closed. First, `scripts/review_run_reviewers.sh` exits green with `REVIEWERS_SUCCESSFUL=0` when every reviewer slot is skipped fail-open (circuit breaker open, or a retryable failure with no failback mapping), and the `Apply fixes with editor model` step has no reviewer-count clause, so the editor still runs. When it then fails on every attempt, only Check 2 of `Validate editor no-op disposition` used to flag the resulting `recoverable_failure` summary, and Check 2 is skipped at zero reviewers: the Telegram / PR-comment alert never fired, the merge-conflict detect/resolver chain ran with nothing to land, and the run summary recorded the editor slot as `success`. Check 1c (which already matched that summary's `partial finalize requested after a recoverable editor failure` sentinel) now sets `EDITOR_NOOP_SUSPICIOUS=true` alongside `EDITOR_NOOP_RECOVERABLE_FAILURE=true`; with reviewers present the outcome is identical to before. Second, the `Append review pipeline iteration summary` step classified such a run as `unexpected_noop` / `editor_noop_suspicious`; it now records `slot_results.editor.failure_class=recoverable_failure` and, when no partial finalize was requested, `finalize_reason=editor_noop_recoverable_failure`. `refusal` / `editor_noop_refusal` keep precedence and every existing value is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Validator check that now sets `EDITOR_NOOP_SUSPICIOUS` | Check 1c (`Validate editor no-op disposition`) |
| Reviewer statuses that reach the editor with `REVIEWERS_SUCCESSFUL=0` | `skipped_open`, `skipped_unmapped` |
| New editor `failure_class` | `recoverable_failure` |
| New `finalize_reason` | `editor_noop_recoverable_failure` |
| Soft-deadline fallback summary | still not matched (budget exhaustion, not an editor failure) |

What this means for operators: a review run whose editor never produced output now always posts the "Editor failed on all N attempts" alert and blocks the resolver chain, regardless of how many reviewers succeeded, and the run summary names the cause as `recoverable_failure` instead of an unexpected no-op. Auto-merge behaviour does not change: the partial-finalize request already held those gates closed on this path.

### For contributors

The Check 1 regex, its `::warning::` literal (grepped by the e2e poller in `test-and-mark-stable.yml`) and the refusal Check 1b are byte-for-byte unchanged; the refusal summary at zero reviewers is pinned unchanged (`EDITOR_NOOP_SUSPICIOUS=false`, `EDITOR_NOOP_REFUSAL=true`). `determine_finalize_reason` still returns `partial_finalize` ahead of every `editor_noop_*` outcome, so on the real recoverable-failure path the editor slot's `failure_class` is the field to read. Pinned by `tests/test_review_autofix_editor_noop_cascade_contract.py` and `tests/test_review_autofix_review_pipeline_contract.py`; runbook §20.10.2 in `probably_unnecessary_but_read_if_stuck.md` carries the reachability trace.
