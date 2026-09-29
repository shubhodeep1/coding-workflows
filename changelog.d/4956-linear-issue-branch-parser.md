<!-- changelog: security -->
- **Reading an issue's `Integration branch:` / `Target branch:` line now takes linear time, so a crafted issue body can no longer stall the PR-close workflow or the merged-issue sweep.** `issue_body_integration_branch` in `scripts/gh_helpers.sh` parses one line at a time with string operations instead of two multiline regexes.

The helper decides which branch an issue targets for `close_merged_issues_sweep` in `scripts/orchestrate_poll_process.sh` and for `.github/workflows/issue_pr_status.yml`. Its regexes let `\s*` run across newlines, so a failed search was rescanned from every line start. Inside a single line, the value pattern backtracked cubically over runs of spaces. Anyone who can open or edit an issue controls that body, and the PR-close step runs under a five-minute timeout (security audit finding #4956, STRIDE denial of service). The branch the helper returns is unchanged for every issue whose label line carries its value, which covers every body the automation writes.

| The numbers that matter | Before | After |
| --- | --- | --- |
| Body with 16,000 blank lines | 7.0 s | 0.03 s |
| One `Integration branch:` line with 2,000 spaces | 29.7 s | 0.02 s |
| One 65,000-character label line | did not finish | 0.02 s |
| New GitHub API calls | 0 | 0 |

What this means for operators: nothing to configure. Issue bodies with many blank lines or long runs of spaces no longer slow down issue closing. A label line with no value (`Integration branch:` alone, the branch on the next line) now yields no branch, where the old regex took the next line's text. The issue is then treated as having no integration branch, so only default-branch and orchestrator-managed merges close it.

### For contributors

The grammar still matches `INTEGRATION_BRANCH_LINE_RE` / `TARGET_BRANCH_LINE_RE` in `scripts/orchestrate_lib.py`, applied one line at a time. `tests/test_gh_helpers_issue_body_integration_branch.py` compares the helper with those regexes over a curated list and a fixed-seed generated corpus, and fails if any of five crafted bodies takes 5 seconds or more. The Python parser and `scripts/resolve_integration_ref.sh` still use the regexes.
