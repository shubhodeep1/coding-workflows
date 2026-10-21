<!-- changelog: fixed -->
- **AI Validate can get a Claude credential again.** The "Stage trusted Claude engine actions" step in `validate.yml` now copies `scripts/claude_pool_token.sh` and `scripts/claude_engine.py` next to the `claude-pool-token` action it stages.

The action runs that script from three folders above itself. Validation copied only the action folders, so the credential step failed with exit 127 (`CLAUDE_POOL available=false reason=script_failed`) and every Claude diagnosis and self-heal call ended with `no_credential`. Under `AI_ENGINE_FALLBACK_POLICY=capacity` codex did not run either, so the diagnosis failed all three attempts and the project was sent a CRITICAL "validation failed after 2 recovery attempt(s)" alert.

| The numbers that matter | Value |
| --- | --- |
| Workflow changed | `.github/workflows/validate.yml` |
| Runs seen failing | tele-funtoken-msg-scoring validate runs 39669571530 (tracking 3928) and 39677334128 (tracking 4038) |

What this means for consumer repos: after the next `@stable` sync, AI Validate runs its diagnosis and self-heal on Claude when the pool has an account, and those projects can be validated again.

### For contributors

`tests/test_claude_engine_utility_roles.py::test_validate_stages_the_script_the_pool_token_action_runs` runs the real staging step and resolves the script path the way `main.js` does.
