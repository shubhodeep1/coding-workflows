<!-- changelog: security -->
- **The review-blocked judge no longer merges single-issue PRs with unresolved high, critical, or unrated security findings after audit exhaustion.** It fixes and re-audits while retries remain, then holds the PR for a clean audit or a human decision.

When the single-issue security pass exhausts its cycles, the judge's open security-finding list now distinguishes blocking severities from medium and low. A merge verdict with blocking findings becomes a fix attempt; a fix that makes no changes holds rather than merging. For blocking findings, an earlier auto-merge enrollment is withdrawn before the judge runs, even if the head has moved; the judge refuses to act on the mismatched head. At the final retry even a `close_and_reissue` verdict leaves the PR open with `ai:security-pass-failed` and `ai:review-blocked`, and the existing review alert reports the hold once per head. Medium and low findings continue through the existing follow-up flow.

What this means for operators: a blocked head cannot merge through the exhaustion judge until a clean audit or a human decision.
