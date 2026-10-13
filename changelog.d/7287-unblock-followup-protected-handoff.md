<!-- changelog: fixed -->
- **A merged unblock fix-up no longer resumes an issue straight into the automation-path guard that blocked it.** Applies only with `UNBLOCK_PROTECTED_HANDOFF_ENABLED=true` (issue #7287, an activation gap of PR #7066 seen on #6887).

The protected-path handoff (#7060) was checked only after the judge's fix-up wait. When a `descope` or `operator_step` fix-up filed before the handoff existed merged, the judge posted the issue's resume command (`/approved`, `/answer` or `/reclarify`), and the next implementation run hit the same automation-path rejection. Now, before that resume, the judge reads the newest pipeline-authored guard comment on a `scope-blocked` issue. If it is still an automation-path rejection, the judge first re-checks the grant: when the issue's `files_touched` now grants every rejected path, it resumes the issue and marks the fix-up wait done (`outcome=followup_regrant`); if that resume fails, the wait stays open (`reason=followup_regrant_failed`). Otherwise, if a pipeline-authored `descope` / `operator_step` verdict was recorded after the rejection, the judge:
- records an `ai:operator-step` entry under its own key (`unblock-handoff-<n>`, so the original verdict's entry and placeholder stay in place), posts the handoff comment ending in `<!-- ai:unblock-wait:v1 item=<n> reason=operator_handoff run=<r> -->` and sends a WARNING (`scripts/unblock_actions.py followup-handoff`);
- marks the fix-up wait done and logs `UNBLOCK_JUDGE ... outcome=followup_handoff fixup=<N> run=<r>`;
- removes no label and posts no resume command, so the issue stays blocked for a person.

If a hold marker already exists, the wait is marked done and the run logs `outcome=skip reason=operator_handoff_pending` with `fixup=`. If a write fails, the wait stays open and the next judge run retries the handoff (`reason=followup_handoff_failed`); the WARNING says so.

What this means for operators:
- With the flag off (the default) nothing changes.
- A fix-up that followed a different verdict (for example `retry_budget`), or an issue whose newest guard comment is not an automation-path rejection, still resumes as before.
- Verdict, rejection and hold markers count only when posted by the pipeline's `GH_PAT` login. The handoff itself adds no GitHub reads; the grant re-check that runs first reads each source issue a re-issue names (one read per source issue, at most 8), as the existing re-grant check does.
