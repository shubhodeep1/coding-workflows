<!-- changelog: fixed -->
- **Implement, repair and diagnose now actually run on Claude, and planning against `stable` no longer crashes.** Since the Phase 5b cutover, every implement job picked Claude and then fell back to codex (`openai/gpt-6-sol` on OpenRouter), because the Claude Code CLI never installed. A plan that checked out `stable` failed outright, because it ran `stable`'s older stall guard.

`.github/workflows/implement.yml` sets `BASH_ENV` in "Activate workspace shell context", which moves every later bash step into the materialized `WORKSPACE_PATH` copy. That copy leaves out `.codex-workflow-src`. The bash step of `.github/actions/install-claude` read its relative `config_path` (`.codex-workflow-src/.github/ai/claude_engine.json`) from that directory, logged `install-claude: no claude_version input and … is missing`, and skipped the install. `claude-pool-token` then reported `available=false reason=cli_missing`, and `IMPLEMENT` logged `AI_ENGINE_FALLBACK reason=cli_missing` (or `no_credential` when a `claude` binary was already on `PATH`). The action now reads a relative `config_path` from `GITHUB_WORKSPACE`, where the caller's `uses:` path and the npm cache key's `hashFiles()` already resolve it. Absolute paths are unchanged.

`plan.yml`, `clarify.yml` and `orchestrate_clarify_respond.yml` copied `ai_engine.sh` from the support ref but left `scripts/codex_stall_guard.sh` to the checked-out branch, and `claude_run` runs that guard with `--engine claude`. Workflow-heal issues plan against `stable`, whose guard predates `--engine`, so every Claude planning attempt ended `codex_stall_guard.sh: unknown option: --engine` and the run failed after three attempts. The three workflows now copy the guard from the support ref too, as `implement.yml` already did, so the job no longer runs a guard script from the checked-out branch.

| The numbers that matter | Value |
| --- | --- |
| Implement runs sampled (2026-10-05 03:40Z to 2026-10-06 03:40Z) | 81 |
| Runs that resolved `IMPLEMENT` to Claude and then fell back to codex | 78 (65 `cli_missing`, 13 `no_credential`) |
| Runs whose `IMPLEMENT` ran on Claude | 0 |
| Plan runs in the same window that failed on the stall-guard error | 64 of 148 |
| Callers of `install-claude` covered by the fix | 5 (`clarify.yml`, `claude-engine-smoke.yml`, `implement.yml`, `orchestrate_clarify_respond.yml`, `plan.yml`) |

What this means for operators: implement, implement-repair and implement-diagnose now use Claude Opus 5.5 from the account pool, which moves their editor calls off OpenRouter. The D1 fallback to codex is unchanged when the broker or every account is unavailable. Consumer repos get the fix with the next `@stable` promotion.

### For contributors

`tests/test_install_claude_action.py` runs the action's install step with a fake `npm` and `claude` under a `BASH_ENV` that changes into a directory without `.codex-workflow-src`, and asserts that both Claude steps in `implement.yml` run after the `BASH_ENV` switch. CI runs it in the "Install Claude CLI action tests" step of `ci.yml`. `tests/test_ai_engine.py::test_every_workflow_staging_ai_engine_also_stages_its_stall_guard` fails when a workflow copies `ai_engine.sh` without the guard; it is the same test as in #6433.
The merged-PR guard now requests confirmation when an `env`-wrapped commit has an unresolved directory, rather than checking the session checkout's PR history; its live and consumer-template copies remain identical.
