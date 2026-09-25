<!-- changelog: fixed -->
- **Workflow failure heal now sees the runs that failed, names reviewer failures correctly, and cleans up heal PRs when their source PR closes.** The identical-failure cap's heal report links the failed review runs, and a failed reviewer step is reported as `reviewers_failed` rather than `editor_empty_noop`. When a pull request in coding-workflows closes, its heal PRs are closed or brought up to its base and re-pointed.

On PR #4323, review/autofix stopped after three identical runs reported as `editor_empty_noop`. The heal issue filed from the cap (#4342) said "no failed run could be linked" and blamed the editor. In fact every reviewer slot and the summariser had exited with systemd status 226, and the editor never ran. The fix PR (#4349) then stayed open and merge-queued on #4323's branch after #4323 closed unmerged. Three changes address this. The `fingerprint-cap-block` job passes the PR comments it already fetched to `scripts/workflow_failure_heal_autofix_report.sh`, which lists the runs named by the head's `review-autofix-failure:v1` markers in `run_refs`. A failed `Run reviewer models` step is reported as `reviewers_failed`, with per-slot and summariser exit codes as evidence. And the new `heal-pr-reconcile` job in `internal-cancel-on-pr-close.yml` runs `scripts/workflow_failure_heal_pr_reconcile.sh` when a pull request closes.

| The numbers that matter | Value |
| --- | --- |
| Failed runs listed in a cap report's `run_refs` | up to 3, newest first |
| New API calls in the cap path | 0 |
| Self-named script error lines kept as reviewer-failure evidence | up to 10 |
| Heal PR outcome when the source PR closes unmerged | closed; heal issue closed as not planned |
| Heal PR outcome when the source PR merges | source head and base merged in (fast-forward push), PR re-pointed |

What this means for operators: a cap-triggered heal issue now carries the failed runs' logs, and an error line such as `untrusted_process_sandbox: …` counts as the crash file for ownership routing when that script exists. Review retries are unchanged: a reviewer failure still posts the "AI review/autofix produced no output — will retry" comment and applies no immediate `ai:review-blocked`. A heal PR no longer outlives its source PR on a successful reconciliation: it is closed, or re-pointed at the base with a diff of only its own changes. An explicit remote rejection against an unchanged heal branch or a failed base update closes the heal PR; ambiguous push failures emit workflow warnings without claiming success. No force push is used, so the repository's non-fast-forward rule is respected. Set `WORKFLOW_HEAL_PR_RECONCILE_ENABLED=false` to skip the reconcile job before checkout.

### For contributors

- New env vars, both with defaults: `AUTOFIX_FAILURE_MARKER_AUTHOR` (reporter, default empty) and `WORKFLOW_HEAL_PR_RECONCILE_ENABLED` (repository variable, default `true`). New run flag: `AUTOFIX_REVIEWERS_FAILED`. New `workflow_failure_heal.py` subcommand `reviewer-failure-evidence` and flag `build-autofix-payload --failure-marker-author`; the reporter passes the flag only when the staged helper supports it.
- `reviewers_failed` ranks above `editor_empty_noop` in `derive_autofix_failure_reason`, in the reporter, and in the run summary's `finalize_reason`. Every fingerprint call site reads `reviewers_failure_evidence.txt` first, and missing files are skipped, so other failures keep their fingerprints.
- `extract_crash_file` now maps `::error::resolve_integration_ref.sh: …`-style lines to `scripts/<name>` when the script sits next to the helper; unknown names still give nothing. Ownership is unchanged: when the PR and its base both changed the file, the PR owns it.
