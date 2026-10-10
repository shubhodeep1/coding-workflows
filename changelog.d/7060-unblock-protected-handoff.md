<!-- changelog: added -->
- **The unblock judge can hand a protected-path rejection to a person instead of filing a fix-up that would hit the same guard.** Off by default; turn it on with `UNBLOCK_PROTECTED_HANDOFF_ENABLED=true` (issue #7060, from the unblock judge's `operator_step` verdict on #6887).

When the automation-path grant guard rejects an implementation run, any fix-up issue the unblock judge files for `descope` or `operator_step` touches the same ungranted paths and is rejected again. With the flag on, the judge reads the newest pipeline-authored guard comment on the issue (`scripts/unblock_ledger.py protected-rejection`). If it is an automation-path rejection, these two verdicts:
- post a handoff comment naming the rejected run and paths, ending in `<!-- ai:unblock-wait:v1 item=<n> reason=operator_handoff run=<r> -->`;
- record an `ai:operator-step` entry and send a Telegram WARNING;
- create no fix-up, remove no label and post no resume command, so the issue stays blocked.

Later judge runs on that issue skip before the model with `UNBLOCK_JUDGE ... outcome=skip reason=operator_handoff_pending` until a newer guard comment appears or a person clears the block label. Without the hold, the round cap would end in the terminal close.

| The numbers that matter | Value |
| --- | --- |
| Fix-up issues filed against a protected-path rejection (flag on) | 0 |
| Changes to the implement guards or `override_guard` eligibility | none |
| New GitHub API calls | 0 |

What this means for operators:
- With the flag off (the default) nothing changes.
- The automation-path rejection is parsed separately from the scope and destructive rejections, so `override_guard` stays unavailable for it.
- A rejection or hold marker posted by anyone other than the pipeline's `GH_PAT` login is ignored.
