<!-- agents: section="Immutable consumer wrapper pins" -->
- Updater safeguards default on. `UPDATER_VERIFY_RELEASE_MANIFEST` and
  `UPDATER_PR_DELIVERY_ENABLED` resolve as `vars.<NAME> || 'true'` and
  `${<NAME>:-true}` in `update_workflows.yml`: unset, empty or `true` (any
  letter case) is on, `false` is off, and any other value is off with a
  `::warning::`. The `verify` job's "Publish verify result and enable
  auto-merge" step posts `ai-update-workflows/verify` with `github.token`
  first. When that is refused, because the consumer's wrapper predates
  `statuses: write`, it retries once with `GH_PAT` and logs `::warning::UPDATER_PR_VERIFY
  posted the status with GH_PAT ...`, so the update PR that fixes the wrapper
  still auto-merges. Only when both fail does it log `reason=status_post_failed`.
