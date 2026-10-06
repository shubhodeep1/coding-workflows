<!-- changelog: fixed -->
- **Project decomposition can now run on Claude in consumer repositories, and a standalone stall judge set to Claude now gets Claude credentials.** Before this fix, both quietly fell back to the codex / OpenCode path.

`orchestrate.yml` now stages `scripts/claude_anthropic_relay.py` with the other support scripts. `scripts/codex_isolated_exec.sh` needs the relay next to it for a Claude run, so every Claude decomposer call in a consumer job used to exit as "Claude unavailable" and run codex instead.

The "Resolve AI engine" step in `orchestrate_poll.yml` used to decide whether to install the Claude CLI and fetch the account pool from tracking-issue labels alone. A standalone stall judge picks its engine from its own issue's labels, so it could select Claude on a tick that fetched no pool. When no tracking issue selects Claude, the step now also checks two things, while `ENABLE_STANDALONE_STALL_RECOVERY` and `ENABLE_STALL_JUDGE` are on:
- whether `STALL_JUDGE` defaults to Claude, which needs no API call;
- otherwise, one read-only listing of open `ai:engine-claude` issues that are neither tracking issues nor labelled `ai:codex`.

If that listing fails, the step warns and skips the fetch, and the judge keeps its sandboxed OpenCode fallback.
