<!-- changelog: fixed -->
- **Claude write roles now start with eight named tools instead of the CLI's default set.** That brings a no-op start-up back under the 25,000-token context budget.

`claude_run` in `scripts/ai_engine.sh` and the review editor's sandbox in `scripts/review_untrusted_sandbox.sh` used to pass `--tools default` to `claude -p` for every write role (PLAN, IMPLEMENT, the judges, the review editor and the rest). On Claude Code CLI 2.1.289 that set loads about 35 tools, most of which a headless pipeline role never uses, and the `write` job of `claude-engine-smoke.yml` failed its context gate at 29,369 start-up tokens. Write roles now get `Read`, `Grep`, `Glob`, `Bash`, `Edit`, `Write`, `WebFetch` and `WebSearch`. Read roles keep their four tools, and the P5 deny rules and `bypassPermissions` mode are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Context gate budget (plan Q32) | < 25,000 start-up input tokens |
| Write-job start-up before the fix (run 37191845530) | 29,369 tokens |
| Read-job start-up (same run) | 16,804 tokens |
| Tools loaded by `--tools default` (CLI 2.1.289) | 35 |
| Tools in the write profile now | 8 |

What this means for operators: the `write` job of `claude-engine-smoke.yml` should pass its context gate again, and Claude write roles spend fewer input tokens on every run. A write role that needs another built-in tool has to have it added to the list.

### For contributors

The list is set in three places: `scripts/ai_engine.sh`, `scripts/review_untrusted_sandbox.sh` and `PROFILE_TOOLS["write"]` in `scripts/claude_engine.py`. `tests/test_ai_engine.py::test_profile_tool_lists_match_claude_engine` fails if they drift apart or if `default` comes back.
