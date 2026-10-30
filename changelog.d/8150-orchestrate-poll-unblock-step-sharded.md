<!-- changelog: changed -->
- **The unblock judge, scan and orphan fix-up close tests now run in 4 parallel processes inside `orchestrate-poll (4)`.** The CI step keeps its name, its group and its three test modules; only its run time drops.

`orchestrate-poll (4)` took 15m13s of its 20-minute `timeout-minutes` on main (run 40198222187). Its only extra work beside its slice of `tests/test_orchestrate_poll_process.py` is the step "Unblock judge, scan and orphan fix-up close tests (replace-claude-sessions Phase 7)". That step now splits the test functions of `tests/test_unblock_judge.py`, `tests/test_unblock_scan.py` and `tests/test_orphan_fixup_close.py` across 4 pytest processes, and every test still runs once. A shard that fails or records no exit code, a collection error, or no tests collected fails the step.

| The numbers that matter | Value |
| --- | --- |
| Recorded cost of the step before (one failing run, 40065164413) | 860s |
| Local shards | 4 |
| `timeout-minutes`, job names, step names, `CI / lint` | unchanged |

What this means for operators: nothing to do. If `orchestrate-poll (4)` still runs near 15 minutes, the next budget-watch issue will carry its step timings.

### For contributors

The step had already moved three times (#8072, #8091, #8118), and every group it landed in then neared the timeout, because no CI job has room for a step recorded at 860s in a failing run (40065164413). Group totals cannot isolate its cost, so it is not measured. It is split the way the merge-train step was split (#7579). A failing shard's last 40 log lines are printed with a `| ` prefix outside the collapsed log groups.
