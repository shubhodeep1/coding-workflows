<!-- changelog: changed -->
- **The pre-push test gate no longer stops a session to ask "confirm to push anyway", and no longer blames a change for lint errors the file already had.** Together these caused about two thirds of the permission prompts found in a scan of 25 recent sessions.

The gate (`.claude/hooks/prepush_affected_tests.py`) lints every changed Python file with `ruff` and every changed shell file with ShellCheck. It used to count every error in those files as caused by the change, and 60 of the 523 Python files in coding-workflows already fail its `ruff` rules on `main` (mostly `E101` mixed tabs and spaces). Touching any of them produced a failing stamp and a prompt, in every permission mode. The gate now runs the same lint with JSON output on the merge-base copy of each file and counts only findings the change added; line numbers are left out, so moved code still matches. A file that is new in the change counts in full. Separately, a stamp that records failures no longer holds the push in any permission mode: the hook gives no permission decision, passes the failures to the session as context and shows them to the user. CI still runs the full suite. A missing stamp, a bulk or tag push, and a push the hook cannot read keep their current behaviour.

| The numbers that matter | Value |
| --- | --- |
| Prompts traced to the gate in the scan | 10 of 15 (6 from pre-existing `ruff` errors) |
| Python files failing the gate's `ruff` rules on `main` | 60 of 523 |
| Permission modes where a failing stamp prompted | all; now none |

What this means for operators: sessions that touch an older file no longer wait for a click, and a push with failing tests goes through with a warning for the session to act on. The AI pipelines that run the gate through `scripts/prepush_test_gate_run.sh` get the same lint comparison, so pre-existing lint errors no longer count as failures there either. Consumer repos pick this up on the next `@stable` sync.
