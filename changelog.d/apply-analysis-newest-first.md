<!-- changelog: changed -->
- **The promote cycle's proving and verifying runs now take the newest analysis doc first.** `scripts/apply_analysis_on_main.sh` used to hand the orchestrator the oldest unprocessed `analysis/workflow-optimization-*.md` doc. It now picks the newest one.

The selection uses a reverse version sort of the dated filenames, so `2026-10-21-10` comes before `2026-10-21-5`, which comes before `2026-10-21`. Docs already recorded in `analysis/recommendation-processing-report.md`, docs with a trusted dispatch marker, and docs already deleted on the dispatch branch tip are still skipped, now in newest-to-oldest order. The `APPLY_ANALYSIS_CANDIDATES` list-only output lists docs in the same order. Older docs stay in the queue and are reached once the newer ones are processed, or removed by the log analysis's 30-day retention purge.

What this means for operators: each daily cycle now works on the findings closest to the current code. Most old findings are already fixed or out of date by the time they are reached.
