<!-- changelog: security -->
- **The Claude issue queue watchdog now trusts only its own re-queue and exhausted markers.** Before this, a collaborator could post a forged `<!-- ai:claude-env-requeue-exhausted:v1 … -->` on an environment-blocked issue and permanently stop its automatic re-queue and its Telegram alert.

The env-requeue step of `claude-issue-queue-watchdog.yml` now reads the login of the account it posts with (its `GH_PAT`) once per run with `gh api user`. It passes that login to `claude_issue_route.py env-requeue-plan --watchdog-login`. An `ai:claude-env-requeue:v1` or `ai:claude-env-requeue-exhausted:v1` marker counts only when that account wrote it, compared case-insensitively. A copy from any other owner, member, or collaborator is treated as plain text. When the login cannot be read, the run re-queues and alerts nothing and logs `env_requeue_skipped reason=watchdog_login_unknown`. The closed-queue-item cleanup in the same step still runs. Blocker markers and `/reclarify` comments keep their current trust rule.

| The numbers that matter | Value |
| --- | --- |
| Finding | `forged-watchdog-exhaustion-marker` (#5135, medium, STRIDE: Tampering) |
| Extra GitHub API calls | 1 `GET /user` per hourly env-requeue run |
| Markers now bound to the watchdog's account | `ai:claude-env-requeue:v1`, `ai:claude-env-requeue-exhausted:v1` |

What this means for operators: nothing changes for issues the watchdog already handled, because it posted every existing marker with the same account. If you rotate `GH_PAT` to a different GitHub account, markers from the old account stop counting. Each blocked issue can then get up to two more re-queues and one more alert.

### For contributors

`env_requeue_decision` and `env_requeue_plan` in `scripts/claude_issue_route.py` take a new trailing keyword `watchdog_login` (default `""`). An empty value fails closed: the decision returns `skip` with reason `watchdog_login_unknown`, and the plan reads nothing and reports one error. A caller that forgets the login therefore never gets the old any-collaborator trust.
