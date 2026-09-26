<!-- changelog: fixed -->
- **The conflict resolver's stall guard now runs as the workflow runner and wraps the unprivileged `sudo -n -u nobody -- env -i …` launch as its child, instead of running inside it.** Resolver attempts no longer die on `PermissionError: [Errno 13] Permission denied: '/tmp/tmp.XXXX'` before OpenCode starts.

After the sandbox-home reorder (#4095) the review_autofix conflict resolver on the `orchestrator/project-3965` lineage got past its `mkdir`, then failed three attempts in a row one step later (run 35182160034, the verification probe for PR #4088). `scripts/review_conflict_resolve.sh` created its stdout capture and stall-guard status files with `mktemp` as the runner (mode 0600) and then launched `codex_stall_guard.sh` through `model_provider_broker_exec_unprivileged`, so the guard ran as `nobody` and could not open those files. The guard is built for the opposite nesting, which `scripts/review_apply_fixes.sh` already uses for the editor: it runs privileged, recognises the `sudo -n -u <user> --` prefix of its child, and switches to privileged process-group signalling. The resolver now builds that argv through the new `model_provider_broker_unprivileged_argv_into` helper in `scripts/codex_helpers.sh` and hands it to the stall guard (or the heartbeat wrapper) as the child; `timeout` stays inside the sudo as before. `model_provider_broker_exec_unprivileged` executes the same helper's argv, so the two launch paths cannot drift.

| The numbers that matter | Value |
| --- | --- |
| Failing run | 35182160034 (2026-09-17, probe PR #4115 for PR #4088), 3 of 3 attempts |
| Failing line | `_open_output` in `scripts/codex_stall_guard.sh`, opening the resolver's `mktemp` stdout file |
| Launch shape now | runner: `codex_stall_guard.sh -- sudo -n -u nobody -- env -i … timeout … bash -c opencode_run_cmd …` |
| Contract test | `tests/test_review_conflict_resolve_stall_guard_nesting.py` |

What this means for operators: conflicted PRs on the integration lineage can be resolved by the pipeline again. A stalled resolver is still killed as `nobody` through the guard's privileged signalling path, and the guard's heartbeat and status files are written by the runner, so stall detection works the same way it does for the review editor.

### For contributors

Any new unprivileged model launch that needs the stall guard or heartbeat wrapper must follow this nesting: build the argv with `model_provider_broker_unprivileged_argv_into <array> <user> <cmd…>` and pass it as the wrapper's `--` child. The wrapper-less fallback branch in the resolver keeps calling `model_provider_broker_exec_unprivileged` directly with a runner-side stdout redirect. The nesting rule is recorded under "Model credential isolation" in `agents.md`.
