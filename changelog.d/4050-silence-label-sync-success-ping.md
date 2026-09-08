<!-- changelog: changed -->
- **The AI label sync no longer sends a Telegram ping when nothing changed.** `sync_ai_labels.yml` now drops its clean-run `🔍 DEBUG: AI label sync for <repo>: created=0, updated=0, unchanged=N, errors=0` message by default; warnings and errors still arrive.

Every `@stable` release fires the `coding-workflows-stable-released` dispatch into each consumer repo's `ai-sync-labels.yml` wrapper, and the reusable `sync_ai_labels.yml` workflow answered every one of those runs with a DEBUG ping even when all labels were already in place. A new repo var, `AI_LABEL_SYNC_SUCCESS_ALERT_LEVEL`, is applied as the `Telegram status` step's threshold only when the run outcome is clean (exit 0, zero per-label errors); it defaults to `SILENT`, so `scripts/tg_helpers.sh` suppresses that one message. Runs with per-label errors (`WARNING`) or a failed sync (`ERROR`) keep honouring the global `ALERT_MSG_LEVEL` exactly as before, and the run's step summary still shows the counts. Dry-runs are gated the same way as real runs.

| The numbers that matter | Value |
| --- | --- |
| New repo var | `AI_LABEL_SYNC_SUCCESS_ALERT_LEVEL` |
| Default | `SILENT` |
| Opt back in | `vars.AI_LABEL_SYNC_SUCCESS_ALERT_LEVEL=DEBUG` |
| Pings affected | the clean-run `DEBUG` message only |

What this means for operators: after the next `@stable` sync the label-sync workflow is quiet unless it created or updated a label with errors, or failed outright. Set `vars.AI_LABEL_SYNC_SUCCESS_ALERT_LEVEL=DEBUG` on any repo where you still want a confirmation ping per run.

### For contributors

The knob follows the `PR_PROCESSED_ALERT_LEVEL` pattern in `review_autofix.yml`: it overrides `ALERT_MSG_LEVEL` for one step, not the workflow, and only on the DEBUG branch of the summarised outcome. The consumer wrapper template `workflow-templates/ai-sync-labels.yml` is unchanged because it already calls the reusable workflow via `@stable`.
