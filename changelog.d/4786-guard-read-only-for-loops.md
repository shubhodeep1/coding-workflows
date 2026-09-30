<!-- changelog: changed -->
- **The `gh api` permission guard now approves a `for` loop over literal IDs whose body only reads, so sessions no longer stop at a prompt for loops like `for r in 1 2; do gh run view $r --json status; done`.**

`.claude/hooks/gh_api_write_guard.py` (CLAUDE.md §23.H) made no decision on any command with a loop, so the Auto-mode classifier decided and often prompted. On 2026-09-28 the #4707 implement session waited about 3.5 hours on one such prompt, a loop reading five past `CI` runs. The guard now allows exactly `for VAR in TOKEN...; do BODY; done`, where every body item is a `gh api` read with `$VAR` only in the endpoint path, `gh run view`, `gh run list`, or `gh pr view` with allowlisted flags and `$VAR` only as a positional argument, or `echo`. Every other loop keeps its old result, and a write inside a loop still asks.

| The numbers that matter | Value |
| --- | --- |
| Loop shapes approved | 1 (`for` over literal tokens, `;`-framed) |
| Read subcommands allowed besides `gh api` | 3 (`gh run view`, `gh run list`, `gh pr view`) |
| New guard test cases | 50 (10 allowed, 36 no decision, 3 ask, 1 hook process) |
| GitHub API calls added | 0 |

What this means for operators: unattended sessions that read run, job, or PR details for a handful of IDs in one loop run without waiting for approval. Nothing that writes, and no loop over computed values, is approved. The change reaches consumer repos on the next `@stable` sync of `.claude/`.

### For contributors

The loop variable must be lowercase (so it can never be `PATH`, `IFS`, `GH_HOST`, or `GH_TOKEN`) and must not contain `proxy` (assigning to an exported `https_proxy` would re-route `gh`), tokens must match `[A-Za-z0-9._-]+` and not start with `-`, and `$VAR` may not sit in a `gh api` endpoint's first path segment or after its `?`. Backslashes, other `$` expansions, file redirects, `||`, `&`, nested loops, a trailing `;` after `done`, newline-separated loops, and a quoted `gh api` call in the body (`gh 'api' …`, `"gh" api …`, which the guard's raw-text check never classifies) all keep the old no-decision result. The new predicate is `_is_approvable_read_loop`; the flag allowlists are `_GH_READ_SUBCOMMAND_FLAGS`.
