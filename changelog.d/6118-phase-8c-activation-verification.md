<!-- changelog: added -->
- **Merged work is now checked for whether it will actually run.** After a PR merges into the default branch, and when an orchestrator project completes, an activation verifier grades the work LIVE or DORMANT and acts on every gap it finds.

Port P4 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` (Phase 8c). The new `activation-verify` job in `issue_pr_status.yml`, and the poller at every project completion path, run `scripts/activation_verify.sh` with the ACTIVATION_VERIFY role (`prompts/mode-activation-verify.txt`, built from the activation scope of `/verify-activation`). The verdict is posted on the linked issue or the tracking issue. Gaps that code in the repository can close become one standalone issue that the normal pipeline implements. Gaps only a person can close (a secret, a repository variable, a release) go to the repository's single `ai:operator-step` issue, and the project keeps going. The model reads the merged code read-only and gets no GitHub or Telegram credentials.

| The numbers that matter | Value |
| --- | --- |
| Verdicts | `LIVE`, `DORMANT` |
| New repository variables | `ACTIVATION_VERIFY_ENABLED` (default `true`), `ACTIVATION_VERIFY_MODEL`, `THINKING_LEVEL_ACTIVATION_VERIFY` |
| New label | `ai:operator-step` |
| Model time limit at project completion | 15 minutes |
| New log prefix | `ACTIVATION_VERIFY` |

What this means for operators: watch the `ai:operator-step` issue. It lists, per merge or project, the exact steps only you can take, and each step names the flag that keeps its feature off until you do. Everything else is fixed by the pipeline. Set `ACTIVATION_VERIFY_ENABLED=false` to turn the check off.

### For contributors

`scripts/operator_step_issue.py upsert` is the only writer of the `ai:operator-step` issue (one keyed section per source, replaced in place; the unblock judge's `operator_step` verdict reuses it). It reconciles observed duplicate trackers, but the issue-body PATCH is not atomic across concurrent writers and can still lose an entry. Failed code-gap searches leave a non-terminal comment instead of creating an unverified duplicate issue. Completed-project poll ticks retry only a trusted partial verdict while fewer than three partial comments exist and within 30 minutes of the first; failed comment writes may cause another attempt in that window but cannot keep the model running indefinitely. A merge that closes an activation-fix issue is not verified again, so fixes never loop. `tests/test_activation_verify.py` runs in its own `ci.yml` step.
