<!-- changelog: changed -->
- **CI's wiring-guard part 1 tests now run in 4 parallel processes inside `tests-heal-plan-and-validation`.** The step keeps its name, its job and its 40 test modules. Only its run time drops.

`tests-heal-plan-and-validation` took 15m06s of its 20-minute `timeout-minutes` on main (run 40626853481). The step "Regression tests wired by the CI wiring guard (part 1)" used to run its 40 modules in one serial pytest process. It now splits their test functions across 4 pytest processes, and every test still runs once. The step fails on a collection error, on a listed module that collects no tests, or on a shard that fails or records no exit code.

| The numbers that matter | Value |
| --- | --- |
| Sandbox run of the modules that could run there, serial | 29.5s |
| Same modules, sharded (same 52 failed / 380 passed / 30 skipped) | 13.0s |
| Local shards | 4 |
| `timeout-minutes`, job names, step names, `CI / lint` | unchanged |

What this means for operators: nothing to do. If the job still runs near 15 minutes, the next budget-watch issue will carry its step timings.

### For contributors

The run's step timings could not be read, so the step was picked by timing step bodies in the implement sandbox. These timings are relative weights only and vary between sandbox runs (the ci.yml comment records a separate serial run of the 33 modules at about 31s). The sandbox has no PyYAML, so most of the job's other steps stopped early there, and 7 of the 40 modules could not run (the ci.yml comment above the step lists them). The step was split in place, not moved. Earlier moves (#8072, #8091, #8118, #8208) each pushed another job toward its timeout. The split follows #7387 and #8150. The heal step's unsharded tail measured about 16s and stays serial. Every shard log line is printed with a `| ` prefix, inside the collapsed log groups and in the repeated last 40 lines of a failing shard. `tests/test_ci_poll_test_sharding.py` pins the module list, the partition and the failure handling.
