<!-- changelog: fixed -->
- **The unblock judge no longer re-issues a CI-failure issue whose pull request already merged with green checks.** Follow-up to #6813, the second re-issue in the chain #6789 → #6801 → #6813, made after PR #6786 had already merged.

Before a `reissue` verdict on a standalone, non-security issue, `scripts/unblock_judge.sh` now looks up the PR that owns the CI failure. It finds the PR from the `ai:check-triage` / `ai:check-triage-escalated` label or the `<!-- check-failure-triage:pr=N -->` marker, and from the issue body's `**Pull request:**` line in the same repository.
- Merged into the default branch, or into the issue's declared integration branch, with the shared required-checks gate (`_pr_checks_completed` in `scripts/pr_checks_lib.sh`) green on its head: the issue is closed as not planned, with one comment naming the PR and its head SHA, and no replacement is created. Log: `UNBLOCK_JUDGE item=<n> op=reissue outcome=superseded pr=<n> head=<sha>`.
- Open, with its head in the same repository: the replacement starts with the triage PR marker and ends with a standalone `- Integration branch: \`<head ref>\`` line, taken from the PR API head ref, never from the PR base or model text.
- Otherwise verified (for example merged with failing checks): the replacement carries the marker, so a later reissue still finds the PR.
- Unreadable, fork-headed, conflicting or foreign-repository PRs: the re-issue is unchanged.

Security findings and project-tracked items keep their current re-issue paths.

| The numbers that matter | Value |
| --- | --- |
| GitHub API reads added, only for a standalone CI-failure reissue | 1 PR read, then the check gate's reads or 1 `git/ref` read |
| New environment variables | 0 |
