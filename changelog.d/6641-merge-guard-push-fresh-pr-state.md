<!-- changelog: fixed -->
- **The merged-PR guard now checks PR state live for every push.** Before, a `git push` or a GitHub MCP push could be allowed by PR state the guard had cached up to 300 seconds earlier. If the branch's open PR merged in that time, the push went through to a branch no open PR tracked any more. A cache file planted in the temp directory could do the same.

A push now never reads the cache. It makes one live lookup per branch per guarded command, and a `git commit && git push` in one command still costs one call. If that lookup fails, the push follows CLAUDE.md §21.C (block when git history shows stranded work, otherwise ask for confirmation) and never falls back to cached data. A bare `git commit` may still be allowed from the cache, because nothing reaches origin until a push re-checks. Consumer repos get the change with the next `@stable` sync of `.claude/hooks/pr_merge_status_guard.py`.

What this means for operators: a push right after a PR merges is blocked instead of slipping through, and a push while GitHub is unreachable asks for confirmation even when the cache shows an open PR.
