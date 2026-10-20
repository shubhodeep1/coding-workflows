<!-- changelog: removed -->
- **The temporary issue #7251 dependency wait is gone, so the poller no longer spends a GraphQL read on it every tick.** #7251 closed on 2026-10-14 and carries its trusted outcome marker, so the wait had nothing left to do.

The poller handler `run_issue_7251_dependency_wait`, the `issue_7251_*` helpers in `scripts/gh_helpers.sh`, the clarify ("Decide clarify route") and plan ("Validate planning phase label") holds, and `tests/test_issue_7251_dependency_wait.py` are removed together, as the `docs/scripts-pending-removal.md` entry required. Every removal preflight check passed first: #7251 is closed with the marker, the last 24 hours of poller logs held no `STALL_SKIP issue=7251 reason=dependency_wait detail=open` line, and no other file referenced the code.

| The numbers that matter | Value |
| --- | --- |
| Repo variable no longer read | `ISSUE_7251_DEPENDENCY_WAIT_ENABLED` |
| GraphQL reads saved | one per poll tick |
| Log lines no longer emitted | `STALL_SKIP issue=7251 reason=dependency_wait`, `AI_PHASE_GATE_V1 ... gate=issue_7251_dependency_wait`, `UNBLOCK_SCAN item=7251 outcome=skip reason=issue_7251_dependency_wait` |

What this means for operators: nothing to do. If `ISSUE_7251_DEPENDENCY_WAIT_ENABLED` is set as a repo variable it is now unused and can be deleted. Consumer repositories never ran the wait.
