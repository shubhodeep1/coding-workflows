<!-- changelog: fixed -->
- **An implement run that finds the work already done now closes the issue instead of failing.** A `BLOCKED:` verdict whose reason says nothing was required is reclassified as a success-no-op.

The implement phase treats `BLOCKED: <reason>` as a terminal failure, which is right for a real obstacle and wrong for the one reason the pipeline already has a success path for: the approved plan needs no edits because the branch already carries the requested state. Issue #3972 asked for an `author_association` gate that `main` already had, the model answered `BLOCKED: Approved plan requires no edits; all actor gates exist and the focused contract test passes (5/5).`, and run 33711184784 failed and posted implementation-failed diagnostics. The retry loop in `.github/workflows/implement.yml` now classifies the reason line before the failure bail and, when it says nothing was required, takes the existing success-no-op path that closes the issue with `ai:closed` and an "Already implemented" comment. `prompts/mode-implement.txt` was the upstream cause and now reserves `BLOCKED:` for real obstacles, directing the model to end with `No changes needed.` for a no-op outcome.

| The numbers that matter | Value |
| --- | --- |
| Motivating issue / run | #3972 / 33711184784 |
| Classifier halves that must both agree | 2 (`BLOCKED_ALREADY_SATISFIED_REGEX`, `BLOCKED_REAL_OBSTACLE_REGEX`) |
| New regression tests | 3 in `tests/test_implement_post_codex_recovery.py` |

What this means for operators: an issue whose work landed via a sibling task, or whose plan was built on stale repository state, now ends as a closed issue rather than a failed run needing triage. Genuine blockers are unchanged.

### For contributors

The classification is deliberately two-sided and asymmetric. The reason must match the positive regex and must not match the real-obstacle veto (`scope-lock-violation`, `cannot`, `unable`, `unavailable`, `denied`, `missing`, `fail*`, `error`, `conflict`, `ambiguous`, `insufficient`, `needs human`, `blocked by`, `out-of-scope`, `timeout`). Anything failing either half keeps the previous failure behaviour, so a miss costs a false failure, never a falsely-closed issue. The already-satisfied path writes `codex_success_noop.flag` and never `codex_blocked.flag`, so the diagnostics comment is not emitted. README section 10j documents the contract.
