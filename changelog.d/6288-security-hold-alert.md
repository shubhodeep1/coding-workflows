<!-- changelog: fixed -->
- **A judge merge held for the security audit no longer sends a CRITICAL alert unless it needs a human.** When the review-blocked judge approves a merge and the single-issue security pass is still auditing the head, the "Telegram review-blocked judge decision" step in `review_autofix.yml` now stays silent.

On 2026-10-06 PRs #6288 and #6209 each sent "🚨 CRITICAL: Review-blocked judge action: security_hold". In both, the judge had chosen merge, the security gate had already dispatched a retry audit, and that audit re-runs the review on its own. The gate (`scripts/review_single_issue_security_pass.sh`) now writes `hold_reason=` next to `hold=true`, and `scripts/review_rb_judge.sh` passes it on as `judge_skip_reason=security_hold_<reason>`. Holds that clear without a human send nothing. Every other hold is still a CRITICAL page, now naming the reason and the next step.

| Hold reason | Alert |
| --- | --- |
| `audit_dispatched`, `audit_pending`, `awaiting_followups` | none |
| `dispatch_failed`, `exhausted_without_completed_audit`, `cycles_exhausted`, `label_write_failed`, `markers_unverifiable`, `extensions_unverifiable` | CRITICAL, with the reason |
| `gate_failed`, `security_mode_unverified`, `unknown` (judge side) | CRITICAL, with the reason |

What this means for operators: a `security_hold` page now means the merge is stuck. Check the security-pass comments on the PR, then re-run the audit or decide the merge.

### For contributors

The gate step's outputs gain `hold_reason`; `hold` and `exhausted` are unchanged, and `judge_action=security_hold` is still emitted. A missing or malformed `hold_reason` (for example from an older staged gate script) is reported as `unknown` and pages.
