<!-- changelog: fixed -->
- **Malformed check-run API output no longer looks like a clean review snapshot.** Empty or invalid responses are retried within the collector's wait budget; if still invalid, the review continues with an `api_error` snapshot rather than reporting zero checks as ready.

The collector validates every paginated check-run response before counting runs. A failed GitHub attempt followed by a successful retry keeps only the successful response, so the collector sees all checks on the PR head. Valid responses with an empty `check_runs` list remain supported.
