<!-- changelog: changed -->
- **The `gh api` permission guard now denies a call that passes jq's own command-line options to `--jq`, instead of stopping at a permission prompt.** The deny reason says how to fix the command, so an unattended session corrects it in the same turn.

On 2026-09-28 an unattended session stopped at a prompt for `gh api "…/runs/$r/jobs?per_page=50" --jq --arg r "$r" '<program>'` (#4891). `gh api` has no `--arg`: `--jq` took `--arg` as its program, and `gh` would have rejected the call before sending any request. `.claude/hooks/gh_api_write_guard.py` read it as an unreadable call, treated it as a write, and asked. The guard now returns `permissionDecision: deny` when a `-q`/`--jq` value matches `^--?[A-Za-z]`, and the reason tells the session to put the value into the jq program, pipe the output to `jq` with its own options, or wrap a program that starts with a minus sign in parentheses. CLAUDE.md §23.H documents the new outcome.

| The numbers that matter | Value |
| --- | --- |
| Forms caught | `--jq <v>`, `--jq=<v>`, `-q <v>`, `-q<v>` |
| Value pattern | `^--?[A-Za-z]` (`--arg`, `-r`, `--raw-output`, `-c`) |
| Still allowed | `--jq '-.size'`, `--jq -1`, `--jq '(-.size)'` |
| Precedence | deny, then ask, then allow; the unparseable-command and hidden-call asks run first, unchanged |

What this means for operators: sessions no longer wait for a human on a `gh api` call that could never work. Every other guard decision is unchanged, and a deny runs nothing, so no permission is widened.

### For contributors

The check is a new `MalformedJq` subclass of `Unreadable` raised in `parse_gh_api_args`; `evaluate` collects it before the generic write path. `tests/test_gh_api_write_guard.py` replays the #4891 command verbatim and covers each form, the valid programs above, and the precedence. The hook is mirrored byte-for-byte under `workflow-templates/.claude/hooks/`.
