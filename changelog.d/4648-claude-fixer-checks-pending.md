<!-- changelog: changed -->
- **A clean review on a `claude/*` pull request no longer wakes a Claude session while its checks are still running.** The review workflow now waits for the checks and merges the pull request itself once they pass.

When the reviewer panel found nothing on a Claude-fixer pull request but some check runs on the head had not finished after the 300-second refresh wait, `scripts/review_autofix_step_claude_fixer_handoff.sh` used to post a `kind=findings` hand-off with zero entries. The Claude session had nothing to fix and no way to converge, so the pull request stayed on hold until someone re-ran the review by hand (run 36295340728 on PR #4554; PRs #4582, #4599 and #4601 held the same way on 2026-09-27). Now the step posts one "clean, waiting for checks" comment per head instead. The 30-minute review sweep (`review_autofix_sweep.yml`) re-dispatches the pull request as before, and for such a head `review_autofix.yml` runs only a merge check, with no reviewer models: green checks enable auto-merge bound to the head, a failing check is handed to the Claude session by name, and running checks wait for the next sweep. Every review round also records its outcome in a `claude-fixer-evidence-<run_id>-<attempt>` workflow artifact, and the merge check only trusts that artifact after verifying the run through the GitHub API, never a PR comment.

| The numbers that matter | Value |
| --- | --- |
| Delay from green checks to auto-merge | at most one sweep interval (about 30 minutes) |
| Reviewer model calls per merge check | 0 |
| API calls per merge check | 3 or 4 REST reads (evidence) plus 1 check-runs read |
| Evidence artifact retention | 30 days |
| New repository variable | `CLAUDE_FIXER_CHECKS_PENDING_ENABLED` (default `true`) |

What this means for operators: `claude/*` pull requests with a clean review and slow CI now merge on their own. Look for `CLAUDE_FIXER_CHECKS_PENDING` lines in the review run logs (`action=wait`, `still_waiting`, `auto_merge`, `handoff_ci`, `evidence_unverified`, `snapshot_unavailable`). Set `CLAUDE_FIXER_CHECKS_PENDING_ENABLED=false` to return to the zero-finding hand-off. Consumer repos get the change on the next `@stable` release.

### For contributors

The gate routes a `workflow_dispatch` to the merge check (`claude_fixer_merge_check` output, log prefix `AUTOFIX_GATE_CLAUDE_FIXER_MERGE_CHECK`) only when the newest workflow-authored marker on the head is `ai:claude-fixer-checks-pending:v1`, the kill switch is on, and GitHub does not report a conflict; the terminal same-head skip does not apply to it. The retrigger guard then forces `max_iterations_reached=true` and `skip_judge=true`, which skips the reviewers and the review-blocked judge while keeping the auto-merge steps eligible. `scripts/review_claude_fixer_evidence.py` writes and verifies the evidence; `scripts/review_autofix_step_claude_fixer_merge_check.sh` is the new step body. Tests: `tests/test_review_claude_fixer_evidence.py` and the checks-pending cases in `tests/test_review_autofix_claude_fixer_mode.py`.
