<!-- changelog: fixed -->
- **Deferred security-pass advisories no longer claim removed code is on the default branch.** When the cited lines did not survive the final merge, the issue body now says so and asks for a check of the current code instead.

The orchestrator files accepted security-pass findings as `ai:security` follow-ups once the integration branch merges. Until now every such body said "the cited location refers to code that is already on the default branch", even when the code had been removed between the audit and the merge. In shubhodeep1/fun-token-multi-chain#710 the cited coinflip recovery code at `services/session-server/src/repository.ts:3015` had been reverted by #667 before PR #596 merged, so planning stopped with "nothing to implement" and needed an unblock-judge reissue. The poller now compares the cited line and its neighbours at the audited head with the merged default branch before filing.

| The numbers that matter | Value |
| --- | --- |
| Lines compared | cited line ± 2, whitespace-trimmed, blank lines dropped |
| Minimum window text | 12 non-blank characters (below that: `unknown`) |
| Extra GitHub API calls | 0 (two local `git fetch` calls per filing tick) |
| New log line | `SECURITY_PASS_ADVISORY_CITED_CODE ... status=present\|absent\|unknown` |

What this means for consumer repos: advisories whose cited code was removed before the merge now arrive with a plan the pipeline can carry out (find out whether the vulnerable behaviour still exists, fix it if so, add a regression test), instead of blocking at planning. Findings are still filed every time. `present` and `unknown` keep the old wording.
