<!-- changelog: changed -->
- **The apply-analysis dispatcher bounds its marker searches.** The workflow log analysis purge now keeps every doc the processing report does not list, so the `analysis/` backlog grows without limit. `scripts/apply_analysis_on_main.sh` used to make one `search/issues` call (plus up to 20 comment reads) for every unlisted doc, in both list-only and dispatch mode. Its scan, still newest first, now stops at whichever limit comes first:

- **Candidate cap.** At most `APPLY_ANALYSIS_MAX_CANDIDATES` (default `3`) unprocessed docs are listed. The cap is raised to `PROMOTE_CYCLE_MIN_DOCS` when that is larger, and `APPLY_ANALYSIS_CANDIDATE_CAP raised_to=<n> reason=promote_cycle_min_docs` goes to stderr. The promote cycle now passes `PROMOTE_CYCLE_MIN_DOCS` to the dispatcher.
- **Marker cap.** After `APPLY_ANALYSIS_MAX_MARKER_SEARCHES` (default `20`) docs skipped for a trusted dispatch marker, the scan stops if another doc still needs a search (stderr `APPLY_ANALYSIS_MARKER_SEARCH_CAP reached=<n> next=<path>`). List-only mode appends ` capped_behind_markers=true` to its `APPLY_ANALYSIS_CANDIDATES` line. Dispatch mode still dispatches any doc it found; with none, it logs `APPLY_ANALYSIS_SKIPPED reason=scan_capped_behind_markers searches=<n>` instead of `all_docs_processed`.
- **Promote cycle.** A capped list with fewer than `PROMOTE_CYCLE_MIN_DOCS` docs skips as `PROMOTE_CYCLE_SKIPPED reason=scan_capped_behind_markers available=<n> required=<m>`, with the same text as a `::warning::`. An uncapped low count still skips as `insufficient_docs`.

| The numbers that matter | Value |
| --- | --- |
| Most `search/issues` calls per run (defaults) | 23 (3 + 20) |
| New GitHub API calls | 0 |
| New repo variables | none (both caps are script-environment defaults) |

What this means for consumer repos: nothing to configure. A backlog whose newest 20 undecided docs were all dispatched before now stops the cycle with a warning; list those docs in the processing report, or raise `APPLY_ANALYSIS_MAX_MARKER_SEARCHES`.

Refs #9116
