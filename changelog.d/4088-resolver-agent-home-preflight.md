<!-- changelog: changed -->
- **The conflict resolver now reports who it is and what `RUNTIME_DIR` looks like before creating its sandbox home, and fails closed with a structured error when it cannot.** One `RESOLVER_AGENT_HOME_PREFLIGHT` line precedes the `mkdir`; a denied directory produces `::error::RESOLVER_AGENT_HOME_PREFLIGHT_DENIED` instead of a bare coreutils message.

Every review_autofix resolver run on the `orchestrator/project-3965` lineage since the resolver isolation landed (f03b8d6, #4071) has failed at the same line of `scripts/review_conflict_resolve.sh` with `mkdir: cannot create directory '<RUNTIME_DIR>/resolver-agent-home': Permission denied`, on PR #4077 (run 34746616712) and on every one of PR #4088's 89 review runs since 2026-09-14 (runs 34960984494, 34992257788 and later). The prepare step had just created files in the same `RUNTIME_DIR`, and the editor path creates its own sandbox there without trouble, so the failure cannot be explained from the log as it stood: nothing recorded the process identity or the directory's owner, mode, ACLs, or mount at the moment of the `mkdir`. The resolver now records all of that in one line and refuses to start the broker or OpenCode when `RUNTIME_DIR` is not a writable, searchable directory for the current identity.

| The numbers that matter | Value |
| --- | --- |
| Failing line | `scripts/review_conflict_resolve.sh`, the `mkdir -p "${MODEL_PROVIDER_BROKER_AGENT_HOME}/tmp" ...` call |
| First failing run seen | 34746616712 (2026-09-13, PR #4077) |
| PR #4088 review runs lost | 89 between 2026-09-14 19:30 UTC and 2026-09-16 02:07 UTC |
| Contract test | `tests/test_review_conflict_resolve_agent_home_preflight.py` |

What this means for operators: the next resolver run on an affected PR still fails until the underlying permission problem is fixed, but its log names the cause (`uid`, `euid`, `owner`, `mode`, `acl`, `mount` of `RUNTIME_DIR`) so the fix can be targeted rather than guessed.

### For contributors

The helper is `_resolver_agent_home_preflight <runtime_dir>`; it is defined and invoked immediately before the agent-home `mkdir` and returns 1 on a missing, non-writable, or non-searchable directory. Both log prefixes are registered under "Stable log prefixes" in `agents.md`. A local trace of the script's preamble and of every sourced helper found no ownership or mode change before the `mkdir`, and the same `RUNTIME_DIR` is writable in the editor path, so the diagnostic is the next evidence-gathering step rather than a speculative fix.
