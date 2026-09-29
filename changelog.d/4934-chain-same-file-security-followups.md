<!-- changelog: changed -->
- **Security follow-ups that change the same file now run one after another.** When one security audit run files several follow-ups for the same file, each later one names the earlier one on a `Depends on: #N` line, and the Claude issue pickup starts it only after #N is closed with `ai:merged`.

`scripts/security_audit.sh` chains follow-ups by the finding's file path: the second follow-up for a file depends on the first, the third on the second. `scripts/claude_issue_intake.sh` reads the `Depends on:` lines from the target issue it already fetches and records them as a `depends_on` key in the bound queue payload. The pickup (`claude_issue_route.py queue-pending`) reads each dependency once per wake and lists a waiting item under `ignored` as `held: waiting on #N (open)` or `held: dependency #N closed without ai:merged`. Follow-ups for different files, and every issue without the line, dispatch exactly as before. This prevents the #4687 / #4688 incident, where two parallel fixes of the same gate in `scripts/review_claude_fixer_nonblocking.py` left final PR #4695 conflicting in 9 files.

| The numbers that matter | Value |
| --- | --- |
| Extra API reads per pickup wake | 1 per distinct dependency of the bound items considered, at most 30 |
| Extra API reads per watchdog run | 1 per distinct dependency of open dependent items |
| Dependencies per issue | at most 10 `Depends on: #N` lines, same repository only |

What this means for operators: a follow-up can now sit in the queue for hours while the follow-up it depends on is built; the pickup reports it as held, and the queue watchdog no longer calls that "pickup stopped". The watchdog alerts at once when a dependency is closed without `ai:merged`. To release such an item, finish or reopen the dependency, or remove the `Depends on:` line from the item's issue and comment `/reclarify`.

### For contributors

A dependency read that returns HTTP 403 or 404 cannot succeed from that session (for example a consumer repository not attached to the pickup's web session), so the pickup skips that dependency and notes it in the entry's `dependency_notes` rather than holding the item forever; any other read failure, including a rate-limit 403, holds the item for one wake. Queue items without a dependency render byte-identically, so existing bindings stay valid. The Codex route ignores the line.
