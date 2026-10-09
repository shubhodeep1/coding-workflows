<!-- changelog: security -->
- **Once the security-pass `keep_fixing` round cap is spent, a remaining `keep_fixing` decision now fails the project instead of accepting the finding.** Security finding #6539 (`security-pass-forced-waiver`, high) is closed.

`MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS` (default `2`) bounds how many exhaustion-judge rounds may grant another fix cycle. Past the cap, `security_pass_exhaustion_judge` in `scripts/orchestrate_poll_process.sh` rewrote every `keep_fixing` decision to `accept_with_followup`, so a finding the judge wanted fixed, including a high-severity one, was recorded as a waiver, the security pass was marked passed, and the project could merge into the default branch with the finding open. The rewrite now targets `fail`: the round posts the judge comment and the project terminalizes as `ai:security-pass-failed`, with no waiver rows or advisory follow-ups, including for findings the judge accepted in the same verdict. The cap still stops the unbounded fix loop from project #3965.

| The numbers that matter | Value |
| --- | --- |
| Findings accepted by the cap | 0 (was: every `keep_fixing` decision past the cap) |
| New GitHub API calls | 0 |
| New state fields or env vars | 0 |

What this means for operators: a project that used to complete at the cap now parks in `ai:security-pass-failed`. It recovers through the unblock judge or the engine-change auto-reset without a human, or through `/re-security-pass` and `/security-pass-waive <finding_id>`. The log line `SECURITY_PASS_JUDGE_KEEP_FIXING_CAPPED` keeps its fields; the justification prefix now reads `[keep_fixing capped after <c> judge round(s); converted to fail — needs a fix or a human waiver]`. The judge prompt now tells the judge to use `fail` when `keep_fixing` is no longer available and it cannot accept a finding. A judge's own `accept_with_followup` decision is unchanged.
