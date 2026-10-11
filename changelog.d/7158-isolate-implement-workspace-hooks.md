<!-- changelog: security -->
- **Implement workspace hooks no longer run on the runner with its credentials.** Security finding `implement-branch-hook-runs-with-host-credentials`.

`scripts/run_workspace_hook.sh` ran every hook outside the `validate` phase directly on the host. An implement run can check out an issue-selected branch, so a branch writer could add `.github/ai/workspace_hooks/implement/after_create.sh` and read the `GH_PAT` that `implement.yml` writes into the checkout's origin URL, or the token in the step environment. Every phase now runs its hook in the same container validation hooks use:
- no network, a read-only root, all capabilities dropped and an empty environment;
- a screened copy of the workspace without `.git`, `.ai`, the workflow-support checkouts, `.env*` or credential files;
- the hook the trusted runner resolved is copied in, so implement's `after_run` and `before_remove` still run the copy taken before the editor, not one the agent changed.

There is no host fallback. Phase names must now start with a letter or digit, so `.` and `..` are rejected.

| The numbers that matter | Value |
| --- | --- |
| Hook phases that run on the host | 0 (was: every phase except `validate`) |
| Largest accepted hook file | 256 KiB |
| Snapshot limits | 5,000 files, 64 MiB, 2 MiB per file |
| New GitHub API calls | 0 |

What this means for operators of repositories with implement hooks:
- A hook's changes to the workspace are discarded. The only output that comes back is non-executable `.txt` files written to `implement/hook-output/<hook>/`, which land in `${RUNNER_TEMP}/workspace-hooks/output/implement/<hook>/` on the runner, never in the committed workspace. Treat them as untrusted data.
- Any other file change refuses the whole result. So do a missing Docker, a failed `python:3.12-slim` pull, or a workspace over the snapshot limits (exit 125). That fails the implement job for `after_create` and `before_run`, and only warns for `after_run` and `before_remove`. A symlinked implement hook or its parent directory fails staging with exit 125 before any hook runs; it cannot be silently skipped.
- Hooks that installed dependencies, generated files or needed the network must move that work elsewhere; the implement sandbox already prepares dependencies through `codex_isolated_exec.sh prepare --deps`.
