<!-- changelog: fixed -->
- **The pre-push test gate no longer blames a change for tests that read tracked dotfiles or `.github/ai/*.json`.** `scripts/prepush_test_gate_run.sh` now builds the PR tree from git, the same way it builds the merge-base tree.

Before, the PR side of the gate (`/source`) was the review sandbox's credential-filtered copy, which leaves out tracked files such as `.gitattributes`, `.gitignore`, `changelog.d/.gitkeep`, most of `.github/ai/`, `.claude/settings.json` and any secret-named path. Tests that read those files failed only on the PR side, so the gate listed them as "caused by this change" even though they passed outside its container. Now `/source` holds every tracked file with its worktree content (staged and unstaged edits and deletions included), and untracked files still enter only when the credential filter admits them. The container keeps `--network none`, `--read-only`, `--cap-drop ALL` and no runner environment, and `.git` is never copied in.

| The numbers that matter | Value |
| --- | --- |
| Files in `/source` on PR #9666 (head `5faaa57`) | 1,523 before, 1,651 after (all tracked files) |
| Gate-listed tests on that head, run on `/source` | 14 failed before, 0 after (915 passed) |
| New wrapper failure token | `source_export_failed` |

What this means for operators and consumer repos: after the next `@stable` sync, the implement, review-autofix, conflict-resolver, heal and integration-repair gates stop reporting false "caused by this change" failures for tests that read tracked config files. Gate results are published in the PR body by implement, and in the autofix editor-summary comment ("Pre-push test gate results") by review rounds, which do not refresh the PR body.

### For contributors

The tracked tree is staged through a temporary index and object directory in the wrapper's private run root and written with `git checkout-index`, so the checkout's real index and `.git` are only read, and a PR's `export-ignore` cannot hide a file. Those Git calls leave out global and system config, so a filter driver configured there (git-lfs, for example) is never chosen by a PR's `.gitattributes`. A tracked `node_modules` symlink in the PR tree is no longer used as a mount point.
