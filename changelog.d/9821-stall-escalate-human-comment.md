<!-- changelog: fixed -->
- **A stall escalation now says why on the issue itself.** When stall recovery or the stall judge moves an issue to `ai:needs-human`, the issue gets one comment with the reason and how to resume.

Before this, an orchestrator-managed issue got a bare `ai:needs-human` label. The stall judge posted its decision on the project tracking issue only, so the issue gave no hint of what a person had to do. Issue #9821 was parked on 2026-11-20 at 21:44 with no comment. The new comment carries the judge's justification when a judge decision led there, links the tracking issue that holds the full diagnostics, and names the resume step: handle the reason, then remove `ai:needs-human`.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls | 1 comment write per escalation, no read |
| Marker | `<!-- ai:stall-escalate-human:v1 issue=<n> phase=<label> -->` |
| Log line | `STALL_ESCALATE_HUMAN_COMMENT issue=<n> phase=<label> source=stall_judge\|stall_recovery outcome=posted\|write_failed` |

What this means for operators: an `ai:needs-human` issue left by stall recovery can be answered from the issue page, without searching the tracking issue or Telegram. A failed comment write changes nothing else about the escalation.
