<!-- changelog: changed -->
- **The CLAUDE.md §21.D fix for the merged-PR guard (#6831) is still pending an operator.** Refs #6831.

Fix-up #7052 merged, but the resumed work blocked again on one change: §21.D (API-call budget) does not yet say that a push never reads the 300-second cache and always takes PR state from a live lookup, which both `pr_merge_status_guard.py` copies already do and cite §21.D for. Pipeline editors cannot write `CLAUDE.md`, so an `agents.d` fragment records the step behind the placeholder `PR_MERGE_GUARD_DOC_SYNC_UNSET_OPERATOR_STEP`. Nothing reads that name, and setting it changes nothing.

What this means for operators: in a trusted session, edit §21.D in `CLAUDE.md` and `workflow-templates/CLAUDE.md` together, keeping them byte-identical. The guard, CI, releases, tagging, dispatches and merges are unchanged until then.
