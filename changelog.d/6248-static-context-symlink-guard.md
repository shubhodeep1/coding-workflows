<!-- changelog: security -->
- **Static prompt assembly skips symlinked checkout files.** Review, clarification and planning omit symlinked README content; clarification, planning and implementation omit symlinked local agents files, and targeted context excludes symlinks and Git metadata.

Review now omits a symlinked `README.md` from its static prompt, and clarification and planning omit symlinked local `agents.md` and `README.md` inputs with a warning. Required instruction and pipeline files that are symlinks cause static prompt assembly to fail before reading their targets. Targeted file context reports symlinked files and `.git` paths without inlining their contents. Regular files continue to be included as before; Git credential persistence in the review checkout is unchanged.

The optional overflow-runbook reference is also omitted, with a warning, when `probably_unnecessary_but_read_if_stuck.md` is a symlink. A regular runbook still receives the reference.

What this means for operators: unsafe links cannot supply prompt text, while a required symlink causes an explicit failure that must be corrected before the phase runs.
