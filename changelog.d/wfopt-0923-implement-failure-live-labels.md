<!-- changelog: fixed -->
- **A failed or cancelled implement run now returns its issue to `ai:awaiting-approval` even when the failure came right after the issue moved to `ai:implementing`.** Before, the `Comment on issue failure` step in `implement.yml` read the issue's labels from a cached snapshot taken before that move, found no `ai:implementing`, and left the issue stuck in `ai:implementing`.

The step now reads the labels once from the GitHub API, only on the failure path and after the deliberate `BLOCKED:` branch, which makes no extra call. If that read fails or returns something other than a list, the step logs a warning, leaves the labels unchanged and still posts the failure comment; before, a failed read stopped the step before the comment was posted. The `BLOCKED:` handling, the label calls and the comment text are unchanged.

What this means for operators: an issue whose implement run failed early no longer needs its labels fixed by hand, and every failed run leaves a failure comment on the issue.
