<!-- changelog: fixed -->
- **Open pull requests that add a default-on switch no longer fail the stage switch registry tests on the merged tree.**

Since #9999, `tests/test_stage_switch_registry.py` requires every default-on `*_ENABLED` switch to be listed in `scripts/stage_switches.json`. Pull requests written before that change add switches without the entry, so their final CI fails once they are tested together with `main`; PR #9359 was the first, on 2026-11-25. This change lists the 32 switches that 17 open pull requests add, so those pull requests pass the registry tests without each having to edit the shared file. Nothing else changes: an unset variable still counts as on, so the stage switch watch raises no alert for them.

| The numbers that matter | Value |
| --- | --- |
| Open pull requests into `main` checked | 53 |
| Pull requests that would fail the registry tests | 17 |
| Switch names added to `scripts/stage_switches.json` | 32 (175 to 207 entries) |
| First failure | `CI [pr:9359]` run 45636095846, `test_every_readme_true_switch_is_in_registry` |

What this means for operators: a final CI failure that names `test_every_readme_true_switch_is_in_registry` or `test_every_workflow_true_switch_is_in_registry` on a pull request opened before 2026-11-25 should stop after its next update from `main`. A pull request opened later still has to list its own switch until #10127 removes the hand-kept list.

### For contributors

The 32 entries sit in one block at the end of the `switches` list, not at their sorted places, so they do not conflict with a pull request that adds its own entry in order (#10125 does). Each names the pull request it comes from in its `stage` text. An entry whose pull request is closed without merging is harmless and can be removed; #10127 replaces the list with discovery at run time.
