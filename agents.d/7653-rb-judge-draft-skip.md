<!-- agents: section="Stable log prefixes (contractual)" -->
- `RB_JUDGE_DRAFT_SKIP` (`scripts/review_rb_judge.sh`: `pr= outcome=skip reason=draft_pr`; the live PR, read once for the closed-PR check, is open and its `draft` field is the JSON boolean `true`, so the judge wrote `judge_handled=true`, `judge_action=skip`, `judge_skip_reason=draft_pr` and stopped before the linked-issue lookup and any model call; the "Telegram review-blocked judge decision" step sends nothing for it; `pull_request.ready_for_review` starts a fresh review that brings the judge back. Gate draft handling is unchanged, so dispatched review and conflict runs on drafts (#7031) still run, issue #7653)

LOG_PREFIX.name=RB_JUDGE_DRAFT_SKIP
