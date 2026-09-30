<!-- changelog: security -->
- **The unattended question guard now accepts only a real blocker posted on the session's own issue.** Before, any successful tool call whose input contained `<!-- ai:claude-blocked:v1 -->` let an unattended issue-mode session end its turn on a question, including a `Bash` `echo` of the marker or a comment on a different issue.

`.claude/hooks/unattended_question_guard.py` (CLAUDE.md §28.G) now reads the current turn of the session transcript for two successful writes on the marker's exact repository and issue. The first is a comment whose body starts with the marker, posted with `mcp__*__add_issue_comment` or a `gh api` POST to `repos/<owner>/<repo>/issues/<N>/comments`, whose result carries that issue's comment URL. The second is a write adding the `ai:claude-blocked` label to the same issue, through `mcp__*__issue_write` with `method: "update"` or a `gh api` POST to `repos/<owner>/<repo>/issues/<N>/labels`. A `Bash` call counts only when it holds `gh api` calls alone, joined by `&&` at most, after an optional leading `cd <path>;`. The block reason now names the exact issue and the accepted command forms.

| The numbers that matter | Value |
| --- | --- |
| Security finding | `forged-blocked-comment-evidence` (STRIDE: Spoofing, medium), issue #5082 |
| GitHub API calls added | 0 (the check reads the local transcript) |
| Stop-block cap | unchanged, 2 per session |

What this means for operators: a §28.C stop in issue mode is now only allowed after the blocker really reached the source issue with its label, so a blocked project always shows up on its issue. A session that posts through a pipe, `--silent`, or a `--jq` that drops the comment URL is blocked once and told how to post in a form the hook accepts.

### For contributors

The live `.claude/hooks/unattended_question_guard.py` is synced from its `workflow-templates/.claude/` twin (Q40 twin-first). `tests/test_unattended_question_guard.py` covers the accepted and rejected shapes, and `test_hook_makes_no_network_calls` now forbids process and socket primitives rather than the literal text `gh api`, which the hook parses as data.
