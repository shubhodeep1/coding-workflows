<!-- changelog: added -->
- **Merges re-validate when the base moved under the PR's files.** Before auto-merge is enabled or a direct merge is attempted, the new merge-base freshness gate in `scripts/pr_checks_lib.sh` checks whether the base branch gained commits that touch a file the PR also touches; if so, the branch is updated from the base and the merge waits for the fresh CI and review round.

PR #6741 merged on 2026-10-09 with check-runs from the previous day; #6549 had meanwhile changed the code its new tests exercised, and `main` CI was red for three hours. GitHub's auto-merge binds to the PR head, not to the base the checks ran against, and requiring every branch to be up to date would cost one CI run per merged sibling on every open PR. The gate reads `compare/{head}...{base}` and, only when the base moved, the PR's file list; a shared path (renames under both names, or a base-side diff of 300 or more files) triggers `PUT pulls/{n}/update-branch` bound to the head, whose `synchronize` run re-validates the combined tree. A base that did not move, or moved only in other files, merges as before, and any API failure logs `MERGE_BASE_FRESHNESS ... outcome=unknown` and proceeds. It is wired into the codex-agent `Enable auto-merge on PR` step, the `deterministic-skip-merge` job, the review-blocked judge, and the poller's backward-scan, `attempt_merge` and review-blocked merge sites.

| The numbers that matter | Value |
| --- | --- |
| Repository variable | `MERGE_BASE_FRESHNESS_ENABLED` (default `true`) |
| API calls per gated merge | 1 compare, plus 1 files listing only when the base moved, plus 1 update on overlap |
| Extra validation rounds | 1 CI and review round, only for a PR whose files the base changed |

What this means for operators: a green PR can no longer land code that was never tested against the current base, and non-overlapping PRs merge at the same speed as before. Set `MERGE_BASE_FRESHNESS_ENABLED=false` to restore the previous behaviour.

### For contributors

`tests/test_pr_base_freshness_gate.py` covers every outcome, the switch and each wiring point, and runs in its own `ci.yml` step. The `deterministic-skip-merge` job reads the library from the gate's verified support commit (`.codex-freshness-src`).
