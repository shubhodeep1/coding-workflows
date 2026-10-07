<!-- changelog: changed -->
- **Implement, implement-repair and implement-diagnose now run on Claude by default.** The implementation editor and its two recovery roles join clarify and plan on the Claude Code CLI, with codex kept as the automatic fallback.

In `implement.yml`, the IMPLEMENT role (the main editor), IMPLEMENT_REPAIR (the syntax and validation repair passes) and IMPLEMENT_DIAGNOSE (the post-failure diagnosis in `scripts/implement_diagnose_post_codex_failure.sh`) now default to Claude Opus 5.5 at each role's existing reasoning level. A new "Resolve AI engine" step resolves all three roles once per job. It installs the Claude CLI and fetches an account from the token pool only when at least one of them is on Claude. Retries keep their context: `scripts/codex_thread_reuse.sh` resumes the role's Claude session across attempts, the way it resumes a codex thread today. When Claude cannot start (no credential, every account at its usage limit, the CLI missing), the same attempt runs the unchanged codex call.

| The numbers that matter | Value |
| --- | --- |
| Roles moved to Claude | `IMPLEMENT`, `IMPLEMENT_REPAIR`, `IMPLEMENT_DIAGNOSE` |
| Model on Claude | `claude-opus-5-5` (a role variable starting with `claude-` overrides it) |
| Roles still on codex | orchestrator, judges, review, validate and utility roles, until Phases 5c–5d |
| Exit code that falls back to codex | `75` (logged `AI_ENGINE_FALLBACK role= reason=`) |

What this means for operators: implementation PRs, repair commits and fix-up issue diagnoses are written by Claude from now on. To put one role back on codex, set the repository variable `AI_ENGINE_IMPLEMENT`, `AI_ENGINE_IMPLEMENT_REPAIR` or `AI_ENGINE_IMPLEMENT_DIAGNOSE` to `codex`. `AI_ENGINE=codex` does it for every role, and an `ai:codex` label does it for one issue. No code change is needed.

### For contributors

`codex_thread_reuse_direct_run` branches to `codex_thread_reuse_claude_direct_run` when `CODEX_THREAD_REUSE_ENGINE=claude`. The Claude session UUID lives in `states/claude-<key>.session` next to the codex thread state and is dropped after a failed run, so the next attempt starts a fresh session. `tests/test_codex_thread_reuse_core.py` and `tests/test_implement_post_codex_recovery.py` run both call sites against a fake `claude_run` and a fake `codex`, each checking the Claude branch, the exit-75 fallback, a Claude crash that does not fall back, and the unchanged codex call.
