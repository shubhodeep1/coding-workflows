<!-- changelog: fixed -->
- **A branch review without a pull request no longer diffs against `main` by mistake.** When `review_autofix.yml` runs with `force_claude_branch_review` and no `base_ref_override`, `scripts/review_collect_pr_metadata.sh` now reads the repository's default branch with retries and stops with an error if it cannot.

Before this change the helper read the default branch once and fell back to `main` when the read failed, without a warning. A transient API failure, or a repository whose default branch is not `main`, made the review compare the branch against the wrong base. The read now goes through the helper's retrying `gh_retry` wrapper, the branch name is checked with `git check-ref-format`, and a failed or invalid read ends the job with `::error::No-PR claude-branch-review path: could not resolve the repository default branch; pass base_ref_override or retry.` A caller-supplied `base_ref_override` still takes precedence, and reviews of pull requests are unchanged.

What this means for operators: if this error appears, re-run the review or pass `base_ref_override` with the branch to compare against.
