<!-- changelog: fixed -->
- **This repository's merged-PR commit guard is identical to the copy consumer repos receive again.** The live hook now has the push and redirect parsing fixes that #6133 put only into the template copy, and both copies ask for confirmation when a push source cannot be resolved instead of checking the wrong branch.

#6133 changed `.claude/hooks/pr_merge_status_guard.py` and `workflow-templates/.claude/hooks/pr_merge_status_guard.py` differently. Only the template copy got redirect-target skipping, the `cd … || exit` worktree handling, `VAR+=` prefix handling and `--repo=` push parsing. The live hook is now byte-identical to the template. A spaced redirect no longer erases a numeric branch refspec; an unresolvable push source now requires confirmation. The CI job remains red while the separate `/audit-plans` command parity failure remains unresolved.

What this means for operators: interactive sessions in this repository stop misreading commands such as `git push origin HEAD 2>&1` or `cd dir || exit; git push`; consumer repos receive the same additional push safety checks on their next template sync.
