<!-- changelog: changed -->
- **The unblock judge, scan and orphan fix-up close tests now run in 4 parallel processes inside `orchestrate-poll (4)`.** The CI step keeps its name, its group and its three test modules; only its run time drops.

`orchestrate-poll (4)` took 15m13s of its 20-minute `timeout-minutes` on main (run 40198222187). Its only extra work beside its slice of `tests/test_orchestrate_poll_process.py` is the step "Unblock judge, scan and orphan fix-up close tests (replace-claude-sessions Phase 7)". That step had already moved four times (#8072, #8091, #8118), and every group it landed in then neared the timeout, because no CI job has the roughly 11 minutes of room it needs. So the step now stays in group 4 and splits the test functions of `tests/test_unblock_judge.py`, `tests/test_unblock_scan.py` and `tests/test_orphan_fixup_close.py` across 4 pytest processes, the way the merge-train step was split (#7579). Every test still runs once. A shard that fails, or records no exit code, fails the step, and a failing shard's last 40 log lines are printed with a `| ` prefix outside the collapsed log groups. A collection error, or no tests collected, also fails the step.

| The numbers that matter | Value |
| --- | --- |
| Estimated cost of the step before (from group totals, not measured) | about 11 minutes |
| Local shards | 4 |
| `timeout-minutes`, job names, step names, `CI / lint` | unchanged |

What this means for operators: nothing to do. If `orchestrate-poll (4)` still runs near 15 minutes, the next budget-watch issue will carry its step timings.
