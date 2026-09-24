<!-- changelog: fixed -->
- **Sandboxed model processes no longer die with systemd status 226 before they start.** `scripts/untrusted_process_sandbox.sh` now keeps its scratch dir and temporary provider credential under `RUNNER_TEMP` instead of the `/tmp` runtime dir.

The sandbox's systemd unit runs with `PrivateTmp=yes`, so paths under the host's `/tmp` do not exist inside it. Every pipeline's `RUNTIME_DIR` is under `/tmp`, and the sandbox created its scratch dir there, then named it in `ReadWritePaths=` and the credential in `InaccessiblePaths=`. systemd could not build the unit's mount namespace and exited 226 before the model ran. On PR #4323 this failed every reviewer slot, the summariser, and the editor on three consecutive review runs, which the review workflow reported as `editor_empty_noop`. Failures are now also named: a path still under `/tmp` exits before launch with `sandbox_private_tmp_path`, and a status-226 unit logs `sandbox_namespace_setup_failed` with the unit's journal lines.

| The numbers that matter | Value |
| --- | --- |
| Failed review runs on PR #4323 | 35933627432, 35940741404, 35945522805 |
| systemd exit status | 226 (namespace setup failed) |
| Journal lines logged per 226 failure | up to 20 |

What this means for operators: reviewer, summariser, editor, resolver, and judge processes, which read their prompt on stdin, no longer fail on the sandbox's own `/tmp` state. A sandbox that still cannot start says why in the job log instead of surfacing later as an empty editor result.

### For contributors

Implement still passes `CODEX_THREAD_REUSE_*_FILE` paths under `/tmp` into the sandbox. Those now stop with `sandbox_private_tmp_path` instead of an unexplained 226; moving them out of `/tmp` is separate work.
