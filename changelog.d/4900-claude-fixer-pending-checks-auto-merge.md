<!-- changelog: fixed -->
- **A clean Claude-fixer review that finishes before CI now auto-merges once the checks go green.** It no longer turns into a 0-finding hand-off that wakes a Claude session with nothing to fix and ends in a hold and a manual merge.

In Claude-fixer mode (every PR-backed `claude/*` head), the reviewer panel often finishes before the head's CI does. The hand-off step then saw a clean ledger but checks still running, refused to auto-merge, and posted a `kind=findings` hand-off with `Reviewer ledger entries: 0`, which `check_in_status.py` reported as a review round. On PR #4869 the six reviewers were clean at 00:51:30Z and `lint` finished green at 00:55:09Z, and the PR still had to be merged by hand. Now that case posts a `## Review round <n>: clean review, waiting for check runs` comment with `<!-- ai:claude-fixer-pending-checks:v1 head=<sha> round=<n> ledger=<sha256> -->` instead. It is not a hand-off, so no Claude session is woken, and a dispatched re-run on that head skips the reviewer panel (`AUTOFIX_GATE_SKIP reason=claude_fixer_pending_checks`). The hourly `claude-pr-catch-all` job of `review_autofix_sweep.yml` re-reads that head's check runs once and, when every run has completed without a failure, enables auto-merge through `scripts/review_enable_auto_merge.sh`, bound to the reviewed head. The reviewers do not run again.

| The numbers that matter | Value |
| --- | --- |
| Reviewer re-runs to merge a pending-checks head | 0 |
| Delay from green CI to auto-merge | up to 1 hour (the `17 * * * *` catch-all tick) |
| Check-run reads per evaluation | 1 paginated read, through `scripts/collect_pr_check_runs_context.py` |
| Repos covered | this repo and the 13 consumers in `.github/ai/consumer_repos.json` (hand-off and gate change on the next `@stable` sync) |

The fail-closed rules stay. A failed check, a missing or malformed snapshot, or a moved head never merges. The marker counts only from `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`, for the current head, with no later hand-off, and its linked review run must have succeeded. A blocked, conflicted, or draft PR is left alone, and so is a repo whose `ENABLE_AUTO_MERGE` variable is not `true`. A check that fails after the review is handed to the Claude session as a `ci-failed` fix, as for any `claude/*` head.

What this means for operators: the "0 findings" holds that needed a manual merge (the Q46 cases) stop appearing; those PRs merge within about an hour of CI finishing. The sweep reads each consumer's `ENABLE_AUTO_MERGE` variable, so `GH_PAT` needs Actions-variables read on every registered repo. Without it the sweep logs `pending_checks ... state=auto_merge_setting_unreadable` and leaves the PR open for a manual merge, as before.

### For contributors

The readiness half lives in `scripts/claude_fixer_pending_checks.py`, called by `scripts/claude_pr_sweep.py` for each candidate whose hand-back verdict is `open`. `.claude/scripts/check_in_status.py` is unchanged: the new comment is not a hand-off, so it keeps reporting `wait`, and a later failure reaches its existing `ci-failed` path. `tests/test_claude_fixer_pending_checks.py` replays the PR #4869 sequence (hand-off step, then the sweep with CI running, green, and failed), and `tests/test_review_autofix_claude_fixer_mode.py` covers the hand-off step's pending branch and the gate skip.
