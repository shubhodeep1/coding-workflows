<!-- changelog: fixed -->
- **Implement now puts back a tracked symlink the editor replaced, retargeted or deleted.** Before, an editor that wrote through a link such as `workflow-templates/CLAUDE.md` could turn it into a regular file. The automation-path guard then refused the whole change set because a path nobody asked for had changed, and the issue stopped in `ai:scope-blocked` (#7623).

Two steps in `implement.yml` check every tracked symlink: one after the implementation editor, before the destructive-commit guard, and one after the post-Codex syntax repair, before the commit guards. A link that is now a regular file, a directory, missing, or pointing somewhere else is restored from `HEAD`, with a warning that names it. The editor's other changes are kept, and every guard still checks the change set. Paths that the staged-support ledger or the fetched-file list owns are left to the commit step, and a link under a symlinked directory is skipped with a warning.

The restore is on by default. Set the repository variable `IMPLEMENT_RESTORE_TRACKED_SYMLINKS_ENABLED` to `false` to turn it off; any other value except `true` also turns it off, with a warning.
