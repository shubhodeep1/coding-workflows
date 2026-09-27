<!-- changelog: security -->
- **The Claude issue intake now checks who sent a dispatch and which issue it names before it queues a Claude session.** A dispatch for an issue its sender cannot write to, a closed issue, a pull request, or an issue from an untrusted author is refused.

`claude-issue-intake.yml` used to check only that the payload's repository was registered, so anyone able to send a `claude-issue` dispatch or run the workflow could start a write-capable Claude session on any issue number in any registered repository, skipping clarify's author gate (#4620). The intake now reads live GitHub data first. Every dispatcher login GitHub reports for the run (`github.actor`, and `github.triggering_actor` when it differs) needs `admin` or `write` on the target repository. The target must be an open issue in that repository, not a pull request or a transferred issue. Its author must pass clarify's rule (`OWNER`, `MEMBER` or `COLLABORATOR`, or `github-actions[bot]`), or a trusted user must have commented `/reclarify` on it. A refused dispatch queues nothing and writes nothing to the target issue. It logs `CLAUDE_ISSUE_INTAKE rejected reason=…`, fails the run, and sends a Telegram ERROR.

| The numbers that matter | Value |
| --- | --- |
| New GitHub reads per dispatch | 1 permission read per distinct dispatcher login, 1 issue read, comments only for an untrusted author |
| Refusal reasons | `dispatcher_unknown`, `dispatcher_not_authorized`, `target_not_issue`, `target_repo_mismatch`, `issue_closed`, `untrusted_issue_author`, `authorization_read_failed` |
| New workflow env (from GitHub context) | `CLAUDE_ISSUE_DISPATCHER`, `CLAUDE_ISSUE_TRIGGERING_ACTOR` |

What this means for operators: routed issues from trusted authors work exactly as before, and consumer repositories need no change. If Telegram reports a refused intake for a legitimate issue, fix the cause the reason names (for example, a `GH_PAT` that cannot read the repository's collaborators) and comment `/reclarify` on the issue.

### For contributors

The decision is `authorize_target` in `scripts/claude_issue_route.py` (CLI `authorize-target`), a pure function. `scripts/claude_issue_intake.sh` performs the reads and refuses through `reject()`, which unlike `fail()` never labels or comments on the target. Tests are in `tests/test_claude_issue_route.py`.
