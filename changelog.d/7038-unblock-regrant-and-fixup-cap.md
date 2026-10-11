<!-- changelog: fixed -->
- **The unblock judge now clears automation-path blocks that today's guard would no longer set, and never files a fix-up for its own fix-up.**

Before asking the model, the judge re-runs `scripts/files_touched_scope_guard.py` on a scope-blocked issue's newest automation-path rejection. When every rejected path is now granted, it resumes the issue with the existing `/approved` path, once per rejection run, with no verdict and no operator alert; the implementation run still checks the grant before it commits. All 16 `ai:scope-blocked` issues open on 2026-10-10 dated from before the guard's pipeline-author grant (#6780), and 13 of them were fix-ups of fix-ups. A blocked fix-up the judge filed itself now gets one entry in the `ai:operator-step` issue and one WARNING instead of another fix-up. The `operator_step` fix-up text and the judge prompt now say its feature switch gates only new code, never releases, tagging, dispatches or merges. The old default-off wording had produced a request to put stable tagging behind an off switch (#7029).

| The numbers that matter | Value |
| --- | --- |
| Stale `ai:scope-blocked` issues on 2026-10-10 | 16, all granted by today's guard |
| Fix-ups of fix-ups among them | 13 |
| `UNBLOCK_JUDGE_REGRANT_ENABLED` | default `true` |

What this means for operators: fewer "Operator step needed" alerts for blocks the pipeline can clear itself, and no more fix-up chains.
