<!-- agents: section="Workflow architecture" -->
The guard-hook template merge in `scripts/review_conflict_prepare.sh` covers
every paired live hook, not only `.claude/hooks/pr_merge_status_guard.py`
(#9004). The prepare step walks the static `GUARD_HOOK_TEMPLATE_PAIRS` list
(`pr_merge_status_guard.py`, `prepush_affected_tests.py`,
`gh_api_write_guard.py`, `pr_watch_guard.py`, `session-start.sh` under
`.claude/hooks/`, each with its `workflow-templates/` twin) and runs the same
checks for each unmerged live hook. That list must equal `PAIRED_LIVE_COPIES`
in `scripts/review_untrusted_workspace.py`;
`tests/test_conflict_guard_hook_template_merge.py` locks the equality. The
`CONFLICT_GUARD_HOOK_TEMPLATE_MERGE_ENABLED` kill switch, the
`Guard-hook template merge:` log text, the `orchestrator/project-*` deferral
and the fatal code-2 result are unchanged; a deferred commit's error lines
name every resolved hook.
