<!-- changelog: changed -->
- **Low and medium `keep_fixing` findings become advisories at the security-pass cap again.** After `MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS` (default 2), the exhaustion judge's low/medium `keep_fixing` decisions are converted to `accept_with_followup` advisories instead of `fail`; high, critical and unrated findings are still never waived.

Issue #6539's fix converted every capped low/medium `keep_fixing` decision to `fail`, so a project whose remaining findings were all low or medium terminalized as `ai:security-pass-failed` and waited for a human `/re-security-pass` or `/security-pass-waive`. That contradicted issue #6729 (the operator re-issue of #6517), which allows only low and medium findings to be converted at the cap, and its tests merged a day later against the changed poller, leaving `main` CI red from 08:42 UTC on 2026-10-09. `scripts/orchestrate_poll_process.sh` now converts those decisions with the justification prefix `[keep_fixing capped after <c> judge round(s); converted to advisory follow-up]`, logs `SECURITY_PASS_JUDGE_KEEP_FIXING_CAPPED ... converted=<n>`, and completes the project with deferred `ai:security` follow-ups the standalone pipeline works unattended. A remaining high, critical or unrated `keep_fixing` decision in the same round still terminalizes the pass without recording any waiver, converted rows included.

| The numbers that matter | Value |
| --- | --- |
| Cap | `MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS=2` (unchanged) |
| Severities converted at the cap | low, medium |
| Severities never waived | high, critical, unrated |

What this means for operators: a project that reaches the cap with only low or medium findings left completes on its own and files follow-up issues; one with a high, critical or unrated finding still stops as `ai:security-pass-failed` and needs the same recovery as before.

### For contributors

The pre-#6549 test `test_security_pass_exhaustion_judge_keep_fixing_cap_converts_to_advisories` is restored; `test_security_pass_cap_never_waives_a_high_finding` and `test_security_pass_cap_does_not_record_advisories_before_terminal_failure` are unchanged. README, agents.md and both copies of `mode-judge-security-pass-exhaustion.txt` state the rule.
