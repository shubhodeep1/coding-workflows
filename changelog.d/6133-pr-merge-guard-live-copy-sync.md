<!-- changelog: fixed -->
- **This repository's merged-PR commit guard is identical to the copy consumer repos receive again, and `main`'s `tests-hooks-and-orchestrator` CI job passes again.** The live hook now has the push and redirect parsing fixes that #6133 put only into the template copy.

#6133 changed `.claude/hooks/pr_merge_status_guard.py` and `workflow-templates/.claude/hooks/pr_merge_status_guard.py` differently. Only the template copy got redirect-target skipping, the `cd … || exit` worktree handling, `VAR+=` prefix handling and `--repo=` push parsing. Since 2026-10-04, five tests in `tests/test_pr_merge_status_guard.py` have failed on every `main` CI run, including `test_template_copies_are_identical`. The same failures reached every integration branch synced from `main`, such as `orchestrator/project-6031`. The live hook is now byte-identical to the template, and all 177 guard tests pass.

What this means for operators: consumer repos already had the corrected guard through the template. Only interactive sessions in this repository change: they stop misreading commands such as `git push origin HEAD 2>&1` or `cd dir || exit; git push`.
