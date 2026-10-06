<!-- changelog: fixed -->
- **Review sandbox cleanup no longer skips an editor commit.** Cleanup runs after the commit but before publication, and restores directory permissions when isolated tools leave read-only caches behind. An unremovable sandbox still fails the job with counts-only diagnostics instead of misreporting the editor's changes as lost.

The review job previously cleaned its isolated workspace before `Commit changes`; a cleanup failure triggered the commit step's implicit `success()` guard and a misleading `editor_changes_lost` alert (PR #6209, run 37431425681). The cleanup now stays fatal ahead of push and auto-merge while keeping sandbox-controlled filenames out of diagnostics. Tests: `tests/test_review_autofix_review_pipeline_contract.py`.
