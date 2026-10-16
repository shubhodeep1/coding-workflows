<!-- changelog: fixed -->
- **A short GitHub API error no longer drops the log-analysis failure label.** The three failure handlers in `workflow-log-analysis.yml` (the analysis, deep-audit and API-redundancy passes) now send the `ai:log-analysis-failed` label create and add-label through `gh_retry`. They fall back to a single call when `scripts/gh_helpers.sh` did not load.

The label create is tried once first. When the label already exists it stops there, because `gh_retry` would otherwise retry gh's "already exists" error up to 5 times. Other create errors go through `gh_retry`. The per-run `AI_PHASE_FAILURE_V1` comment is still posted with a single attempt, because retrying a comment create could post it twice.

What this means for operators: a failed log-analysis pass now carries the `ai:log-analysis-failed` label even when one GitHub API call fails along the way. An existing label still costs one API call.
