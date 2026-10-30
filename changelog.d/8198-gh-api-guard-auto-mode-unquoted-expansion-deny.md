<!-- changelog: security -->
- **In Auto mode, the `gh api` permission guard now refuses a call whose argument holds an unquoted `$VAR` or backtick, instead of leaving it to Auto mode's classifier.** A command like `EP="-XDELETE repos/o/r/git/refs/heads/x"; gh api $EP` could delete a branch with no fixed check in front of it.

The guard reads `$EP` as one GET endpoint, so it treated the call as a read, and in Auto mode it gave no decision (#8198). Bash splits the variable into `-XDELETE repos/...`, so the delete ran on the classifier's judgement alone. In Auto mode such a call is now denied, and the reason tells the session how to rewrite it: one plain `gh api` call per ID, or a `for ID in 1 2 3; do gh api .../$ID; done` loop over literal IDs, which is still allowed. In every other mode these calls still ask, as before.

| The numbers that matter | Value |
| --- | --- |
| Auto-mode outcome for an unquoted `gh api` argument | deny (was no decision) |
| Commands still allowed | the validated literal-ID read loop |
| Other permission modes | unchanged (ask) |

What this means for operators: an Auto-mode session that ran a `gh api` loop with a file redirect, a `grep`, or a later `python3` step over `$i` is now refused once and retries with plain calls. No prompt waits for a person.
