<!-- changelog: fixed -->
- **The README's `ENABLE_STALL_HUMAN_TERMINALIZATION` rows now document the closed/merged implementation-PR exception.** Both the orchestrate_poll env table row and the defaults table row list it after the existing `MAX_JUDGE_REPLAY` exception.

When the stall judge returns `escalate_human`, the linked PR is closed or merged, and `_pr_json_is_issue_implementation_pr` accepts it as the issue's own implementation PR, the escalation is kept even when `ENABLE_STALL_HUMAN_TERMINALIZATION` is `false`, because `attempt_merge` cannot act on that PR; the issue then gets `ai:needs-human`. A closed PR that only mentions the issue is still normalised to `attempt_merge`. The code change ships with #9482; this entry only changes docs. Refs #9482.

What this means for consumer repos: no behaviour change from this entry.
