<!-- changelog: fixed -->
- **Workflow failure heal issues now carry the failed job's log tail, and a promote or auto-release run that failed only because its smoke gate failed is no longer filed as a second issue.**

The intake's job-log fetch (`gh api …/actions/jobs/<id>/logs`) was refused on every run because Actions logs contain ANSI escape sequences and `gh` requires `--allow-escape-sequences` to return such a body, even to a file. `scripts/workflow_failure_heal_intake.sh` now passes the flag. Before this, every `ai:workflow-heal` issue said `(job log unavailable)`, the dedup signature fell back to the failing step name, and the `downstream_gate_failure` skip that reads `PROMOTE_CYCLE_FAILED reason=smoke_gate_failed` from the promote run's log never matched (issues #4189 and #4190 on 2026-09-21).

What this means for operators: heal issues show the real error lines, the same failure across runs dedups onto one issue, and a failed smoke gate produces one issue instead of two.
