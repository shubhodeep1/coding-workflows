<!-- changelog: changed -->
- **The unblock judge no longer re-issues an issue whose own pull request is the work, and a re-issue closes the old pull request.** Issue #10241 (Refs #9892).

  - For an issue item the unblock judge now reads the issue's own open pull requests (same-repository `ai/issue-<n>` head, opened by the pipeline login) with their head SHA, merge state, pipeline `ai-review/head-gate` and `ci/final` states and the merge-lease wait label. The read shares one GraphQL call with the existing implementation-PR lookup, and the model sees the facts as `own_pull_requests`.
  - A `reissue` verdict on a standalone issue creates and closes nothing while one of those pull requests has a head-gate success, carries `ai:merge-lease-wait`, or could not be read (its head-gate included). One comment explains why, the round is not used, and `UNBLOCK_JUDGE item=<n> op=reissue outcome=refused reason=own_pr_green|own_pr_lease_wait|own_pr_unknown pr=<m>` is logged.
  - A re-issue that goes ahead comments "Superseded by #<new>" on each remaining own pull request and closes it. Its branch is kept, and `cancel_on_pr_close.yml` cancels its runs and frees its lease.
  - The merge-lease tick skips and unlabels a waiter whose `ai/issue-<n>` source issue is closed as not planned (`reason=source_issue_not_planned`), with one batched read per base, and the update-mode lease gate does not acquire a free lease for such a pull request. A failed read grants as before and is logged.
  - Switches: `UNBLOCK_REISSUE_OWN_PR_GUARD_ENABLED` and `MERGE_LEASE_SOURCE_ISSUE_GUARD_ENABLED`, both default `true`. Only `false` turns either off.

What this means for consumer repos: after the next `@stable` sync, the unblock judge keeps a green or lease-waiting implementation pull request instead of re-issuing its issue, and the merge lease stops granting slots to pull requests whose issue was closed as not planned. Set either repository variable to `false` to keep the old behaviour.
