<!-- changelog: added -->
- **Workflow-log collector rows now name the root cause of a failed review/autofix run, not only the last failed step.** Run 35604714444 was filed under its last failed step, `Commit changes`, although its `REVIEW_AUTOFIX_RUN_SUMMARY_V1` line said `finalize_reason=editor_changes_lost`, so the 2026-09-22 workflow analysis looked in the wrong place.

`scripts/collect_workflow_logs.py` adds two fields to every report and cached row. `root_failure_class` is the `finalize_reason` of the last `REVIEW_AUTOFIX_RUN_SUMMARY_V1` line in a failed or cancelled run's own logs. It is kept only when it matches `^[a-z0-9_]{1,64}$`, and only a whole log line counts (an optional Actions timestamp may lead it), so echoed script text is ignored. `symptom_step` copies `failure_point.step_name`, which is unchanged. A run with no summary, an unparseable summary, logs that were not downloaded, or a fork or unknown-origin run (`WORKFLOW_LOG_FORK_EXCLUDED`) gets `null`. Cached values are re-checked against the same pattern when reused.

| The numbers that matter | Value |
| --- | --- |
| New row fields | 2 (`root_failure_class`, `symptom_step`) |
| New GitHub API calls | 0 (parsed from archives already downloaded) |

What this means for workflow-log analysis: group failures by `root_failure_class` where it is set and fall back to `symptom_step`. Rows cached before this change read `null` until their log archive is downloaded again.
