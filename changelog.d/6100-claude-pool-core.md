<!-- changelog: added -->
- **Claude worker pool, part 1: a reusable Actions worker that runs one Claude Code job on the least-used pool account.** Nothing is dispatched to it yet; the Claude issue pickup and the §26 checkers work as before.

`.github/workflows/claude-pool-worker.yml` is a `workflow_call`-only workflow that the private runner repo `shubhodeep1/claude-workers` calls with `secrets: inherit`. Each run probes every `CLAUDE_POOL_TOKEN_<NAME>` secret with a one-word Haiku run, picks the account with the lowest 5-hour or 7-day utilization under 90%, and runs the item's slash command headless on `claude-opus-5-5` at high effort, with the Edit and Write tools denied on the checkout's own `.claude/**`. The result (`success`, `auth_failed`, `usage_limit`, `all_gated`, `no_accounts`, `crashed`, or `timeout`) is the `claude-pool-result` artifact, and the transcript, with token values (plain and base64) replaced by `***`, is kept 14 days; a transcript that could not be redacted is not uploaded. The worker cannot steer the redaction step: after every CLI run the step kills the processes it left behind and discards any environment or path changes it wrote for later steps. All decisions live in `scripts/claude_pool.py`; `.github/ai/claude_pool.json` ships with `dispatch_types: []`, which keeps the pool off.

| The numbers that matter | Value |
| --- | --- |
| Account gate | 0.90 of the 5-hour or 7-day window |
| Worker time limit | 350 min for issues and stages, 120 min for PR fixes |
| Transcript retention | 14 days |
| Smoke run on the phase branch | run 37015227644 in `shubhodeep1/claude-workers`, all checks green |

What this means for operators: adding a pool account is one secret, `CLAUDE_POOL_TOKEN_<NAME>` in `shubhodeep1/claude-workers`, made with `npx -y @anthropic-ai/claude-code setup-token`; deleting the secret removes it. The runner repo's wrapper PR merges after this project lands on `main`. See README "Claude worker pool".

### For contributors

Run names (`pool <item_type> q<queue issue> a<attempt>`), the result JSON, and the config keys are contracts the phase 2 dispatcher reads; agents.md "Claude worker pool contract" lists them. `tests/test_claude_pool.py` runs in its own `ci.yml` step and fails if any other coding-workflows workflow names `CLAUDE_POOL_TOKEN`.
