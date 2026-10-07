<!-- changelog: fixed -->
- **A failed standalone security-audit dispatch no longer bypasses the merge gate.** Eligible PRs remain on hold instead of merging unaudited.

A dispatch failure records a failed cycle in a PR comment, and a later review run retries the audit. Once the five cycles and the per-head retry attempts (`SECURITY_PASS_EXHAUSTED_HEAD_AUDIT_ATTEMPTS`, default 2) are used, the PR is labelled `ai:security-pass-failed`; a failed retry dispatch also counts as a used attempt. If that comment cannot be posted, the PR still stays on hold and the review run fails, so the unrecorded failure is visible and workflow recovery retries it. A consumer repository whose `ai-security-audit.yml` does not accept `pr_number` will keep failing dispatch until its wrapper is synced.

What this means for operators: sync the consumer wrapper via `ai-update-workflows.yml` to restore the audit, or set `SINGLE_ISSUE_SECURITY_PASS_ENABLED=false` to opt out of the pass.
