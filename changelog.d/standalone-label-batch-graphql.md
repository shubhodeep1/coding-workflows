<!-- changelog: changed -->
- **Standalone stall recovery lists its pipeline-label issues with one GraphQL request instead of seven `gh issue list` calls.** Each poll tick used to make one REST listing per label (`ai:clarification`, `ai:planning`, `ai:awaiting-approval`, `ai:implementing`, `ai:done`, `ai:ready-to-merge`, `ai:review-blocked`); it now asks for all seven at once.

The candidate issues are the same as before. When the batch cannot cover a label (more than 100 open issues for it, a missing answer for it, or an error that names it), only that label is listed over REST as before. When the whole request fails or its answer cannot be read, all seven labels are listed over REST, the old path. Each fallback writes one `::notice::Standalone stall recovery: label GraphQL batch fell back to REST for: <labels>` line.

| The numbers that matter | Value |
| --- | --- |
| GitHub API calls per tick for this listing, normal case | 1 (was 7) |
| On overflow or a per-label error | 1 plus one REST call per affected label |
| On total failure | 1 failed request plus the 7 old REST calls |
| New variables, labels or log prefixes | none |

What this means for operators: the poller spends fewer GitHub API calls on every tick, and nothing else changes.

### For contributors

The helper is `_fetch_standalone_pipeline_label_issues_graphql` in `scripts/orchestrate_poll_process.sh`, next to `_fetch_standalone_marker_issues_graphql`. It reads `repository.issues(labels:)`, not `search`, because the search index can lag. Owner and name go in as `-f` variables, and the fixed label literals are checked against `^[A-Za-z0-9:._-]+$` before they are written into the query. The test fake answers the batch only when `mock_pipeline_label_graphql=True`; without it, every label takes the REST path as before.
