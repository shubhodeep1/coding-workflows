<!-- changelog: fixed -->
- **Apply-analysis no longer dispatches a doc main has already deleted, and the daily purge keeps docs that open projects still read.** A promote cycle that read its docs from an older checkout can no longer start a project whose plan cannot find its source doc.

The promote cycle reads `analysis/workflow-optimization-*.md` from the checkout it pinned when it started. The daily log analysis deletes reports older than 30 days from main. On 2026-10-10 the purge removed `analysis/workflow-optimization-2026-09-09.md` at 01:03, the cycle dispatched project #7021 for it at 02:59, and planning for #7022 blocked with "source doc unavailable". `scripts/apply_analysis_on_main.sh` now checks that the selected doc still exists on the dispatch branch tip and otherwise moves on to the next doc. The purge in `workflow-log-analysis.yml` keeps every doc an open apply-analysis project still names.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls per dispatch | 1 contents read per doc checked |
| Extra API calls per purge | 1 issue list + 1 comment read per open tracking issue |
| New log lines | `APPLY_ANALYSIS_DOC_GONE`, `APPLY_ANALYSIS_IN_FLIGHT_DOCS`, `WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED` |

What this means for operators: an apply-analysis project always starts on a doc its plan can read, and a project that runs past the 30-day mark keeps its doc until its own final PR deletes it.
