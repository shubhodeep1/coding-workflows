<!-- changelog: security -->
- **Review prompt assembly no longer follows symlinked pull-request READMEs.** Non-regular README files are omitted from static review context instead of being read with job credentials in the environment.

The review workflow reads only a regular, size-bounded `README.md` through a no-follow reader. Its content stays outside the trusted static prompt and reaches the review models as fenced untrusted data stored in an owner-only runtime directory. A rejected README emits a path-free warning and review continues without that section; a reader failure stops the step. The same step refuses to write `pre_assembled_static.txt` if the checkout contains a symlink or non-regular file at that path.

What this means for operators: symlinked READMEs no longer expose runner environment data to reviewer prompts; replace the link with a regular file to restore the README section.

The same no-follow, size-bounded handling now covers clarify, plan, orchestrate, clarify-respond, orchestrator judge, and validation prompt assembly. Accepted README text is explicitly framed as untrusted repository data in every covered phase.

READMEs that would exceed 200,000 bytes after per-line untrusted-data framing are omitted with a warning, preventing short-line expansion from overrunning review prompts. Orchestrate and clarify-respond also reject non-regular static-context output paths before writing them.
