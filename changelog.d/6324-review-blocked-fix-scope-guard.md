<!-- changelog: security -->
- **Review-blocked poller fixes cannot publish unrelated or newly protected files.** The poller checks staged paths against the blocked PR's complete changed-file list and validated judge citations before committing. Missing file listings or out-of-scope edits reject the entire fix, notify operators, and consume a bounded retry without pushing.
