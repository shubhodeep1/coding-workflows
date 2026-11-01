<!-- agents: section="Stable log prefixes (contractual)" -->
- `CI_SUBTEST_START` (`.github/workflows/ci.yml`, `orchestrate-poll` group 3 step "Orchestrate poll single-file module tests", workflow-optimization recommendation Speed 1 / Reliability 1 / Cost 1 of 2026-09-28: `subtest=<module path>`, printed when each of the three modules is started in the background).
- `CI_SUBTEST_END` (same step: `subtest=<module path> elapsed_ms=<int> rc=<int>`, printed for each module after every module process is reaped; a missing or non-numeric rc is reported as `rc=1` and fails the step; a missing or non-numeric start time gives `elapsed_ms=0`; a missing or non-numeric end time is replaced by the current clock, so `elapsed_ms` then runs up to the time the line is printed).
- `CI_SUBTEST_HEARTBEAT` (same step: `running=<comma-separated module paths> elapsed_ms=<int since step start>`, about every 30s while any module has not written its rc file, from a background loop that an EXIT trap kills so it never outlives the step or changes its exit status). These lines carry only module paths and integers.
LOG_PREFIX.name=CI_SUBTEST_START
LOG_PREFIX.name=CI_SUBTEST_END
LOG_PREFIX.name=CI_SUBTEST_HEARTBEAT
