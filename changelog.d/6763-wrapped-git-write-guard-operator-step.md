<!-- changelog: security -->
- **The merged-PR guard still does not inspect Git writes wrapped in nested shell text.** Commits and pushes inside `bash -c`, `eval`, `$(…)`, backticks or process substitution are not checked yet.

The fix is recorded as an operator step, `WRAPPED_GIT_WRITE_GUARD_UNSET_OPERATOR_STEP`, for a person to make in a trusted checkout, because the pipeline cannot edit the live `.claude/hooks/` copy. No hook behaviour changes, nothing reads the placeholder, and finding #6755 stays open. Refs #6755.
