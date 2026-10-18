<!-- changelog: changed -->
- **A failed smoke gate in the daily promote cycle now names the failed job and step.** The `PROMOTE_CYCLE_FAILED reason=smoke_gate_failed` line from `scripts/promote_main_cycle.sh` ends in `failed_job=<name> failed_step=<name>`, so the failure can be read without opening the gate run's logs.

The line keeps its existing fields in the same order: `run=<id> conclusion=<c> failed_job=<name> failed_step=<name>`. The names come from one read of the gate run's jobs (`actions/runs/<id>/jobs`): the first job that failed or timed out, and the first failed step inside it. Whitespace in a name becomes `_`, every character outside `A-Za-z0-9._:()/-` is dropped, and each name is cut to 80 characters, so a name cannot add a log field.

A name that cannot be read, because the jobs read failed or no job or step failed, is logged as `unknown`. The cycle still fails with the same reason and exit code. The extra read happens only when the gate fails; a successful or cancelled gate makes no extra API call.

What this means for operators: when the daily promote cycle fails at the smoke gate, the run log names the job and step that failed, so you can start on the cause without opening the gate run.
