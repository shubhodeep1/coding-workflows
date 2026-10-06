<!-- changelog: fixed -->
- **The orchestrator no longer marks a project validated because of another project's validation run.** Validate runs now carry their tracking issue in the run name, and the poller's workflow-run fallback only credits a success from a run for the same project.

When the `ai:validated` label is missing, the poller falls back to the conclusion of the latest completed validation run created after its dispatch. That lookup did not check which project a run belonged to: on 2026-09-26, project #3965 was marked validated, and moved on to its security pass, because a standalone validation of `main` (tracking issue 0, run 36242757577) finished after #3965's dispatch, while #3965's own three runs had failed. `internal-validate.yml` and the consumer template `workflow-templates/ai-validate.yml` now set `run-name` to `... [tracking:<N>]`, and `get_last_validation_run_info` in `scripts/orchestrate_poll_process.sh` filters on it using the run listing it already fetches, with no extra API calls.

| The numbers that matter | Value |
| --- | --- |
| Run name | `AI Validate [tracking:<N>]` / `Internal: AI Validate [tracking:<N>]` |
| Success credited from | runs marked with the project's own tracking issue only |
| Unmarked runs (wrapper not synced) | may report a failure, never a success |
| New log line | `VALIDATION_RUN_ATTRIBUTION tracking=<N> … selected_run=<id\|none>` |

What this means for consumer repos: the updated `ai-validate.yml` wrapper arrives with the next workflow sync. Until then, a lost `ai:validated` label is no longer recovered from an unmarked successful run; the project waits for the label or the next validation cycle instead of completing on unproven evidence.

### For contributors

`has_active_validation_run` still counts any in-progress validation run in the repository, so another project's run can delay a dispatch; it cannot produce a verdict, so it is unchanged here. Tests: the `test_validation_run_*` cases in `tests/test_orchestrate_poll_process.py`.
