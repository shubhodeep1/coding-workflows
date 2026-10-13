<!-- agents: section="Workflow architecture" -->
Check-failure triage can also route its fix with a `Target branch:` line:
with `CHECK_TRIAGE_PR_BRANCH_ROUTING_ENABLED=true` (default `false`) a new
triage issue carries one `Target branch: <head ref>` line, written only for a
verified open same-repository PR head that is not the default branch. A
verified route bypasses the base-branch gate (`base_gate outcome=file
reason=target_branch_route`), because the fix lands on the PR's branch. The
diagnose step re-checks the flag, the ref shape and that the route equals the
recorded head ref (`routing outcome=skip reason=handoff_mismatch` otherwise);
the final body check allows exactly that line, once, above the first `---`.
