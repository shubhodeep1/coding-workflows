<!-- changelog: changed -->
- **CI triage no longer opens one issue per pull request for a failure `main` already has and a heal issue already tracks, and a planning block that only waits on another open PR now pages at WARNING.**

When the `ubuntu-latest` runners moved to Ubuntu 26.04, CI on `main` broke and heal issue #9215 was filed for it. Check-failure triage then filed a separate issue for each open PR whose CI failed the same way (#9224, #9228, #9230, #9231, #9232). Each one reached planning, found that PR #9246 already delivers the fix, and sent a CRITICAL "Planning blocked: human input required" page. Now `scripts/check_failure_triage.sh` checks for an open `ai:workflow-heal` issue first. If the newest CI run on the default branch failed and such an issue tracks that same workflow file on that branch, triage files no issue (`CHECK_TRIAGE skip reason=base_failure_tracked`) and the new "Note PR failure on its base-branch heal issue" step adds one comment to the heal issue naming the PR. Separately, `plan.yml` now treats the planner's `work is delivered by open PR #<N>; resume once it is merged` block as a wait: the issue comment says the unblock judge resumes it after that PR merges, and the Telegram page is WARNING instead of CRITICAL. Every other block reason keeps the old comment and the CRITICAL page.

| The numbers that matter | Value |
| --- | --- |
| Per-PR triage issues filed for the #9215 breakage | 5 |
| CRITICAL pages they produced on 2026-11-11 | 5 |
| Extra API reads per triage run | 1, only when the default branch failed the same workflow |
| Kill switch | `CHECK_TRIAGE_BASE_TRACKED_SKIP_ENABLED` (default `true`; repository variable, forwarded by `check_failure_triage.yml`) |

What this means for operators: a red `main` produces one heal issue and comments on it, not an issue and a CRITICAL page for every open PR. A planning block that is only waiting for another PR to merge no longer asks you for input.

### For contributors

The skip applies only to a `workflow_run` triage (not a `check_run` event), only when the PR's base is the repository's default branch, and only when the base run's conclusion is `failure`. A heal issue matches when its author is OWNER/MEMBER/COLLABORATOR, its fourth line is `<!-- workflow-failure-heal:source=<repo>#run -->`, one of its first seven lines is `<!-- workflow-failure-heal:workflow=<path> -->`, and it has the exact line ``- **Target branch:** `<base>` ``. A PR whose base is an orchestrator integration branch is still triaged per PR. The existing run read now returns the workflow id and `.path` together, so the only new call is the heal-issue listing.
