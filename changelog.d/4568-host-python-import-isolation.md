<!-- changelog: security -->
- **Host-side Python in the clarify, review, workspace and resolver helpers no longer imports modules from the checkout.** A same-repository branch can no longer plant a `pathlib.py`, `json.py` or `sitecustomize.py` that runs before the sandbox with the provider credential in the environment.

The security pass for orchestrator project #3965 found that `scripts/clarify_isolated_run.sh` built its source snapshot with a plain `python3 -` from the checkout while `OPENROUTER_API_KEY` was set (finding `clarify-pre-sandbox-python-import-hijack`, issue #4568). The snapshot now runs under `env -i` with `python3 -I -B`, as do the review-sandbox snapshot, refresh, config and transfer steps. The same class of launch is closed in the helpers that run next to it: both credential-bearing brokers start with `-I -B`, and the stdin, `-m` and `-c` launches in `workspace_init.sh`, `run_workspace_hook.sh`, `write_guard.sh`, `review_conflict_resolve.sh`, `ledger_emit_substate.sh`, `review_agents_md_materiality.sh`, `review_filter_uninteresting_files.sh`, `review_reject_verify.sh`, `post_review_comment.sh`, `check_resolver_diff.sh` and `drift_audit.sh` run isolated. The ledger's `ai_memory.py` child keeps its sibling imports by adding only its own support directory to `sys.path`.

| The numbers that matter | Value |
| --- | --- |
| Scripts changed | 13 |
| Host Python launches isolated | 27 (5 in the snapshot and review-sandbox steps, 2 brokers, 16 stdin heredocs, 3 in `check_resolver_diff.sh`, 1 ledger child) |
| Finding | `clarify-pre-sandbox-python-import-hijack` (critical, #4568) |

What this means for operators: nothing to configure. Helper behaviour is unchanged for normal checkouts; a checkout that ships its own copy of a standard-library module name no longer affects these helpers.

### For contributors

`tests/test_host_python_import_isolation.py` pins every isolated launch and runs `review_filter_uninteresting_files.sh` from a directory holding hostile `argparse.py`, `fnmatch.py` and `sitecustomize.py`. `tests/test_model_provider_broker.py` and `tests/test_plan_clarify_header_prompt_staging.py` cover the review-sandbox and clarify snapshot paths.
