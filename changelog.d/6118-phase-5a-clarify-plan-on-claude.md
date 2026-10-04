<!-- changelog: changed -->
- **Clarify, clarify-respond and plan now run on Claude by default.** These are the first roles cut over from codex to the Claude Code CLI, with codex kept as the automatic fallback.

The CLARIFY role in `clarify.yml`, the CLARIFY_RESPOND role in `orchestrate_clarify_respond.yml` (the answer, the self-critique and the revision) and the PLAN role in `plan.yml` (through `scripts/run_plan_codex.sh`) now default to Claude Opus 5.5 at each role's existing reasoning level. Each job first resolves the role's engine. It installs the Claude CLI and fetches an account from the token pool only when that engine is Claude. When Claude cannot start (no credential, every account at its usage limit, the CLI missing, or a clarify image build failure), the same attempt runs the unchanged codex call and the rest of the job stays on codex. Clarify keeps its sandbox: the Claude CLI runs inside the same container, and the real token stays on the host.

| The numbers that matter | Value |
| --- | --- |
| Roles moved to Claude | `CLARIFY`, `CLARIFY_RESPOND`, `PLAN` |
| Model on Claude | `claude-opus-5-5` (a role variable starting with `claude-` overrides it) |
| Roles still on codex | every other role, until Phases 5b–5d |
| Exit code that falls back to codex | `75` (logged `AI_ENGINE_FALLBACK role= reason=`) |

What this means for operators: clarification questions, orchestrator answers and implementation plans are written by Claude from now on. To put one role back on codex, set the repository variable `AI_ENGINE_CLARIFY`, `AI_ENGINE_CLARIFY_RESPOND` or `AI_ENGINE_PLAN` to `codex`. `AI_ENGINE=codex` does it for every role, and an `ai:codex` label does it for one issue. No code change is needed.

### For contributors

The defaults live in `.github/ai/claude_engine.json`; the code defaults in `scripts/claude_engine.py` stay `codex`, so a missing config file still means codex everywhere. The codex commands are unchanged: `tests/test_plan_codex_step_extraction.py` runs `run_plan_codex.sh` against a fake `claude_run` and a fake `codex`, and `tests/test_plan_clarify_blocked_output.py` runs the clarify retry loop with a stand-in sandbox, each checking the Claude branch, the exit-75 fallback and the exact codex call.
