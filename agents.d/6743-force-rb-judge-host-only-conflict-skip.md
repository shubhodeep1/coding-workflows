<!-- agents: section="Workflow architecture" -->
Review-blocked judge after a host-only resolver refusal (issue #6743, heal
#6733): on a `force_rb_judge` dispatch of `review_autofix.yml`, the `gate` job
reads the newest `review-autofix-failure:v1` marker that the `GH_PAT` account
wrote for the current head under the current review-support version. When its
reason is exactly `conflict_resolver_sandbox_path_host_only`, the gate output
`force_rb_judge_conflict_skip=true` makes `codex-agent` skip Detect merge
conflicts and both resolver steps, so the review-blocked judge runs instead of
the resolver refusing again. Markers from another author, head or support
version, a newer marker with another reason (including `provider_unavailable`,
which the cap's count passes over; the helper reports it as `newest_reason=`), or a failed identity or comment
read keep the conflict steps. The check runs even with
`REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED=false`; its own kill switch is
`REVIEW_FORCE_RB_JUDGE_HOST_ONLY_SKIP_ENABLED` (default `true`). The resolver's
host-only refusal and the review-blocked handoff are unchanged.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `AUTOFIX_FORCE_RB_JUDGE_CONFLICT_SKIP` (`review_autofix.yml` gate: `pr= head= outcome=skip|run reason=host_only_marker|disabled|head_sha_unavailable|helper_missing|<comment fetch state>|parse_error|no_trusted_marker|reason_mismatch|newest_reason_mismatch marker_reason=`)
LOG_PREFIX.name=AUTOFIX_FORCE_RB_JUDGE_CONFLICT_SKIP
