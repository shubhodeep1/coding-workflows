<!-- changelog: fixed -->
- **The `Project security pass exhausted` tracking comment now lists the remaining blocking findings.** Operators asked to intervene after `MAX_SECURITY_PASS_CYCLES` no longer have to guess what the auditor still flags.

When the project security pass in `orchestrate_poll_process.sh` spends its fix-cycle budget it used to post only a count ("still reports 4 blocking finding(s)") and label the tracking issue `ai:security-pass-failed`. The findings-JSON file behind that count lives only in the runner's runtime directory, so the count was the operator's entire record of what blocked completion. The exhaustion comment now carries the same ID / category / severity / confidence / location / exploit / recommendation table that a consolidated `[security-pass]` fix issue would have carried, under a `Remaining blocking findings (integration head <sha>)` heading. Rendering is best-effort: an unreadable findings file, or a table that would push the comment past GitHub's 65536-byte limit, falls back to the count-only comment with a workflow warning instead of skipping the terminal transition.

| The numbers that matter | Value |
| --- | --- |
| Trigger | tele-funtoken-msg-scoring#3928, cycle 3/3 exhausted on 2026-09-06 with 4 unpublished findings |
| Comment budget guard | table dropped above 60000 bytes |

What this means for operators: after `ai:security-pass-failed`, open the tracking issue's `Project security pass exhausted` comment, address the listed rows, then comment `/re-security-pass`.

### For contributors

`create_security_pass_fix_issue` and `security_pass_terminal_failure` now share one renderer, `render_security_pass_findings_table`, so the fix-issue body and the exhaustion comment cannot drift. `security_pass_terminal_failure` takes the findings file as an optional fourth positional argument; existing three-argument callers keep the count-only comment.
