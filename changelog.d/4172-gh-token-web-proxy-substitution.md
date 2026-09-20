<!-- changelog: fixed -->
- **The SessionStart hook no longer reports a valid `GH_TOKEN` as "invalid or expired" in Claude Code on the web.** It now probes over REST and says plainly when the session's agent proxy is substituting its own GitHub credential.

Every web session opened with `WARNING: 'gh auth status' failed ... (likely invalid or expired)` even when the PAT in the cloud environment was fine. Two things caused it. Claude Code on the web routes every `api.github.com` call through an agent proxy that strips the `Authorization` header and signs the request with its own short-lived GitHub App token, so `GH_TOKEN` never reaches GitHub at all. And `gh auth status` verifies over GraphQL, which that proxy refuses with HTTP 403, so the command misreports the token whatever its state. `.claude/hooks/session-start.sh` now checks with `gh api user`, sends one unauthenticated `GET /user`, and when that also returns 200 prints a `NOTE` naming the real situation: calls authenticate as the proxy's identity, reach only repositories attached to the session, GraphQL and some Actions paths are refused, and PAT-backed access needs a local Claude Code session. CLAUDE.md §23.A gains a "Web sessions" paragraph with the same facts and the `add_repo` / GitHub App installation route for reaching more repositories; §23.D and the README `GH_TOKEN` row stop recommending `gh auth status`.

| The numbers that matter | Value |
| --- | --- |
| API calls added at session start | 1 unauthenticated `GET /user` (the GraphQL `gh auth status` call is removed) |
| Files shipped to consumer repos on the next `@stable` sync | `.claude/hooks/session-start.sh`, `CLAUDE.md` |
| Pull request | #4172 |

What this means for operators: on the web, do not expect a session-environment PAT to widen GitHub reach; enable the repositories in the Claude GitHub App installation or attach them per session instead, and use a local CLI, desktop, or IDE session when the PAT itself is needed (other repositories without attaching them, GraphQL, repository variables). The hook now distinguishes confirmed proxy substitution, absence of an always-on proxy credential, and an inconclusive substitution probe without claiming that a failed probe proves the PAT was forwarded.

### For contributors

The substitution probe deliberately sends no credential rather than a bogus one, so the hook never produces bad-credential attempts against the user's account. `workflow-templates/.claude/hooks/session-start.sh` must stay byte-identical to the root copy; `tests/test_session_start_extract_repo_slug.py` enforces it.
