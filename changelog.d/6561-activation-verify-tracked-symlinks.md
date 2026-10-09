<!-- changelog: fixed -->
- **Activation verification no longer reports tracked symlinks as missing.** The verifier now tells the model which symbolic links the repository tracks, so a link such as `workflow-templates/CLAUDE.md` is no longer filed as an activation gap.

The activation verifier's model reads a read-only snapshot of the merged checkout. That snapshot leaves out symbolic links, and so does its synthetic git history, so the model never saw `workflow-templates/CLAUDE.md -> ../CLAUDE.md`. After PR #6435 it opened #6561 asking for a link that was already committed. `scripts/activation_verify.sh` now lists the tracked symlinks on the host from git objects, without following any link, and adds them to the context the model gets as `tracked_symlinks` (path, target, and `target_exists`, which is false for a link whose target is not a tracked file or directory, so a dangling link can still be reported). Both copies of `prompts/mode-activation-verify.txt` tell the model the snapshot is filtered and that a listed path is never missing. The sandbox's isolation is unchanged.

| The numbers that matter | Value |
| --- | --- |
| New context keys | `tracked_symlinks`, `tracked_symlinks_truncated` |
| Links listed | at most 200; paths and targets over 1,024 bytes, not UTF-8, or with control characters are dropped |
| Extra GitHub API calls | none (local git only) |
| On a git failure | an empty list and `::warning::ACTIVATION_VERIFY tracked_symlinks unavailable reason=git_failed\|parse_failed`; verification goes on as before |

### For contributors

Tests: the `test_tracked_symlinks_*` cases and `test_prompt_tells_model_snapshot_omits_symlinks` in `tests/test_activation_verify.py`.
