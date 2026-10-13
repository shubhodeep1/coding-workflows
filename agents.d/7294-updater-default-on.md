<!-- agents: section="Immutable consumer wrapper pins" -->
- Updater safeguards default on. `UPDATER_VERIFY_RELEASE_MANIFEST` and
  `UPDATER_PR_DELIVERY_ENABLED` resolve as `vars.<NAME> || 'true'` and
  `${<NAME>:-true}` in `update_workflows.yml`: unset, empty or `true` (any
  letter case) is on, `false` is off, and any other value is off with a
  `::warning::`. The `verify` job posts every `ai-update-workflows/verify`
  status (the pending post in "Validate verify inputs and mark pending", the
  success post in "Publish verify result and enable auto-merge" and the post
  in "Mark verify failure") with `github.token` first. When that is refused,
  because the consumer's wrapper predates `statuses: write`, it retries once
  with `GH_PAT` and logs `::warning::UPDATER_PR_VERIFY posted the status with
  GH_PAT ...` (the failure post only warns), so the update PR that fixes the
  wrapper still auto-merges. Only when both tokens are refused do the pending
  and success posts log `reason=status_post_failed` and fail the job.
