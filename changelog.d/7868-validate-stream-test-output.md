<!-- changelog: fixed -->
- **Runtime validation no longer kills a long test step that is still working.** Each validation test script's output now reaches `validation.log` while it runs, and the `python-mongo-repo-checks` and `node-runtime` families echo every `custom_tests` command's output as `# ` TAP comments as it runs.

The validation idle watchdog stops a run after `validation_timeout` minutes (default 15) in which `validation.log` does not grow. `scripts/validate_driver.sh` printed a test script's output only after the script exited, and the repo-checks families sent each command's output only to `logs/repo_check_<n>.log`. A `custom_tests` step that ran a full `pip install` and `pytest` suite for more than 15 minutes was therefore killed mid-run, and the log showed nothing after the last quick check. New `# repo check <n> started/finished (exit=...)` lines with timestamps show where the time goes. Command output stays a TAP comment, so a line such as `ok 1` printed by a command never counts as a result.

| The numbers that matter | Value |
| --- | --- |
| Idle timeout (`validation_timeout`) | 15 minutes, unchanged |
| Families changed | `python-mongo-repo-checks`, `node-runtime` |
| Runs seen killed mid-step | tele-funtoken-msg-scoring 39725752780, 39726381586, 39736025924 |

What this means for consumer repos: after the next `@stable` sync, validation counts a long install or test suite as idle only when it is actually silent, and the log shows each custom test command's output as it runs.

### For contributors

`tests/test_validation_streaming_output.py` renders both families' `40_repo_checks.sh` and runs it against a fake `docker`, and runs the driver's tee pipeline with a slow probe.
