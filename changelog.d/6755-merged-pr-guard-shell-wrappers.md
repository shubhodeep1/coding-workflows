<!-- changelog: security -->
- **Security finding #6755 is resolved: the merged-PR guard checks Git writes inside shell wrappers.** An earlier entry recorded this finding as open behind the `WRAPPED_GIT_WRITE_GUARD_UNSET_OPERATOR_STEP` placeholder. The fix has since landed in both `pr_merge_status_guard.py` copies (#6777, with follow-ups in #6791). `agents.md` now describes the shipped behaviour in place of the open-finding note, and the placeholder is retired. Nothing ever read it, so no setting changes.

What this means for operators: there is no pending operator step for #6755. Commits and pushes inside `bash -c`, `eval`, `$(...)`, backticks or `<(...)` get the normal merged-PR check, and wrapped text the guard cannot read asks for confirmation. Refs #6755. Refs #6558.
