<!-- changelog: security -->
- **The §21 merged-PR guard now blocks a `git commit` or `git push` that follows a `cd` whose target `CDPATH` may redirect.** Before, `CDPATH=.. cd .git && git push origin HEAD:<branch>` was judged in the wrong repository and could let stranded work through.

Bash looks a `cd` operand up in `CDPATH` before the current directory unless it starts with `/` or is `.`, `..`, `./…` or `../…`, so `.git` is searched. The effective-repository walker in `.claude/hooks/pr_merge_status_guard.py` resolved such an operand against the current directory only, so the guard could judge a clean repository while Bash pushed from another one (issue #6090, found by the security audit of the #5144 project). The guard cannot reproduce Bash's search, and falling back to the session checkout could judge the wrong repository just the same. So when `CDPATH` may be set, every guarded call after such a `cd` or `pushd` (also as `builtin [--] cd` or `command cd`) is now blocked. The block message names the one-edit fix: `cd ./<dir>` or an absolute path.

| The numbers that matter | Value |
| --- | --- |
| `CDPATH` sources counted | non-empty `CDPATH` in the hook's environment; any word containing `CDPATH` in an earlier segment; a non-empty `CDPATH=` prefix on the `cd` itself (that `cd` only) |
| Turns it off | an empty `CDPATH=` prefix on that `cd` |
| Not affected | operands `/…`, `.`, `..`, `./…`, `../…`; unquoted `~` / `~/…`; `cd` with no operand; `cd -`; deletion and tag-only pushes |
| GitHub API calls for a blocked call | 0 |

What this means for operators and supervising sessions: in a shell where `CDPATH` is set, write `cd ./<dir>` (or an absolute path) before a `git commit` / `git push`. Without `CDPATH`, nothing changes.

### For contributors

`GuardTarget` gains a trailing `unjudgeable_reason` field (default `""`), so every existing constructor call still works. `guard_targets` tracks the `CDPATH` state and marks the directory unjudgeable, `_git_invocation_targets` emits the target, and `_evaluate_bash` blocks it before any lookup. `tests/test_pr_merge_status_guard.py` covers the rules against the `workflow-templates/.claude/hooks/` twin, including an end-to-end test that checks with real Bash that the `cd` is redirected. The root `.claude/hooks/` copy lands through a `[claude-twin-sync]` commit.
