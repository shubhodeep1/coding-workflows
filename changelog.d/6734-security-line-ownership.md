<!-- changelog: changed -->
- **The project security pass now blocks only on findings in code the project wrote.**

The findings-JSON security audit runs `git blame` on each finding's cited line at the audited head. A finding whose line no commit in the project's `merge-base..head` range wrote is marked `"advisory": true` in `security_audit_findings.v1`. The orchestrator poller files these as non-blocking `ai:security` follow-ups (after the final merge, by default) instead of gating the project. They no longer enter the consolidated fix issue or count toward `MAX_SECURITY_PASS_CYCLES`, and a result that holds only advisories records the pass as `passed`. A blame failure keeps the finding blocking. `SECURITY_PASS_CLEAN` and `SECURITY_PASS_BLOCKED` gain a trailing `advisory=<n>` field.

What this means for operators: projects stop spending fix cycles on vulnerabilities that already existed on the default branch. Set the repository variable `SECURITY_AUDIT_LINE_OWNERSHIP=off` to restore the previous behaviour exactly.
