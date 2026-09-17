<!-- changelog: fixed -->
- **The conflict resolver now starts the model-provider broker before transferring its mode-0700 agent home to the unprivileged resolver, and reports the runtime-directory state before creating that home.** One `RESOLVER_AGENT_HOME_PREFLIGHT` line precedes the `mkdir`; an unusable parent, unusable existing home, or symlinked home produces `::error::RESOLVER_AGENT_HOME_PREFLIGHT_DENIED` instead of a bare coreutils message or an ownership change outside `RUNTIME_DIR`.

Every review_autofix resolver run on the `orchestrator/project-3965` lineage since the resolver isolation landed (f03b8d6, #4071) failed with `mkdir: cannot create directory '<RUNTIME_DIR>/resolver-agent-home': Permission denied`, on PR #4077 (run 34746616712) and on every one of PR #4088's 89 review runs since 2026-09-14 (runs 34960984494, 34992257788 and later). The resolver had changed the home to mode 0700 and transferred it to `nobody` before `model_provider_broker_start` repeated its `mkdir` and `chmod` operations as the runner, which could no longer traverse or modify that home. Broker startup now completes while the runner still owns the home, then ownership transfers before OpenCode launches; the preflight also records the process identity and directory state and refuses to start when the parent or an existing resolver home is unusable.

| The numbers that matter | Value |
| --- | --- |
| Failing line | `scripts/review_conflict_resolve.sh`, the `mkdir -p "${MODEL_PROVIDER_BROKER_AGENT_HOME}/tmp" ...` call |
| First failing run seen | 34746616712 (2026-09-13, PR #4077) |
| PR #4088 review runs lost | 89 between 2026-09-14 19:30 UTC and 2026-09-16 02:07 UTC |
| Contract test | `tests/test_review_conflict_resolve_agent_home_preflight.py` |

What this means for operators: resolver runs no longer deterministically lose runner access to the broker's agent home during startup. Any remaining parent or stale-home permission denial, including a symlinked home, fails before the broker starts and includes `uid`, `euid`, `owner`, `mode`, `acl`, and mount details in the workflow log.

### For contributors

The helper is `_resolver_agent_home_preflight <runtime_dir>`; it is defined and invoked immediately before the agent-home `mkdir` and returns 1 on a missing, non-writable, or non-searchable parent, an unusable existing resolver home, or any existing resolver-home symlink. Both log prefixes are registered under "Stable log prefixes" in `agents.md`. The contract test also pins broker startup before ownership transfer and ownership transfer before the unprivileged OpenCode launch.
