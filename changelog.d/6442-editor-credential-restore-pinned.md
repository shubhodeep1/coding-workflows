<!-- changelog: security -->
- **Plan and implement now verify the staged git-credential helper before running it.** The helper executes from checked shell memory instead of an editor-writable path, and planning runs its editor script from memory to prevent in-place changes to post-editor restoration.
