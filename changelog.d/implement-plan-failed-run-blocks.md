<!-- changelog: fixed -->
- **`/implement-plan-claude` no longer treats a failed security-audit or validation run as a clean pass.** A run that ends in anything but `success` now stops the project at `Status: BLOCKED` and asks, instead of moving on to the next stage.

A failed `security-audit.yml` run posts no section to the `AI Security Audit Tracker` issue and opens no `ai:security` follow-ups, which looks exactly like a clean audit unless the run's conclusion is read first. The end-to-end test of the command hit this: run 35821734999 failed on a codex `ENOENT` error and the next stage went straight on to validation. Steps 8 and 9 now read the run conclusion before anything else, record the failing step from `gh run view --log-failed`, and ask whether to re-dispatch after a fix or skip the pass. `.claude/scripts/check_in_status.py --run` now reports `state: failed` for a non-`success` conclusion, so the Haiku checker starts the stage as `security-pass <k>/5 — run failed` / `validation <k>/3 — run failed`.

| The numbers that matter | Value |
| --- | --- |
| Conclusions treated as success | `success` |
| Run that exposed the bug | 35821734999 (`security-audit.yml`, `failure`) |

What this means for operators: a broken audit or validation workflow now shows up as a blocked `/implement-plan-claude` session with the failing step named, rather than a project that reaches `docs/completed/` without a real security pass.
