<!-- changelog: fixed -->
- **`permission_prompts.py lookup` no longer answers "not found" for a report outside a 3-issue window.** It now checks every candidate before it says a blocked session has no report.

`lookup --session <id>` gives the operator's master poller the command behind a permission prompt that blocks an unattended session. When Claude Code Web's agent proxy refused `search/issues`, it checked only the 3 most recently updated `ai:permission-prompt` issues, so activity on three other pattern issues hid the session's report and `lookup` printed `found: false` (security finding #5126, A09:2021). The search path had the same 3-hit limit. `lookup` now reads every search hit and, when the search is refused, every `ai:permission-prompt` issue, newest-updated first. A labelled issue with 0 comments costs no comments read. When several issues hold reports for the session, `lookup` returns the newest one, not the one on the most recently updated issue, which another session's comment could have bumped. A search that answers `incomplete_results: true` on any page now exits 2 with an `error`, the same as a failed read, even when its partial hits hold a report, because a newer one may be missing. The output keys, exit codes, and the OWNER / MEMBER / COLLABORATOR rule are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Candidates checked before `found: false` | 3 before, all now |
| Search page size | 3 before, 100 per page now |
| Reads for a not-found lookup when search is refused (38 labelled issues, 2026-09-29) | 40 |

What this means for operators: a `found: false` from `lookup` now means the session has no immediate report, not that it fell outside a window, and a found report is the session's latest prompt. Exit 2 still means "unknown, try again".

### For contributors

The change is in `_lookup_candidates` and `lookup` in `.claude/scripts/permission_prompts.py` and its `workflow-templates/.claude/` twin. The search goes through `check_in_status._gh_api_paginated_object`, which now keeps `incomplete_results: true` from any page instead of only the first; any search read failure falls back to the labelled list, as before. `lookup` stops scanning at the first candidate whose `updated_at` is older than the newest report found, and it reads the comments of an issue whose body holds a report, since a comment report there is newer. `LOOKUP_MAX_HITS` is no longer used and stays defined for existing importers. The lookup tests in `tests/test_permission_prompts.py` load the twin.
