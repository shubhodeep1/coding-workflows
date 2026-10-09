<!-- changelog: fixed -->
- **The OpenCode CLI install retries a transient npm registry failure instead of failing the whole review run.** `.github/actions/install-opencode` now tries `npm install -g opencode-ai@<pin>` up to 3 times with a 10 s, then 20 s, pause before giving up.

One connection reset from the npm registry (`ECONNRESET`, review run 37862635273 on 2026-10-09) failed a review run before any reviewer started, and the workflow-failure heal pipeline then filed issue #6818 for a code defect that did not exist. The retry is bounded and applies only to the registry fetch; the version check, `opencode models --refresh` and the `run --help` probe still fail on the first attempt. The attempt count is the new optional `install_max_attempts` input, default `3`, and must be a positive integer.

| The numbers that matter | Value |
| --- | --- |
| Attempts | 3 |
| Pauses between attempts | 10 s, 20 s |
| Workflows that use the action | 3 |

What this means for operators: a single registry blip no longer costs a review round or a heal issue; a registry that stays down still fails the run within about a minute with the attempt count in the error.
