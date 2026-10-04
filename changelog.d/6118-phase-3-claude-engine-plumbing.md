<!-- changelog: added -->
- **The Actions pipelines can now run a role on the Claude Code CLI, but nothing uses it yet.** Phase 3 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` adds the engine plumbing with every role still defaulting to codex, so no run changes until a role's cutover.

`scripts/ai_engine.sh` picks a role's engine (the `ai:codex` / `ai:engine-claude` labels, then `AI_ENGINE_<ROLE>`, then `AI_ENGINE`, then the default in `.github/ai/claude_engine.json`) and runs `claude -p` through `claude_run`, which writes the final answer to the same output file the codex path writes. A usage-limited or rejected account moves the run to the next account; when Claude cannot run at all it logs `AI_ENGINE_FALLBACK`, sends one Telegram note per job and returns 75 so the caller runs codex. Every run gets the P5 permission policy from `scripts/claude_settings.json.tmpl`: `gh pr merge`, `gh api … DELETE`, force pushes, remote branch deletes and edits to the checkout's `.github/workflows/**` and `.claude/**` are denied, and `gh_api_write_guard.py` checks every Bash call. The sandboxed clarify and review-editor scripts gain a Claude branch behind `scripts/claude_anthropic_relay.py`, which keeps the OAuth token on the host.

| The numbers that matter | Value |
| --- | --- |
| Pinned CLI | `@anthropic-ai/claude-code` 2.1.289 |
| Roles with an engine switch | 30 (the six reviewer slots have none) |
| Roles defaulting to Claude | 0 |
| Fallback exit code | 75 |
| Context gate | start-up input below 25,000 tokens, `CLAUDE.md` not visible |
| New GitHub API calls | 0 |

What this means for operators: nothing changes on any run yet. `claude-engine-smoke.yml` can be dispatched by hand; until the token broker ships it reports `available=false` and checks the codex fallback. `codex_stall_guard.sh` and `codex_heartbeat.sh` accept `--engine claude`, which only adds `engine=claude` to their log lines, and `scripts/cost_audit.py` totals Claude usage in a new "Claude engine usage" table.

### For contributors

Call sites source `scripts/ai_engine.sh`, call `ai_engine_for_role <ROLE>`, and on `claude` run `claude_run <ROLE> <prompt> <out> <workdir> [session_id]` with `AI_ENGINE_MODEL_HINT` / `AI_ENGINE_EFFORT_HINT` set to the role's existing model and reasoning values; exit 75 means "run the codex command". The account pool is `$CLAUDE_ENGINE_POOL_DIR` (default `$RUNNER_TEMP/claude-pool`) with an `order` file and `tokens/<NAME>` files. `clarify_isolated_run.sh` takes optional `claude <ROLE>` arguments and `review_untrusted_sandbox.sh` takes `prepare claude` and a seventh `claude` argument to `run`; the codex and OpenCode command lines are unchanged. Tests: `tests/test_ai_engine.py`, `tests/test_claude_engine.py`, `tests/test_claude_settings_policy.py`, `tests/test_claude_anthropic_relay.py`, each in its own `ci.yml` step.
