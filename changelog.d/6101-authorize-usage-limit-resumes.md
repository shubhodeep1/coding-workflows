<!-- changelog: security -->
- **The Claude issue pickup now resumes only sessions its own workflows started.** A session a person opened in the app, or one working in a repository outside coding-workflows and its consumers, is never sent a usage-limit resume trigger.

The pickup finds stopped sessions with an account-wide `list_sessions` (`mine: true`) listing. Until now `.claude/scripts/usage_limit_resumes.py` picked any stopped `IDLE` session from it, so the pickup could tell an unrelated Auto-mode session to "continue from your latest instructions" (security finding #6101). The selector now checks three fields the server sets on every session before anything else, and skips the session when any of them is missing or wrong. The skip reason is listed under `skipped`, so an operator can see why a session was left alone.

| The numbers that matter | Value |
| --- | --- |
| Repository | every GitHub source in `session_context.sources` is coding-workflows (`--self-repo`) or a `.github/ai/consumer_repos.json` repository (`--registry`), else `no_repo` / `foreign_repo` |
| Origin | `origin` is `claude_code_mcp_seed` (started by another session with `create_session`), else `unknown_origin` |
| Lineage | a `parent_session_id` is present, else `no_lineage` |
| Unreadable registry | only coding-workflows is authorized, and one line goes to `errors` |
| API calls added | none |

What this means for operators: the pickup's step 1a command line is unchanged, and checkers, stage sessions, fixers, and implementation sessions in coding-workflows and its consumer repositories are resumed exactly as before. A session you started yourself in the app stays stopped after a usage-limit reset; resume it by hand as before.

### For contributors

The check runs in `_skip_reason` right after the `pickup` check, on both the `text` and the `rate_limit_info` signal. `select` takes a keyword-only `allowed_repos`; called without it, it authorizes nothing. The parent chain is not required to reach the pickup session, because chains are often rooted at an operator's session or at a pickup that was since restarted, and ancestors older than the 3-day listing are not on it.
