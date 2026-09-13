<!-- changelog: fixed -->
- **The "Editor no-op suspicious" alert now says when the editor simply failed.** When every editor attempt fails, the Telegram warning and PR comment report the attempt count and the final attempt's provider error instead of claiming the editor "claimed no changes were needed".

`review_autofix.yml` blocks auto-merge whenever the editor's no-op disposition cannot be verified, which is correct, but one of the cases it covers is the `recoverable_failure` partial-finalize summary that `scripts/review_apply_fixes.sh` writes after all editor attempts fail. In that case the editor never produced output, yet the operator alert read "Editor claimed no changes needed but disposition could not be verified. Manual review or re-run required." On PR #4077 the editor's model broker answered HTTP 429 on every attempt for 21 consecutive runs (Actions runs 34692519987, 34700918528 and 34702442346 among them) while the alert kept asking for a re-run. The `Validate editor no-op disposition` step now sets an additive `EDITOR_NOOP_RECOVERABLE_FAILURE=true` when it sees the partial-finalize sentinel, and the `Telegram editor-noop-suspicious warning` step uses it to post "Editor failed on all N attempts" with the last `Error:` line from the final attempt's stderr, clipped to 240 characters. `EDITOR_NOOP_SUSPICIOUS`, `EDITOR_NOOP_REFUSAL`, the auto-merge gate and the PR-comment literal the orchestrator poller greps for are unchanged.

| The numbers that matter | Value |
| --- | --- |
| New env var | `EDITOR_NOOP_RECOVERABLE_FAILURE` (default `false`) |
| Sentinel matched | `partial finalize requested after a recoverable editor failure` |
| Error excerpt limit | 240 characters, final attempt only |
| Incident PR / runs | #4077 / 34692519987, 34700918528, 34702442346 |

What this means for operators: a Telegram warning that begins "Editor failed on all N attempts" means the editor never ran to completion and quotes the provider error to chase; a re-run only helps when that error was transient. The generic "Editor no-op suspicious" wording is now reserved for runs where the editor did produce a summary that could not be verified.

### For contributors

The sentinel is matched verbatim from the `Runtime failure path:` line of the recoverable_failure fallback summary in `scripts/review_apply_fixes.sh`; `tests/test_review_autofix_editor_noop_cascade_contract.py` pins it in lockstep across the script and the workflow. The soft-deadline fallback summary is deliberately not classified here.
