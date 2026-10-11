<!-- changelog: fixed -->
- **The workflow failure heal intake no longer files heal issues for review/autofix failures another run cannot fix.** A verified `autofix_failure` report whose reason is non-retryable (`conflict_resolver_sandbox_path_host_only`, `conflict_resolver_sandbox_path_unsupported` or `conflict_resolver_sandbox_support_missing`) now gets one diagnosis comment on the pull request instead of a heal issue. The comment's remediation depends on the reason: for the host-only stop it names the conflicted host-only paths, such as `.claude/hooks/pr_merge_status_guard.py`, and says they need a manual merge; for missing resolver support it points at workflow support. The intake also labels the PR `ai:needs-human` and sends a Telegram WARNING. The check runs only after the report's run provenance is verified. An `identical_failure_cap` report is routed this way only when the trusted account's own failure markers on the head carry a non-retryable reason. The review/autofix reporter now forwards host-only stops to the intake instead of dropping them (#6745); any intake path that does not diagnose one still skips it as `host_only_conflict_manual_merge`, so it never becomes a heal issue.

  Before this change, such failures became heal issues that the pipeline could never implement: #6750 was filed for PR #6535, which conflicted on a host-executed hook that the resolver sandbox refuses by design.

  | Item | Value |
  |---|---|
  | Log line | `WORKFLOW_HEAL skip reason=non_retryable_<suffix> outcome=diagnosed\|duplicate label=<status>` (`non_retryable_host_only` for host-only paths) |
  | PR marker | `<!-- ai:workflow-heal-non-retryable:v1 pr=<n> head=<sha> reason=<r> -->`, trusted only from the intake's account |
  | Kill switch | `WORKFLOW_HEAL_NON_RETRYABLE_ROUTING_ENABLED` (default `true`, passed by `workflow-failure-heal-intake.yml`); `false` keeps host-only stops skipped and heals the other two reasons |
  | Reporter skip | `WORKFLOW_HEAL_REPORT skip reason=non_retryable_diagnosed` |

  What this means for operators: a PR that hits a host-only conflict is labelled `ai:needs-human` with an explanation, and no heal issue is opened. Merge the conflict by hand, push, and remove the label. A repeated report for the same head posts nothing and sends no alert; it only re-adds `ai:needs-human` if the first label write failed. The `ai:needs-human` label that the intake applies does not start another heal report, unless the PR fails again after the diagnosis.
