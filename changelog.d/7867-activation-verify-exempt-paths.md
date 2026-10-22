<!-- changelog: fixed -->
- **Activation verification no longer builds merged plans or raw design drops on its own.** A merge whose every changed file matches `ACTIVATION_VERIFY_EXEMPT_GLOBS` (default `docs/plans/**`) is not verified, so it gets no `ai:activation-fix:v1` issue.

Until now, merging a plan or a designer's raw game build into a consumer's default branch made the `activation-verify` job in `issue_pr_status.yml` file a fix issue. The pipeline then clarified, planned, implemented and auto-merged that issue with no human step. In tele-funtoken-msg-scoring this shipped three games from raw uploads and duplicated a human-driven plan (#4726). Now such merges wait for an explicit order: a plan dispatch, `/implement-plan-ai`, or an approval. A PR that mixes exempt and other files is verified on the other files only, and a PR labelled `ai:skip-activation-verify` is not verified at all.

| The numbers that matter | Value |
| --- | --- |
| Default exempt pattern | `docs/plans/**` |
| New repository variable | `ACTIVATION_VERIFY_EXEMPT_GLOBS` (a value replaces the default) |
| New label | `ai:skip-activation-verify` |
| Skip log | `ACTIVATION_VERIFY mode=pr ... outcome=skip reason=exempt_paths\|skip_label` |
| Extra GitHub API calls | 0 |

What this means for consumer operators: if your repository keeps raw drops or reference sources outside `docs/plans/`, set `ACTIVATION_VERIFY_EXEMPT_GLOBS` to the full list, for example `docs/plans/**,docs/game-sources/**,ft.games/templates/Game-pages/**`. Code merges are checked as before, and project-completion verification is unchanged.

### For contributors

The split runs in `scripts/activation_verify.sh` after the changed-file list is resolved, using path-aware matching (`**` spans folders, `*` stays in one). The matching files reach the model as `exempt_changed_files`, which `prompts/mode-activation-verify.txt` forbids turning into gaps. A failed split logs a warning and verifies every file.
