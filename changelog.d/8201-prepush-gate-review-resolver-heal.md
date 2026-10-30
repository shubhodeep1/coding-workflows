<!-- changelog: added -->
- **Review autofix, the conflict resolver and the workflow-heal editor now test their change before they push, and fix test failures they caused in one extra editor turn.** Until now only AI implement runs did this, so a review fix or merge resolution that broke a test was first caught by PR CI on the next round.

The `Pre-push affected-test gate` from implement (#8190) now also runs in three more places. It runs the tests and registries the change affects in a container with no network and no credentials.

- In `review_autofix.yml`, a new step of that name runs after the editor and before "Commit changes". Its result is appended to the editor summary comment as a `## Pre-push test gate` section.
- The conflict resolver runs it on the finished merge resolution, just before its `[ai-merge-resolve]` commit.
- The workflow-heal editor runs it after its syntax check and before its edits reach the checkout.

Each path handles failures the change caused, as opposed to ones that also fail on the base commit, in the same way. They get exactly one repair turn through that path's own sandboxed editor, and edits outside the affected tests and files are rolled back. The repair is reverted if it breaks syntax, or, for the resolver, if it fails the resolver's own checks. The push is never blocked: an unavailable gate, a timeout or a failed repair still commits and pushes.

| The numbers that matter | Value |
| --- | --- |
| Repair turns per run | at most 1 |
| Growth of `review_autofix.yml` | 1,603 bytes |
| New GitHub API calls | 0 |

What this means for operators: nothing to set up. The `PREPUSH_TEST_GATE_MODE` variable controls implement, the review step and the conflict resolver: `off` turns the gate off, and `report` keeps the result without the repair turn. The workflow-heal editor does not receive it yet, because `implement.yml`'s "Run Codex implementation" step does not pass it on, so heal always runs in `repair` mode until that step forwards it. The review sandbox image now carries `shellcheck` and `ruff`.
