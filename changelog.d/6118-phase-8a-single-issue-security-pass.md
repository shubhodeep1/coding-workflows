<!-- changelog: added -->
- **Standalone PRs now get a security audit before they auto-merge.** After a clean review, a PR into the default branch waits until a security audit of its head comes back clean, the same gate an orchestrator project already passes.

Port P1 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` (Phase 8a). The new "Single-issue security pass" step in `review_autofix.yml` runs right before "Enable auto-merge on PR" and holds both merge steps until a trusted `<!-- ai:single-issue-security-pass:v1 status=clean head=<sha> -->` marker exists for the current head. If none exists, it dispatches `security-audit.yml` (consumers: `ai-security-audit.yml`) for the PR's head branch with the new `pr_number` input. The audit posts its result on the PR and re-runs the review on clean or failed. Findings become follow-up issues that target the PR branch, so their merges start the next cycle. The pass skips orchestrator child and integration PRs, fork heads, `e2e-smoke-test` PRs, and PRs whose linked issue is a verified automation follow-up (`scripts/security_pass_skip.py`), so follow-ups never recurse.

| The numbers that matter | Value |
| --- | --- |
| Audit cycles per PR | 5 (`MAX_SECURITY_PASS_CYCLES`) |
| A pending audit is treated as lost after | 6 hours |
| New repository variable | `SINGLE_ISSUE_SECURITY_PASS_ENABLED`, default `true` |
| New `security-audit.yml` / `ai-security-audit.yml` input | `pr_number` |
| New log prefix | `SINGLE_ISSUE_SECURITY_PASS` |

What this means for operators: a standalone PR merges only after its own audit is clean, with no waiver path. A PR still failing after five cycles is labelled `ai:security-pass-failed` and handed to the unblock judge. Until a consumer's `ai-security-audit.yml` wrapper is synced with the `pr_number` input, the dispatch fails and the PR merges as before, with a warning. Set `SINGLE_ISSUE_SECURITY_PASS_ENABLED=false` to turn the pass off.

### For contributors

`scripts/review_single_issue_security_pass.sh` has two modes: `gate` (review job; it reuses the PR payload and comments the job already fetched) and `report` (the new "Report single-issue security pass" step in `security-audit.yml`, which reads the audit's summary line from the tee'd run log). The project-level security-exhaustion judge works on orchestrator project state, so a single PR goes straight to the unblock judge on exhaustion. `tests/test_single_issue_security_pass.py` runs in its own `ci.yml` step.
