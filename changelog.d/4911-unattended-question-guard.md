<!-- changelog: added -->
- **Unattended issue-mode sessions can no longer end a turn on a question nobody reads.** A new hook, `.claude/hooks/unattended_question_guard.py`, blocks the stop and denies `AskUserQuestion`. The block reason tells the session to auto-decide the question or post it on the issue.

On 2026-09-29, the `#4687` and `#4750` issue sessions ended their turns on in-session `Q1` questions, and the `#4886` session stalled on an `AskUserQuestion` permission prompt. None of the three posted on its issue, so none of those projects would have moved again. The hook is wired on `Stop` and on `PreToolUse` for `AskUserQuestion` in `.claude/settings.json`, and it acts only in sessions that marked themselves unattended.
- **Who marks:** `/implement-issue-claude` step 0, and every `/implement-plan-claude` session of an issue-mode plan, run `unattended_question_guard.py mark --repo <owner>/<repo> --issue <N>`.
- **A blocked stop:** the final message asks a §2 question or asks for permissions, and nothing in the turn posted the `ai:claude-blocked` comment. The reason restates CLAUDE.md §28: take the RECOMMENDED option and record an `AD-<n>` entry, or post the §28.C item on the issue with the label.
- **Documentation:** CLAUDE.md §28.G.

| The numbers that matter | Value |
| --- | --- |
| Stop blocks per session | at most 2; the third stop is allowed with a `unattended-question-guard: cap reached` system message |
| `AskUserQuestion` in a marked session | always denied |
| Marker | `~/.claude/unattended-issue-mode/<CLAUDE_CODE_REMOTE_SESSION_ID>.json` |
| GitHub API calls from the hook | 0 (the blocked-comment check reads the local transcript) |
| Sessions affected without a marker | none; interactive and local sessions are never blocked |

What this means for operators: an unattended issue session that hits a question now either keeps working with a recorded auto-decision or leaves an `ai:claude-blocked` comment on the issue. It no longer sits idle with the question visible only in its own view. If a session still stops after two blocks, the `cap reached` system message in its view names the session and the issue.

### For contributors

- **Fail-open contract:** the hook follows the §21/§25/§26 hooks. A bad payload, an unreadable marker or state file, or an internal error allows the call with a `systemMessage`. Empty input is allowed silently.
- **Question detection:** a `Q<n>:` line with at least two lettered-choice lines, or the inline `Q<n>: A/B` form. The `Permission prompts:` line of a stage report never matches.
- **Posted-comment check:** a tool call in the current turn whose input contains `<!-- ai:claude-blocked:v1 -->` and whose result is not an error.
- **Twin:** the hook, the `settings.json` wiring, and the two command steps are mirrored under `workflow-templates/.claude/`.
- **Tests:** `tests/test_unattended_question_guard.py`, run in its own `ci.yml` step.
