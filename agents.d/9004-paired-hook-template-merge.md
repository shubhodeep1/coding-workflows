<!-- agents: section="Workflow architecture" -->
The guard-hook template merge in `scripts/review_conflict_prepare.sh` covers
every paired live hook, not only `.claude/hooks/pr_merge_status_guard.py`
(#9004). `_conflict_prepare_guard_hook_pairs` reads the pairs from
`PAIRED_LIVE_COPIES` in the trusted support checkout
(`${SUPPORT_SCRIPTS_DIR}/review_untrusted_workspace.py`). When that list
cannot be read (`pair_source=fallback
reason=<support_missing|read_failed|no_valid_pairs>`), it uses the static
`GUARD_HOOK_TEMPLATE_PAIRS` array in the same script
(`pr_merge_status_guard.py`, `prepush_affected_tests.py`,
`gh_api_write_guard.py`, `pr_watch_guard.py`, `session-start.sh` under
`.claude/hooks/`, each with its `workflow-templates/` twin) instead of the
guard pair alone. The array must equal `PAIRED_LIVE_COPIES` item for item;
`test_static_pair_list_equals_paired_live_copies` in
`tests/test_conflict_guard_hook_template_merge.py` locks the equality, so a
pair added to one list must be added to the other. The call site runs the
same checks for each unmerged live hook. The
`CONFLICT_GUARD_HOOK_TEMPLATE_MERGE_ENABLED` kill switch, the
`Guard-hook template merge:` log text, the `orchestrator/project-*` deferral
and the fatal code-2 result are unchanged; a deferred commit's error lines
name every resolved hook.
