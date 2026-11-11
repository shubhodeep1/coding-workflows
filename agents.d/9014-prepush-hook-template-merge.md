<!-- agents: section="Workflow architecture" -->
Since #9014 the guard-hook template merge in
`scripts/review_conflict_prepare.sh` covers every live/template pair in
`review_untrusted_workspace.PAIRED_LIVE_COPIES` (for example
`.claude/hooks/prepush_affected_tests.py` as well as
`.claude/hooks/pr_merge_status_guard.py`), with the same per-pair checks and
the same `CONFLICT_GUARD_HOOK_TEMPLATE_MERGE_ENABLED` kill switch (default
`true`) for every pair. The pair list is read only from the trusted support
checkout (`${SUPPORT_SCRIPTS_DIR}/review_untrusted_workspace.py`), and each
pair must be `.claude/hooks/<name>` -> `workflow-templates/.claude/hooks/<name>`;
a pair of any other shape logs `skipped reason=pair_rejected`. When the list
cannot be read, only the built-in `pr_merge_status_guard.py` pair is handled
and the run logs `Guard-hook template merge: pair_source=fallback
reason=<support_missing|read_failed|no_valid_pairs>`; otherwise it logs
`pair_source=paired_live_copies pairs=<n>`. Every
`Guard-hook template merge: skipped reason=<...>` line now ends with
`path=<live>`.
