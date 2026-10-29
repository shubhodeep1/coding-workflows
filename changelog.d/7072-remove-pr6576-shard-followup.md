<!-- changelog: removed -->
- **Removed the one-time `PR 6576 orchestrate-poll shard follow-up` workflow (#7072).** Its job is done, so pushes to `main` no longer start it.

The workflow posted its single result comment on #7072 on 2026-10-26 (run 40280336599): the PR #6576 failure did not reproduce on `main` in two attempts, so it filed no defect issue. Since then every push to `main` still queued its `self-test` job only to skip. This removes `.github/workflows/pr6576-shard-followup.yml`, `scripts/pr6576_shard_followup.py`, `tests/test_pr6576_shard_followup.py`, their `docs/INVENTORY.md` lines, the generated workflow list entry in `agents.md`, the checkout-audit exemption and the `docs/scripts-pending-removal.md` entry. All three removal preflight checks in that entry passed before removal.

| The numbers that matter | Value |
| --- | --- |
| Result comment on #7072 | posted 2026-10-26 by the pipeline account, run 40280336599 |
| Open `ai:pr6576-shard-followup-defect` issues | 0 |
| In-progress runs at removal | 0 |

What this means for operators: one fewer workflow run per push to `main`, and nothing else changes. No consumer repo ever ran this workflow.
