<!-- changelog: security -->
- **Issue prose can no longer inject implementation job environment variables.** The implement workflow reads the issue body from a file instead of exporting it to every later step.

An issue containing a standalone `EOF` line no longer sets environment variables for subsequent implement steps. The workflow keeps the body in `ISSUE_BODY_FILE` and reads it when building the editor's implementation context. The alt-model smoke override also reads that file, retaining its fallback when the override cannot be parsed. Other issue-derived multiline exports and guard outputs use collision-checked random delimiters; if a no-op guard output cannot be written safely, the commit step fails rather than hiding excluded paths.

What this means for operators: issue descriptions remain available to the implementer without becoming job-wide environment values, and unsafe no-op diagnostics stop the run instead of masking filtered work.
