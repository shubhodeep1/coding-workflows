<!-- changelog: fixed -->
- **Integration final PRs no longer loop on a conflict the resolver can never land.** A conflict on `.ai/validate.yml` now resolves to the default branch's version, a non-retryable resolver failure escalates on the first run, and the poller parks the conflict instead of re-dispatching the resolver and the judge on an unchanged head.

On 2026-10-23, tele-funtoken-msg-scoring final PR #3933 (`orchestrator/project-3928` → `main`) conflicted on 24 files, including `.ai/validate.yml`. The resolver sandbox excludes `.ai/`, so every resolver run failed before any model ran (`sandbox_path_host_only`). The integration judge posted three diagnoses that contradicted each other. After three identical failures the review gate's cap stopped new runs, but the poller kept dispatching and spending the lifetime budget. Escalation never fired: `gh pr edit` could not write the retry state without the `read:org` scope.

| The numbers that matter | Value |
| --- | --- |
| Failed resolver runs on #3933 before any model ran | 3 (39882356505, 39886330204, 39887520970) |
| Failed retry-state writes (`gh pr edit`) | 5 of 5 |
| Identical failures needed to escalate before | 20 (`RESOLVER_ESCAPE_THRESHOLD_N` 5 × 4 tiers) |
| Identical failures after which the review gate stops a head | 3 (`REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL`) |
| Non-retryable failures needed to escalate now | 1 |
| New GitHub API calls per poll tick | 0 |

- `scripts/review_conflict_prepare.sh` takes the default branch's `.ai/validate.yml` on `orchestrator/project-*` heads (`CONFLICT_RESOLVER_AI_ASSET_TAKE_BASE`). The integration branch's dropped diff is posted on the PR, and the merge commit still waits for the integration fingerprint check. Switch: `CONFLICT_AI_ASSET_TAKE_BASE_ENABLED` (default `true`).
- `scripts/review_conflict_resolve.sh` writes `AUTOFIX_RESOLVER_RETRY_STATE_V1` with `gh api -X PATCH repos/{owner}/{repo}/pulls/{n}` and the `ai:resolver-escalated` label over REST. `sandbox_path_host_only`, `sandbox_path_unsupported` and `sandbox_support_missing` escalate on the first failure (`RESOLVER_NON_RETRYABLE_ESCALATE_ENABLED`, default `true`). The state now also records the failure reason, run id and refused paths.
- `scripts/orchestrate_poll_process.sh` parks the conflict (`INTEGRATION_CONFLICT_PARKED`, `integration_sync_status=escalated`) when the final PR's retry state shows a non-retryable failure, or `INTEGRATION_CONFLICT_PARK_GATE_CAP` identical failures, on the unchanged head. No lifetime slot is spent, and a new head resumes healing. Switch: `INTEGRATION_CONFLICT_PARK_ENABLED` (default `true`).
- The integration judge prompt now includes the last resolver failure and the judge's own previous diagnosis for the PR. Its "only lines from either merge side" rule is replaced by the resolver's synthesize rule.

What this means for consumer repos: the next `@stable` sync stops an integration final PR from spending its whole `INTEGRATION_CONFLICT_LIFETIME_MAX` budget on a head nothing can fix. It now either merges past a `.ai/validate.yml` conflict or parks with a tracking comment that names the reason, the run and the paths. Re-apply any integration-branch changes to `.ai/validate.yml` that the take-base comment shows as dropped, if you still need them.

### For contributors

Scoping security-pass findings to a project's own diff, and a judge verdict that lets the default branch's design supersede a project's, are planned in `docs/plans/integration-conflict-duplicate-fix-prevention-plan.md` and not implemented here. Tests: `tests/test_conflict_ai_asset_take_base.py`, `tests/test_integration_conflict_park.py`, plus additions to `tests/test_review_conflict_resolve_retry_state.py`.
