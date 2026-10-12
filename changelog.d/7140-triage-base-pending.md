<!-- changelog: fixed -->
- **Check-failure triage no longer files an issue for a PR's own failure while the base branch's CI is still running.** It now defers until the base result is known.

The base-branch gate in `scripts/check_failure_triage.sh` files an issue only when the base branch fails the same check, but it read a base run still in progress as unknown and filed anyway. #7140 was filed for PR #7135's own contract-test failure while main's CI run was in progress, and planning then blocked because nothing belonged on `main`. A pending base run or check now logs `CHECK_TRIAGE skip reason=base_pending` and files nothing. A missing base result or a failed read still files the issue.

What this means for operators: fewer triage issues that can only end in "planning blocked". A real base-branch breakage is still filed by the next triage once the base run completes, since every PR then fails the same check.
