<!-- changelog: changed -->
- **AI commits now name the engine that wrote them.** A commit made by the Claude engine is authored by `claude-bot`, and every other pipeline commit by `codex-bot`. Neither email links to a GitHub account anymore.

Until now every pipeline commit was authored by `codex-bot <codex@users.noreply.github.com>`, even when Claude wrote the change, so PRs labelled `ai:engine-claude` showed Codex as the author of every `[ai-autofix] apply PR fixes` commit. The review autofix editor, implement (main and repair), the conflict resolver and the review-blocked judge (in `review_autofix.yml` and in the poller) now commit as `claude-bot <claude-bot@users.noreply.invalid>` when their role ran on Claude and did not fall back. When the role ran on Codex or OpenCode, they commit as `codex-bot <codex-bot@users.noreply.invalid>`. Mechanical commits (merges, memory records, stall-recovery commits) stay `codex-bot`. The old email linked every pipeline commit to the GitHub account `codex`, which this project does not own. Both new emails use the reserved `.invalid` domain, so GitHub shows a plain name with no linked account.

| The numbers that matter | Value |
| --- | --- |
| Writers that pick the identity by engine | 5 (`review_commit_changes.sh`, `implement_commit_changes.sh`, `review_conflict_resolve.sh`, `review_rb_judge.sh`, the poller's combined review-blocked judge) |
| Identities the pipeline still recognises as its own | 3 (legacy `codex-bot <codex@users.noreply.github.com>`, `codex-bot <codex-bot@users.noreply.invalid>`, `claude-bot <claude-bot@users.noreply.invalid>`) |
| New log prefix | `AI_COMMIT_IDENTITY` |

What this means for operators: the commit list on a PR now shows which engine did the work. The review-blocked unstick check and the noop force-merge gate accept all three identities, so older commits keep counting. New pipeline commits carry no GitHub login. If you set `AUTOFIX_SKIP_SELF_TRIGGERED=true`, the self-triggered skip (which matches `AUTOFIX_BOT_LOGIN`) no longer matches them, and the verification review runs instead of being skipped. `scripts/check_external_branch_advance.sh` reports `ADVANCE=unknown` instead of `self_only` for them; the caller continues either way.

### For contributors

`NOOP_PRODUCTIVE_COMMIT_NAME` / `NOOP_PRODUCTIVE_COMMIT_EMAIL` keep their legacy values. The new `NOOP_PRODUCTIVE_COMMIT_IDENTITIES_JSON` list holds all three pairs. `REVIEW_EDITOR_ENGINE_FELL_BACK` (written to `GITHUB_ENV` by `scripts/review_apply_fixes.sh`) and `${RUNTIME_DIR}/rb_judge_last_engine` (written by `poller_judge_isolated`) carry the engine that actually ran to the commit step. `tests/test_review_autofix_claude_fixer_mode.py` covers each writer's choice, and fails if any writer goes back to the GitHub-linked email.
