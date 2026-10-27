<!-- changelog: added -->
- **AI implement runs now test their change before they push, and fix test failures they caused in one extra editor turn.** A stale test or a missing registry entry is caught and repaired before review instead of failing CI on the PR.

Until now nothing ran tests between the implement editor and the push, so the first test run on an AI change was PR CI, and every failure there cost a full review and autofix round. A new `Pre-push affected-test gate` step in `implement.yml` runs the tests and registries the change affects, using the gate shipped in #7137, in a container with no network and no credentials. Failures the change caused, as opposed to ones that also fail on the base commit, get exactly one isolated editor turn with a prompt that forbids deleting, skipping or weakening tests. The gate then runs once more. The PR body gets a `## Pre-push test gate` section with the result (test names and paths only). The push is never blocked: an unavailable gate, a timeout or a failed repair still pushes, and the summary says so.

| The numbers that matter | Value |
| --- | --- |
| Repair turns per run | at most 1 |
| Failure text sent to the editor | at most 16 KiB |
| Default time budget per gate run | 15 minutes |
| New GitHub API calls | 0 |

What this means for operators: nothing to set up. To turn the gate off, set the repository variable `PREPUSH_TEST_GATE_MODE=off`; `report` keeps the PR-body section without the repair turn. `PREPUSH_TEST_GATE_TIMEOUT_MINUTES`, `PREPUSH_TEST_GATE_SHARDS` and `PREPUSH_TEST_GATE_LARGE_FILE_TESTS` tune the run.
