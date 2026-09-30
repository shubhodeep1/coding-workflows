<!-- changelog: fixed -->
- **The unattended question guard now posts the blocker on the source issue when it reaches its cap.** A session that ends its turn on a question a third time no longer stops with only a local log line.

`.claude/hooks/unattended_question_guard.py` blocks an unattended issue-mode session from ending its turn on an in-session question twice, then has to allow the stop. Until now nothing reached the issue at that point, so the chain stalled with nobody told (security finding `silent-stop-after-block-cap`, issue #5083). At the cap the hook now posts one `<!-- ai:claude-blocked:v1 -->` comment on the marked issue and adds the `ai:claude-blocked` label. The comment is a fixed template that never quotes the session's text, and a per-session marker keeps it to one comment per session. Only an owner, member, or collaborator comment that starts with the blocked marker counts as that session's blocker, so a comment quoting the marker cannot suppress the post.

| The numbers that matter | Value |
| --- | --- |
| Attempts per stop | 3 (1 s, 2 s backoff; 6 s per call; 24 s budget) |
| Retry after a failed publish | at every later `Stop` in the session |
| API calls, only at the cap | 1 GET per 100 comments, at most 1 comment POST, 1 label POST per attempt |

What this means for operators: an issue whose session gave up on the guard now shows `ai:claude-blocked` and a comment that says to answer and `/reclarify`, the same as any other §28.C stop.

### For contributors

The publish runs through `gh api` (`run_gh_api`, no shell) against the marker's validated repo and issue only. State lives in `~/.claude/unattended-issue-mode/<id>.state.json` (`cap_blocker: posted | pending | invalid`), and each publish adds a `cap_blocker_*` line to `stop-guard.jsonl`. Tests inject a fake runner; see `tests/test_unattended_question_guard.py`.
