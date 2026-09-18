<!-- changelog: fixed -->
- **Two no-op plans no longer fail their pipeline phase.** The plan phase stops rejecting files a plan explicitly promised *not* to change, and the implement phase recognises a validation-only `BLOCKED:` verdict as a success-no-op instead of a hard failure.

Both defects turned a correct outcome into an ERROR Telegram alert and sent a working issue back to a human. In `plan.yml`, the template-owned path guard scanned the whole "Files likely to change" section, including the "No changes are expected to:" list that plans use to name the files they deliberately leave alone. In `implement.yml`, the BLOCKED verdict classifier recognised "no changes are required" but not "requires validation only ... no repository edit is permitted", so a plan whose work had already landed failed the run instead of closing the issue. The guard now suppresses negative sublists and resumes scanning at a later `Files ... change` lead-in regardless of indentation, and the classifier gained a `validation-only` branch that still yields to the existing real-obstacle veto.

| The numbers that matter | Value |
| --- | --- |
| Plan-phase false positive | binance-blessings issue #268, run 34127286952 |
| Implement-phase false failure | tele-funtoken-msg-scoring issue #4090, run 34125645374 |
| Path wrongly rejected | `scripts/build_static_context.sh` |
| Regression coverage | Verbatim production inputs plus plan and classifier edge cases |

What this means for operators: a plan that lists unchanged files as context now reaches the implementation phase, and an issue whose fix already shipped closes itself through the existing no-op path. Both previously produced an ERROR alert that needed manual triage.

### For contributors

The plan guard stays conservative in the other direction: a path mentioned positively with a negation later in the same line is still treated as an edit target, and suppression ends at a later "Files to change:" lead-in even when it is indented. The implement classifier remains two-sided, so `BLOCKED_REAL_OBSTACLE_REGEX` still vetoes a validation-only verdict that names a failure, an unavailable tool, or a denied permission.
