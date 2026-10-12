<!-- changelog: fixed -->
- **The §19 auto-close check now reads the PR's commit messages and catches colon forms such as `Resolved: #N`.**

On 2026-10-12 the squash merge of PR #7190 closed orchestrator tracking issue #6664, although its PR body was clean. This repository's squash merges copy the PR's commit messages into the merge commit, and one of those messages had the word "resolved", a colon, and the issue number. The `Lint PR body for auto-close keywords against orchestrator-tracking issues` workflow checked only the PR title and body. `scripts/lint_pr_body_auto_close.py` also treated colon forms as harmless, but GitHub closes issues on them. For pull requests into the default branch, the workflow now reads every commit message on the PR and fails on the same keyword rules. A violation in a commit message says to reword that commit on the PR branch. The script matches `keyword: #N` and `keyword:#N` everywhere it runs, which also covers the implement pre-flight and the consumer updater.

| The numbers that matter | Value |
| --- | --- |
| Commit messages read per PR | up to 250, one paginated REST read |
| Open PRs into `main` flagged by the stricter check on 2026-10-12 | 0 of 33 |
| PRs into `stable` | title and body only, as before |

What this means for operators: a commit message can no longer close a tracking issue and stall its project without the PR check failing first.
