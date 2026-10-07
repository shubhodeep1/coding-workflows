<!-- changelog: changed -->
- **The validation agents now run on the Claude engine by default.** Discover and diagnose (`VALIDATE`), self-heal (`VALIDATE_SELF_HEAL`) and the daily refresh's manifest discovery (`VALIDATION_REFRESH`) use Opus 5.5 in the same credential-free, network-isolated container. When Claude is unavailable, they run codex.

The three roles move to `"engine": "claude"` in `.github/ai/claude_engine.json`; model and profile are unchanged. A new `claude_run_selected` helper in `scripts/ai_engine.sh` picks the engine for each call. On codex, or when Claude is unavailable (exit 75), it runs the caller's existing codex command line unchanged. The Claude run is read-only, like every codex validate attempt, so workspace hooks, write guards and the `UNVERIFIED` validator behaviour do not change. `validate.yml` copies the engine helpers from its verified support clone into `${RUNNER_TEMP}/claude-engine-support` (`CLAUDE_ENGINE_SUPPORT_DIR`); the checkout being validated never supplies them. It reads the tracking issue's labels once for `AI_ENGINE_LABELS`, then installs the pinned CLI and fetches the Claude pool, but only when a validate role resolves to Claude. `validation-refresh.yml` does the same from its own checkout.

| The numbers that matter | Value |
| --- | --- |
| Roles moved to Claude | 3 (`VALIDATE`, `VALIDATE_SELF_HEAL`, `VALIDATION_REFRESH`) |
| Extra GitHub API calls per validate run | at most 1 (tracking issue labels) |
| Codex command line when a role resolves to codex | unchanged |

What this means for operators: to roll back, set `AI_ENGINE_VALIDATE=codex`, `AI_ENGINE_VALIDATE_SELF_HEAL=codex` or `AI_ENGINE_VALIDATION_REFRESH=codex`, or add the `ai:codex` label to the tracking issue. If the trusted engine helpers cannot be staged, the job log shows `AI_ENGINE_FALLBACK role=<ROLE> reason=engine_support_missing` and codex runs.

### For contributors

`ai_engine_stage_support <source_root> <dest_root>` copies the fixed list of engine support files, and refuses missing files and symlinks. Callers source `ai_engine.sh` only from that root. `discover_manifest_via_codex` takes an optional `engine_resolver`. Refs #6664. Tests: `tests/test_claude_engine_utility_roles.py`, `tests/test_ai_engine.py`, `tests/test_claude_engine.py`.
