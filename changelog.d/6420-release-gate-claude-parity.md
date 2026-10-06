<!-- changelog: security -->
- **Stable releases now stop when shipped Claude templates differ from this repo's committed live copies.** Both `mark-stable.yml` and `test-and-mark-stable.yml` run the existing template/live parity test in `validate-scripts` before tagging.

Template-only PRs can pass CI with live copies prepared in a disposable checkout, but the release gates previously did not re-check the committed tree. The new gate makes no additional GitHub API calls and rejects unallowlisted drift before those templates reach consumer repositories. A pending or held `ai/sync-claude-live-copies*` PR must be merged, or both copies edited by hand, before the next release can pass; if automatic promotion has exhausted its retries, promote manually after parity is restored.
