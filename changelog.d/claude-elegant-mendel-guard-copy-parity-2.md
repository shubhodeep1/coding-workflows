<!-- changelog: fixed -->
- **The live merged-PR guard hook matches its template copy again (second time today).** #6605's review autofix round could only edit `workflow-templates/.claude/hooks/pr_merge_status_guard.py`, because the review sandbox refuses the live `.claude/hooks` path, so #6605 merged with the two copies 29 lines apart and `tests-hooks-and-orchestrator` has failed on every push to `main` since 12:58 UTC.

The live copy is now byte-identical to the template, which carries the reviewed heredoc-reader narrowing and the `|&` operator from that autofix round. All 483 guard tests pass with the copies in step. Until the review sandbox can carry both copies, any autofix that edits the template needs this same sync; the `Sync live .claude copies` workflow exists for it and its run on the #6605 merge is the item to check.

What this means for operators: CI on `main` is green again and auto-merge can resume on the open PRs.
