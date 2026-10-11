<!-- agents: section="Workflow architecture" -->
Workflow failure heal and non-retryable review/autofix failures (#6756):
right after its provenance gate, the intake routes a verified
`autofix_failure` report with a `NON_RETRYABLE_FAILURE_REASONS` reason
(directly, or for an `identical_failure_cap` report from the trusted
account's own failure markers on the head) away from heal issues. It posts
one PR comment, ending in
`<!-- ai:workflow-heal-non-retryable:v1 pr= head= reason= -->`, whose
remediation text depends on the reason; for the host-only stop it names the
paths from the trusted failure comment's **First error** line (#6750 / PR
#6535: `.claude/hooks/pr_merge_status_guard.py`). It then adds
`ai:needs-human`, sends a WARNING (which says when the label write failed),
and logs `WORKFLOW_HEAL skip reason=non_retryable_<suffix> outcome=diagnosed
label=applied|failed`. It reuses the provenance comment snapshot. A repeat
for the same pr/head/reason logs `outcome=duplicate label=present|reapplied|failed`
and re-adds the label only when the PR read by claim binding lacks it. A
failed comment post adds no label. The host-only stop (#6745) is no longer
dropped by the review/autofix reporter (`skip-reason --defer-host-only-autofix`)
or by the intake's early skip while routing is on; every intake path that
does not diagnose it still ends in `skip reason=host_only_conflict_manual_merge
stage=after_provenance`, so it never becomes a heal issue. The label reporter
skips the label event the diagnosis causes (`skip reason=non_retryable_diagnosed`)
while the account's newest failure, editor-summary or diagnosis comment is
that marker. Kill
switch: `WORKFLOW_HEAL_NON_RETRYABLE_ROUTING_ENABLED` (default `true`, wired in
`workflow-failure-heal-intake.yml`); `false` restores #6745's early host-only
skip and heal issues for the other two reasons.
