<!-- changelog: changed -->
- **Protected-path phases in coding-workflows no longer stop to ask how to run.** Until #4785's Actions twin sync is live, a `/implement-plan-claude` or `/implement-issue-claude` phase that touches `.claude/**` applies the operator's twin-first rule (Q40) by itself and stops only for the `[claude-twin-sync]` copy.

Before this change, a phase marked `protected paths:` always stopped at `Status: BLOCKED` before it started and asked whether to run in a watched session, drop the `.claude/` part, or try unattended. The answer was always the same twin-first rule, which the master session had to post by hand, followed by `/reclarify`. On 2026-09-29 alone that happened for #4858, #4891, #4817, #4755 and #4750. Now, in a repo that has `workflow-templates/.claude/`, the stage records `Protected-path approval: phase <n> — twin-first (automatic, interim until #4785) (<date>)` in the progress log and edits only the `workflow-templates/.claude/**` twins. A `.claude/` file with no twin is not edited: its exact diff and sha256 go into the sync blocker. After the phase PR opens, the stage posts a `hold` claim and the twin-sync blocker, as before.

| The numbers that matter | Value |
| --- | --- |
| Human stops per protected-path phase | 2 → 1 (the `[claude-twin-sync]` copy remains) |
| Cases that still ask the A/B/C question | an edit denied in the twin tree; a plan that says the phase needs a watched session |
| Where it applies | repos with `workflow-templates/.claude/` (coding-workflows); consumers keep the question |
| Sunset | the PR that makes #4785's sync live (`scripts/claude_twin_sync.py`) |

What this means for operators: a protected-path project reaches the twin-sync blocker without an answer from you. A `Protected-path approval:` answer that is already in a log stands and is never overwritten. The approval line format is unchanged, and the new value is one more answer.

### For contributors

The rule lives in a new paragraph of `/implement-plan-claude` step 4 and a new CLAUDE.md §28.C bullet, next to the text #4785 rewrites rather than inside it, so the two changes merge in either order. `tests/test_implement_plan_claude_command.py` fails once `scripts/claude_twin_sync.py` exists while the interim text remains, which makes the #4785 sync PR remove it.
