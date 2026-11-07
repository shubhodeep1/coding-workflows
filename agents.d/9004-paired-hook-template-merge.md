<!-- agents: section="Workflow architecture" -->
### Paired host-only hook template merge (#9004)

- `_conflict_prepare_guard_hook_template_merge` in `scripts/review_conflict_prepare.sh` loops over `GUARD_HOOK_TEMPLATE_PAIRS` (`live|template` strings) and runs the per-pair checks in `_conflict_prepare_guard_hook_template_merge_one` for every live path in the resolver allowlist. The list must equal `PAIRED_LIVE_COPIES` in `scripts/review_untrusted_workspace.py`; `test_shell_pair_list_matches_paired_live_copies` enforces that. Add a new pair in both places.
- `.claude/hooks/prepush_affected_tests.py` therefore resolves from its template on integration-sync PRs with no model run (PR #8436, run 42355218108), with the commit still deferred until the integration fingerprint check. Kill switch, log prefix (`Guard-hook template merge:`) and return codes are unchanged; a refused pair stays unmerged and keeps the `sandbox_path_host_only` fail-closed path.
