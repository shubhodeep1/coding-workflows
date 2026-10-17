<!-- changelog: fixed -->
- **Clarify now holds an issue that CI-triage routing refuses as `label_missing`, instead of failing every run.** The issue gets one comment explaining the refusal and the `ai:blocked` label, and clarify stops there.

An issue that carries the `check-failure-triage:` header markers without the `ai:check-triage` label is refused by `scripts/resolve_integration_ref.sh` with `reason=label_missing`; on an `ai:orchestrator-managed` issue this also means its `- Tracking issue (untrusted): #N` line is missing. Before this, clarify's "Resolve integration ref" step exited 1 on that refusal. The same refusal repeated on every run and filed a new workflow failure heal report each time (#7511 and #7515, heal #7516).

Clarify now treats only that refusal for the issue being clarified as a hold:

- it posts one comment that names what is missing and how to fix it, ending in `<!-- ai:integration-ref-label-missing:v1 issue=<n> -->`; a marker counts only when the `GH_PAT` account wrote it, so a rerun posts no second comment;
- it adds `ai:blocked` after the comment;
- it writes the step output `label_missing_hold=true`, and every later clarify step skips, so nothing is checked out or run on the default branch;
- it logs `INTEGRATION_REF_RESOLVE outcome=held reason=label_missing issue=<n> comment=posted|present label=applied` and the run succeeds, so no heal report is filed.

If the account login, the comment read, the comment or the label cannot be written, the step still fails (`INTEGRATION_REF_RESOLVE outcome=hold_failed reason=identity_unavailable|comments_unavailable|comment_failed|label_failed`). Every other refusal still fails clarify, and plan, implement, clarify-respond and validate are unchanged.

The "Semantic cache lookup", "Write run summary" and "Telegram failure notification" steps now skip with a warning when the job stopped before the runtime workspace or support scripts were set up, so the real error stays the run's first error.

What this means for operators: to release a held issue, either restore `ai:check-triage` and remove `ai:orchestrator-managed`, or add the canonical tracking line to the issue body, then comment `/reclarify`. For #7511 and #7515, whose pull requests have merged, restoring the labels lets clarify close them through the check-triage resolution gate.

### For contributors

The labels on #7511 and #7515 most likely come from stall recovery's close-and-reissue in `scripts/orchestrate_poll_process.sh` (`execute_stall_recovery_action`), which copies the original body, always adds `ai:orchestrator-managed`, carries `ai:workflow-heal` but not `ai:check-triage`, and writes no tracking line. That script is outside this fix's scope and is left to a follow-up issue. The hold also covers any other writer that produces the same shape.
