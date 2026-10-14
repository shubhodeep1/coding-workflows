<!-- agents: section="Stable log prefixes (contractual)" -->
- `RB_JUDGE_STALE_HEAD` (`scripts/review_rb_judge.sh`: `pr= judged_head= live_head= action=`; the PR head moved during the judge call, so the judge skipped with `judge_skip_reason=stale_head` before any comment, label, status or close. An unreadable live head refuses `fix` / `close_and_reissue` with `judge_skip_reason=head_unverified`, and the spot-fix reissue baseline uses `RB_JUDGED_HEAD_SHA`)

LOG_PREFIX.name=RB_JUDGE_STALE_HEAD
