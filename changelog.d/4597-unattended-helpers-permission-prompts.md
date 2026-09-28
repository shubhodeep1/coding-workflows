<!-- changelog: added -->
- **`/implement-plan-claude` stages now use allowlisted helpers for workflow dispatches and comment edits, and every permission prompt a session hits is logged and filed as an issue.** The command also requires Auto mode and stops before any phase that must edit `.claude/**`.

Stage sessions used to find a dispatched run with `gh run list -L 1`, which can return the previous run, so they wrote polling loops with `sleep` and `$(gh api ...)`. They also edited the progress comment with `gh api ... > $S/body.md && python3 - <<'EOF' ... && gh api -X PATCH`. Both shapes stopped unattended sessions at permission prompts. Three helpers replace them, each allowed by an exact rule in `.claude/settings.json`: `.claude/scripts/dispatch_workflow.py` dispatches an allowlisted workflow and prints the id of the run it started, `.claude/scripts/edit_comment.py` edits one comment in place, and `.claude/scripts/permission_prompts.py` reports prompts. A new hook, `.claude/hooks/permission_prompt_logger.py`, records every permission prompt and Auto-mode denial. At the end of each stage, `permission_prompts.py file` lists them in the report and, in coding-workflows, files each new pattern as an `ai:permission-prompt` issue that clarify routes to the Claude issue implementer. `/implement-plan-claude` step 0 now refuses to start outside Auto mode, and a phase that edits `.claude/**` stops at `Status: BLOCKED` and asks how to run it, because Claude Code never auto-approves those edits.

| The numbers that matter | Value |
| --- | --- |
| Workflows `dispatch_workflow.py` accepts | 6, the same files allowed as `gh workflow run <file> *` |
| Wait for the dispatched run | polls every 5 s, up to 90 s |
| Issue per prompt pattern | 1, later occurrences comment on it; no cap on open issues |
| Command text kept in an issue | 2,000 characters, heredoc bodies removed, token-like strings masked |
| Repos that file issues | coding-workflows only; the 13 consumers in `.github/ai/consumer_repos.json` log and report |

What this means for operators: run `/implement-plan-claude` from a session in Auto mode, or it stops at step 0 and asks you to switch. Expect `ai:permission-prompt` issues in coding-workflows whenever a stage still hits a prompt; each starts a Claude implementation project whose fix usually edits `.claude/`, so it will stop and ask you before that phase. Close an issue as not planned when the prompt is by design (a protected-path edit or an ask-first operation). Plans that edit `.claude/**` now need you once per such phase: answer in the blocked stage whether to run it in a session you watch.

### For contributors

CLAUDE.md §23.I documents the helpers and reports, and §28.C the protected-path stop. The logger writes `~/.claude/permission-prompts/<session id>.jsonl`, never decides, and makes no API calls. `permission_prompts.py` keeps `filed-state.json` next to the logs so a later run in the same session files only new occurrences, and dedupes against existing issues by the `<!-- ai:permission-prompt:v1 sig=<sig> -->` marker. The `ai:permission-prompt` label is in `.github/ai/label_contract.v1.json` and `scripts/label_helpers.sh`. Tests: `tests/test_dispatch_workflow.py`, `tests/test_edit_comment.py`, `tests/test_permission_prompts.py`, in one `ci.yml` step.
