<!-- changelog: fixed -->
- **The live merged-PR guard hook matches its template copy again.** Since #6509 merged, `.claude/hooks/pr_merge_status_guard.py` on `main` lacked the seven-line block that `workflow-templates/.claude/hooks/pr_merge_status_guard.py` carried, so interactive sessions ran a hook that let a shell-controlled commit with per-command Git configuration (`git -c`, `--config-env`, `GIT_CONFIG_*`) through with a warning where #6509's decision Q30 says it asks for confirmation.

The live copy is now byte-identical to the template, which is what `tests/test_pr_merge_status_guard.py::test_template_copies_are_identical` requires. On `main` that test and seven guard behaviour tests failed; all 454 pass with the copies in step. Consumers receive the same file through the `.claude/` sync, so nothing changes for them.

What this means for operators: a commit such as `if true; then git -c user.name=bot commit -m x; fi` in an interactive session asks for confirmation again, as documented in `agents.md`.
