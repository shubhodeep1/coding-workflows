<!-- changelog: fixed -->
- **Plans that change files outside `files_touched` are no longer auto-approved.** Before this, the automatic `/approved` checked only automation paths, so a plan that listed an ordinary file outside the issue's `files_touched` list was approved and its implementation run was then refused by the `files_touched` guard, latching `ai:scope-blocked`.

`scripts/files_touched_scope_guard.py --plan-auto-approval-gate` now compares the exact files in the plan's "Files ... change" section with the issue's `files_touched` list, using the same matcher as the implement guard. A plan that names a file outside the list stays in `ai:awaiting-approval` with reason `plan_files_outside_files_touched`. This covers `plan.yml`'s "Auto-approve clear plan" step and the poller's stall-recovery approvals. The hold comment names the files and asks a maintainer to add them to `files_touched` or narrow the plan, because `/approved` alone would still hit the implement guard. The check is off whenever implement would not block (`ENFORCE_FILES_TOUCHED` not `true`, or `ALLOW_OUT_OF_SCOPE_FILES=true`) and can be switched off with the new repo variable `PLAN_FILES_TOUCHED_GATE_ENABLED=false`.

| The numbers that matter | Value |
| --- | --- |
| Implementation run spent on tele-funtoken-msg-scoring #4795 before the scope block | 1 run (job 12:57–13:19 UTC on 2026-10-23), $4.67 of editor model time |
| Files that plan added outside `files_touched` | 3 (`backend/tele_wallet_commands.py`, `backend/withdrawal_flow.py`, `tests/test_withdrawal_otp_hmac.py`) |
| New repo variable | `PLAN_FILES_TOUCHED_GATE_ENABLED`, default `true` |

What this means for operators: a plan that widens an issue's scope now waits for a person before any implementation run starts, instead of failing at commit time. Issues without a `files_touched` list are not affected.
