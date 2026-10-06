<!-- changelog: security -->
- **Workflow failure heal now checks the runs a label escalation links before it reads their logs, so a comment can no longer point the intake at an unrelated failed run.** Refs #3576.

When an escalation label (`ai:needs-human`, `ai:scope-blocked`, ...) lands on an issue or pull request, the reporter sends the failed runs it found to this repository's intake. The intake used to fetch those runs' job logs without further checks. The logs then went into the diagnosis prompt and could be reproduced in the heal issue. The reporter also put any run linked in any comment first, so an outside commenter could choose which runs fill the three-run cap.

The intake now reads each referenced run from GitHub before collecting jobs, and keeps the run only when all of the following hold:

- it is in the escalated issue's repository and has the claimed id;
- it completed with `failure`, `timed_out` or `cancelled`;
- it is tied to the escalated issue or PR: a comment from an OWNER, MEMBER, COLLABORATOR or `github-actions[bot]` links it (in this repository, only the pipeline account counts), or, for a pull request, it belongs to that pull request (`pull_requests` or a `[pr:<N>]` dispatch name). A matching title alone cannot identify the escalated item.

Runs that fail a check are dropped and logged. The escalation itself still proceeds, without those logs. The reporter now lets only links from the issue's trusted author or a trusted comment reorder its title-matched runs.

| The numbers that matter | Value |
| --- | --- |
| Extra GitHub API calls per label report | at most 3 run reads, 1 paginated comment read, and 1 `/user` read (this repository only) |
| New log lines | `WORKFLOW_HEAL provenance_rejected ... kind=issue\|pull_request ... reason=<r>` and `provenance_verified ... runs=<n>` (existing formats) |
| Rejection reasons | `run_lookup_failed`, `repo_mismatch`, `run_id_mismatch`, `not_failed`, `not_linked_to_issue` |

What this means for consumer repos: nothing to change. The check runs in this repository's intake. If a label escalation has no trusted run link or PR association, the report still proceeds, but without those runs' logs.

### For contributors

`verify_run_provenance` in `scripts/workflow_failure_heal.py` now handles `issue` / `pull_request` reports through `_verify_label_run_refs`. It always returns `status: ok`, with `reason: no_verified_runs` when nothing is left; it no longer returns `not_applicable` for them. The new `_trusted_run_link_comment` helper decides which comments may vouch for a run link. The reporter's comment projection now keeps `user.login` and `author_association`. Tests: `test_verify_run_provenance_label_escalations`, `test_build_issue_payload_ignores_untrusted_comment_priority` and `test_intake_verifies_label_escalation_run_refs_before_reading_logs` in `tests/test_workflow_failure_heal.py`.
