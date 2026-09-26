<!-- changelog: fixed -->
- **`/implement-plan-claude` now runs one checker session per project instead of one per stage, so long projects no longer stall at the session depth limit.**

The claude-code-remote tools refuse `create_session`, `send_later`, and `create_trigger` from a session 8 parent links below its root. Each stage used to create its own Sonnet checker, and each checker created the next stage, so every hand-off added two links. The `heal-deterministic-autofix-failures` project hit the limit at its fourth security cycle on 2026-09-25. The only fallback left was a session-local cron job, which died with the container, so the project stalled for about 14 hours with nobody notified. The command now keeps a single checker, `implement-plan <slug> — checker`, for the whole project. That checker starts every stage session, and each stage hands it the next wait through a one-shot trigger. A depth-limit refusal now stops the project as `BLOCKED`, with a push notification and a ready-to-paste resume prompt.

| The numbers that matter | Value |
| --- | --- |
| Session lineage limit | 8 parent links |
| Depth per hand-off, before / after | +2 per stage / constant (checker at *d+1*, stages at *d+2*) |
| Hand-offs before a stall, before / after | about 4 / unlimited |
| Checkers per project, before / after | one per wait / one |

Every stage also cleans up stray checkers. In step 0 it archives any `implement-plan <slug> — checker` or older `implement-plan <slug> — waiting: …` session that is not the recorded checker. Before handing over a wait, it deletes the reused checker's pending check-ins. Each check-in names its wait, so a superseded wait cannot start a second copy of a stage. The checker is archived when the project reaches LIVE or hands off to `/deploy-activate`.

What this means for operators: a project now runs from its first phase to activation without needing to be restarted from a new top-level session. The session list shows one checker plus the current stage per project.

### For contributors

Projects already in flight keep working. Their next stage finds no reusable `— checker` session (older checkers are titled `— waiting: …`), creates the project checker once, and archives the old one. The new rules are pinned in `tests/test_implement_plan_claude_command.py`, and the command and its `workflow-templates/` copy stay byte-identical.
