<!-- changelog: fixed -->
- **A PR whose CI fails after an update from its base branch now goes back to review and autofix instead of sitting with red CI.**

When the merge-base freshness gate updates an approved PR from its base, the resulting head skips the reviewers and only waits for CI before merging (#7084). If that CI failed, the merge was refused and nothing ran review or autofix on the PR again. PR #7153, the security-pass fix for orchestrator project #6664, sat that way from 2026-10-11. The `deterministic-skip-merge` job in `.github/workflows/review_autofix.yml` now runs `scripts/review_freshness_ci_failed.sh`. It marks the head with a PR comment and dispatches the PR's review workflow, and `scripts/review_head_gate.sh` reviews a marked head normally, so autofix sees the failing checks. The new repository variable `FRESHNESS_CI_FAILED_REDISPATCH_ENABLED` is on by default; set it to `false` to turn it off.

What this means for operators: an approved PR no longer stalls silently after a base update breaks its CI; it gets another autofix round like any other PR with failing checks.
