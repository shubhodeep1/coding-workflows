<!-- changelog: fixed -->
- **The consumer drift audit now also checks `review_rb_judge_dispatch.yml` and `unblock_judge_dispatch.yml`.** Before this, it compared only wrappers named `ai-*.yml`, so a consumer could report clean while either dispatch wrapper was missing or stale.

`scripts/audit_consumer_drift.py` now takes the expected wrappers from the `ai-*.yml` templates plus every wrapper listed in `workflow-templates/profiles/full.txt`, the full-profile install list the workflow sync uses. A listed name counts only when it is a plain `*.yml` file name with no path and the template exists. Both dispatch wrappers are pinned to the release commit the same way as the others.

What this means for operators: a consumer that lacks either dispatch wrapper, or has an old copy, is now reported as drift (`missing=true` when it is absent), the same rule every other wrapper follows. When the manifest is missing or unreadable, the audit checks only the `ai-*.yml` templates, as before. Each consumer that has these files costs at most two more file reads per audit.
