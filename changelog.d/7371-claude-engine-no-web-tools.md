<!-- changelog: fixed -->
- **Claude-engine runs are now told they have no web tools, and planners take outside facts from the issue instead of trying to look them up.**

The plan and clarify prompts said "Web search IS available", which is true for Codex runs but not for Claude ones: `scripts/claude_anthropic_relay.py` refuses provider web search and web fetch (#6494). On 14 October a Claude planner blocked #7357 for a human because it could not verify the commit SHA of `jlumbroso/free-disk-space` v1.3.1. Every Claude run now gets an "Engine capabilities" note in its system instructions, saying web search and web fetch are unavailable and that outside facts come only from the task text and the repository. The plan and clarify prompts (`prompts/mode-plan.txt`, `prompts/mode-clarify.txt`, the inline clarify prompt in `.github/workflows/clarify.yml` and `scripts/run_plan_codex.sh`) now say web search exists only on Codex runs, use a fact the issue states as verified as given, and ask for a missing one instead of guessing. The orchestrator's decomposition prompt (`prompts/mode-orchestrate.txt`) asks the issue writer to put verified outside facts in the issue body, with how each was verified.

What this means for operators: a Claude-engine plan no longer stalls trying a web lookup it cannot make. When an issue needs an outside value, put it in the issue (or answer the clarification) and the planner uses it.

### For contributors

`_ai_engine_instructions_file` in `scripts/ai_engine.sh` now prints a copy of the trusted `unattended_system_instructions.md` under `RUNNER_TEMP` with `AI_ENGINE_CLAUDE_NO_WEB_NOTE` appended. It is used by `claude_run`, by the Claude branch of `scripts/clarify_isolated_run.sh` and by the Claude roles in `scripts/review_untrusted_sandbox.sh`. If reading the original fails, it prints the original path as well. If the copy cannot be written it prints the original path, so the run still starts. Codex runs are unchanged.
