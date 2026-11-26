<!-- changelog: changed -->
- **The CI size watcher now warns before the 430,000-byte contract-test ceiling, and `review_autofix.yml` has 54 KB of headroom again.**

`review_autofix.yml` had grown to 429,400 bytes, 600 bytes under the 430,000-byte ceiling three contract tests enforce, while the `budget-watch` job only warned at 440,000 bytes. Nine open PRs went over the ceiling once merged with `main` and learned it from a red final CI. Five `codex-agent` step bodies moved to `scripts/review_autofix_step_*.sh` with their steps' `name:`, `id:`, `if:` and `env:` unchanged, so the workflow is now 375,578 bytes.

`scripts/ci_budget_watch.py` gains `WORKFLOW_TEST_CEILING_BYTES` (430,000), which the three size tests now import, and the `CI_BUDGET_WORKFLOW_WARN_BYTES` default drops from 440,000 to 400,000. Workflow-size findings are no longer held back by the re-file cooldown. Expected first effect: `test-and-mark-stable.yml` (407,289 bytes) and `implement.yml` (404,457 bytes) get `ai:ci-budget` issues on the next push to `main`.
