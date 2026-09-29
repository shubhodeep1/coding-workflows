<!-- changelog: security -->
- **Large issue bodies can no longer switch off the orchestrator's close sweep.** `close_merged_issues_sweep` now streams its issue lists to `jq` on stdin, and it reports a queue it cannot build instead of silently treating it as empty.

Since issue #4813, the sweep in `scripts/orchestrate_poll_process.sh` fetches each open `ai:merged` and `ai:ready-to-merge` issue's body so it can read the issue's `Integration branch:` line. It then passed both lists to `jq` as `--argjson` command-line arguments. Linux limits one argument to 128 KiB, and GitHub allows 64 KiB per issue body, so three large bodies with the same label made `jq` fail to start. The `|| echo "[]"` fallback then turned the queue into an empty list, and the sweep closed nothing, with no log line saying why (security finding #4955, `STRIDE: Denial of Service`, medium). Anyone who can open or edit an issue that later carries one of those labels could trigger it. The lists now reach `jq` through a pipe, which has no size limit. When the queue still cannot be built, for example because a list is not valid JSON, the sweep logs `::warning::CLOSE_MERGED_SWEEP queue_build_failed merged_bytes=<n> ready_bytes=<n> — skipping this cycle.` with the start of `jq`'s error, and tries again on the next poll.

| The numbers that matter | Value |
| --- | --- |
| Linux per-argument limit (`MAX_ARG_STRLEN`) | 131,072 bytes |
| GitHub issue body limit | 65,536 characters |
| Bodies that silently disabled the sweep | three 61,440-byte bodies (a 184,506-byte list) |
| New GitHub API calls | 0 |

What this means for operators: merged issues keep closing however long their bodies are. A sweep that cannot read its queue now shows up as a `queue_build_failed` warning in the poller log instead of a quiet `Found 0 open issue(s)`. `ENABLE_CLOSE_MERGED_ISSUES=false` is still the emergency stop.

### For contributors

`tests/test_orchestrate_poll_process.py` adds `test_close_merged_issues_sweep_closes_issues_with_large_bodies`, which fails on the old `--argjson` call, and `test_close_merged_issues_sweep_surfaces_unparseable_issue_list`. The latter uses a new opt-in `mock_gh_issue_list_raw_by_label` store key, set through `mock_store_extra`, which returns raw text for the sweep's `--json number,labels,body` listing of one label.
